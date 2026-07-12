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

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING, cast

from agently.core.Workspace.Retention import resolve_retention_policy, serialized_size
from agently.types.data import (
    WorkspaceFileRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetainedReference,
    WorkspaceRetentionPolicy,
    WorkspaceRetentionResult,
    WorkspaceRetentionTerminalStatus,
)
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


def _serialized_size(value: Any) -> int:
    return serialized_size(value)


async def prepare_agent_execution_terminal_retention(
    owner: "AgentExecution",
) -> tuple[Any, list[WorkspaceRecordRef]]:
    """Return the bounded terminal-event result and canonical retained refs."""

    result = _terminal_result_value(owner)
    try:
        policy = resolve_retention_policy(
            cast(WorkspaceRetentionPolicy | None, owner.options.get("workspace_retention_policy")),
            supports_cold=True,
        )
    except Exception as error:
        defer_agent_execution_terminal_retention(
            owner,
            code="agent_execution.retention.policy_invalid",
            error=error,
        )
        policy = resolve_retention_policy(None, supports_cold=True)
    owner._terminal_retention_policy = policy
    inline_result_limit = cast(int, policy.get("inline_result_limit"))
    retained_records, retained_refs, file_backed = await _canonical_result_refs(owner, result)
    if owner._terminal_retention_deferred:
        owner._terminal_inline_result = None
        owner._terminal_retained_refs = []
        return {
            "status": owner.status,
            "kind": "agent_execution_terminal_result_untrusted",
        }, []
    if file_backed:
        event_result = _compact_referenced_result(result, retained_refs, file_backed=True)
        owner._terminal_inline_result = None
        owner._terminal_retained_refs = retained_refs
        return event_result, retained_records
    if _serialized_size(result) <= inline_result_limit:
        owner._terminal_inline_result = result
        owner._terminal_retained_refs = retained_refs
        return result, retained_records
    if retained_refs:
        event_result = _compact_referenced_result(result, retained_refs, file_backed=False)
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
            policy=cast(WorkspaceRetentionPolicy, owner._terminal_retention_policy),
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
) -> tuple[list[WorkspaceRecordRef], list[WorkspaceRetainedReference], bool]:
    if owner.workspace is None or not isinstance(result, Mapping):
        return [], [], False
    retained_records: list[WorkspaceRecordRef] = []
    retained_refs: list[WorkspaceRetainedReference] = []
    file_backed = False
    seen: set[str] = set()
    for raw_ref, selected in _result_ref_candidates(owner, result):
        if not isinstance(raw_ref, Mapping):
            continue
        action_artifact_id = str(raw_ref.get("artifact_id") or "").strip()
        if action_artifact_id:
            key = f"action:{action_artifact_id}"
            if key in seen:
                continue
            if not selected:
                continue
            promoted_ref = await _promote_selected_action_artifact(owner, raw_ref)
            if promoted_ref is None:
                continue
            retained_records.append(promoted_ref)
            retained_refs.append(promoted_ref)
            seen.add(key)
            continue
        envelope_id = str(raw_ref.get("record_id") or "")
        if envelope_id and "workspace_id" in raw_ref:
            key = f"record:{envelope_id}"
            if key in seen:
                continue
            canonical_ref, canonical_envelope, content = await _canonical_artifact_record(owner, raw_ref)
            if canonical_ref is None or canonical_envelope is None:
                continue
            if not _envelope_identity_matches(raw_ref, canonical_envelope):
                _defer_untrusted_ref(
                    owner,
                    "agent_execution.retention.reference_identity_mismatch",
                    "Terminal Workspace envelope does not match its persisted canonical identity.",
                )
                continue
            retained_refs.append(canonical_envelope)
            file_backed = file_backed or _content_is_file_ref(content)
            seen.add(key)
            continue

        ref_id = str(raw_ref.get("id") or "")
        if ref_id and _looks_like_record_ref(raw_ref):
            key = f"record:{ref_id}"
            if key in seen:
                continue
            canonical_ref, _envelope, content = await _canonical_artifact_record(owner, raw_ref)
            if canonical_ref is None:
                continue
            if not _record_identity_matches(raw_ref, canonical_ref):
                _defer_untrusted_ref(
                    owner,
                    "agent_execution.retention.reference_identity_mismatch",
                    "Terminal Workspace record ref does not match its persisted canonical identity.",
                )
                continue
            retained_records.append(canonical_ref)
            retained_refs.append(canonical_ref)
            file_backed = file_backed or _content_is_file_ref(content)
            seen.add(key)
            continue

        if ref_id:
            _defer_untrusted_ref(
                owner,
                "agent_execution.retention.reference_invalid",
                "Terminal Workspace record candidate is not a complete WorkspaceRecordRef.",
            )
            continue

        if _looks_like_file_ref(raw_ref):
            path = str(raw_ref.get("path") or "")
            key = f"file:{path}"
            if key in seen:
                continue
            try:
                readback = await owner.workspace.read_file(path, max_bytes=1)
            except Exception as error:
                _defer_untrusted_ref(
                    owner,
                    "agent_execution.retention.file_readback_failed",
                    f"Terminal Workspace file ref readback failed: {_compact_error(error)}",
                )
                continue
            if str(readback.get("sha256") or "") != str(raw_ref.get("sha256") or ""):
                _defer_untrusted_ref(
                    owner,
                    "agent_execution.retention.file_identity_mismatch",
                    "Terminal Workspace file ref digest does not match persisted readback.",
                )
                continue
            if int(readback.get("bytes") or 0) != int(raw_ref.get("bytes") or 0):
                _defer_untrusted_ref(
                    owner,
                    "agent_execution.retention.file_identity_mismatch",
                    "Terminal Workspace file ref size does not match persisted readback.",
                )
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
            file_backed = True
            seen.add(key)
            continue

        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.reference_invalid",
            "Terminal artifact ref candidate is not a supported Workspace reference.",
        )
    return retained_records, retained_refs, file_backed


