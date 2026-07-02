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
from typing import Any, cast

from agently.types.data import ActionArtifact, ActionResult, WorkspaceFileRef

from .ActionNormalization import normalize_execution_record


class ActionArtifactManager:
    _RECALL_ACTION_ID = "read_action_artifact"
    _MODEL_VISIBLE_RECORD_MAX_BYTES = 6000
    _MODEL_VISIBLE_RESULT_PREVIEW_MAX_BYTES = 2400
    _MODEL_VISIBLE_INSTRUCTION_MAX_BYTES = 1200
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
    ) -> ActionArtifact:
        artifact_id = f"act_art_{uuid.uuid4().hex}"
        safe_value = self._redact_value(value)
        preview = self._compact_value(safe_value, limit=4000)
        raw_bytes = self._json_bytes(safe_value)
        size = len(raw_bytes)
        preview_size = self._safe_json_size(preview)
        role = self._artifact_role(artifact_type)
        stored = {
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "role": role,
            "label": label,
            "media_type": media_type,
            "value": safe_value,
            "meta": meta or {},
            "size": size,
            "bytes": size,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        }
        self._artifacts[artifact_id] = stored
        return {
            "artifact_id": artifact_id,
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
            "meta": meta or {},
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
    ) -> ActionArtifact:
        artifact_id = str(ref.get("artifact_id") or f"act_art_{uuid.uuid4().hex}")
        role = self._artifact_role(artifact_type)
        preview = self._compact_value(ref, limit=4000)
        preview_size = self._safe_json_size(preview)
        path = ref.get("path") or ref.get("uri") or ref.get("url")
        stored = {
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "role": role,
            "label": label,
            "media_type": media_type,
            "value": self._redact_value(ref),
            "meta": meta or {},
            "size": int(ref.get("size", ref.get("bytes", 0)) or 0),
            "bytes": int(ref.get("bytes", ref.get("size", 0)) or 0),
            "sha256": str(ref.get("sha256", "")),
        }
        if path is not None:
            stored["path"] = str(path)
        self._artifacts[artifact_id] = stored

        artifact_ref: ActionArtifact = {
            "artifact_id": artifact_id,
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
            "meta": meta or {},
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
            if prefer_existing or item.get("artifact_id"):
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
                )
                path = item.get("path") or item.get("uri") or item.get("url")
                if path is not None:
                    registered["path"] = str(path)
                normalized.append(registered)
                return
            if any(key in item for key in ("path", "uri", "url")):
                normalized.append(
                    self.register_external_artifact_ref(
                        action_call_id=action_call_id,
                        artifact_type=str(item.get("artifact_type", "artifact_ref")),
                        label=str(item.get("label", item.get("name", item.get("path", item.get("uri", "artifact"))))),
                        ref=item,
                        media_type=str(item.get("media_type", item.get("mime_type", "application/json"))),
                        meta=cast(dict[str, Any], item.get("meta", {})) if isinstance(item.get("meta"), dict) else {},
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

        data = record.get("data", record.get("result"))
        meta = record.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        explicit_artifact_refs = self._normalize_explicit_artifacts(
            action_call_id=action_call_id,
            artifact_refs=record.get("artifact_refs", []),
            artifacts=record.get("artifacts", []),
        )

        should_externalize = (
            self._is_instruction_heavy_record(record)
            or self._safe_json_size(data) > 8000
            or meta.get("max_output_bytes_exceeded") is True
        )
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
        return record

    def normalize_execution_records(
        self,
        records: Any,
        commands: list[Any],
    ) -> list[ActionResult]:
        if not isinstance(records, list):
            return []

        normalized: list[ActionResult] = []
        seen_call_ids: set[str] = set()
        for index, record in enumerate(records):
            command = commands[index] if index < len(commands) else None
            finalized = self.finalize_action_result(normalize_execution_record(record, command, index))
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
            return record
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
        visible["artifact_refs"] = artifact_refs if isinstance(artifact_refs, list) else []
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
            "artifact_id",
            "action_call_id",
            "artifact_type",
            "role",
            "label",
            "media_type",
            "available",
            "full_value_available",
            "size",
            "bytes",
            "sha256",
            "truncated",
            "preview_size",
            "meta",
        )
        compact = {key: ref.get(key) for key in keep_keys if key in ref}
        if "preview" in ref:
            compact["preview_omitted"] = True
            compact["readback_action_id"] = cls._RECALL_ACTION_ID
        return compact

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
