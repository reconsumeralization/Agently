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

import json
import uuid
from typing import Any, cast

from agently.types.data import ActionArtifact, ActionResult

from .ActionNormalization import normalize_execution_record


class ActionArtifactManager:
    _RECALL_ACTION_ID = "read_action_artifact"
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
        self._registry = registry

    # ── artifact storage access ────────────────────────────────────────────

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self._artifacts.get(str(artifact_id))

    # ── redaction / compaction ─────────────────────────────────────────────

    @classmethod
    def _is_sensitive_key(cls, key: Any) -> bool:
        lowered = str(key).lower()
        return any(keyword in lowered for keyword in cls._SENSITIVE_KEYWORDS)

    @staticmethod
    def _compact_text(value: Any, *, limit: int = 700) -> str:
        text = str(value)
        if len(text) <= limit:
            return text
        return f"{text[:limit]}... [truncated {len(text) - limit} chars]"

    @classmethod
    def _compact_value(cls, value: Any, *, limit: int = 700, depth: int = 0) -> Any:
        if cls._is_sensitive_key(value) and depth < 0:
            return "[REDACTED]"
        if isinstance(value, dict):
            if depth >= 2:
                return f"[dict keys={list(value.keys())[:8]}]"
            compact: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 12:
                    compact["..."] = f"{len(value) - index} more keys"
                    break
                if cls._is_sensitive_key(key):
                    compact[str(key)] = "[REDACTED]"
                else:
                    compact[str(key)] = cls._compact_value(item, limit=limit, depth=depth + 1)
            return compact
        if isinstance(value, (list, tuple, set)):
            items = list(value)
            compact_items = [cls._compact_value(item, limit=limit, depth=depth + 1) for item in items[:8]]
            if len(items) > 8:
                compact_items.append(f"... {len(items) - 8} more items")
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
    ) -> ActionArtifact:
        artifact_id = f"act_art_{uuid.uuid4().hex}"
        safe_value = self._redact_value(value)
        preview = self._compact_value(safe_value, limit=500)
        size = self._safe_json_size(safe_value)
        stored = {
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "label": label,
            "media_type": media_type,
            "value": safe_value,
            "meta": meta or {},
            "size": size,
        }
        self._artifacts[artifact_id] = stored
        return {
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "label": label,
            "media_type": media_type,
            "preview": preview,
            "truncated": size > 500,
            "full_value_available": True,
            "available": True,
            "size": size,
            "meta": meta or {},
        }

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
                    "preview": self._compact_value(kwargs.get(key), limit=900),
                }
        return self._compact_value(kwargs, limit=500)

    def _build_execution_digest(
        self,
        record: ActionResult,
        *,
        artifact_refs: list[ActionArtifact],
        redaction_report: list[str],
    ) -> dict[str, Any]:
        data = record.get("data", record.get("result"))
        digest: dict[str, Any] = {
            "action_call_id": record.get("action_call_id", ""),
            "action_id": record.get("action_id", ""),
            "purpose": record.get("purpose", ""),
            "status": record.get("status", ""),
            "success": bool(record.get("success", record.get("ok", False))),
            "executor_type": record.get("executor_type", ""),
            "instruction": self._summarize_action_instruction(record),
            "result_preview": self._compact_value(data, limit=900),
            "artifact_refs": artifact_refs,
        }
        error = record.get("error", "")
        if isinstance(error, str) and error:
            digest["error"] = self._compact_text(error, limit=700)
        if redaction_report:
            digest["redaction_report"] = redaction_report
        return digest

    # ── result finalization ────────────────────────────────────────────────

    def finalize_action_result(self, result: Any) -> ActionResult:
        record = normalize_execution_record(result, None, 0) if not isinstance(result, dict) else cast(ActionResult, result)
        meta = record.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        recall_meta = meta.get("execution_recall", {})
        if isinstance(recall_meta, dict) and recall_meta.get("finalized") is True:
            return record

        action_call_id = str(record.get("action_call_id", "") or f"act_call_{uuid.uuid4().hex}")
        record["action_call_id"] = action_call_id

        if not self._is_instruction_heavy_record(record):
            return record

        kwargs = record.get("kwargs", {})
        data = record.get("data", record.get("result"))
        artifact_refs: list[ActionArtifact] = []
        if isinstance(kwargs, dict) and kwargs:
            artifact_refs.append(
                self.register_execution_artifact(
                    action_call_id=action_call_id,
                    artifact_type="action_input",
                    label="Action input arguments",
                    value=kwargs,
                )
            )
        if data is not None:
            artifact_refs.append(
                self.register_execution_artifact(
                    action_call_id=action_call_id,
                    artifact_type="action_output",
                    label="Action raw output",
                    value=data,
                )
            )

        existing_artifacts = record.get("artifacts", [])
        if isinstance(existing_artifacts, list):
            for artifact in existing_artifacts:
                if not isinstance(artifact, dict):
                    continue
                if artifact.get("artifact_id"):
                    artifact_refs.append(cast(ActionArtifact, artifact))
                    continue
                if "value" in artifact:
                    artifact_refs.append(
                        self.register_execution_artifact(
                            action_call_id=action_call_id,
                            artifact_type=str(artifact.get("artifact_type", "artifact")),
                            label=str(artifact.get("label", artifact.get("artifact_type", "artifact"))),
                            value=artifact.get("value"),
                            media_type=str(artifact.get("media_type", "application/json")),
                            meta=cast(dict[str, Any], artifact.get("meta", {})) if isinstance(artifact.get("meta"), dict) else {},
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
        record["artifacts"] = artifact_refs
        record["redaction_report"] = redaction_report
        record["model_digest"] = self._build_execution_digest(
            record,
            artifact_refs=artifact_refs,
            redaction_report=redaction_report,
        )
        return record

    def normalize_execution_records(
        self,
        records: Any,
        commands: list[Any],
    ) -> list[ActionResult]:
        if not isinstance(records, list):
            return []

        normalized: list[ActionResult] = []
        for index, record in enumerate(records):
            command = commands[index] if index < len(commands) else None
            normalized.append(self.finalize_action_result(normalize_execution_record(record, command, index)))
        return normalized

    # ── model-visible transformation ───────────────────────────────────────

    @classmethod
    def _to_model_visible_record(cls, record: ActionResult) -> ActionResult:
        if not isinstance(record, dict):
            return record
        digest = record.get("model_digest")
        if not isinstance(digest, dict):
            return record
        visible = cast(ActionResult, dict(record))
        visible["result"] = digest
        visible["data"] = digest
        artifact_refs = record.get("artifact_refs", record.get("artifacts", []))
        visible["artifacts"] = artifact_refs if isinstance(artifact_refs, list) else []
        return visible

    @classmethod
    def to_model_visible_records(cls, records: list[ActionResult] | None) -> list[ActionResult]:
        if not isinstance(records, list):
            return []
        return [cls._to_model_visible_record(record) for record in records]

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