def _result_ref_candidates(
    owner: "AgentExecution",
    result: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], bool]]:
    candidates: list[tuple[Mapping[str, Any], bool]] = []
    action_selection_accepted = result.get("accepted") is True
    for key in ("artifact_refs", "file_refs"):
        values = result.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            candidates.extend(
                (value, action_selection_accepted)
                for value in values
                if isinstance(value, Mapping)
            )
    final_result = result.get("final_result")
    if isinstance(final_result, Mapping) and _mapping_has_ref_shape(final_result):
        candidates.append((final_result, action_selection_accepted))
    if isinstance(final_result, Mapping):
        for key in ("artifact_refs", "file_refs"):
            values = final_result.get(key)
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
                candidates.extend(
                    (value, action_selection_accepted)
                    for value in values
                    if isinstance(value, Mapping)
                )
    evidence = result.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes, bytearray)):
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            for key in ("artifact_refs", "file_refs"):
                values = item.get(key)
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
                    candidates.extend(
                        (value, action_selection_accepted)
                        for value in values
                        if isinstance(value, Mapping)
                    )
    action_refs = owner.logs.get("artifact_refs")
    if isinstance(action_refs, Sequence) and not isinstance(action_refs, (str, bytes, bytearray)):
        candidates.extend((value, False) for value in action_refs if isinstance(value, Mapping))
    return candidates


async def _promote_selected_action_artifact(
    owner: "AgentExecution",
    raw_ref: Mapping[str, Any],
) -> WorkspaceRecordRef | None:
    artifact_id = str(raw_ref.get("artifact_id") or "").strip()
    action_call_id = str(raw_ref.get("action_call_id") or "").strip()
    if not artifact_id or not action_call_id:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_invalid",
            "Selected Action artifact ref is missing artifact_id or action_call_id.",
        )
        return None
    if _action_artifact_identity(raw_ref) not in _bridged_action_artifact_identities(owner):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_not_bridged",
            "Selected Action artifact ref was not emitted by this AgentExecution action bridge.",
        )
        return None
    action = getattr(owner.agent, "action", None)
    artifact_manager = getattr(action, "_artifact_manager", None)
    get_artifact = getattr(artifact_manager, "get_artifact", None)
    get_artifact_value = getattr(artifact_manager, "get_artifact_value", None)
    if not callable(get_artifact) or not callable(get_artifact_value):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_store_unavailable",
            "Selected Action artifact store is unavailable for terminal promotion.",
        )
        return None
    stored_artifact = get_artifact(artifact_id)
    stored_identity = _action_artifact_identity(stored_artifact) if isinstance(stored_artifact, Mapping) else None
    if stored_identity != _action_artifact_identity(raw_ref):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_identity_mismatch",
            "Selected Action artifact ref no longer matches the in-memory artifact identity.",
        )
        return None
    value = get_artifact_value(artifact_id)
    if value is None:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_missing",
            "Selected Action artifact value is no longer available for terminal promotion.",
        )
        return None
    try:
        return await owner.workspace.put_artifact_ref(
            owner.id,
            value,
            metadata={
                "scope": {"execution_id": owner.id},
                "kind": "agent_execution_action_artifact",
                "summary": f"Selected Action artifact {artifact_id} for AgentExecution {owner.id}",
                "action_artifact_id": artifact_id,
                "action_call_id": action_call_id,
            },
        )
    except Exception as error:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_promotion_failed",
            f"Selected Action artifact promotion failed: {_compact_error(error)}",
        )
        return None


def _bridged_action_artifact_identities(owner: "AgentExecution") -> set[tuple[str, str, str, int]]:
    values = owner.logs.get("artifact_refs")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return set()
    return {
        _action_artifact_identity(value)
        for value in values
        if isinstance(value, Mapping) and value.get("artifact_id")
    }


