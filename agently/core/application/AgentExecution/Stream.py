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

import asyncio
import html
import json
from contextlib import suppress
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, cast

from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter


def project_agent_execution_text_delta(item: Any) -> str | None:
    """Project structured execution stream items onto the public text delta stream."""
    path = str(getattr(item, "path", "") or "")
    value = getattr(item, "value", None)
    source = str(getattr(item, "source", "") or "")
    if _is_retry_status_marker_source(path, value):
        return _format_retry_marker(value)
    if getattr(item, "event_type", None) == "delta":
        delta = getattr(item, "delta", None)
        if delta is None:
            return None
        return str(delta)
    return _project_done_item_text(path, value, getattr(item, "meta", None), source=source)


def _project_done_item_text(path: str, value: Any, meta: Any, *, source: str) -> str | None:
    item_meta = meta if isinstance(meta, Mapping) else {}
    stream_kind = str(item_meta.get("stream_kind") or "")
    taskboard_status = _taskboard_status_text(path, value)
    if taskboard_status is not None:
        return taskboard_status
    if stream_kind == "progress":
        if str(item_meta.get("progress_source") or "") == "model":
            return None
        return _paragraph(_mapping_text(value, "message"))
    if stream_kind == "snapshot":
        return _paragraph(_mapping_text(value, "message"))
    if stream_kind == "heartbeat":
        if isinstance(value, Mapping):
            stage = str(value.get("stage") or "the current step")
            quiet_for = value.get("quiet_for_seconds")
            if quiet_for is not None:
                return _paragraph(f"Still working on {stage}; no new stream events for {quiet_for} seconds.")
            return _paragraph(f"Still working on {stage}.")
        return _paragraph("Still working; no new stream events yet.")
    if stream_kind == "action_observation":
        return _paragraph(_action_observation_text(value, item_meta))
    if stream_kind == "phase":
        return _paragraph(_phase_text(value))
    if path == "agent_task.error":
        return _paragraph(_terminal_error_text(value))
    if path == "result" and source == "agent_task":
        return _paragraph(_terminal_result_text(value))
    return None


