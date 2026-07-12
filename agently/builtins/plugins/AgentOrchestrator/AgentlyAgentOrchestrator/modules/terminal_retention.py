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

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING, cast

from agently.types.data import (
    WorkspaceFileRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetainedReference,
    WorkspaceRetentionResult,
    WorkspaceRetentionTerminalStatus,
)
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


_TERMINAL_INLINE_RESULT_LIMIT = 4096


def _serialized_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


async def prepare_agent_execution_terminal_retention(
    owner: "AgentExecution",
) -> tuple[Any, list[WorkspaceRecordRef]]:
    """Return the bounded terminal-event result and canonical retained refs."""

    result = _terminal_result_value(owner)
    retained_records, retained_refs = await _canonical_result_refs(owner, result)
    if _serialized_size(result) <= _TERMINAL_INLINE_RESULT_LIMIT:
        owner._terminal_inline_result = result
        owner._terminal_retained_refs = retained_refs
        return result, retained_records
    if retained_refs:
        event_result = _compact_referenced_result(result, retained_refs)
        owner._terminal_inline_result = None
        owner._terminal_retained_refs = retained_refs
        return event_result, retained_records
    if owner.workspace is None:
        raise RuntimeError("AgentExecution has no Workspace binding for a large terminal result.")

    record_ref = await owner.workspace.put_artifact_ref(
        owner.id,
        result,
        metadata={
            "scope": {"execution_id": owner.id},
            "kind": "agent_execution_terminal_result",
            "summary": f"Terminal result for AgentExecution { owner.id }",
        },
    )
    await owner.workspace.add_retention_anchor(
        owner.id,
        anchor_type="deliverable",
        record_ref=record_ref,
        meta={"owner": "AgentExecution", "kind": "terminal_result"},
    )
    owner._terminal_anchored_ref_ids.add(record_ref["id"])
    event_result = await owner.workspace.ref_envelope(record_ref)
    owner._terminal_inline_result = None
    owner._terminal_retained_refs = [record_ref]
    return event_result, [record_ref]


async def apply_agent_execution_terminal_retention(
    owner: "AgentExecution",
    *,
    status: WorkspaceRetentionTerminalStatus,
) -> WorkspaceRetentionResult | None:
    """Apply Workspace-owned cleanup without changing business status or result."""

    if owner.workspace is None:
        return None
    if owner._terminal_retention_deferred:
        _record_deferred_diagnostics(owner)
        return None
    try:
        preview = await owner.workspace.inspect_retention(
            {},
            lifecycle={
                "execution_id": owner.id,
                "status": status,
                "terminal_at": datetime.now(timezone.utc).isoformat(),
                "state_version": None,
                "recovery_active": False,
                "lease_active": False,
            },
            retained_refs=[
                ref
                for ref in owner._terminal_retained_refs
                if str(ref.get("id") or "") not in owner._terminal_anchored_ref_ids
            ],
            inline_result=owner._terminal_inline_result,
            policy=cast(Any, owner.options.get("workspace_retention_policy")),
        )
        if preview["status"] == "ready":
            result = await owner.workspace.apply_retention(preview)
        else:
            result = cast(
                WorkspaceRetentionResult,
                {
                    "status": "deferred",
                    "plan_fingerprint": preview["plan_fingerprint"],
                    "manifest_ref": None,
                    "retained_refs": preview["retained_refs"],
                    "accounting": preview["accounting"],
                    "diagnostics": preview["diagnostics"],
                },
            )
    except Exception as error:
        owner.diagnostics["workspace_retention"] = {
            "status": "deferred",
            "manifest_ref": None,
            "accounting": {},
            "diagnostics": [
                {
                    "code": "agent_execution.retention.apply_failed",
                    "message": _compact_error(error),
                }
            ],
        }
        return None
    owner.diagnostics["workspace_retention"] = {
        "status": result["status"],
        "manifest_ref": DataFormatter.sanitize(result["manifest_ref"]),
        "accounting": DataFormatter.sanitize(result["accounting"]),
        "diagnostics": [
            {
                "code": str(item.get("code") or ""),
                "message": str(item.get("message") or "")[:360],
            }
            for item in result["diagnostics"][:8]
        ],
    }
    return result


def defer_agent_execution_terminal_retention(owner: "AgentExecution", *, code: str, error: BaseException) -> None:
    owner._terminal_retention_deferred = True
    owner._terminal_retention_diagnostics.append({"code": code, "message": _compact_error(error)})


def _terminal_result_value(owner: "AgentExecution") -> Any:
    if owner.result is not None:
        return DataFormatter.sanitize(owner.result)
    error = owner._error
    if error is None:
        return {"status": owner.status}
    return {
        "status": owner.status,
        "error": {
            "type": error.__class__.__name__,
            "message": _compact_error(error),
        },
    }


