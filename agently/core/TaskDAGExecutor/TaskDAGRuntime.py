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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agently.types.data import TaskDAG, TaskDAGNode
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import FunctionShifter

from .TaskDAGHelpers import (
    _approval_payload,
    _approval_required,
    _approval_type,
    _chunk_name,
    _collect_semantic_outputs,
    _done_graph_event,
    _done_task_event,
    _extract_artifact_refs,
    _failed_task_event,
    _fallback_action,
    _graph_signature,
    _is_approval_task,
    _start_task_event,
)
from .TaskDAGResolver import DynamicTaskContext, DynamicTaskResolver, _coerce_resolver
from .TaskDAGValidation import TaskDAGValidation, validate_task_dag

if TYPE_CHECKING:
    from agently.core.TriggerFlow import TriggerFlow
    from agently.core.TriggerFlow.Execution import TriggerFlowExecution


_DYNAMIC_CACHE_ATTR = "_task_dag_executor_cache"


@dataclass
class CompiledTaskDAG:
    graph: TaskDAG
    flow: "TriggerFlow"
    validation: TaskDAGValidation

    def create_execution(self, **kwargs: Any) -> "TriggerFlowExecution":
        return self.flow.create_execution(**kwargs)

    async def async_run(
        self,
        graph_input: Any = None,
        *,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        execution = self.create_execution(
            auto_close=False,
            concurrency=concurrency,
            runtime_resources=runtime_resources,
        )
        await execution.async_start(graph_input)
        return await execution.async_close(timeout=timeout)

def compile_task_dag(
    graph: TaskDAG | Mapping[str, Any],
    *,
    resolver: DynamicTaskResolver | Mapping[str, Any] | None = None,
    flow: "TriggerFlow | None" = None,
    name: str | None = None,
) -> CompiledTaskDAG:
    from agently.core.TriggerFlow import TriggerFlow

    resolved_resolver = _coerce_resolver(resolver)
    validation = validate_task_dag(graph, resolver=resolved_resolver)
    normalized = validation.graph
    target_flow = flow if flow is not None else TriggerFlow(name=name or f"dynamic-task:{ normalized.graph_id }")
    _assert_graph_signature_compatible(target_flow, normalized)

    cache = _get_compile_cache(target_flow)
    result_lock = cache["result_locks"].setdefault(normalized.graph_id, asyncio.Lock())

    kickoff = _cached_handler(
        cache,
        normalized.graph_id,
        "kickoff",
        "*",
        lambda: _make_kickoff_handler(normalized, validation.root_task_ids),
    )
    target_flow.to(kickoff, name=_chunk_name(normalized.graph_id, "kickoff", "*"))

    for task in normalized.tasks:
        runner = _cached_handler(
            cache,
            normalized.graph_id,
            "run",
            task.id,
            lambda task=task: _make_task_runner(
                graph=normalized,
                task=task,
                resolver=resolved_resolver,
                result_lock=result_lock,
            ),
        )
        trigger = _task_trigger(target_flow, task)
        trigger.to(runner, name=_chunk_name(normalized.graph_id, "run", task.id))

    finalize = _cached_handler(
        cache,
        normalized.graph_id,
        "finalize",
        "*",
        lambda: _make_finalize_handler(normalized),
    )
    target_flow.when(_done_graph_event(normalized.graph_id)).to(
        finalize,
        name=_chunk_name(normalized.graph_id, "finalize", "*"),
    )
    return CompiledTaskDAG(
        graph=normalized,
        flow=target_flow,
        validation=validation,
    )


def _get_compile_cache(flow: "TriggerFlow") -> dict[str, Any]:
    cache = getattr(flow, _DYNAMIC_CACHE_ATTR, None)
    if cache is None:
        cache = {
            "graph_signatures": {},
            "handlers": {},
            "result_locks": {},
        }
        setattr(flow, _DYNAMIC_CACHE_ATTR, cache)
    return cache


def _assert_graph_signature_compatible(flow: "TriggerFlow", graph: TaskDAG) -> None:
    cache = _get_compile_cache(flow)
    signature = _graph_signature(graph)
    existing = cache["graph_signatures"].get(graph.graph_id)
    if existing is not None and existing != signature:
        raise ValueError(
            f"Task DAG '{ graph.graph_id }' was already compiled with a different definition."
        )
    cache["graph_signatures"][graph.graph_id] = signature


def _cached_handler(
    cache: dict[str, Any],
    graph_id: str,
    phase: str,
    task_id: str,
    factory: Callable[[], Callable[[TriggerFlowRuntimeData], Any]],
):
    key = (graph_id, phase, task_id)
    if key not in cache["handlers"]:
        cache["handlers"][key] = factory()
    return cache["handlers"][key]


def _make_kickoff_handler(
    graph: TaskDAG,
    root_task_ids: tuple[str, ...],
):
    async def kickoff(data: TriggerFlowRuntimeData):
        await data.async_set_state("graph_id", graph.graph_id, emit=False)
        await data.async_set_state("graph_input", data.value, emit=False)
        await data.async_set_state("task_results", {}, emit=False)
        await data.async_set_state("task_statuses", {}, emit=False)
        await data.async_set_state("task_failures", {}, emit=False)
        await data.async_set_state("artifact_refs", {}, emit=False)
        await data.async_set_state("semantic_outputs", {}, emit=False)
        await data.async_set_state("task_dag", graph.to_dict(), emit=False)
        for task_id in root_task_ids:
            data.emit_nowait(_start_task_event(task_id), {"task_id": task_id, "graph_id": graph.graph_id})

    return kickoff


def _make_task_runner(
    *,
    graph: TaskDAG,
    task: TaskDAGNode,
    resolver: DynamicTaskResolver,
    result_lock: asyncio.Lock,
):
    async def run_task(data: TriggerFlowRuntimeData):
        graph_input = data.get_state("graph_input")
        task_results = dict(data.get_state("task_results", {}) or {})
        dependency_results = {dependency: task_results[dependency] for dependency in task.depends_on}
        task_input = {
            "task_id": task.id,
            "task": task.to_dict(),
            "graph_id": graph.graph_id,
            "graph_input": graph_input,
            "inputs": task.inputs,
            "deps": dependency_results,
            "dependency_payload": data.value,
        }
        await _put_task_event(data, graph, task, "start", input=task_input)

        try:
            if _approval_required(task) and not data.is_resume:
                await _put_task_event(data, graph, task, "approval_required", input=task_input)
                return await data.async_pause_for(
                    type=_approval_type(task),
                    payload=_approval_payload(task, task_input),
                    interrupt_id=f"task:{ graph.graph_id }:{ task.id }",
                    resume_to="self",
                )
            output = data.resume.value if _is_approval_task(task) and data.is_resume else None
            if not _is_approval_task(task):
                context = DynamicTaskContext(
                    graph=graph,
                    task=task,
                    task_input=task_input,
                    graph_input=graph_input,
                    dependency_results=dependency_results,
                    dependency_payload=data.value,
                    resources=data.resources.to_dict(),
                    runtime_data=data,
                )
                handler = resolver.resolve(task)
                output = await _execute_handler(handler, context)
        except Exception as error:
            await _record_task_failure(data, graph, task, error, result_lock=result_lock)
            await _put_task_event(data, graph, task, "fail", error=str(error))
            if _fallback_action(task) == "skip":
                output = {"status": "skipped", "reason": str(error)}
                await _record_task_success(data, graph, task, output, result_lock=result_lock)
                await _put_task_event(data, graph, task, "skipped", output=output)
                return output
            raise

        await _record_task_success(data, graph, task, output, result_lock=result_lock)
        await _put_task_event(data, graph, task, "complete", output=output)
        return output

    return run_task


def _make_finalize_handler(graph: TaskDAG):
    async def finalize(data: TriggerFlowRuntimeData):
        task_results = dict(data.get_state("task_results", {}) or {})
        artifact_refs = dict(data.get_state("artifact_refs", {}) or {})
        semantic_outputs = _collect_semantic_outputs(graph, task_results, artifact_refs)
        await data.async_set_state("semantic_outputs", semantic_outputs, emit=False)
        execution_result = {
            "graph_id": graph.graph_id,
            "task_results": task_results,
            "artifact_refs": artifact_refs,
            "semantic_outputs": semantic_outputs,
            "diagnostics": [dict(item) for item in graph.diagnostics],
        }
        await data.async_set_state("task_dag_execution", execution_result, emit=False)
        await _put_graph_event(data, graph, "complete", result=execution_result)
        return execution_result

    return finalize


async def _execute_handler(handler: Any, context: DynamicTaskContext):
    from agently.core.TriggerFlow import TriggerFlow

    if isinstance(handler, TriggerFlow):
        execution = handler.create_execution(
            auto_close=True,
            runtime_resources=dict(context.resources),
            parent_run_context=context.runtime_data.chunk_run_context,
        )
        return await execution.async_start(dict(context.task_input))
    if callable(handler):
        return await FunctionShifter.asyncify(handler)(context)
    raise ValueError(
        f"Dynamic task '{ context.task.id }' kind '{ context.task.kind }' has no executable handler."
    )


async def _record_task_success(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    task: TaskDAGNode,
    output: Any,
    *,
    result_lock: asyncio.Lock,
) -> None:
    async with result_lock:
        results = dict(data.get_state("task_results", {}) or {})
        statuses = dict(data.get_state("task_statuses", {}) or {})
        artifact_refs = dict(data.get_state("artifact_refs", {}) or {})
        results[task.id] = output
        statuses[task.id] = "completed"
        extracted_artifacts = _extract_artifact_refs(output)
        if extracted_artifacts:
            artifact_refs[task.id] = extracted_artifacts
        await data.async_set_state("task_results", results, emit=False)
        await data.async_set_state("task_statuses", statuses, emit=False)
        if extracted_artifacts:
            await data.async_set_state("artifact_refs", artifact_refs, emit=False)
        data.emit_nowait(_done_task_event(task.id), {"task_id": task.id, "result": output})
        if len(results) == len(graph.tasks):
            data.emit_nowait(_done_graph_event(graph.graph_id), dict(results))


async def _record_task_failure(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    task: TaskDAGNode,
    error: Exception,
    *,
    result_lock: asyncio.Lock,
) -> None:
    async with result_lock:
        failures = dict(data.get_state("task_failures", {}) or {})
        statuses = dict(data.get_state("task_statuses", {}) or {})
        failures[task.id] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        statuses[task.id] = "failed"
        await data.async_set_state("task_failures", failures, emit=False)
        await data.async_set_state("task_statuses", statuses, emit=False)
        data.emit_nowait(_failed_task_event(task.id), failures[task.id])


async def _put_task_event(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    task: TaskDAGNode,
    action: Literal["start", "complete", "fail", "skipped", "approval_required"],
    **payload: Any,
) -> None:
    await data.execution.async_put_into_stream(
        {
            "type": "task_dag.task",
            "action": action,
            "graph_id": graph.graph_id,
            "task_id": task.id,
            "task_kind": task.kind,
            "payload": payload,
        },
        _skip_contract_validation=True,
    )


async def _put_graph_event(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    action: Literal["complete"],
    **payload: Any,
) -> None:
    await data.execution.async_put_into_stream(
        {
            "type": "task_dag.graph",
            "action": action,
            "graph_id": graph.graph_id,
            "payload": payload,
        },
        _skip_contract_validation=True,
    )


def _task_trigger(flow: "TriggerFlow", task: TaskDAGNode):
    if not task.depends_on:
        return flow.when(_start_task_event(task.id))
    if len(task.depends_on) == 1:
        return flow.when(_done_task_event(task.depends_on[0]))
    return flow.when(
        {"event": [_done_task_event(dependency) for dependency in task.depends_on]},
        mode="and",
    )