def _mapping_text(value: Any, key: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    text = value.get(key)
    return str(text).strip() if text is not None else ""


def _paragraph(text: str | None) -> str | None:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    return f"{normalized}\n\n"


def _terminal_error_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return f"Task failed: {_value_to_text(value)}"
    error_type = str(value.get("type") or "error").strip()
    message = str(value.get("message") or "").strip()
    if message:
        return f"Task failed: {error_type}: {message}"
    return f"Task failed: {error_type}"


def _phase_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    phase = str(value.get("phase") or "").strip()
    if not phase:
        return ""
    iteration = value.get("iteration")
    status = str(value.get("status") or "").strip()
    prefix = f"Iteration {iteration}: " if iteration not in (None, "") else ""
    suffix = f" ({status})" if status else ""
    return f"{prefix}phase {phase}{suffix}."


def _taskboard_status_text(path: str, value: Any) -> str | None:
    if not path.startswith("agent_task.taskboard.") or not isinstance(value, Mapping):
        return None
    if path == "agent_task.taskboard.plan":
        return _format_taskboard_status_block(
            title="TaskBoard planned",
            revision=value.get("revision"),
            schedule=None,
            card_results=None,
        )
    if path.startswith("agent_task.taskboard.tick.") and path.endswith(".scheduled"):
        return _format_taskboard_status_block(
            title=_taskboard_tick_title(path, "scheduled"),
            revision=value.get("revision"),
            schedule=value.get("schedule"),
            card_results=value.get("card_results"),
        )
    if path.startswith("agent_task.taskboard.tick.") and path.endswith(".completed"):
        return _format_taskboard_status_block(
            title=_taskboard_tick_title(path, "updated"),
            revision=value.get("revision"),
            schedule=value.get("schedule"),
            card_results=value.get("card_results"),
        )
    return None


def _taskboard_tick_title(path: str, fallback: str) -> str:
    parts = path.split(".")
    for index, part in enumerate(parts):
        if part == "tick" and index + 1 < len(parts):
            tick_index = parts[index + 1].strip()
            if tick_index:
                return f"TaskBoard tick {tick_index} {fallback}"
    return f"TaskBoard {fallback}"


def _format_taskboard_status_block(
    *,
    title: str,
    revision: Any,
    schedule: Any,
    card_results: Any,
) -> str | None:
    cards = _taskboard_display_cards(revision, schedule, card_results)
    if not cards:
        return None
    board_id = _mapping_text(revision, "board_id") or "taskboard"
    revision_id = _mapping_text(revision, "revision_id") or _mapping_text(schedule, "revision_id")
    counts = _taskboard_status_counts(cards)
    total = len(cards)
    completed = counts.get("completed", 0)
    header = f"**{title}** `{_markdown_inline_code(board_id)}`"
    if revision_id:
        header += f" - revision `{_markdown_inline_code(revision_id)}`"
    summary_bits = [
        f"{completed}/{total} completed",
        f"{counts.get('in_progress', 0)} in progress",
        f"{counts.get('not_started', 0)} not started",
    ]
    if counts.get("failed", 0):
        summary_bits.append(f"{counts['failed']} failed")
    if counts.get("degraded", 0):
        summary_bits.append(f"{counts['degraded']} degraded")
    lines = [
        header,
        f"Progress: {' - '.join(summary_bits)}",
        "",
        "| State | Card | Task |",
        "| --- | --- | --- |",
    ]
    max_rows = 8
    for card in cards[:max_rows]:
        state = card["display_state"]
        lines.append(
            "| "
            + _markdown_table_cell(_taskboard_state_label(state))
            + " | "
            + f"`{_markdown_inline_code(card['id'])}`"
            + " | "
            + _markdown_table_cell(card.get("objective") or card["id"])
            + " |"
        )
    omitted = len(cards) - max_rows
    if omitted > 0:
        lines.append(f"| ... | ... | {omitted} more cards omitted. |")
    return "\n".join(lines) + "\n\n"


def _taskboard_display_cards(revision: Any, schedule: Any, card_results: Any) -> list[dict[str, Any]]:
    revision_view = revision if isinstance(revision, Mapping) else {}
    schedule_view = schedule if isinstance(schedule, Mapping) else {}
    result_view = _taskboard_result_view(revision_view, card_results)
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    graph_value = revision_view.get("graph")
    graph: Mapping[str, Any] = graph_value if isinstance(graph_value, Mapping) else {}
    for raw_card in _sequence_of_mappings(graph.get("cards")):
        card_id = _clean_taskboard_text(raw_card.get("id") or raw_card.get("card_id"))
        if not card_id:
            continue
        seen.add(card_id)
        cards.append(
            {
                "id": card_id,
                "objective": _clean_taskboard_text(raw_card.get("objective") or raw_card.get("goal")),
                "status": _clean_taskboard_text(raw_card.get("status")),
                "failure_policy": _clean_taskboard_text(raw_card.get("failure_policy")),
                "display_state": _taskboard_display_state(card_id, raw_card, result_view.get(card_id), schedule_view),
            }
        )
    for card_id in _taskboard_schedule_ids(schedule_view) + list(result_view):
        if card_id in seen:
            continue
        seen.add(card_id)
        result = result_view.get(card_id)
        cards.append(
            {
                "id": card_id,
                "objective": "",
                "status": _clean_taskboard_text(result.get("status")) if isinstance(result, Mapping) else "",
                "failure_policy": "",
                "display_state": _taskboard_display_state(card_id, {}, result, schedule_view),
            }
        )
    return cards


def _taskboard_result_view(revision: Mapping[str, Any], card_results: Any) -> dict[str, Mapping[str, Any]]:
    raw_results = card_results if isinstance(card_results, Mapping) else revision.get("card_results")
    results: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_results, Mapping):
        for raw_id, raw_result in raw_results.items():
            card_id = _clean_taskboard_text(raw_id)
            if not card_id:
                continue
            if isinstance(raw_result, Mapping):
                results[card_id] = raw_result
            elif raw_result not in (None, ""):
                results[card_id] = {"status": str(raw_result)}
    raw_statuses = revision.get("card_result_statuses")
    if isinstance(raw_statuses, Mapping):
        for raw_id, raw_status in raw_statuses.items():
            card_id = _clean_taskboard_text(raw_id)
            if card_id and card_id not in results:
                results[card_id] = {"status": str(raw_status)}
    return results


