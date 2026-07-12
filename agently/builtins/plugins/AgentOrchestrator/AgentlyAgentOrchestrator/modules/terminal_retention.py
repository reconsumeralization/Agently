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
) -> tuple[Any, list[WorkspaceRetainedReference]]:
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
    retained_records, retained_refs, file_backed, promoted_action_refs = await _canonical_result_refs(owner, result)
    if owner._terminal_retention_deferred:
        owner._terminal_inline_result = None
        owner._terminal_retained_refs = []
        return {
            "status": owner.status,
            "kind": "agent_execution_terminal_result_untrusted",
        }, []
    projected_result = _project_promoted_action_refs(result, promoted_action_refs)
    if file_backed:
        event_result = _compact_referenced_result(projected_result, retained_refs, file_backed=True)
        owner._terminal_inline_result = None
        owner._terminal_retained_refs = retained_refs
        return event_result, retained_refs
    if _serialized_size(projected_result) <= inline_result_limit:
        owner._terminal_inline_result = projected_result
        owner._terminal_retained_refs = retained_refs
        return projected_result, retained_refs
    if retained_refs:
        event_result = _compact_referenced_result(projected_result, retained_refs, file_backed=False)
        owner._terminal_inline_result = None
        owner._terminal_retained_refs = retained_refs
        return event_result, retained_refs
    if owner.workspace is None:
        raise RuntimeError("AgentExecution has no Workspace binding for a large terminal result.")

    record_ref = await owner.workspace.put_artifact_ref(
        owner.id,
        projected_result,
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
                "state_version": owner._workspace_state_version,
                "recovery_active": owner._workspace_recovery_active,
                "lease_active": owner._workspace_lease_active,
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
) -> tuple[
    list[WorkspaceRecordRef],
    list[WorkspaceRetainedReference],
    bool,
    dict[str, WorkspaceRecordRef],
]:
    if owner.workspace is None or not isinstance(result, Mapping):
        return [], [], False, {}
    retained_records: list[WorkspaceRecordRef] = []
    retained_refs: list[WorkspaceRetainedReference] = []
    file_backed = False
    promoted_action_refs: dict[str, WorkspaceRecordRef] = {}
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
            owner._terminal_selected_action_artifact_ids.add(action_artifact_id)
            promoted_ref = await _promote_selected_action_artifact(owner, raw_ref)
            if promoted_ref is None:
                continue
            retained_records.append(promoted_ref)
            retained_refs.append(promoted_ref)
            promoted_action_refs[action_artifact_id] = promoted_ref
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
    return retained_records, retained_refs, file_backed, promoted_action_refs


def _result_ref_candidates(
    owner: "AgentExecution",
    result: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], bool]]:
    candidates: list[tuple[Mapping[str, Any], bool]] = []
    action_selection_accepted = _host_action_selection_eligible(owner)
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


def _host_action_selection_eligible(owner: "AgentExecution") -> bool:
    """Selection authority belongs to route completion, never result fields."""

    if owner.status not in {"success", "completed"}:
        return False
    selected_route = str(owner.route_info.get("selected_route") or owner.close_snapshot.get("route") or "")
    if selected_route == "model_request" or (not selected_route and owner.strategy_name == "direct"):
        return True
    if selected_route == "agent_task":
        task = getattr(owner, "task_record", None)
        return getattr(task, "status", None) == "completed"
    return False


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
    expected_scope = {"kind": "agent_execution", "id": owner.id}
    if _action_artifact_scope(raw_ref) != expected_scope:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_scope_mismatch",
            "Selected Action artifact ref does not belong to this AgentExecution scope.",
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
    read_artifact_transfer = getattr(artifact_manager, "read_artifact_transfer", None)
    if not callable(read_artifact_transfer):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_store_unavailable",
            "Selected Action artifact store is unavailable for terminal promotion.",
        )
        return None
    transfer = cast(
        tuple[dict[str, Any], Any] | None,
        read_artifact_transfer(artifact_id, expected_scope=expected_scope),
    )
    if transfer is None:
        owner._terminal_preserved_action_artifact_ids.add(artifact_id)
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_stored_scope_mismatch",
            "Selected Action artifact is unavailable in this AgentExecution scope.",
        )
        return None
    stored_artifact, value = transfer
    stored_identity = _action_artifact_identity(stored_artifact)
    if stored_identity != _action_artifact_identity(raw_ref):
        owner._terminal_preserved_action_artifact_ids.add(artifact_id)
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.action_artifact_identity_mismatch",
            "Selected Action artifact ref no longer matches the in-memory artifact identity.",
        )
        return None
    if value is None:
        owner._terminal_preserved_action_artifact_ids.add(artifact_id)
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
                "summary": f"Selected Action artifact for AgentExecution {owner.id}",
                "action_call_id": action_call_id,
            },
        )
    except Exception as error:
        owner._terminal_preserved_action_artifact_ids.add(artifact_id)
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


def _action_artifact_scope(ref: Mapping[str, Any]) -> dict[str, str] | None:
    meta = ref.get("meta")
    if not isinstance(meta, Mapping):
        return None
    scope = meta.get("artifact_scope")
    if not isinstance(scope, Mapping):
        return None
    kind = str(scope.get("kind") or "").strip()
    scope_id = str(scope.get("id") or "").strip()
    return {"kind": kind, "id": scope_id} if kind and scope_id else None


