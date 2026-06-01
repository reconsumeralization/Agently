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
import time
from collections.abc import Mapping
from typing import Any, Literal, cast

from agently.types.data import AgentExecutionOutputPolicy, AgentExecutionStreamData
from agently.utils import DataFormatter


class _DeltaBuffer:
    def __init__(self, item: AgentExecutionStreamData):
        now = time.time()
        self.item = item
        self.count = 1
        self.chars = len(item.delta or "")
        self.first_event_at = now
        self.last_event_at = now


class AgentExecutionStream:
    """Execution-local stream buffer and TriggerFlow bridge."""

    def __init__(
        self,
        *,
        execution_id: str | None = None,
        execution_mode: str | None = None,
        lineage: Mapping[str, Any] | None = None,
        output_policy: AgentExecutionOutputPolicy | Mapping[str, Any] | None = None,
    ):
        self.items: list[AgentExecutionStreamData] = []
        self.queues: list[asyncio.Queue[Any]] = []
        self.execution_id = execution_id
        self.execution_mode = execution_mode
        self.lineage = dict(lineage or {})
        self.output_policy = dict(output_policy or {})
        self._delta_buffer: _DeltaBuffer | None = None

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
        is_complete: bool = True,
        event_type: Literal["delta", "done"] = "done",
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        item_meta = dict(meta or {})
        if self.execution_id is not None:
            item_meta.setdefault("execution_id", self.execution_id)
        if self.execution_mode is not None:
            item_meta.setdefault("execution_mode", self.execution_mode)
        if self.lineage:
            item_meta.setdefault("lineage", dict(self.lineage))
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            delta=delta,
            is_complete=is_complete,
            event_type=event_type,
            source=source,
            route=route,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=DataFormatter.sanitize(item_meta) if item_meta else None,
        )
        if self._should_buffer_delta(item):
            flushed = await self._buffer_delta(item)
            return flushed or item
        if self.output_policy.get("flush_on_done", True) or event_type != "delta":
            await self.flush_delta_buffer()
        return await self._publish(item)

    async def close(self):
        await self.flush_delta_buffer()
        for queue in list(self.queues):
            await queue.put(None)

    async def flush_delta_buffer(self) -> AgentExecutionStreamData | None:
        if self._delta_buffer is None:
            return None
        buffered = self._delta_buffer
        self._delta_buffer = None
        item_meta = dict(buffered.item.meta or {})
        item_meta.update(
            {
                "coalesced": True,
                "coalesced_count": buffered.count,
                "coalesced_chars": buffered.chars,
                "first_event_at": buffered.first_event_at,
                "last_event_at": buffered.last_event_at,
            }
        )
        item = buffered.item.model_copy(update={"meta": DataFormatter.sanitize(item_meta)})
        return await self._publish(item)

    async def _publish(self, item: AgentExecutionStreamData) -> AgentExecutionStreamData:
        self.items.append(item)
        for queue in list(self.queues):
            await queue.put(item)
        return item

    def _should_buffer_delta(self, item: AgentExecutionStreamData) -> bool:
        interval = self.output_policy.get("delta_emit_interval", 0.0)
        if interval in (None, -1, "-1"):
            interval = 0.0
        try:
            interval_value = float(cast(Any, interval))
        except (TypeError, ValueError):
            interval_value = 0.0
        return interval_value > 0 and item.event_type == "delta" and isinstance(item.delta, str)

    async def _buffer_delta(self, item: AgentExecutionStreamData) -> AgentExecutionStreamData | None:
        if self._delta_buffer is None or not self._is_compatible_delta(self._delta_buffer.item, item):
            await self.flush_delta_buffer()
            self._delta_buffer = _DeltaBuffer(item)
            return None

        buffer = self._delta_buffer
        merged_delta = (buffer.item.delta or "") + (item.delta or "")
        merged_value = (buffer.item.value or "") + (item.delta or "") if isinstance(buffer.item.value, str) else item.value
        buffer.item = buffer.item.model_copy(update={"delta": merged_delta, "value": merged_value})
        buffer.count += 1
        buffer.chars += len(item.delta or "")
        buffer.last_event_at = time.time()
        if self._should_flush_buffer(buffer):
            return await self.flush_delta_buffer()
        return None

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

    def _should_flush_buffer(self, buffer: _DeltaBuffer) -> bool:
        max_items = self.output_policy.get("delta_max_items")
        max_chars = self.output_policy.get("delta_max_chars")
        interval = self.output_policy.get("delta_emit_interval", 0.0)
        if isinstance(max_items, int) and max_items > 0 and buffer.count >= max_items:
            return True
        if isinstance(max_chars, int) and max_chars > 0 and buffer.chars >= max_chars:
            return True
        if isinstance(interval, (int, float)) and interval > 0 and (time.time() - buffer.first_event_at) >= float(interval):
            return True
        return False

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
