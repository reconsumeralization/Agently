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
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TYPE_CHECKING, TypedDict, cast

from agently.types.data import (
    WorkspaceRetainedReference,
    WorkspaceRetentionResult,
)

if TYPE_CHECKING:
    from .Execution import TriggerFlowExecution


_TERMINAL_INLINE_RESULT_LIMIT = 4096


class TriggerFlowTerminalRetention(TypedDict):
    close_result: Any
    completed_event_id: str
    completed_event_timestamp: int
    completed_event_state_version: int
    completed_event_payload: dict[str, Any]
    closed_event_id: str
    closed_event_timestamp: int | None
    closed_event_state_version: int | None
    closed_event_payload: dict[str, Any] | None
    inline_result: Any
    retained_refs: list[WorkspaceRetainedReference]
    event_result: Any
    result: WorkspaceRetentionResult | None


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


def _empty_accounting() -> dict[str, Any]:
    return {
        "entities": {},
        "logical_bytes_deleted": 0,
        "physical_bytes_reclaimed": 0,
        "physical_bytes_pending": 0,
    }


def _deferred_result(code: str, message: str) -> WorkspaceRetentionResult:
    return cast(
        WorkspaceRetentionResult,
        {
            "status": "deferred",
            "plan_fingerprint": "",
            "manifest_ref": None,
            "retained_refs": [],
            "accounting": _empty_accounting(),
            "diagnostics": [
                {
                    "code": code,
                    "message": message,
                    "retryable": True,
                    "entity": "triggerflow_terminal_retention",
                }
            ],
        },
    )


def _finalize_completed_event(
    execution: "TriggerFlowExecution[Any, Any, Any]",
    prepared: TriggerFlowTerminalRetention,
) -> TriggerFlowTerminalRetention:
    prepared["completed_event_payload"] = {
        "result": prepared["event_result"],
        "origin_chunk": execution._get_origin_chunk_payload(),
    }
    return prepared


async def prepare_triggerflow_terminal_retention(
    execution: "TriggerFlowExecution[Any, Any, Any]",
    result: Any,
) -> TriggerFlowTerminalRetention:
    """Promote the known close result before terminal lifecycle events are emitted."""

    serialized_result = execution._to_serializable_value(result)
    prepared: TriggerFlowTerminalRetention = {
        "close_result": result,
        "completed_event_id": uuid.uuid4().hex,
        "completed_event_timestamp": int(time.time() * 1000),
        "completed_event_state_version": execution._state_version,
        "completed_event_payload": {},
        "closed_event_id": uuid.uuid4().hex,
        "closed_event_timestamp": None,
        "closed_event_state_version": None,
        "closed_event_payload": None,
        "inline_result": serialized_result,
        "retained_refs": [],
        "event_result": serialized_result,
        "result": None,
    }
    workspace = execution._get_runtime_resource("workspace", None)
    if workspace is None:
        return _finalize_completed_event(execution, prepared)
    if _serialized_size(serialized_result) <= _TERMINAL_INLINE_RESULT_LIMIT:
        return _finalize_completed_event(execution, prepared)

    put_artifact_ref = getattr(workspace, "put_artifact_ref", None)
    add_retention_anchor = getattr(workspace, "add_retention_anchor", None)
    ref_envelope = getattr(workspace, "ref_envelope", None)
    if not all(
        callable(method)
        for method in (put_artifact_ref, add_retention_anchor, ref_envelope)
    ):
        prepared["inline_result"] = None
        prepared["event_result"] = {
            "kind": "triggerflow_terminal_result_unavailable",
            "execution_id": execution.id,
        }
        prepared["result"] = _deferred_result(
            "triggerflow.retention.workspace_capability_missing",
            "Workspace does not expose terminal result promotion and reference methods.",
        )
        return _finalize_completed_event(execution, prepared)
    put_artifact_ref = cast(Callable[..., Awaitable[Any]], put_artifact_ref)
    add_retention_anchor = cast(
        Callable[..., Awaitable[Any]],
        add_retention_anchor,
    )
    ref_envelope = cast(Callable[..., Awaitable[Any]], ref_envelope)

    try:
        artifact_ref = await put_artifact_ref(
            execution.id,
            serialized_result,
            metadata={
                "scope": dict(getattr(workspace, "default_scope", {}) or {}),
                "kind": "triggerflow_terminal_result",
                "summary": f"Terminal result for TriggerFlow execution {execution.id}",
            },
        )
        await add_retention_anchor(
            execution.id,
            anchor_type="deliverable",
            record_ref=artifact_ref,
            meta={"owner": "TriggerFlow", "kind": "terminal_result"},
        )
        envelope = await ref_envelope(artifact_ref)
    except Exception as error:
        prepared["inline_result"] = None
        prepared["event_result"] = {
            "kind": "triggerflow_terminal_result_unavailable",
            "execution_id": execution.id,
        }
        prepared["result"] = _deferred_result(
            "triggerflow.retention.result_promotion_failed",
            f"TriggerFlow terminal result promotion failed: {error}",
        )
        return _finalize_completed_event(execution, prepared)

    prepared["inline_result"] = None
    prepared["retained_refs"] = [artifact_ref]
    prepared["event_result"] = envelope
    return _finalize_completed_event(execution, prepared)


