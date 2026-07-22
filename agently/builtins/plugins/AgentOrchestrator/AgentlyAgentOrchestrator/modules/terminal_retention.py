# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TYPE_CHECKING, cast

from agently.types.data import TaskWorkspaceFileRef, TaskWorkspaceRetentionResult, TaskWorkspaceTerminalStatus
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


_TERMINAL_EVENT_INLINE_LIMIT = 64_000
_OMIT_TRANSIENT_REF = object()


def _compact_error(error: BaseException) -> str:
    return (str(error).strip() or error.__class__.__name__)[:360]


def _serialized_size(value: Any) -> int:
    return len(json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, default=str).encode("utf-8"))


def _terminal_result_value(owner: "AgentExecution") -> Any:
    if owner.result is not None:
        return DataFormatter.sanitize(owner.result)
    if owner._error is None:
        return {"status": owner.status}
    projection = getattr(owner, "_terminal_error_projection", None)
    return {
        "status": owner.status,
        "error": DataFormatter.sanitize(
            projection
            if isinstance(projection, Mapping)
            else {"type": owner._error.__class__.__name__, "message": _compact_error(owner._error)}
        ),
    }


def _file_ref_key(ref: Mapping[str, Any]) -> tuple[str, str, str, int, str]:
    raw_size = ref.get("size")
    size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else -1
    return (
        str(ref.get("task_workspace_id") or ""),
        str(ref.get("execution_id") or ""),
        str(ref.get("path") or ""),
        size,
        str(ref.get("sha256") or ""),
    )


def _looks_like_trusted_file_ref(value: Mapping[str, Any]) -> bool:
    task_workspace_id, execution_id, path, size, digest = _file_ref_key(value)
    return bool(
        value.get("type") == "file"
        and task_workspace_id
        and execution_id
        and path
        and size >= 0
        and digest
    )


def _collect_file_refs(value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int, str]] = set()

    def collect(item: Any) -> None:
        if isinstance(item, Mapping):
            if _looks_like_trusted_file_ref(item):
                key = _file_ref_key(item)
                if key not in seen:
                    seen.add(key)
                    refs.append(dict(DataFormatter.sanitize(dict(item))))
                return
            for key in ("artifact_refs", "file_refs", "result", "final_result"):
                if key in item:
                    collect(item.get(key))
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for child in item:
                collect(child)

    collect(value)
    return refs


def _looks_like_transient_action_ref(value: Mapping[str, Any]) -> bool:
    return bool(
        str(value.get("selection_key") or "").strip()
        or str(value.get("artifact_id") or "").strip()
        or (
            str(value.get("action_call_id") or "").strip()
            and str(value.get("artifact_type") or "").strip()
        )
    )


def _project_terminal_value(value: Any) -> Any:
    """Remove run-local Action refs while preserving ordinary result data."""

    if isinstance(value, Mapping):
        if _looks_like_trusted_file_ref(value):
            return dict(value)
        if _looks_like_transient_action_ref(value):
            return _OMIT_TRANSIENT_REF
        projected: dict[str, Any] = {}
        for key, item in value.items():
            projected_item = _project_terminal_value(item)
            if projected_item is _OMIT_TRANSIENT_REF:
                continue
            projected[str(key)] = projected_item
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        projected_items = []
        for item in value:
            projected_item = _project_terminal_value(item)
            if projected_item is not _OMIT_TRANSIENT_REF:
                projected_items.append(projected_item)
        return projected_items
    return value


async def _verify_owner_file_ref(owner: "AgentExecution", ref: Mapping[str, Any]) -> bool:
    task_workspace = owner.task_workspace
    if task_workspace is None:
        return False
    if str(ref.get("task_workspace_id") or "") != task_workspace.task_workspace_id:
        return False
    if str(ref.get("execution_id") or "") != task_workspace.execution_id:
        return False
    try:
        readback = await task_workspace.read_file(str(ref.get("path") or ""), max_bytes=1)
    except Exception:
        return False
    return (
        int(readback.get("bytes") or 0) == int(ref.get("size") or 0)
        and str(readback.get("sha256") or "") == str(ref.get("sha256") or "")
    )


