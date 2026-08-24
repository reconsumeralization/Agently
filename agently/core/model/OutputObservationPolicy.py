# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_SETTINGS_PATH = "model_request.output_observation.sensitive_paths"
_RAW_OUTPUT_FIELDS = (
    "raw_text",
    "cleaned_text",
    "streamed_text",
    "response_text",
    "reasoning",
)


def normalize_sensitive_output_paths(paths: Sequence[str]) -> list[str]:
    if isinstance(paths, (str, bytes, bytearray)):
        raise TypeError("sensitive_paths must be a sequence of output paths")
    normalized: list[str] = []
    for raw_path in paths:
        if not isinstance(raw_path, str):
            raise TypeError("sensitive output paths must be strings")
        path = raw_path.strip()
        if not path:
            raise ValueError("sensitive output paths must not be empty")
        segments = _parse_path(path)
        canonical = "$" if not segments else ".".join(segments)
        if len(canonical.encode("utf-8")) > 512:
            raise ValueError("sensitive output path exceeds 512 UTF-8 bytes")
        if canonical not in normalized:
            normalized.append(canonical)
    if len(normalized) > 64:
        raise ValueError("at most 64 sensitive output paths may be declared")
    return normalized


def _parse_path(path: str) -> tuple[str, ...]:
    if path in {"$", "/"}:
        return ()
    if path.startswith("/"):
        return tuple(segment.replace("~1", "/").replace("~0", "~") for segment in path[1:].split("/") if segment != "")
    if path.startswith("$."):
        path = path[2:]
    segments = tuple(segment for segment in path.split(".") if segment != "")
    if not segments:
        raise ValueError(f"invalid sensitive output path: {path!r}")
    return segments


def _canonical_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    except Exception:
        return str(value).encode("utf-8", errors="replace")


def _redaction_fact(value: Any, *, path: str | None = None) -> dict[str, Any]:
    raw = _canonical_bytes(value)
    fact: dict[str, Any] = {
        "redacted": True,
        "reason": "sensitive_model_output",
        "bytes": len(raw),
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }
    if path is not None:
        fact["path"] = path
    return fact


def _safe_copy(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        return _redaction_fact(value)
    if value is None or type(value) in {str, bool, int, float}:
        return value
    if isinstance(value, Mapping):
        return {str(key): _safe_copy(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_copy(item, depth=depth + 1) for item in value]
    try:
        return deepcopy(value)
    except Exception:
        return _redaction_fact(value)


def _redact_path(value: Any, segments: tuple[str, ...], *, display_path: str) -> tuple[Any, int]:
    if not segments:
        return _redaction_fact(value, path=display_path), 1
    head, *tail_items = segments
    tail = tuple(tail_items)
    if isinstance(value, Mapping):
        if head not in value:
            return _safe_copy(value), 0
        projected = _safe_copy(value)
        projected[head], count = _redact_path(
            value[head],
            tail,
            display_path=display_path,
        )
        return projected, count
    if isinstance(value, list):
        try:
            index = int(head)
        except (TypeError, ValueError):
            return _safe_copy(value), 0
        if index < 0 or index >= len(value):
            return _safe_copy(value), 0
        projected = _safe_copy(value)
        projected[index], count = _redact_path(
            value[index],
            tail,
            display_path=display_path,
        )
        return projected, count
    return _safe_copy(value), 0


@dataclass(frozen=True)
class OutputObservationPolicy:
    sensitive_paths: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Any) -> "OutputObservationPolicy":
        raw_paths = settings.get(_SETTINGS_PATH, []) if settings is not None else []
        if not isinstance(raw_paths, (list, tuple)):
            return cls()
        try:
            normalized = normalize_sensitive_output_paths(raw_paths)
        except (TypeError, ValueError):
            return cls()
        return cls(tuple(normalized))

    @property
    def active(self) -> bool:
        return bool(self.sensitive_paths)

    def project_structured_value(self, value: Any) -> tuple[Any, int]:
        if not self.active:
            return _safe_copy(value), 0
        if not isinstance(value, (Mapping, list)):
            return _redaction_fact(value), 1
        projected = _safe_copy(value)
        redacted_count = 0
        for display_path in self.sensitive_paths:
            segments = _parse_path(display_path)
            projected, count = _redact_path(
                projected,
                segments,
                display_path=display_path,
            )
            redacted_count += count
        return projected, redacted_count

    def project_parser_observation(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        projected = dict(observation)
        if not self.active:
            return projected
        raw_payload = observation.get("payload")
        payload = _safe_copy(raw_payload) if isinstance(raw_payload, Mapping) else {}
        redacted_fields: list[str] = []
        redacted_count = 0

        if "delta" in payload:
            payload["delta"] = _redaction_fact(payload.get("delta"), path="delta")
            redacted_fields.append("delta")
            projected["message"] = "Sensitive model output streaming was redacted."

        if "result" in payload:
            result_value = payload.get("result")
            if str(observation.get("kind", "")) == "parse_failed":
                payload["result"] = _redaction_fact(result_value, path="result")
                redacted_count += 1
            else:
                payload["result"], count = self.project_structured_value(result_value)
                redacted_count += count
            redacted_fields.append("result")

        for field in _RAW_OUTPUT_FIELDS:
            if field not in payload:
                continue
            if field == "raw_text" and isinstance(payload.get(field), str) and "estimated_output_chars" not in payload:
                payload["estimated_output_chars"] = len(payload[field])
                payload["estimated_output_source"] = "sensitive_output_observation.raw_text"
            payload[field] = _redaction_fact(payload.get(field), path=field)
            redacted_fields.append(field)

        error = observation.get("error")
        if error is not None:
            error_fact = _redaction_fact(str(error), path="error")
            error_fact["error_type"] = type(error).__name__
            payload["error_observation_redaction"] = error_fact
            projected["error"] = None
            redacted_fields.append("error")

        if redacted_fields:
            payload["output_observation_redaction"] = {
                "active": True,
                "sensitive_paths": list(self.sensitive_paths),
                "redacted_fields": sorted(set(redacted_fields)),
                "structured_match_count": redacted_count,
            }
        projected["payload"] = payload
        return projected

    def project_event_payload(
        self,
        payload: Mapping[str, Any],
        *,
        raw_text_fields: Sequence[str] = (),
        opaque_fields: Sequence[str] = (),
    ) -> dict[str, Any]:
        projected = _safe_copy(payload)
        if not self.active:
            return projected
        redacted_fields: list[str] = []
        for field in raw_text_fields:
            if field not in projected:
                continue
            projected[field] = _redaction_fact(projected.get(field), path=field)
            redacted_fields.append(field)
        for field in opaque_fields:
            if field not in projected or projected.get(field) is None:
                continue
            projected[field] = _redaction_fact(projected.get(field), path=field)
            redacted_fields.append(field)
        if redacted_fields:
            projected["output_observation_redaction"] = {
                "active": True,
                "sensitive_paths": list(self.sensitive_paths),
                "redacted_fields": sorted(set(redacted_fields)),
            }
        return projected
