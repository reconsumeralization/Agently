# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
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
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from .Execution import TriggerFlowExecution


_TERMINAL_INLINE_RESULT_LIMIT = 4096
TriggerFlowRecoverySnapshotStatus = Literal[
    "deleted",
    "not_configured",
    "delete_unsupported",
    "deferred",
]


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
    retained_refs: list[dict[str, Any]]
    event_result: Any
    result: "TriggerFlowRetentionResult | None"


class TriggerFlowRetentionResult(TypedDict):
    """TriggerFlow-owned terminal cleanup facts, independent from file storage."""

    status: Literal["applied", "noop", "deferred"]
    execution_id: str
    retained_refs: list[dict[str, Any]]
    recovery_snapshot_status: TriggerFlowRecoverySnapshotStatus
    diagnostics: list[dict[str, Any]]


def _collect_file_refs(value: Any, collected: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if (
            value.get("type") == "file"
            and isinstance(value.get("path"), str)
            and isinstance(value.get("sha256"), str)
        ):
            collected.append(dict(value))
            return
        for item in value.values():
            _collect_file_refs(item, collected)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_file_refs(item, collected)


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


async def prepare_triggerflow_terminal_retention(
    execution: "TriggerFlowExecution[Any, Any, Any]",
    result: Any,
) -> TriggerFlowTerminalRetention:
    """Freeze terminal output in memory and select only explicit file refs."""
    serialized_result = execution._to_serializable_value(result)
    retained_refs: list[dict[str, Any]] = []
    _collect_file_refs(serialized_result, retained_refs)
    deduplicated = {
        (
            str(ref.get("task_workspace_id") or ""),
            str(ref.get("execution_id") or ""),
            str(ref.get("path") or ""),
            str(ref.get("sha256") or ""),
        ): ref
        for ref in retained_refs
    }
    retained_refs = list(deduplicated.values())
    inline_result = serialized_result
    event_result = serialized_result
    if _serialized_size(serialized_result) > _TERMINAL_INLINE_RESULT_LIMIT:
        inline_result = None
        if retained_refs:
            event_result = {
                "kind": "triggerflow_terminal_result_refs",
                "artifact_refs": retained_refs,
            }
        else:
            event_result = {
                "kind": "triggerflow_terminal_result_omitted",
                "execution_id": execution.id,
                "reason": (
                    "Terminal event projection exceeded the inline log limit; "
                    "the caller still receives the full close result."
                ),
            }
    timestamp = int(time.time() * 1000)
    return {
        "close_result": result,
        "completed_event_id": uuid.uuid4().hex,
        "completed_event_timestamp": timestamp,
        "completed_event_state_version": execution._state_version,
        "completed_event_payload": {
            "result": event_result,
            "origin_chunk": execution._get_origin_chunk_payload(),
        },
        "closed_event_id": uuid.uuid4().hex,
        "closed_event_timestamp": None,
        "closed_event_state_version": None,
        "closed_event_payload": None,
        "inline_result": inline_result,
        "retained_refs": retained_refs,
        "event_result": event_result,
        "result": None,
    }


async def apply_triggerflow_terminal_retention(
    execution: "TriggerFlowExecution[Any, Any, Any]",
    prepared: TriggerFlowTerminalRetention,
) -> TriggerFlowTerminalRetention:
    """Clean execution recovery state; TaskWorkspace owns every file decision."""
    diagnostics: list[dict[str, Any]] = []
    snapshot_store = execution._snapshot_store
    recovery_snapshot_status: TriggerFlowRecoverySnapshotStatus = "not_configured"
    if snapshot_store is not None:
        delete_snapshot = getattr(snapshot_store, "delete_snapshot", None)
        if callable(delete_snapshot):
            run_id = execution.run_context.run_id or execution.id
            try:
                await cast(Callable[[str], Awaitable[Any]], delete_snapshot)(run_id)
                recovery_snapshot_status = "deleted"
            except Exception as error:
                recovery_snapshot_status = "deferred"
                diagnostics.append(
                    {
                        "code": "triggerflow.recovery_cleanup_failed",
                        "message": str(error).strip() or error.__class__.__name__,
                        "retryable": True,
                    }
                )
        else:
            recovery_snapshot_status = "delete_unsupported"

    prepared["result"] = {
        "status": (
            "deferred"
            if diagnostics
            else "applied"
            if recovery_snapshot_status == "deleted"
            else "noop"
        ),
        "execution_id": execution.id,
        "retained_refs": list(prepared["retained_refs"]),
        "recovery_snapshot_status": recovery_snapshot_status,
        "diagnostics": diagnostics,
    }
    return prepared
