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
from typing import Any, Literal, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .execution import AgentExecution
    from agently.types.data import OutputValidateHandler


async def run_model_request_route(
    execution: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"],
    ensure_keys: list[str] | None,
    ensure_all_keys: bool | None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
    key_style: Literal["dot", "slash"],
    max_retries: int,
    raise_ensure_failure: bool,
) -> Any:
    agent = execution.agent
    turn_run_context = execution.agent_turn_run_context
    execution._agent_turn_completion_owned_by_response = True
    if ensure_all_keys is not None:
        agent.request.prompt.set("ensure_all_keys", ensure_all_keys)
    response = agent.request.get_response(parent_run_context=turn_run_context)
    execution.record_model_response_id(response.id)
    has_structured_stream = bool(execution.prompt_snapshot.get("output"))
    if has_structured_stream:
        async for item in response.get_async_generator(type="instant"):
            await execution.bridge_model_stream_item(item, route="model_request")
    else:
        async for event, data in response.get_async_generator(type="all"):
            if event in {"action", "tool"}:
                await execution.record_action_log(
                    data,
                    route="model_request",
                    source="action" if event == "action" else "tool",
                )
            elif event == "delta":
                await execution.emit_stream(
                    "model.delta",
                    data,
                    route="model_request",
                    source="model_request",
                    delta=str(data),
                    event_type="delta",
                    is_complete=False,
                )
            elif event == "done":
                await execution.emit_stream(
                    "model.text",
                    data,
                    route="model_request",
                    source="model_request",
                )
    data = await response.async_get_data(
        type=type,
        ensure_keys=ensure_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
    )
    full_result_data = getattr(getattr(response, "result", None), "full_result_data", {})
    extra = full_result_data.get("extra", {}) if isinstance(full_result_data, dict) else {}
    if isinstance(extra, dict):
        action_logs = extra.get("action_logs", [])
        if isinstance(action_logs, list):
            for log in action_logs:
                await execution.record_action_log(log, route="model_request", source="action")
        tool_logs = extra.get("tool_logs", [])
        if isinstance(tool_logs, list):
            for log in tool_logs:
                await execution.record_action_log(log, route="model_request", source="tool")
    execution.close_snapshot = {"status": "success", "route": "model_request"}
    return data


async def run_skills_route(execution: "AgentExecution", route_meta: dict[str, Any]) -> Any:
    mode = cast(Any, route_meta.get("mode", "model_decision"))
    task = execution.task_target()
    agent = cast(Any, execution.agent)
    output = execution.prompt_snapshot.get("output")
    output_format = execution.prompt_snapshot.get("output_format") or "auto"
    route_options = execution.route_options("skills")
    effort = route_options.get("effort")
    if effort is not None:
        effort = str(effort)
        execution.record_consumed_option("routes.skills.effort", effort, owner="AgentlySkillsExecutor")
    plan = await agent.async_resolve_skills_plan(
        task,
        mode=mode,
        output=output,
        output_format=output_format,
    )
    await execution.emit_stream(
        "route.skills.plan",
        plan,
        route="skills",
        source="skills_executor",
        meta={"status": plan.get("status")},
    )
    if not plan.get("selected_skills"):
        if mode == "required" or plan.get("status") in {"blocked", "rejected"}:
            async def bridge_runtime_stream(item: dict[str, Any]):
                await execution.bridge_task_dag_stream_item(item, route="skills")

            skills_execution = await agent.async_execute_skills_plan(
                task,
                plan=plan,
                output_format=output_format,
                stream_handler=bridge_runtime_stream,
                effort=effort,
            )
            execution.raise_if_limit_exceeded()
            execution.close_snapshot = skills_execution.close_snapshot
            execution.logs["route_logs"] = dict(skills_execution.to_dict())
            execution.status = skills_execution.status
            return skills_execution.output
        await execution.emit_stream(
            "route.fallback",
            {"from": "skills", "to": "model_request", "reason": "no_matching_skill"},
            route="model_request",
        )
        return await run_model_request_route(
            execution,
            type="parsed",
            ensure_keys=None,
            ensure_all_keys=None,
            validate_handler=None,
            key_style="dot",
            max_retries=3,
            raise_ensure_failure=True,
        )

    async def bridge_runtime_stream(item: dict[str, Any]):
        await execution.bridge_task_dag_stream_item(item, route="skills")

    skills_execution = await agent.async_execute_skills_plan(
        task,
        plan=plan,
        output_format=output_format,
        stream_handler=bridge_runtime_stream,
        effort=effort,
    )
    execution.raise_if_limit_exceeded()
    for log in skills_execution.skill_logs:
        await execution.emit_stream(
            f"skills.{ log.get('skill_id', 'skill') }",
            log,
            route="skills",
            source="skills_executor",
            stage_id=None,
        )
    for log in skills_execution.action_logs:
        await execution.record_action_log(log, route="skills", source="action")
    execution.close_snapshot = skills_execution.close_snapshot
    execution.logs["route_logs"] = dict(skills_execution.to_dict())
    execution.status = skills_execution.status
    return skills_execution.output