async def apply_triggerflow_terminal_retention(
    execution: "TriggerFlowExecution[Any, Any, Any]",
    prepared: TriggerFlowTerminalRetention,
) -> TriggerFlowTerminalRetention:
    """Inspect and apply Workspace cleanup after completed/closed events exist."""

    if prepared["result"] is not None:
        return prepared
    workspace = execution._get_runtime_resource("workspace", None)
    if workspace is None:
        return prepared
    inspect_retention = getattr(workspace, "inspect_retention", None)
    apply_retention = getattr(workspace, "apply_retention", None)
    if not callable(inspect_retention) or not callable(apply_retention):
        prepared["result"] = _deferred_result(
            "triggerflow.retention.workspace_capability_missing",
            "Workspace does not expose inspect_retention/apply_retention.",
        )
        return prepared
    inspect_retention = cast(Callable[..., Awaitable[Any]], inspect_retention)
    apply_retention = cast(Callable[..., Awaitable[Any]], apply_retention)

    default_scope = dict(getattr(workspace, "default_scope", {}) or {})
    if str(default_scope.get("execution_id") or "") != execution.id:
        prepared["result"] = _deferred_result(
            "triggerflow.retention.execution_scope_missing",
            "Workspace default_scope.execution_id must match the TriggerFlow execution before cleanup.",
        )
        return prepared

    status = execution.get_status()
    if status not in {"failed", "cancelled"}:
        status = "completed"
    lease_until = execution._lease_until
    lease_active = bool(
        execution._owner_id
        and lease_until is not None
        and float(lease_until) > time.time()
    )
    recovery_active = bool(
        execution.get_pending_interrupts()
        or execution.get_pending_interventions()
    )
    terminal_at = datetime.now(timezone.utc).isoformat()
    if execution._closed_at is not None:
        terminal_at = datetime.fromtimestamp(
            execution._closed_at,
            tz=timezone.utc,
        ).isoformat()

    try:
        preview = await inspect_retention(
            {},
            lifecycle={
                "execution_id": execution.id,
                "status": status,
                "terminal_at": terminal_at,
                "state_version": (
                    prepared["closed_event_state_version"]
                    if prepared["closed_event_state_version"] is not None
                    else execution._state_version
                ),
                "recovery_active": recovery_active,
                "lease_active": lease_active,
            },
            retained_refs=prepared["retained_refs"],
            inline_result=prepared["inline_result"],
        )
        if preview["status"] != "ready":
            prepared["result"] = cast(
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
            return prepared
        prepared["result"] = await apply_retention(preview)
    except Exception as error:
        prepared["result"] = _deferred_result(
            "triggerflow.retention.apply_failed",
            f"TriggerFlow terminal Workspace retention failed: {error}",
        )
    return prepared