def _project_promoted_action_refs(
    result: Any,
    promoted_refs: Mapping[str, WorkspaceRecordRef],
) -> Any:
    if not promoted_refs or not isinstance(result, Mapping):
        return result
    projected = dict(result)
    _project_ref_lists(projected, promoted_refs)
    final_result = projected.get("final_result")
    if isinstance(final_result, Mapping):
        projected["final_result"] = _project_structured_ref_container(final_result, promoted_refs)
    evidence = projected.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes, bytearray)):
        projected["evidence"] = [
            _project_structured_ref_container(item, promoted_refs)
            if isinstance(item, Mapping)
            else item
            for item in evidence
        ]
    return projected


def _project_structured_ref_container(
    value: Mapping[str, Any],
    promoted_refs: Mapping[str, WorkspaceRecordRef],
) -> Mapping[str, Any]:
    artifact_id = str(value.get("artifact_id") or "")
    if artifact_id in promoted_refs:
        return promoted_refs[artifact_id]
    projected = dict(value)
    _project_ref_lists(projected, promoted_refs)
    return projected


def _project_ref_lists(
    container: dict[str, Any],
    promoted_refs: Mapping[str, WorkspaceRecordRef],
) -> None:
    for key in ("artifact_refs", "file_refs"):
        values = container.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            continue
        container[key] = [
            promoted_refs.get(str(value.get("artifact_id") or ""), value)
            if isinstance(value, Mapping)
            else value
            for value in values
        ]


async def _canonical_artifact_record(
    owner: "AgentExecution",
    raw_ref: Mapping[str, Any],
) -> tuple[WorkspaceRecordRef | None, WorkspaceReferenceEnvelope | None, Any]:
    is_envelope = "workspace_id" in raw_ref
    record_id = str((raw_ref.get("record_id") if is_envelope else raw_ref.get("id")) or "").strip()
    trusted_task_handoff = any(
        isinstance(ref, Mapping) and dict(ref) == dict(raw_ref)
        for ref in list(getattr(owner, "_terminal_task_handoff_refs", []) or [])
    )
    if trusted_task_handoff and not is_envelope:
        return await _canonical_task_handoff_record(owner, raw_ref, record_id)
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


async def _canonical_task_handoff_record(
    owner: "AgentExecution",
    raw_ref: Mapping[str, Any],
    record_id: str,
) -> tuple[WorkspaceRecordRef | None, WorkspaceReferenceEnvelope | None, Any]:
    """Validate one route-owned child ref by exact identity without broad search."""

    canonical_meta = raw_ref.get("meta")
    canonical_source = raw_ref.get("source")
    if not (
        record_id
        and raw_ref.get("collection") == "artifacts"
        and isinstance(canonical_meta, Mapping)
        and canonical_meta.get("artifact_ref") is True
        and isinstance(canonical_source, Mapping)
        and canonical_source.get("type") == "workspace"
        and canonical_source.get("name") == "artifact_ref"
    ):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.task_handoff_provenance_mismatch",
            "AgentTask handoff is not a canonical Workspace artifact ref.",
        )
        return None, None, None
    try:
        canonical_envelope = await owner.workspace.ref_envelope(record_id)
        canonical_size = int(raw_ref.get("size") or 0)
        readback = await owner.workspace.read_bounded(record_id, offset=0, limit=canonical_size + 1)
    except Exception as error:
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.task_handoff_readback_failed",
            f"AgentTask handoff readback failed: {_compact_error(error)}",
        )
        return None, None, None
    if not (
        canonical_envelope.get("record_id") == record_id
        and canonical_envelope.get("collection") == raw_ref.get("collection")
        and canonical_envelope.get("kind") == raw_ref.get("kind")
        and canonical_envelope.get("content_ref") == raw_ref.get("path")
        and canonical_envelope.get("digest") == raw_ref.get("sha256")
        and canonical_envelope.get("size") == canonical_size
    ):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.task_handoff_identity_mismatch",
            "AgentTask handoff does not match its persisted canonical identity.",
        )
        return None, None, None
    readback_content = str(readback.get("content") or "")
    readback_raw = readback_content.encode("utf-8")
    if not (
        readback.get("eof") is True
        and int(readback.get("size") or 0) == canonical_size
        and len(readback_raw) == canonical_size
        and hashlib.sha256(readback_raw).hexdigest() == str(raw_ref.get("sha256") or "")
    ):
        _defer_untrusted_ref(
            owner,
            "agent_execution.retention.task_handoff_integrity_mismatch",
            "AgentTask handoff content does not match its canonical record ref.",
        )
        return None, None, None
    try:
        content = json.loads(readback_content)
    except (TypeError, ValueError):
        content = readback_content
    return cast(WorkspaceRecordRef, dict(raw_ref)), canonical_envelope, content


def _looks_like_record_ref(ref: Mapping[str, Any]) -> bool:
    return all(
        key in ref
        for key in ("id", "collection", "kind", "path", "size", "summary", "scope", "source", "created_at", "meta")
    )


def _looks_like_file_ref(ref: Mapping[str, Any]) -> bool:
    if "bytes" not in ref:
        return False
    try:
        size = int(ref["bytes"])
    except (TypeError, ValueError):
        return False
    return bool(str(ref.get("path") or "") and str(ref.get("sha256") or "") and size >= 0)


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
