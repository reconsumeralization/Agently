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

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from agently.types.data import TaskDAG, TaskDAGNode

from .TaskDAGResolver import (
    _GRAPH_SCHEMA_VERSION,
    _TASK_ID_PATTERN,
    TaskDAGContext,
    TaskDAGHandler,
    TaskDAGResolver,
    task_dag_resolver_factory,
    _coerce_resolver,
)
from .TaskDAGRuntime import CompiledTaskDAG, compile_task_dag
from .TaskDAGValidation import (
    TaskDAGValidation,
    TaskDAGValidator,
    validate_task_dag,
    validate_task_dag_planner_output,
)

if TYPE_CHECKING:
    from agently.core.orchestration.TriggerFlow import TriggerFlow


class TaskDAGExecutor:
    def __init__(
        self,
        resolver: TaskDAGResolver | Mapping[str, Any] | None = None,
        *,
        flow: "TriggerFlow | None" = None,
        name: str | None = None,
        validator: TaskDAGValidator | None = None,
    ):
        self.resolver = _coerce_resolver(resolver)
        self.flow = flow
        self.name = name
        self.validator = validator if validator is not None else TaskDAGValidator(resolver=self.resolver)

    def compile(
        self,
        graph: TaskDAG | Mapping[str, Any],
        *,
        resolver: TaskDAGResolver | Mapping[str, Any] | None = None,
        flow: "TriggerFlow | None" = None,
        name: str | None = None,
    ) -> CompiledTaskDAG:
        merged_resolver = self._merge_resolver(resolver)
        validation = self.validator.validate(graph, resolver=merged_resolver)
        compiled = compile_task_dag(
            validation.graph,
            resolver=merged_resolver,
            flow=flow if flow is not None else self.flow,
            name=name if name is not None else self.name,
        )
        if self.flow is None:
            self.flow = compiled.flow
        return compiled

    async def async_run(
        self,
        graph: TaskDAG | Mapping[str, Any],
        graph_input: Any = None,
        *,
        resolver: TaskDAGResolver | Mapping[str, Any] | None = None,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        compiled = self.compile(graph, resolver=resolver)
        return await compiled.async_run(
            graph_input,
            timeout=timeout,
            concurrency=concurrency,
            runtime_resources=runtime_resources,
        )

    def _merge_resolver(self, resolver: TaskDAGResolver | Mapping[str, Any] | None):
        if resolver is None:
            return self.resolver
        merged = TaskDAGResolver(self.resolver.to_mapping())
        for key, value in _coerce_resolver(resolver).to_mapping().items():
            merged.register(key, value)
        return merged

    @staticmethod
    def resolver_factory(func: Callable[[TaskDAGNode], Any]):
        return task_dag_resolver_factory(func)