def _compact_file_backed_result(result: Any, refs: Sequence[Mapping[str, Any]]) -> Any:
    if not isinstance(result, Mapping):
        return {"status": "completed", "artifact_refs": list(refs)}
    projected = dict(result)
    projected["artifact_refs"] = list(refs)
    final_result = projected.get("final_result")
    if isinstance(final_result, str) and len(final_result) > 1600:
        projected["final_result"] = "File-backed result; inspect artifact_refs."
    projected.pop("taskboard", None)
    return DataFormatter.sanitize(projected)


async def prepare_agent_execution_terminal_retention(
    owner: "AgentExecution",
) -> tuple[Any, list[TaskWorkspaceFileRef]]:
    """Prepare a bounded terminal event without materializing RecordStore state."""

    result = _terminal_result_value(owner)
    task_handoff = [
        dict(ref)
        for ref in list(getattr(owner, "_terminal_task_handoff_refs", []) or [])
        if isinstance(ref, Mapping) and _looks_like_trusted_file_ref(ref)
    ]
    task_keys = {_file_ref_key(ref) for ref in task_handoff}
    candidates = _collect_file_refs(result)
    retained: list[dict[str, Any]] = []
    owner_retained: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int, str]] = set()
    for ref in [*task_handoff, *candidates]:
        key = _file_ref_key(ref)
        if key in seen:
            continue
        if key in task_keys:
            trusted = True
        else:
            trusted = await _verify_owner_file_ref(owner, ref)
        if not trusted:
            continue
        seen.add(key)
        retained.append(ref)
        if str(ref.get("execution_id") or "") == owner.task_workspace.execution_id:
            owner_retained.append(ref)

    projected_result = _project_terminal_value(result)
    if projected_result is _OMIT_TRANSIENT_REF:
        projected_result = {"status": owner.status}
    owner._terminal_inline_result = projected_result if not retained else None
    owner._terminal_retained_refs = owner_retained
    event_result = _compact_file_backed_result(projected_result, retained) if retained else projected_result
    if not retained and _serialized_size(event_result) > _TERMINAL_EVENT_INLINE_LIMIT:
        event_result = {
            "status": owner.status,
            "kind": "agent_execution_terminal_result_omitted",
            "reason": "Terminal event projection exceeded the inline log limit; the caller still receives the full result.",
        }
    return event_result, cast(list[TaskWorkspaceFileRef], retained)


async def apply_agent_execution_terminal_retention(
    owner: "AgentExecution",
    *,
    status: TaskWorkspaceTerminalStatus,
) -> TaskWorkspaceRetentionResult | None:
    """Delete only unretained files in this execution's private fallback area."""

    close_status = "completed" if status == "completed" else "cancelled" if status == "cancelled" else "failed"
    closed = await owner.task_workspace._close_execution_files(
        retained_refs=cast(Any, list(owner._terminal_retained_refs)),
        status=close_status,
    )
    result = cast(TaskWorkspaceRetentionResult, closed)
    owner.diagnostics["task_workspace_retention"] = {
        "status": result["status"],
        "retained_bytes": result["retained_bytes"],
        "deleted_bytes": result["deleted_bytes"],
        "diagnostics": DataFormatter.sanitize(
            [*owner._terminal_retention_diagnostics, *result["diagnostics"]]
        ),
    }
    return result


def defer_agent_execution_terminal_retention(
    owner: "AgentExecution",
    *,
    code: str,
    error: BaseException,
) -> None:
    owner._terminal_retention_deferred = True
    owner._terminal_retention_diagnostics.append({"code": code, "message": _compact_error(error)})


__all__ = [
    "apply_agent_execution_terminal_retention",
    "defer_agent_execution_terminal_retention",
    "prepare_agent_execution_terminal_retention",
]
