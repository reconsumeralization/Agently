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

import inspect
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
        blocks: Any = None,
    ):
        self.resolver = _coerce_resolver(resolver)
        self.flow = flow
        self.name = name
        self.validator = validator if validator is not None else TaskDAGValidator(resolver=self.resolver)
        self.blocks = blocks

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

    def compile_blocks(
        self,
        graph: TaskDAG | Mapping[str, Any],
        *,
        resolver: TaskDAGResolver | Mapping[str, Any] | None = None,
        blocks: Any = None,
        plan_id: str | None = None,
    ):
        """Compile a validated TaskDAG segment through the Blocks plugin.

        This is the Blocks lifecycle path for bounded DAG segments. TaskDAG
        validation remains owned here; Blocks receives validated graph data and
        lowers nodes to an ExecutionBlockGraph without re-validating DAG rules or
        owning the complete business-task lifecycle.
        """

        merged_resolver = self._merge_resolver(resolver)
        validation = self.validator.validate(graph, resolver=merged_resolver)
        return self._compile_validated_blocks(
            validation,
            blocks=blocks,
            plan_id=plan_id,
        )

    def _compile_validated_blocks(
        self,
        validation: TaskDAGValidation,
        *,
        blocks: Any = None,
        plan_id: str | None = None,
    ):
        blocks_entrypoint = self._resolve_blocks(blocks)
        return blocks_entrypoint.compile(
            {
                "plan_id": plan_id or f"task_dag:{ validation.graph.graph_id }",
                "plan_blocks": [
                    {
                        "id": validation.graph.graph_id,
                        "plan_block_id": "dag_segment",
                        "kind": "dag_segment",
                        "bound_inputs": {
                            "task_dag": validation.graph.to_dict(),
                            "task_dag_validation": validation,
                            "handler_prefix": self._blocks_handler_prefix(validation),
                        },
                    }
                ],
            }
        )

    async def async_run_blocks(
        self,
        graph: TaskDAG | Mapping[str, Any],
        graph_input: Any = None,
        *,
        resolver: TaskDAGResolver | Mapping[str, Any] | None = None,
        blocks: Any = None,
        flow: "TriggerFlow | None" = None,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged_resolver = self._merge_resolver(resolver)
        validation = self.validator.validate(graph, resolver=merged_resolver)
        blocks_entrypoint = self._resolve_blocks(blocks)
        execution_graph = self._compile_validated_blocks(validation, blocks=blocks_entrypoint)
        target_flow = blocks_entrypoint.bind_runtime(execution_graph, flow)
        resolved_runtime_resources = self._blocks_runtime_resources(
            validation,
            merged_resolver,
            runtime_resources,
        )
        execution = target_flow.create_execution(
            auto_close=False,
            concurrency=concurrency,
            runtime_resources=resolved_runtime_resources,
        )
        await execution.async_start(graph_input)
        snapshot = await execution.async_close(timeout=timeout)
        return {
            "execution_block_graph": execution_graph.to_dict(),
            "snapshot": snapshot,
            "evidence": blocks_entrypoint.map_evidence(execution_graph, snapshot).to_dict(),
            "result": dict(blocks_entrypoint.map_result(execution_graph, snapshot)),
        }

    def _merge_resolver(self, resolver: TaskDAGResolver | Mapping[str, Any] | None):
        if resolver is None:
            return self.resolver
        merged = TaskDAGResolver(self.resolver.to_mapping())
        for key, value in _coerce_resolver(resolver).to_mapping().items():
            merged.register(key, value)
        return merged

    def _resolve_blocks(self, blocks: Any = None):
        if blocks is not None:
            return blocks
        if self.blocks is not None:
            return self.blocks
        from agently.base import blocks as default_blocks

        return default_blocks

    @staticmethod
    def _blocks_handler_prefix(validation: TaskDAGValidation) -> str:
        return f"task_dag:{ validation.graph.graph_id }"

    def _blocks_runtime_resources(
        self,
        validation: TaskDAGValidation,
        resolver: TaskDAGResolver,
        runtime_resources: dict[str, Any] | None,
    ) -> dict[str, Any]:
        resolved = dict(runtime_resources or {})
        blocks_handlers = dict(resolved.get("blocks.handlers") or {})
        prefix = self._blocks_handler_prefix(validation)
        for task in validation.graph.tasks:
            blocks_handlers[f"{ prefix }:{ task.id }"] = self._blocks_task_handler(
                validation,
                resolver,
                task,
            )
        resolved["blocks.handlers"] = blocks_handlers
        return resolved

    @staticmethod
    def _blocks_task_handler(
        validation: TaskDAGValidation,
        resolver: TaskDAGResolver,
        task: TaskDAGNode,
    ):
        async def run_task(context: Mapping[str, Any]):
            runtime_data = context["runtime_data"]
            dependency_results = context.get("dependency_results", {})
            if not isinstance(dependency_results, Mapping):
                dependency_results = {}
            graph_input = context.get("graph_input")
            task_input = {
                "task_id": task.id,
                "task": task.to_dict(),
                "graph_id": validation.graph.graph_id,
                "init_input": graph_input,
                "graph_input": graph_input,
                "inputs": task.inputs,
                "deps": dict(dependency_results),
                "state": context.get("state", {}),
                "trigger": context.get("input"),
                "dependency_payload": context.get("input"),
            }
            task_context = TaskDAGContext(
                graph=validation.graph,
                task=task,
                task_input=task_input,
                graph_input=graph_input,
                dependency_results=dict(dependency_results),
                dependency_payload=context.get("input"),
                resources=runtime_data.resources.to_dict(),
                runtime_data=runtime_data,
            )
            handler = resolver.resolve(task)
            if handler is None:
                raise ValueError(
                    f"Dynamic task '{ task.id }' kind '{ task.kind }' has no executable handler."
                )
            result = handler(task_context)
            if inspect.isawaitable(result):
                result = await result
            return result

        return run_task

    @staticmethod
    def resolver_factory(func: Callable[[TaskDAGNode], Any]):
        return task_dag_resolver_factory(func)