def _taskboard_schedule_ids(schedule: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("completed_card_ids", "runnable_card_ids", "blocked_card_ids"):
        for card_id in _sequence_of_strings(schedule.get(key)):
            if card_id not in ids:
                ids.append(card_id)
    return ids


def _taskboard_display_state(
    card_id: str,
    card: Mapping[str, Any],
    result: Any,
    schedule: Mapping[str, Any],
) -> str:
    result_status = _normalize_taskboard_status(result.get("status")) if isinstance(result, Mapping) else ""
    card_status = _normalize_taskboard_status(card.get("status"))
    failure_policy = _normalize_taskboard_status(card.get("failure_policy"))
    metadata: Mapping[str, Any] = {}
    if isinstance(result, Mapping):
        metadata_value = result.get("metadata")
        if isinstance(metadata_value, Mapping):
            metadata = metadata_value
    if result_status in {"completed", "accepted", "succeeded", "success", "ok"}:
        return "completed"
    if result_status in {"degraded", "partial", "setback", "skipped", "deferred"}:
        return "degraded"
    if result_status in {"failed", "error", "timeout", "timed_out", "cancelled", "blocked"}:
        if failure_policy in {"optional", "degradable"} or metadata.get("deferred") is True:
            return "degraded"
        return "failed"
    completed_ids = set(_sequence_of_strings(schedule.get("completed_card_ids")))
    runnable_ids = set(_sequence_of_strings(schedule.get("runnable_card_ids")))
    if card_id in completed_ids or card_status in {"completed", "accepted", "succeeded", "success", "ok"}:
        return "completed"
    if card_status in {"degraded", "partial", "setback", "skipped", "deferred"}:
        return "degraded"
    if card_status in {"failed", "error", "timeout", "timed_out", "cancelled"}:
        return "failed"
    if card_id in runnable_ids or card_status in {"running", "ready", "active", "in_progress"}:
        return "in_progress"
    return "not_started"


def _taskboard_status_counts(cards: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"not_started": 0, "in_progress": 0, "completed": 0, "failed": 0, "degraded": 0}
    for card in cards:
        state = str(card.get("display_state") or "not_started")
        counts[state if state in counts else "not_started"] += 1
    return counts


def _taskboard_state_label(state: str) -> str:
    labels = {
        "not_started": "⏳ Not started",
        "in_progress": "🔄 In progress",
        "completed": "✅ Completed",
        "failed": "❌ Failed",
        "degraded": "⚠️ Degraded",
    }
    return labels.get(state, labels["not_started"])


def _normalize_taskboard_status(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _clean_taskboard_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return " ".join(str(value).split()).strip()


def _sequence_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list | tuple):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _sequence_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    result: list[str] = []
    for item in value:
        text = _clean_taskboard_text(item)
        if text:
            result.append(text)
    return result


def _markdown_table_cell(value: Any) -> str:
    text = _compact_inline_text(value, max_chars=96)
    return text.replace("|", "\\|") or "-"


def _markdown_inline_code(value: Any) -> str:
    return _compact_inline_text(value, max_chars=80).replace("`", "'") or "-"


def _action_observation_text(value: Any, meta: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        return ""
    action_id = str(value.get("action_id") or value.get("action_call_id") or "action").strip()
    action_label = action_id or "action"
    kind = str(value.get("kind") or value.get("action_type") or "").strip()
    label = f"{action_label} ({kind})" if kind else action_label
    phase = str(meta.get("phase") or "").strip().lower()
    status = str(value.get("status") or "").strip().lower()
    if phase == "started" or status == "started":
        text = f"Action started: {label}."
        input_summary = _compact_inline_text(value.get("input_summary"))
        if input_summary:
            text += f" Input: {input_summary}"
        return text
    if phase == "failed" or status in {"failed", "error", "timeout", "timed_out", "blocked"}:
        text = f"Action failed: {label}."
        error = _compact_inline_text(value.get("error"))
        if error:
            text += f" Error: {error}"
        return text
    if phase == "completed" or value.get("success") is True or status in {"success", "succeeded", "completed", "ok"}:
        text = f"Action completed: {label}."
        output_summary = _compact_inline_text(value.get("output_summary"))
        if output_summary:
            text += f" Result: {output_summary}"
        refs_text = _action_refs_text(value)
        if refs_text:
            text += f" Refs: {refs_text}"
        return text
    return f"Action update: {label} ({status})." if status else f"Action update: {label}."


def _action_refs_text(value: Mapping[str, Any]) -> str:
    refs: list[str] = []
    for key in ("artifact_refs", "file_refs", "source_refs"):
        raw_refs = value.get(key)
        if not isinstance(raw_refs, list):
            continue
        for item in raw_refs:
            if not isinstance(item, Mapping):
                continue
            ref_text = str(
                item.get("path")
                or item.get("value")
                or item.get("url")
                or item.get("uri")
                or item.get("id")
                or ""
            ).strip()
            ref_text = _compact_inline_text(ref_text, max_chars=120)
            if ref_text and ref_text not in refs:
                refs.append(ref_text)
            if len(refs) >= 3:
                return ", ".join(refs)
    return ", ".join(refs)


def _compact_inline_text(value: Any, *, max_chars: int = 280) -> str:
    if value in (None, "", [], {}):
        return ""
    text = _value_to_text(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 14)].rstrip() + " [truncated]"


def _terminal_result_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        text = _value_to_text(value)
        return f"Final result:\n{text}" if text else "Task finished."
    status = str(value.get("status") or "").strip()
    accepted = value.get("accepted")
    final_result = value.get("final_result")
    reason = str(value.get("reason") or "").strip()
    if final_result not in (None, ""):
        heading = "Task completed" if status == "completed" or accepted is True else f"Task finished with status {status or 'unknown'}"
        return f"{heading}.\nFinal result:\n{_value_to_text(final_result)}"
    if reason:
        if status:
            return f"Task finished with status {status}: {reason}"
        return f"Task finished: {reason}"
    if status:
        return f"Task finished with status {status}."
    return "Task finished."


def _value_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(DataFormatter.sanitize(value), ensure_ascii=False)
    except Exception:
        return str(value).strip()


def _is_retry_status_marker_source(path: str, value: Any) -> bool:
    return (
        (path == "$status" or path.endswith(".$status"))
        and isinstance(value, Mapping)
        and value.get("status") == "failed"
        and value.get("retry") is True
    )


def _format_retry_marker(value: Any) -> str:
    reason = value.get("reason") if isinstance(value, Mapping) else None
    text = str(reason).strip() if reason is not None else ""
    if not text:
        text = "Retrying model request."
    return f"<$retry>{html.escape(text, quote=False)}</$retry>"


class AgentExecutionStream:
    """Execution-local raw stream buffer and TriggerFlow bridge."""

    def __init__(
        self,
        *,
        execution_id: str | None = None,
        lineage: Mapping[str, Any] | None = None,
    ):
        self.items: list[AgentExecutionStreamData] = []
        self.queues: list[asyncio.Queue[Any]] = []
        self.execution_id = execution_id
        self.lineage = dict(lineage or {})
        self._execution: Any = None

    def bind_execution(self, execution: Any):
        self._execution = execution
        return self

    def __call__(self, *args: Any, **kwargs: Any):
        if self._execution is None:
            raise TypeError("AgentExecutionStream is not bound to an AgentExecution.")
        return self._execution.get_async_generator(*args, **kwargs)

    async def emit(
        self,
        path: str,
        value: Any,
        *,
        delta: str | None = None,
        route: str | None = None,
        source: str | None = "agent_execution",
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        is_complete: bool | None = None,
        event_type: Literal["delta", "done"] = "done",
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        item_meta = dict(meta or {})
        if self.execution_id is not None:
            item_meta.setdefault("execution_id", self.execution_id)
        if self.lineage:
            item_meta.setdefault("lineage", dict(self.lineage))
        completed = event_type == "done"
        if is_complete is not None:
            completed = is_complete
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            delta=delta,
            is_complete=completed,
            event_type=event_type,
            source=source,
            route=route,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=DataFormatter.sanitize(item_meta) if item_meta else None,
        )
        return await self._publish(item)

    async def close(self):
        for queue in list(self.queues):
            await queue.put(None)

    async def flush_delta_buffer(self) -> AgentExecutionStreamData | None:
        return None

    async def _publish(self, item: AgentExecutionStreamData) -> AgentExecutionStreamData:
        self.items.append(item)
        for queue in list(self.queues):
            await queue.put(item)
        if self._execution is not None:
            emit_runtime_projection = getattr(self._execution, "_async_emit_stream_runtime_event", None)
            if callable(emit_runtime_projection):
                with suppress(Exception):
                    await cast(
                        Callable[[AgentExecutionStreamData], Awaitable[None]],
                        emit_runtime_projection,
                    )(item)
        return item

    def _is_compatible_delta(self, left: AgentExecutionStreamData, right: AgentExecutionStreamData) -> bool:
        return (
            left.path == right.path
            and left.source == right.source
            and left.route == right.route
            and left.stage_id == right.stage_id
            and left.task_id == right.task_id
            and left.action_id == right.action_id
            and left.graph_id == right.graph_id
            and (left.meta or {}).get("execution_id") == (right.meta or {}).get("execution_id")
            and (left.meta or {}).get("response_id") == (right.meta or {}).get("response_id")
            and (left.meta or {}).get("field_path") == (right.meta or {}).get("field_path")
        )

    async def bridge_model_stream_item(
        self,
        item: Any,
        *,
        route: str,
        source: str = "model_request",
        path_prefix: str | None = None,
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ):
        raw_path = str(getattr(item, "path", "") or "model")
        path = f"{path_prefix}.{raw_path}" if path_prefix else raw_path
        raw_event_type = getattr(item, "event_type", "done")
        event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
        item_meta = {
            "field_path": raw_path,
            "wildcard_path": getattr(item, "wildcard_path", None),
            "indexes": getattr(item, "indexes", None),
        }
        if meta:
            item_meta.update(meta)
        await self.emit(
            path,
            getattr(item, "value", None),
            delta=getattr(item, "delta", None),
            route=route,
            source=source,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            event_type=event_type,
            meta=item_meta,
        )

    async def bridge_task_dag_item(self, item: Any, *, route: str):
        if not isinstance(item, dict):
            await self.emit("runtime.stream", item, route=route, source="triggerflow")
            return
        item_type = str(item.get("type") or "runtime.stream")
        action = str(item.get("action") or "event")
        payload = item.get("payload", {})
        task_id = str(item.get("task_id") or "") or None
        stage_id = str(item.get("stage_id") or "") or None
        graph_id = str(item.get("graph_id") or "") or None
        if item_type == "skills.stage_field" and stage_id:
            field_path = str(item.get("field_path") or "model")
            raw_event_type = str(item.get("event_type") or action)
            event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
            await self.emit(
                f"skills.stages.{stage_id}.fields.{field_path}",
                item.get("value"),
                delta=item.get("delta") if isinstance(item.get("delta"), str) else None,
                route=route,
                source="model_request",
                stage_id=stage_id,
                task_id=task_id,
                graph_id=graph_id,
                is_complete=bool(item.get("is_complete", event_type == "done")),
                event_type=event_type,
                meta=payload if isinstance(payload, dict) else None,
            )
            return
        if item_type == "skills.model_stream":
            field_path = str(item.get("path") or "model")
            raw_event_type = str(item.get("event_type") or action)
            event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
            await self.emit(
                f"skills.model.fields.{field_path}",
                item.get("value"),
                delta=item.get("delta") if isinstance(item.get("delta"), str) else None,
                route=route,
                source="model_request",
                stage_id=stage_id,
                graph_id=graph_id,
                is_complete=bool(item.get("is_complete", event_type == "done")),
                event_type=event_type,
                meta=payload if isinstance(payload, dict) else None,
            )
            return
        if item_type == "task_dag.model_field" and task_id:
            field_path = str(item.get("field_path") or "model")
            raw_event_type = str(item.get("event_type") or action)
            event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
            await self.emit(
                f"task_dag.tasks.{task_id}.fields.{field_path}",
                item.get("value"),
                delta=item.get("delta") if isinstance(item.get("delta"), str) else None,
                route=route,
                source="model_request",
                task_id=task_id,
                graph_id=graph_id,
                is_complete=bool(item.get("is_complete", event_type == "done")),
                event_type=event_type,
                meta=payload if isinstance(payload, dict) else None,
            )
            return
        if item_type == "task_dag.task" and task_id:
            path = f"task_dag.tasks.{ task_id }.{ action }"
        elif item_type == "task_dag.graph" and graph_id:
            path = f"task_dag.graphs.{ graph_id }.{ action }"
        else:
            path = item_type.replace("/", ".")
        await self.emit(
            path,
            item,
            route=route,
            source="triggerflow",
            stage_id=stage_id,
            task_id=task_id,
            graph_id=graph_id,
            meta=payload if isinstance(payload, dict) else None,
        )

    async def bridge_agent_task_item(self, item: Any, *, route: str = "agent_task"):
        if not isinstance(item, AgentExecutionStreamData):
            await self.emit("agent_task.stream", item, route=route, source="agent_task")
            return
        item_meta = dict(item.meta or {})
        await self.emit(
            item.path,
            item.value,
            delta=item.delta,
            route=route,
            source=item.source or "agent_task",
            stage_id=item.stage_id,
            task_id=item.task_id,
            action_id=item.action_id,
            graph_id=item.graph_id,
            is_complete=item.is_complete,
            event_type="delta" if item.event_type == "delta" else "done",
            meta=item_meta,
        )
