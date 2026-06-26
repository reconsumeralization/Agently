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
from collections.abc import Awaitable, Callable, Mapping
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
