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
from typing import Any, Literal

from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter


class AgentExecutionStream:
    """Execution-local stream buffer and TriggerFlow bridge."""

    def __init__(self):
        self.items: list[AgentExecutionStreamData] = []
        self.queues: list[asyncio.Queue[Any]] = []

    async def emit(
        self,
        path: str,
        value: Any,
        *,
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
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            is_complete=is_complete,
            event_type=event_type,
            source=source,
            route=route,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=DataFormatter.sanitize(meta) if meta is not None else None,
        )
        self.items.append(item)
        for queue in list(self.queues):
            await queue.put(item)
        return item

    async def close(self):
        for queue in list(self.queues):
            await queue.put(None)

    async def bridge_task_dag_item(self, item: Any, *, route: str):
        if not isinstance(item, dict):
            await self.emit("runtime.stream", item, route=route, source="triggerflow")
            return
        item_type = str(item.get("type") or "runtime.stream")
        action = str(item.get("action") or "event")
        payload = item.get("payload", {})
        task_id = str(item.get("task_id") or "") or None
        graph_id = str(item.get("graph_id") or "") or None
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
            task_id=task_id,
            graph_id=graph_id,
            meta=payload if isinstance(payload, dict) else None,
        )