async def run_dynamic_task_route(execution: "AgentExecution", route_meta: dict[str, Any]) -> Any:
    candidate = dict(route_meta.get("candidate") or {})
    mode = str(candidate.get("mode") or "auto")
    target = execution.task_target()
    action_candidates = execution.action_candidates()
    actions = candidate.get("actions")
    if actions is None and action_candidates:
        actions = getattr(execution.agent, "action", None)
    task = execution.agent.create_dynamic_task(
        target,
        plan=candidate.get("plan"),
        planner=candidate.get("planner"),
        model=candidate.get("model"),
        actions=actions,
        skills=candidate.get("skills"),
        handlers=candidate.get("handlers"),
        name=candidate.get("name"),
        max_tasks=candidate.get("max_tasks"),
        output_schema=candidate.get("output_schema"),
        ensure_keys=candidate.get("ensure_keys"),
        output_format=candidate.get("output_format"),
        _prompt_snapshot=execution.prompt_snapshot,
    )
    graph = candidate.get("plan")
    if graph is None or mode == "auto":
        graph = await task.async_plan(max_retries=int(candidate.get("max_retries", 3) or 3))
    task.validate(graph, strict_schema_version=True)
    graph_dict = graph.to_dict() if hasattr(graph, "to_dict") else dict(graph)
    await execution.emit_stream("route.dynamic_task.graph", graph_dict, route="dynamic_task", source="dynamic_task")

    compiled = task.compile(graph)
    dag_execution = compiled.create_execution(
        auto_close=False,
        parent_run_context=execution.agent_turn_run_context,
    )
    graph_input, graph_input_source = _resolve_dynamic_task_graph_input(execution, candidate, target)

    async def runner():
        try:
            await dag_execution.async_start(graph_input)
            return await dag_execution.async_close(timeout=candidate.get("timeout", 30))
        except BaseException as error:
            await dag_execution.async_stop_stream()
            if _is_init_placeholder_error(error):
                raise ValueError(
                    f"{ error } Agent Dynamic Task route resolved graph_input from "
                    f"{ graph_input_source }."
                ) from error
            raise

    run_task = asyncio.create_task(runner())
    while not getattr(dag_execution, "_started", False) and not run_task.done():
        await asyncio.sleep(0)
    if not getattr(dag_execution, "_started", False):
        close_snapshot = await run_task
        task_result = close_snapshot.get("state", {}).get("task_dag_execution", close_snapshot)
        execution.close_snapshot = close_snapshot
        execution.logs["route_logs"] = {"task_dag": close_snapshot}
        return task_result
    stream = dag_execution.get_async_runtime_stream(timeout=None)
    try:
        async for item in stream:
            await execution.bridge_task_dag_stream_item(item, route="dynamic_task")
        close_snapshot = await run_task
    except BaseException:
        if not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        raise
    task_result = close_snapshot.get("state", {}).get("task_dag_execution", close_snapshot)
    execution.close_snapshot = close_snapshot
    execution.logs["route_logs"] = {"task_dag": close_snapshot}
    return task_result


def _resolve_dynamic_task_graph_input(
    execution: "AgentExecution",
    candidate: dict[str, Any],
    target: str,
) -> tuple[Any, str]:
    if candidate.get("graph_input_provided", False):
        return candidate.get("graph_input"), "use_dynamic_task(graph_input=...)"

    prompt_snapshot = getattr(execution, "prompt_snapshot", {})
    if isinstance(prompt_snapshot, dict) and "input" in prompt_snapshot and prompt_snapshot.get("input") is not None:
        return prompt_snapshot.get("input"), "execution prompt snapshot input slot"

    return {"target": target}, "fallback target"


def _is_init_placeholder_error(error: BaseException) -> bool:
    message = str(error)
    return "runtime placeholder" in message and "${INIT" in message