async def _canonical_result_refs(
    owner: "AgentExecution",
    result: Any,
) -> tuple[list[WorkspaceRecordRef], list[WorkspaceRetainedReference]]:
    if owner.workspace is None or not isinstance(result, Mapping):
        return [], []
    retained_records: list[WorkspaceRecordRef] = []
    retained_refs: list[WorkspaceRetainedReference] = []
    seen: set[str] = set()
    for raw_ref in _result_ref_candidates(result):
        if not isinstance(raw_ref, Mapping):
            continue
        envelope_id = str(raw_ref.get("record_id") or "")
        if envelope_id and "workspace_id" in raw_ref:
            key = f"record:{envelope_id}"
            if key in seen:
                continue
            try:
                canonical_envelope = await owner.workspace.ref_envelope(envelope_id)
            except Exception:
                continue
            if not _envelope_identity_matches(raw_ref, canonical_envelope):
                continue
            retained_refs.append(canonical_envelope)
            seen.add(key)
            continue

        ref_id = str(raw_ref.get("id") or "")
        if ref_id and _looks_like_record_ref(raw_ref):
            key = f"record:{ref_id}"
            if key in seen:
                continue
            try:
                envelope = await owner.workspace.ref_envelope(cast(dict[str, Any], dict(raw_ref)))
            except Exception:
                continue
            if envelope.get("record_id") != ref_id:
                continue
            record_ref = cast(WorkspaceRecordRef, DataFormatter.sanitize(dict(raw_ref)))
            retained_records.append(record_ref)
            retained_refs.append(record_ref)
            seen.add(key)
            continue

        if _looks_like_file_ref(raw_ref):
            path = str(raw_ref.get("path") or "")
            key = f"file:{path}"
            if key in seen:
                continue
            try:
                readback = await owner.workspace.read_file(path, max_bytes=1)
            except Exception:
                continue
            if str(readback.get("sha256") or "") != str(raw_ref.get("sha256") or ""):
                continue
            if int(readback.get("bytes") or 0) != int(raw_ref.get("bytes") or 0):
                continue
            file_ref = cast(
                WorkspaceFileRef,
                {
                    "path": path,
                    "bytes": int(readback.get("bytes") or 0),
                    "sha256": str(readback.get("sha256") or ""),
                    "media_type": raw_ref.get("media_type") or readback.get("media_type"),
                    "content_kind": str(raw_ref.get("content_kind") or readback.get("content_kind") or "unknown"),
                    "role": str(raw_ref.get("role") or "workspace_artifact"),
                },
            )
            retained_refs.append(file_ref)
            seen.add(key)
    return retained_records, retained_refs


def _result_ref_candidates(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    for key in ("artifact_refs", "file_refs"):
        values = result.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            candidates.extend(value for value in values if isinstance(value, Mapping))
    final_result = result.get("final_result")
    if isinstance(final_result, Mapping):
        candidates.append(final_result)
    return candidates


def _looks_like_record_ref(ref: Mapping[str, Any]) -> bool:
    return all(
        key in ref
        for key in ("id", "collection", "kind", "path", "size", "summary", "scope", "source", "created_at", "meta")
    )


def _looks_like_file_ref(ref: Mapping[str, Any]) -> bool:
    try:
        size = int(ref.get("bytes") or 0)
    except (TypeError, ValueError):
        return False
    return bool(str(ref.get("path") or "") and str(ref.get("sha256") or "") and size > 0)


def _envelope_identity_matches(
    candidate: Mapping[str, Any],
    canonical: WorkspaceReferenceEnvelope,
) -> bool:
    for key in ("workspace_id", "record_id", "collection", "kind", "content_ref", "digest", "size"):
        if candidate.get(key) != canonical.get(key):
            return False
    return True


def _compact_referenced_result(result: Any, refs: list[WorkspaceRetainedReference]) -> dict[str, Any]:
    compact: dict[str, Any] = {"artifact_refs": DataFormatter.sanitize(refs)}
    if isinstance(result, Mapping):
        for key in (
            "status",
            "accepted",
            "artifact_status",
            "task_id",
            "execution_strategy",
            "effective_execution_strategy",
            "final_result",
        ):
            value = result.get(key)
            if value is not None and _serialized_size(value) <= 1600:
                compact[key] = DataFormatter.sanitize(value)
    return compact


def _record_deferred_diagnostics(owner: "AgentExecution") -> None:
    owner.diagnostics["workspace_retention"] = {
        "status": "deferred",
        "manifest_ref": None,
        "accounting": {},
        "diagnostics": DataFormatter.sanitize(owner._terminal_retention_diagnostics[:8]),
    }


def _compact_error(error: BaseException) -> str:
    return (str(error).strip() or error.__class__.__name__)[:360]