def _action_artifact_identity(ref: Mapping[str, Any]) -> tuple[str, str, str, int]:
    try:
        size = int(ref.get("size") or ref.get("bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    return (
        str(ref.get("artifact_id") or ""),
        str(ref.get("action_call_id") or ""),
        str(ref.get("sha256") or ""),
        size,
    )


async def _canonical_artifact_record(
    owner: "AgentExecution",
    raw_ref: Mapping[str, Any],
) -> tuple[WorkspaceRecordRef | None, WorkspaceReferenceEnvelope | None, Any]:
    is_envelope = "workspace_id" in raw_ref
    record_id = str((raw_ref.get("record_id") if is_envelope else raw_ref.get("id")) or "").strip()
    try:
        matches = await owner.workspace.search(filters={"id": record_id})
    except Exception as error:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.reference_lookup_failed",
            f"Terminal Workspace record lookup failed: {_compact_error(error)}",
        )
        return None, None, None
    if len(matches) != 1:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.reference_lookup_failed",
            "Terminal Workspace record is absent from the current scoped Workspace.",
        )
        return None, None, None
    canonical_ref = cast(WorkspaceRecordRef, matches[0])
    canonical_meta = canonical_ref.get("meta")
    canonical_source = canonical_ref.get("source")
    if not (
        canonical_ref.get("collection") == "artifacts"
        and isinstance(canonical_meta, Mapping)
        and canonical_meta.get("artifact_ref") is True
        and isinstance(canonical_source, Mapping)
        and canonical_source.get("type") == "workspace"
        and canonical_source.get("name") == "artifact_ref"
    ):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.reference_provenance_mismatch",
            "Persisted Workspace record is not a canonical artifact ref.",
        )
        return None, None, None
    try:
        canonical_size = int(canonical_ref.get("size") or 0)
        readback = await owner.workspace.read_bounded(canonical_ref, offset=0, limit=canonical_size + 1)
        canonical_envelope = await owner.workspace.ref_envelope(canonical_ref)
    except Exception as error:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.reference_readback_failed",
            f"Terminal Workspace record readback failed: {_compact_error(error)}",
        )
        return None, None, None
    readback_content = str(readback.get("content") or "")
    readback_raw = readback_content.encode("utf-8")
    if not (
        readback.get("eof") is True
        and int(readback.get("size") or 0) == canonical_size
        and len(readback_raw) == canonical_size
        and hashlib.sha256(readback_raw).hexdigest() == str(canonical_ref.get("sha256") or "")
    ):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.reference_integrity_mismatch",
            "Persisted Workspace artifact content does not match its canonical record ref.",
        )
        return None, None, None
    try:
        content = json.loads(readback_content)
    except (TypeError, ValueError):
        content = readback_content
    return canonical_ref, canonical_envelope, content


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


def _mapping_has_ref_shape(ref: Mapping[str, Any]) -> bool:
    return bool("workspace_id" in ref or _looks_like_record_ref(ref) or _looks_like_file_ref(ref))


def _record_identity_matches(candidate: Mapping[str, Any], canonical: WorkspaceRecordRef) -> bool:
    return all(
        candidate.get(key) == canonical.get(key)
        for key in (
            "id",
            "collection",
            "kind",
            "path",
            "sha256",
            "size",
            "summary",
            "scope",
            "source",
            "created_at",
            "meta",
        )
    )


def _envelope_identity_matches(
    candidate: Mapping[str, Any],
    canonical: WorkspaceReferenceEnvelope,
) -> bool:
    for key in ("workspace_id", "record_id", "collection", "kind", "content_ref", "digest", "size"):
        if candidate.get(key) != canonical.get(key):
            return False
    return True


def _content_is_file_ref(content: Any) -> bool:
    return isinstance(content, Mapping) and _looks_like_file_ref(content)


def _compact_referenced_result(
    result: Any,
    refs: list[WorkspaceRetainedReference],
    *,
    file_backed: bool,
) -> dict[str, Any]:
    compact: dict[str, Any] = {"artifact_refs": DataFormatter.sanitize(refs)}
    if isinstance(result, Mapping):
        for key in (
            "status",
            "accepted",
            "artifact_status",
            "task_id",
            "execution_strategy",
            "effective_execution_strategy",
        ):
            value = result.get(key)
            if value is not None and _serialized_size(value) <= 1600:
                compact[key] = DataFormatter.sanitize(value)
        if not file_backed:
            value = result.get("final_result")
            if value is not None and _serialized_size(value) <= 1600:
                compact["final_result"] = DataFormatter.sanitize(value)
    return compact


def _defer_untrusted_ref(owner: "AgentExecution", code: str, message: str) -> None:
    owner._terminal_retention_deferred = True
    owner._terminal_retention_diagnostics.append({"code": code, "message": message[:360]})


def _record_deferred_diagnostics(owner: "AgentExecution") -> None:
    owner.diagnostics["workspace_retention"] = {
        "status": "deferred",
        "manifest_ref": None,
        "accounting": {},
        "diagnostics": DataFormatter.sanitize(owner._terminal_retention_diagnostics[:8]),
    }


def _compact_error(error: BaseException) -> str:
    return (str(error).strip() or error.__class__.__name__)[:360]
