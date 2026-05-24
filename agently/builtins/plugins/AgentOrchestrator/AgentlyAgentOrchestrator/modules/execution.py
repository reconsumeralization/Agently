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
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, Literal, TYPE_CHECKING

from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter, FunctionShifter

from .routing import HybridRoutePlanner
from .routes import run_dynamic_task_route, run_model_request_route, run_skills_route
from .stream import AgentExecutionStream

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import OutputValidateHandler, RunContext


class AgentExecution:
    """Response-style execution facade for one Agent turn."""

    def __init__(self, agent: "BaseAgent", *, parent_run_context: "RunContext | None" = None):
        self.agent = agent
        self.id = uuid.uuid4().hex
        self.parent_run_context = parent_run_context
        self.route_plan: dict[str, Any] = {}
        self.close_snapshot: dict[str, Any] = {}
        self.logs: dict[str, Any] = {}
        self.result: Any = None
        self.status = "created"
        prompt_snapshot = agent.request.prompt.get()
        self.prompt_snapshot: dict[str, Any] = prompt_snapshot if isinstance(prompt_snapshot, dict) else {}

        self._started = False
        self._completed = False
        self._start_lock = asyncio.Lock()
        self.route_planner = HybridRoutePlanner(agent, prompt_snapshot=self.prompt_snapshot)
        self.stream = AgentExecutionStream()
        self._error: BaseException | None = None

        self.start = FunctionShifter.syncify(self.async_start)
        self.get_data = FunctionShifter.syncify(self.async_get_data)
        self.get_text = FunctionShifter.syncify(self.async_get_text)
        self.get_meta = FunctionShifter.syncify(self.async_get_meta)
        self.get_generator = self._get_generator

    def task_target(self) -> str:
        return self.route_planner.task_target()

    async def emit_stream(
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
        delta: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        return await self.stream.emit(
            path,
            value,
            delta=delta,
            route=route,
            source=source,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            is_complete=is_complete,
            event_type=event_type,
            meta=meta,
        )

    async def close_streams(self):
        await self.stream.close()

    def dynamic_task_candidates(self) -> list[dict[str, Any]]:
        return self.route_planner.dynamic_task_candidates()

    def action_candidates(self) -> list[dict[str, Any]]:
        return self.route_planner.action_candidates()

    def skill_candidate_summary(self) -> dict[str, Any]:
        return self.route_planner.skill_candidate_summary()

    async def select_route(self) -> tuple[str, dict[str, Any]]:
        return await self.route_planner.select_route()

    async def bridge_task_dag_stream_item(self, item: Any, *, route: str):
        await self.stream.bridge_task_dag_item(item, route=route)

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
        await self.stream.bridge_model_stream_item(
            item,
            route=route,
            source=source,
            path_prefix=path_prefix,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=meta,
        )

    async def async_start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> Any:
        async with self._start_lock:
            if self._completed:
                if self._error is not None:
                    raise self._error
                return self.result
            if self._started:
                while not self._completed:
                    await asyncio.sleep(0.01)
                if self._error is not None:
                    raise self._error
                return self.result
            self._started = True
            self.status = "running"
            try:
                route, route_meta = await self.select_route()
                self.route_plan = self.route_planner.build_route_plan(
                    execution_id=self.id,
                    route=route,
                    route_meta=route_meta,
                )
                await self.emit_stream("route.selected", self.route_plan, route=route)
                if route == "skills":
                    self.result = await run_skills_route(self, route_meta)
                elif route == "dynamic_task":
                    self.result = await run_dynamic_task_route(self, route_meta)
                else:
                    self.result = await run_model_request_route(
                        self,
                        type=type,
                        ensure_keys=ensure_keys,
                        ensure_all_keys=ensure_all_keys,
                        validate_handler=validate_handler,
                        key_style=key_style,
                        max_retries=max_retries,
                        raise_ensure_failure=raise_ensure_failure,
                    )
                if self.status == "running":
                    self.status = "success"
                await self.emit_stream("result", self.result, route=route, source="agent_execution")
                return self.result
            except BaseException as error:
                self.status = "error"
                self._error = error
                await self.emit_stream(
                    "error",
                    {"type": error.__class__.__name__, "message": str(error)},
                    source="agent_execution",
                )
                raise
            finally:
                self._completed = True
                await self.close_streams()

    async def async_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> Any:
        return await self.async_start(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    async def async_get_text(self) -> str:
        data = await self.async_get_data()
        if isinstance(data, str):
            return data
        return json.dumps(DataFormatter.sanitize(data), ensure_ascii=False)

    async def async_get_meta(self) -> dict[str, Any]:
        if not self._completed:
            await self.async_start()
        return {
            "execution_id": self.id,
            "status": self.status,
            "route_plan": DataFormatter.sanitize(self.route_plan),
            "close_snapshot": DataFormatter.sanitize(self.close_snapshot),
            "logs": DataFormatter.sanitize(self.logs),
        }

    async def get_async_generator(
        self,
        type: Literal["instant", "streaming_parse", "all"] | str | None = "instant",
        content: Any = None,
        **_: Any,
    ) -> AsyncGenerator[Any, None]:
        if content is not None and type is None:
            type = content
        if self._completed:
            for item in self.stream.items:
                yield ("agent_execution", item) if type == "all" else item
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in self.stream.items:
            await queue.put(item)
        self.stream.queues.append(queue)
        start_task = asyncio.create_task(self.async_start())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield ("agent_execution", item) if type == "all" else item
            await start_task
        finally:
            if queue in self.stream.queues:
                self.stream.queues.remove(queue)

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return FunctionShifter.syncify_async_generator(self.get_async_generator(*args, **kwargs))
