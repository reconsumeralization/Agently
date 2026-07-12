# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Artifact lifecycle management for Action execution.

Handles artifact registration, redaction, compaction, digest building,
and model-visible record transformation. Extracted from Action.py to keep
the core class focused on orchestration.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import RLock
from typing import Any, cast

from agently.types.data import ActionArtifact, ActionResult, WorkspaceFileRef

from .ActionNormalization import normalize_execution_record


class ActionArtifactManager:
    _RECALL_ACTION_ID = "read_action_artifact"
    _MODEL_VISIBLE_RECORD_MAX_BYTES = 6000
    _MODEL_VISIBLE_RESULT_PREVIEW_MAX_BYTES = 2400
    _MODEL_VISIBLE_INSTRUCTION_MAX_BYTES = 1200
    _ACTION_CARRIER_MAX_BYTES = 16000
    _INSTRUCTION_HEAVY_EXECUTOR_TYPES = {
        "bash_sandbox",
        "python_sandbox",
        "nodejs",
        "docker",
        "sqlite",
        "browse",
        "search",
    }
    _INSTRUCTION_HEAVY_KWARGS = {
        "cmd",
        "command",
        "python_code",
        "js_code",
        "code",
        "query",
        "sql",
        "url",
    }
    _SENSITIVE_KEYWORDS = {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "header",
        "password",
        "secret",
        "token",
    }

    def __init__(self, *, registry: Any = None):
        self._artifacts: dict[str, dict[str, Any]] = {}
        self._artifact_scopes: dict[str, tuple[str, str]] = {}
        self._selection_index: dict[str, str] = {}
        self._artifact_lock = RLock()
        self._registry = registry
        self._current_artifact_scope: ContextVar[dict[str, str] | None] = ContextVar(
            f"agently_action_artifact_scope_{id(self)}",
            default=None,
        )

    @contextmanager
    def bind_artifact_scope(
        self,
        artifact_scope: Mapping[str, Any],
    ) -> Iterator[dict[str, str]]:
        """Bind the exact execution-owned scope for nested artifact readback."""

        scope = self._normalize_artifact_scope(artifact_scope, fallback_id="")
        if scope is None:
            raise ValueError("Action artifact scope requires non-empty kind and id values.")
        token = self._current_artifact_scope.set(scope)
        try:
            yield scope
        finally:
            self._current_artifact_scope.reset(token)

    def current_artifact_scope(self) -> dict[str, str] | None:
        scope = self._current_artifact_scope.get()
        return dict(scope) if scope is not None else None

    # ── artifact storage access ────────────────────────────────────────────

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._artifact_lock:
            artifact = self._artifacts.get(str(artifact_id))
            return deepcopy(artifact) if artifact is not None else None

    def get_artifact_value(self, artifact_id: str) -> Any | None:
        with self._artifact_lock:
            artifact = self._artifacts.get(str(artifact_id))
            return deepcopy(artifact.get("value")) if artifact is not None else None

    def read_artifact_transfer(
        self,
        artifact_id: str,
        *,
        expected_scope: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Any] | None:
        """Atomically read canonical identity plus one exact-value copy."""

        scope = self._normalize_artifact_scope(expected_scope, fallback_id="")
        if scope is None:
            return None
        artifact_key = str(artifact_id)
        with self._artifact_lock:
            artifact = self._artifacts.get(artifact_key)
            stored_scope = self._artifact_scopes.get(artifact_key)
            if artifact is None or stored_scope != (scope["kind"], scope["id"]):
                return None
            identity = {key: deepcopy(value) for key, value in artifact.items() if key != "value"}
            return identity, deepcopy(artifact.get("value"))

    def read_selection_transfer(
        self,
        selection_key: str,
        *,
        expected_scope: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Any] | None:
        """Resolve one host-issued selection key to canonical identity and value."""

        scope = self._normalize_artifact_scope(expected_scope, fallback_id="")
        if scope is None:
            return None
        with self._artifact_lock:
            artifact_id = self._selection_index.get(str(selection_key))
            artifact = self._artifacts.get(artifact_id or "")
            stored_scope = self._artifact_scopes.get(artifact_id or "")
            if artifact is None or stored_scope != (scope["kind"], scope["id"]):
                return None
            identity = {key: deepcopy(value) for key, value in artifact.items() if key != "value"}
            return identity, deepcopy(artifact.get("value"))

    def get_artifact_id_for_selection(self, selection_key: str) -> str | None:
        with self._artifact_lock:
            artifact_id = self._selection_index.get(str(selection_key))
            return str(artifact_id) if artifact_id is not None else None

    def get_artifact_scope(self, artifact_id: str) -> dict[str, str] | None:
        with self._artifact_lock:
            scope = self._artifact_scopes.get(str(artifact_id))
            return {"kind": scope[0], "id": scope[1]} if scope is not None else None

    def release_scope(self, artifact_scope: Mapping[str, Any]) -> int:
        return self.release_scope_except(artifact_scope, retained_artifact_ids=())

    def release_scope_except(
        self,
        artifact_scope: Mapping[str, Any],
        *,
        retained_artifact_ids: Mapping[str, Any] | list[str] | tuple[str, ...] | set[str],
    ) -> int:
        scope = self._normalize_artifact_scope(artifact_scope, fallback_id="")
        if scope is None:
            return 0
        scope_key = (scope["kind"], scope["id"])
        retained_ids = {str(value) for value in retained_artifact_ids}
        with self._artifact_lock:
            artifact_ids = [
                artifact_id
                for artifact_id, owner_scope in self._artifact_scopes.items()
                if owner_scope == scope_key and artifact_id not in retained_ids
            ]
            for artifact_id in artifact_ids:
                artifact = self._artifacts.pop(artifact_id, None)
                self._artifact_scopes.pop(artifact_id, None)
                if isinstance(artifact, dict):
                    self._selection_index.pop(str(artifact.get("selection_key") or ""), None)
            return len(artifact_ids)

    @classmethod
    def project_released_scope(
        cls,
        value: Any,
        *,
        artifact_scope: Mapping[str, Any],
    ) -> Any:
        """Return a defensive projection whose released refs are truthful."""

        projected = deepcopy(value)
        normalized_scope = cls._normalize_artifact_scope(artifact_scope, fallback_id="")
        if normalized_scope is None:
            return projected
        seen: set[int] = set()

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                item_id = id(item)
                if item_id in seen:
                    return
                seen.add(item_id)
                meta = item.get("meta")
                item_scope = meta.get("artifact_scope") if isinstance(meta, dict) else None
                if (
                    item.get("selection_key")
                    or item.get("artifact_id") and item_scope == normalized_scope
                ):
                    item["available"] = False
                    item["full_value_available"] = False
                for nested in item.values():
                    visit(nested)
            elif isinstance(item, list):
                item_id = id(item)
                if item_id in seen:
                    return
                seen.add(item_id)
                for nested in item:
                    visit(nested)

        visit(projected)
        return projected

    @staticmethod
    def _normalize_artifact_scope(
        artifact_scope: Mapping[str, Any] | None,
        *,
        fallback_id: str,
    ) -> dict[str, str] | None:
        if isinstance(artifact_scope, Mapping):
            kind = str(artifact_scope.get("kind") or "").strip()
            scope_id = str(artifact_scope.get("id") or "").strip()
            if kind and scope_id:
                return {"kind": kind, "id": scope_id}
        if fallback_id:
            return {"kind": "action_call", "id": fallback_id}
        return None

    # ── redaction / compaction ─────────────────────────────────────────────

    @classmethod
    def _is_sensitive_key(cls, key: Any) -> bool:
        lowered = str(key).lower()
        return any(keyword in lowered for keyword in cls._SENSITIVE_KEYWORDS)

    @staticmethod
    def _compact_text(value: Any, *, limit: int = 4000) -> str:
        text = str(value)
        if len(text) <= limit:
            return text
        return f"{text[:limit]}... [truncated {len(text) - limit} chars]"

    @classmethod
    def _compact_value(cls, value: Any, *, limit: int = 4000, depth: int = 0) -> Any:
        if cls._is_sensitive_key(value) and depth < 0:
            return "[REDACTED]"
        if isinstance(value, dict):
            if depth >= 2:
                return f"[dict keys={list(value.keys())[:8]}]"
            compact: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 40:
                    compact["..."] = f"{len(value) - index} more keys"
                    break
                if cls._is_sensitive_key(key):
                    compact[str(key)] = "[REDACTED]"
                else:
                    compact[str(key)] = cls._compact_value(item, limit=limit, depth=depth + 1)
            return compact
        if isinstance(value, (list, tuple, set)):
            items = list(value)
            compact_items = [cls._compact_value(item, limit=limit, depth=depth + 1) for item in items[:40]]
            if len(items) > 40:
                compact_items.append(f"... {len(items) - 40} more items")
            return compact_items
        if isinstance(value, str):
            return cls._compact_text(value, limit=limit)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return cls._compact_text(value, limit=limit)

    @classmethod
    def _redaction_report_for_value(cls, value: Any, *, path: str = "") -> list[str]:
        report: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                item_path = f"{path}.{key}" if path else str(key)
                if cls._is_sensitive_key(key):
                    report.append(item_path)
                else:
                    report.extend(cls._redaction_report_for_value(item, path=item_path))
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                item_path = f"{path}[{index}]" if path else f"[{index}]"
                report.extend(cls._redaction_report_for_value(item, path=item_path))
        return report

    @classmethod
    def _redact_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if cls._is_sensitive_key(key):
                    redacted[str(key)] = "[REDACTED]"
                else:
                    redacted[str(key)] = cls._redact_value(item)
            return redacted
        if isinstance(value, list):
            return [cls._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._redact_value(item) for item in value)
        return value

    @classmethod
    def _safe_json_size(cls, value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            return len(str(value).encode("utf-8", errors="ignore"))

    @classmethod
    def _json_preview(cls, value: Any, *, max_bytes: int) -> dict[str, Any]:
        raw = cls._json_bytes(value)
        preview = raw[:max_bytes].decode("utf-8", errors="ignore")
        return {
            "preview": preview,
            "truncated": len(raw) > max_bytes,
            "original_size": len(raw),
            "preview_size": len(preview.encode("utf-8", errors="ignore")),
        }

    @classmethod
    def _json_bytes(cls, value: Any) -> bytes:
        try:
            return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")
        except Exception:
            return str(value).encode("utf-8", errors="ignore")

    @classmethod
    def _preview_meta(cls, value: Any, *, limit: int, path: str = "", depth: int = 0) -> list[dict[str, Any]]:
        if depth > 4:
            return []
        if isinstance(value, str):
            original_bytes = len(value.encode("utf-8"))
            if len(value) <= limit:
                return []
            preview_bytes = len(value[:limit].encode("utf-8", errors="ignore"))
            return [
                {
                    "path": path or "$",
                    "truncated": True,
                    "original_bytes": original_bytes,
                    "preview_bytes": preview_bytes,
                }
            ]
        if isinstance(value, dict):
            metadata: list[dict[str, Any]] = []
            for key, item in value.items():
                item_path = f"{path}.{key}" if path else str(key)
                metadata.extend(cls._preview_meta(item, limit=limit, path=item_path, depth=depth + 1))
            return metadata
        if isinstance(value, list):
            metadata = []
            for index, item in enumerate(value[:40]):
                item_path = f"{path}[{index}]" if path else f"[{index}]"
                metadata.extend(cls._preview_meta(item, limit=limit, path=item_path, depth=depth + 1))
            return metadata
        return []

    @staticmethod
    def _artifact_role(artifact_type: str) -> str:
        if artifact_type.endswith("input") or artifact_type == "action_input":
            return "input"
        if artifact_type.endswith("output") or artifact_type == "action_output":
            return "output"
        return "artifact"

    @classmethod
    def _collect_file_refs(cls, record: ActionResult) -> list[WorkspaceFileRef]:
        collected: list[WorkspaceFileRef] = []

        def collect(value: Any):
            if isinstance(value, dict):
                refs = value.get("file_refs")
                if isinstance(refs, list):
                    for ref in refs:
                        if isinstance(ref, dict):
                            collected.append(cast(WorkspaceFileRef, dict(ref)))

        collect(record)
        collect(record.get("data"))
        result = record.get("result")
        if result is not record.get("data"):
            collect(result)

        deduped: list[WorkspaceFileRef] = []
        seen: set[tuple[str, str, str]] = set()
        for ref in collected:
            key = (str(ref.get("path", "")), str(ref.get("sha256", "")), str(ref.get("role", "")))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)
        return deduped

    # ── artifact registration ──────────────────────────────────────────────

    def register_execution_artifact(
        self,
        *,
        action_call_id: str,
        artifact_type: str,
        label: str,
        value: Any,
        media_type: str = "application/json",
        meta: dict[str, Any] | None = None,
        artifact_scope: Mapping[str, Any] | None = None,
    ) -> ActionArtifact:
        artifact_id = f"act_art_{uuid.uuid4().hex}"
        selection_key = f"sel_{uuid.uuid4().hex}"
        exact_value = deepcopy(value)
        safe_value = self._redact_value(exact_value)
        preview = self._compact_value(safe_value, limit=4000)
        raw_bytes = self._json_bytes(exact_value)
        size = len(raw_bytes)
        preview_size = self._safe_json_size(preview)
        role = self._artifact_role(artifact_type)
        resolved_meta = dict(meta or {})
        resolved_meta["artifact_scope"] = self._normalize_artifact_scope(
            artifact_scope,
            fallback_id=action_call_id,
        )
        resolved_scope = cast(dict[str, str], resolved_meta["artifact_scope"])
        stored = {
            "artifact_id": artifact_id,
            "selection_key": selection_key,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "role": role,
            "label": label,
            "media_type": media_type,
            "value": exact_value,
            "meta": deepcopy(resolved_meta),
            "size": size,
            "bytes": size,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        }
        with self._artifact_lock:
            self._artifacts[artifact_id] = stored
            self._artifact_scopes[artifact_id] = (resolved_scope["kind"], resolved_scope["id"])
            self._selection_index[selection_key] = artifact_id
        return {
            "artifact_id": artifact_id,
            "selection_key": selection_key,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "role": role,
            "label": label,
            "media_type": media_type,
            "preview": preview,
            "preview_size": preview_size,
            "truncated": preview_size < size,
            "full_value_available": True,
            "available": True,
            "size": size,
            "bytes": size,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "meta": deepcopy(resolved_meta),
        }

    def register_external_artifact_ref(
        self,
        *,
        action_call_id: str,
        artifact_type: str,
        label: str,
        ref: dict[str, Any],
        media_type: str = "application/json",
        meta: dict[str, Any] | None = None,
        artifact_scope: Mapping[str, Any] | None = None,
    ) -> ActionArtifact:
        artifact_id = f"act_art_{uuid.uuid4().hex}"
        selection_key = f"sel_{uuid.uuid4().hex}"
        role = self._artifact_role(artifact_type)
        preview = self._compact_value(ref, limit=4000)
        preview_size = self._safe_json_size(preview)
        path = ref.get("path") or ref.get("uri") or ref.get("url")
        resolved_meta = dict(meta or {})
        external_artifact_id = str(ref.get("artifact_id") or "").strip()
        if external_artifact_id:
            resolved_meta["external_artifact_id"] = external_artifact_id
        resolved_meta["artifact_scope"] = self._normalize_artifact_scope(
            artifact_scope,
            fallback_id=action_call_id,
        )
        resolved_scope = cast(dict[str, str], resolved_meta["artifact_scope"])
        stored = {
            "artifact_id": artifact_id,
            "selection_key": selection_key,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "role": role,
            "label": label,
            "media_type": media_type,
            "value": deepcopy(ref),
            "meta": deepcopy(resolved_meta),
            "size": int(ref.get("size", ref.get("bytes", 0)) or 0),
            "bytes": int(ref.get("bytes", ref.get("size", 0)) or 0),
            "sha256": str(ref.get("sha256", "")),
        }
        if path is not None:
            stored["path"] = str(path)
        with self._artifact_lock:
            self._artifacts[artifact_id] = stored
            self._artifact_scopes[artifact_id] = (resolved_scope["kind"], resolved_scope["id"])
            self._selection_index[selection_key] = artifact_id

        artifact_ref: ActionArtifact = {
            "artifact_id": artifact_id,
            "selection_key": selection_key,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "role": role,
            "label": label,
            "media_type": media_type,
            "preview": preview,
            "preview_size": preview_size,
            "truncated": False,
            "full_value_available": False,
            "available": True,
            "size": stored["size"],
            "bytes": stored["bytes"],
            "meta": deepcopy(resolved_meta),
        }
        if path is not None:
            artifact_ref["path"] = str(path)
        if stored["sha256"]:
            artifact_ref["sha256"] = stored["sha256"]
        return artifact_ref

    def _normalize_explicit_artifacts(
        self,
        *,
        action_call_id: str,
        artifact_refs: Any,
        artifacts: Any,
        artifact_scope: Mapping[str, Any] | None,
    ) -> list[ActionArtifact]:
        normalized: list[ActionArtifact] = []
        seen: set[tuple[str, str, str]] = set()

        def append_ref(ref: dict[str, Any], *, prefer_existing: bool = False):
            if not isinstance(ref, dict):
                return
            item = dict(ref)
            if not item.get("action_call_id"):
                item["action_call_id"] = action_call_id
            key = (
                str(item.get("artifact_id", "")),
                str(item.get("path") or item.get("uri") or item.get("url") or ""),
                str(item.get("sha256", "")),
            )
            if key in seen:
                return
            seen.add(key)
            if prefer_existing and item.get("artifact_id"):
                expected_scope = self._normalize_artifact_scope(artifact_scope, fallback_id=action_call_id)
                if expected_scope is not None:
                    transfer = self.read_artifact_transfer(
                        str(item["artifact_id"]),
                        expected_scope=expected_scope,
                    )
                    if transfer is not None:
                        canonical, _value = transfer
                        if str(canonical.get("selection_key") or "") == str(item.get("selection_key") or ""):
                            normalized.append(cast(ActionArtifact, item))
                            return
            if "value" in item:
                registered = self.register_execution_artifact(
                    action_call_id=action_call_id,
                    artifact_type=str(item.get("artifact_type", "artifact")),
                    label=str(item.get("label", item.get("artifact_type", "artifact"))),
                    value=item.get("value"),
                    media_type=str(item.get("media_type", "application/json")),
                    meta=cast(dict[str, Any], item.get("meta", {})) if isinstance(item.get("meta"), dict) else {},
                    artifact_scope=artifact_scope,
                )
                path = item.get("path") or item.get("uri") or item.get("url")
                if path is not None:
                    registered["path"] = str(path)
                normalized.append(registered)
                return
            if any(key in item for key in ("artifact_id", "path", "uri", "url")):
                normalized.append(
                    self.register_external_artifact_ref(
                        action_call_id=action_call_id,
                        artifact_type=str(item.get("artifact_type", "artifact_ref")),
                        label=str(item.get("label", item.get("name", item.get("path", item.get("uri", "artifact"))))),
                        ref=item,
                        media_type=str(item.get("media_type", item.get("mime_type", "application/json"))),
                        meta=cast(dict[str, Any], item.get("meta", {})) if isinstance(item.get("meta"), dict) else {},
                        artifact_scope=artifact_scope,
                    )
                )

        if isinstance(artifact_refs, list):
            for ref in artifact_refs:
                if isinstance(ref, dict):
                    append_ref(ref, prefer_existing=True)
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, dict):
                    append_ref(artifact)
        return normalized

    # ── instruction-heavy detection ────────────────────────────────────────

    def _is_instruction_heavy_record(self, record: ActionResult) -> bool:
        executor_type = str(record.get("executor_type", ""))
        if executor_type in self._INSTRUCTION_HEAVY_EXECUTOR_TYPES:
            return True
        kwargs = record.get("kwargs", {})
        if isinstance(kwargs, dict) and any(key in kwargs for key in self._INSTRUCTION_HEAVY_KWARGS):
            return True
        action_id = str(record.get("action_id", ""))
        return action_id in {
            "run_bash",
            "run_python",
            "run_nodejs",
            "query_sqlite",
            "write_sqlite",
            "browse",
            "search",
            "search_news",
            "search_wikipedia",
            "search_arxiv",
        }

    def _summarize_action_instruction(self, record: ActionResult) -> dict[str, Any]:
        kwargs = record.get("kwargs", {})
        if not isinstance(kwargs, dict):
            return {}
        for key in ("cmd", "command", "python_code", "js_code", "code", "query", "sql", "url"):
            if key in kwargs:
                return {
                    "kind": key,
                    "preview": self._compact_value(kwargs.get(key), limit=6000),
                }
        return self._compact_value(kwargs, limit=4000)

    def _build_execution_digest(
        self,
        record: ActionResult,
        *,
        artifact_refs: list[ActionArtifact],
        redaction_report: list[str],
    ) -> dict[str, Any]:
        data = record.get("data", record.get("result"))
        result_preview = self._compact_value(data, limit=8000)
        result_size = self._safe_json_size(data)
        result_preview_size = self._safe_json_size(result_preview)
        digest: dict[str, Any] = {
            "action_call_id": record.get("action_call_id", ""),
            "action_id": record.get("action_id", ""),
            "purpose": record.get("purpose", ""),
            "status": record.get("status", ""),
            "success": bool(record.get("success", record.get("ok", False))),
            "executor_type": record.get("executor_type", ""),
            "instruction": self._summarize_action_instruction(record),
            "result_preview": result_preview,
            "result_preview_meta": {
                "truncated": result_preview_size < result_size,
                "original_size": result_size,
                "preview_size": result_preview_size,
                "truncated_paths": self._preview_meta(data, limit=8000),
            },
            "artifact_refs": artifact_refs,
            "file_refs": record.get("file_refs", []),
        }
        error = record.get("error", "")
        if isinstance(error, str) and error:
            digest["error"] = self._compact_text(error, limit=4000)
        if redaction_report:
            digest["redaction_report"] = redaction_report
        return digest

    # ── result finalization ────────────────────────────────────────────────

    def finalize_action_result(
        self,
        result: Any,
        *,
        artifact_scope: Mapping[str, Any] | None = None,
    ) -> ActionResult:
        record = normalize_execution_record(result, None, 0) if not isinstance(result, dict) else cast(ActionResult, result)
        meta = record.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        recall_meta = meta.get("execution_recall", {})
        if isinstance(recall_meta, dict) and recall_meta.get("finalized") is True:
            return record

        action_call_id = str(record.get("action_call_id", "") or f"act_call_{uuid.uuid4().hex}")
        record["action_call_id"] = action_call_id

        data = record.get("data", record.get("result"))
        meta = record.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        explicit_artifact_refs = self._normalize_explicit_artifacts(
            action_call_id=action_call_id,
            artifact_refs=record.get("artifact_refs", []),
            artifacts=record.get("artifacts", []),
            artifact_scope=artifact_scope,
        )

        result_exceeds_inline_limit = (
            self._safe_json_size(data) > 8000
            or meta.get("max_output_bytes_exceeded") is True
        )
        should_externalize = self._is_instruction_heavy_record(record) or result_exceeds_inline_limit
        file_refs = self._collect_file_refs(record)
        if file_refs:
            record["file_refs"] = file_refs

        if explicit_artifact_refs and not should_externalize:
            meta["execution_recall"] = {
                "finalized": True,
                "digest_version": 1,
                "artifact_count": len(explicit_artifact_refs),
            }
            record["meta"] = meta
            record["artifact_refs"] = explicit_artifact_refs
            record["artifacts"] = explicit_artifact_refs
            return record

        if not should_externalize:
            return record

        kwargs = record.get("kwargs", {})
        artifact_refs: list[ActionArtifact] = list(explicit_artifact_refs)
        if isinstance(kwargs, dict) and kwargs:
            artifact_refs.append(
                self.register_execution_artifact(
                    action_call_id=action_call_id,
                    artifact_type="action_input",
                    label="Action input arguments",
                    value=kwargs,
                    artifact_scope=artifact_scope,
                )
            )
        if data is not None:
            artifact_refs.append(
                self.register_execution_artifact(
                    action_call_id=action_call_id,
                    artifact_type="action_output",
                    label="Action raw output",
                    value=data,
                    artifact_scope=artifact_scope,
                )
            )

        redaction_report = self._redaction_report_for_value(kwargs) if isinstance(kwargs, dict) else []
        meta["execution_recall"] = {
            "finalized": True,
            "digest_version": 1,
            "artifact_count": len(artifact_refs),
        }
        record["meta"] = meta
        record["artifact_refs"] = artifact_refs
        record["file_refs"] = file_refs
        record["artifacts"] = artifact_refs
        record["redaction_report"] = redaction_report
        record["model_digest"] = self._build_execution_digest(
            record,
            artifact_refs=artifact_refs,
            redaction_report=redaction_report,
        )
        # Keep the host-facing ActionResult complete in process. Model and
        # RuntimeEvent boundaries call the explicit projection helpers; doing
        # that here would collapse the authoritative host result and mix the
        # carrier policy into artifact finalization.
        return record

    def normalize_execution_records(
        self,
        records: Any,
        commands: list[Any],
        *,
        artifact_scope: Mapping[str, Any] | None = None,
    ) -> list[ActionResult]:
        if not isinstance(records, list):
            return []

        normalized: list[ActionResult] = []
        seen_call_ids: set[str] = set()
        for index, record in enumerate(records):
            command = commands[index] if index < len(commands) else None
            finalized = self.finalize_action_result(
                normalize_execution_record(record, command, index),
                artifact_scope=artifact_scope,
            )
            action_call_id = str(finalized.get("action_call_id", ""))
            if action_call_id and action_call_id in seen_call_ids:
                continue
            if action_call_id:
                seen_call_ids.add(action_call_id)
            normalized.append(finalized)
        return normalized

    # ── model-visible transformation ───────────────────────────────────────

    @classmethod
    def _to_model_visible_record(cls, record: ActionResult) -> ActionResult:
        if not isinstance(record, dict):
            return record
        digest = record.get("model_digest")
        if not isinstance(digest, dict):
            return cls._project_model_artifact_refs(record)
        if digest.get("same_as") == "result" and isinstance(record.get("result"), dict):
            return cls._project_model_artifact_refs(record)
        visible_digest = cls._to_hot_path_digest(digest)
        visible = cast(ActionResult, {
            "action_call_id": record.get("action_call_id", visible_digest.get("action_call_id", "")),
            "action_id": record.get("action_id", visible_digest.get("action_id", "")),
            "tool_name": record.get("tool_name", record.get("action_id", visible_digest.get("action_id", ""))),
            "purpose": record.get("purpose", visible_digest.get("purpose", "")),
            "status": record.get("status", visible_digest.get("status", "")),
            "success": bool(record.get("success", visible_digest.get("success", False))),
            "ok": bool(record.get("ok", record.get("success", visible_digest.get("success", False)))),
            "todo_suggestion": record.get("todo_suggestion", record.get("next", "")),
            "next": record.get("next", record.get("todo_suggestion", "")),
            "executor_type": record.get("executor_type", visible_digest.get("executor_type", "")),
        })
        visible["result"] = visible_digest
        preview_meta = visible_digest.get("result_preview_meta")
        hot_path_compacted = isinstance(preview_meta, dict) and preview_meta.get("hot_path_compacted") is True
        if hot_path_compacted:
            visible["data"] = {
                "same_as": "result",
                "action_call_id": visible_digest.get("action_call_id", ""),
                "hot_path_compacted": True,
            }
            visible["model_digest"] = {
                "same_as": "result",
                "action_call_id": visible_digest.get("action_call_id", ""),
                "hot_path_compacted": True,
            }
        else:
            visible["data"] = visible_digest
            visible["model_digest"] = visible_digest
        artifact_refs = visible_digest.get("artifact_refs", [])
        visible["artifact_refs"] = [
            cls._to_model_selection_candidate(ref)
            for ref in artifact_refs
            if isinstance(ref, dict)
        ] if isinstance(artifact_refs, list) else []
        if isinstance(visible.get("result"), dict):
            visible["result"]["artifact_refs"] = visible["artifact_refs"]
        visible["artifacts"] = visible["artifact_refs"]
        if record.get("error"):
            visible["error"] = cls._compact_text(record.get("error"), limit=1200)
        return visible

    @classmethod
    def to_model_visible_records(cls, records: list[ActionResult] | None) -> list[ActionResult]:
        if not isinstance(records, list):
            return []
        return [cls._to_model_visible_record(record) for record in records]

    @classmethod
    def _to_action_flow_return_records(
        cls,
        records: list[ActionResult] | None,
    ) -> list[ActionResult]:
        """Bound large ActionFlow results without collapsing small host results."""

        if not isinstance(records, list):
            return []
        return [
            cls._to_action_carrier_record(record)
            if cls._safe_json_size(record) > cls._ACTION_CARRIER_MAX_BYTES
            else record
            for record in records
        ]

    @classmethod
    def _to_action_carrier_record(cls, record: ActionResult) -> ActionResult:
        """Project an oversized complete record without carrying raw previews."""

        visible = cls._to_model_visible_record(record)
        host_refs = [
            cls._compact_action_carrier_artifact_ref(ref)
            for ref in cls.canonicalize_artifact_aliases(record, model_visible=False)
        ]
        visible["artifact_refs"] = cast(list[ActionArtifact], host_refs)
        visible["artifacts"] = visible["artifact_refs"]
        digest = visible.get("result")
        if isinstance(digest, dict):
            compact_digest = dict(digest)
            if "instruction" in compact_digest:
                compact_digest["instruction"] = {"omitted": True}
            if "result_preview" in compact_digest:
                compact_digest["result_preview"] = {"omitted": True}
            visible["result"] = compact_digest
            visible["data"] = {
                "same_as": "result",
                "action_call_id": compact_digest.get("action_call_id", ""),
                "carrier_compacted": True,
            }
            visible["model_digest"] = dict(visible["data"])
        return visible

    @classmethod
    def _compact_action_carrier_artifact_ref(cls, ref: Mapping[str, Any]) -> ActionArtifact:
        keep_keys = (
            "artifact_id",
            "selection_key",
            "action_call_id",
            "artifact_type",
            "role",
            "label",
            "media_type",
            "available",
            "full_value_available",
            "truncated",
            "preview_size",
            "size",
            "bytes",
            "sha256",
            "path",
            "meta",
        )
        compact = {key: deepcopy(ref.get(key)) for key in keep_keys if key in ref}
        if "preview" in ref:
            compact["preview_omitted"] = True
        return cast(ActionArtifact, compact)

    @classmethod
    def _to_hot_path_digest(cls, digest: dict[str, Any]) -> dict[str, Any]:
        if cls._safe_json_size(digest) <= cls._MODEL_VISIBLE_RECORD_MAX_BYTES:
            return dict(digest)
        compact = dict(digest)
        compact["instruction"] = cls._compact_hot_path_field(
            digest.get("instruction"),
            max_bytes=cls._MODEL_VISIBLE_INSTRUCTION_MAX_BYTES,
        )
        compact["result_preview"] = cls._compact_hot_path_field(
            digest.get("result_preview"),
            max_bytes=cls._MODEL_VISIBLE_RESULT_PREVIEW_MAX_BYTES,
        )
        preview_meta = dict(digest.get("result_preview_meta") or {})
        preview_meta["hot_path_compacted"] = True
        preview_meta["hot_path_preview_size"] = cls._safe_json_size(compact["result_preview"])
        compact["result_preview_meta"] = preview_meta
        compact["artifact_refs"] = [
            cls._compact_hot_path_artifact_ref(ref)
            for ref in digest.get("artifact_refs", [])
            if isinstance(ref, dict)
        ]
        if "artifacts" in compact:
            compact["artifacts"] = compact["artifact_refs"]
        if "file_refs" in compact:
            compact["file_refs"] = cls._compact_hot_path_field(compact.get("file_refs"), max_bytes=1200)
        if "error" in compact:
            compact["error"] = cls._compact_text(compact.get("error"), limit=1200)
        return compact

    @classmethod
    def _compact_hot_path_field(cls, value: Any, *, max_bytes: int) -> Any:
        compact_value = cls._compact_value(value, limit=max(400, max_bytes // 2))
        if cls._safe_json_size(compact_value) <= max_bytes:
            return compact_value
        return cls._json_preview(compact_value, max_bytes=max_bytes)

    @classmethod
    def _compact_hot_path_artifact_ref(cls, ref: dict[str, Any]) -> dict[str, Any]:
        keep_keys = (
            "selection_key",
            "artifact_type",
            "role",
            "label",
            "media_type",
            "available",
            "full_value_available",
            "truncated",
            "preview_size",
            "preview_omitted",
            "readback_action_id",
        )
        compact = {key: ref.get(key) for key in keep_keys if key in ref}
        if "preview" in ref:
            compact["preview_omitted"] = True
            compact["readback_action_id"] = cls._RECALL_ACTION_ID
        return compact

    @classmethod
    def _to_model_selection_candidate(cls, ref: Mapping[str, Any]) -> ActionArtifact:
        ref_data = dict(ref)
        candidate = cls._compact_hot_path_artifact_ref(ref_data)
        if "preview" in ref and "preview_omitted" not in candidate:
            candidate["preview"] = cls._compact_value(ref.get("preview"), limit=1200)
        return cast(ActionArtifact, candidate)

    @classmethod
    def _project_model_artifact_refs(cls, record: ActionResult) -> ActionResult:
        visible = cast(ActionResult, deepcopy(record))
        visible_refs = cls.canonicalize_artifact_aliases(record, model_visible=True)
        visible["artifact_refs"] = cast(list[ActionArtifact], visible_refs)
        visible["artifacts"] = cast(list[ActionArtifact], visible_refs)
        return visible

    @classmethod
    def canonicalize_artifact_aliases(
        cls,
        container: Mapping[str, Any],
        *,
        model_visible: bool,
    ) -> list[dict[str, Any]]:
        """Merge both aliases once and return one deduplicated canonical list."""

        merged: list[dict[str, Any]] = []
        primary_values = container.get("artifact_refs")
        primary_alias = (
            "artifact_refs"
            if isinstance(primary_values, Sequence)
            and not isinstance(primary_values, (str, bytes, bytearray))
            else "artifacts"
        )
        seen_primary: set[tuple[str, str, str, str]] = set()
        for alias in (primary_alias, "artifacts" if primary_alias == "artifact_refs" else "artifact_refs"):
            values = container.get(alias)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                continue
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                item = dict(value)
                key = (
                    str(item.get("selection_key") or ""),
                    str(item.get("artifact_id") or ""),
                    str(item.get("record_id") or item.get("id") or ""),
                    str(item.get("path") or item.get("content_ref") or ""),
                )
                if alias != primary_alias and key in seen_primary:
                    continue
                if alias == primary_alias:
                    seen_primary.add(key)
                merged.append(
                    dict(cls._to_model_selection_candidate(item))
                    if model_visible and item.get("selection_key")
                    else item
                )
        return merged

    # ── recall action injection ────────────────────────────────────────────

    def with_action_artifact_recall_action(
        self,
        action_list: list[dict[str, Any]],
        records: list[ActionResult] | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(records, list):
            return action_list
        has_artifact_refs = any(
            isinstance(record, dict)
            and (
                isinstance(record.get("artifact_refs"), list)
                and len(record.get("artifact_refs", [])) > 0
                or isinstance(record.get("artifacts"), list)
                and any(isinstance(item, dict) and item.get("artifact_id") for item in record.get("artifacts", []))
            )
            for record in records
        )
        if not has_artifact_refs:
            return action_list
        if any(item.get("action_id") == self._RECALL_ACTION_ID for item in action_list if isinstance(item, dict)):
            return action_list
        if self._registry is None:
            return action_list
        recall_spec = self._registry.get_spec(self._RECALL_ACTION_ID)
        if recall_spec is None:
            return action_list
        return [*action_list, dict(recall_spec, expose_to_model=True)]
