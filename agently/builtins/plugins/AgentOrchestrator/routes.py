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
    from agently.builtins.plugins.AgentOrchestrator.execution import AgentExecution
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
    turn_run_context = agent._create_agent_turn_run_context(parent_run_context=execution.parent_run_context)
    await agent._async_emit_agent_turn_started(turn_run_context)
    if ensure_all_keys is not None:
        agent.request.prompt.set("ensure_all_keys", ensure_all_keys)
    response = agent.request.get_response(parent_run_context=turn_run_context)
    data = await response.async_get_data(
        type=type,
        ensure_keys=ensure_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
    )
    execution.close_snapshot = {"status": "success", "route": "model_request"}
    execution.logs = {"model_response_id": response.id}
    return data


async def run_skills_route(execution: "AgentExecution", route_meta: dict[str, Any]) -> Any:
    mode = cast(Any, route_meta.get("mode", "model_decision"))
    task = execution.task_target()
    agent = cast(Any, execution.agent)
    plan = await agent.async_resolve_skills_plan(task, mode=mode, scope="execution")
    await execution.emit_stream(
        "route.skills.plan",
        plan,
        route="skills",
        source="skills_executor",
        meta={"status": plan.get("status")},
    )
    if not plan.get("selected_skills"):
        if mode == "required" or plan.get("status") in {"blocked", "rejected"}:
            skills_execution = await agent.async_execute_skills_plan(task, plan=plan)
            execution.close_snapshot = skills_execution.close_snapshot
            execution.logs = dict(skills_execution.to_dict())
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

    skills_execution = await agent.async_execute_skills_plan(task, plan=plan)
    for item in skills_execution.runtime_stream:
        await execution.bridge_task_dag_stream_item(item, route="skills")
    for log in skills_execution.skill_logs:
        await execution.emit_stream(
            f"skills.stages.{ log.get('stage_id', 'stage') }",
            log,
            route="skills",
            source="skills_executor",
            stage_id=str(log.get("stage_id") or "") or None,
        )
    for log in skills_execution.action_logs:
        await execution.emit_stream(
            f"actions.{ log.get('action_id', 'action') }",
            log,
            route="skills",
            source="action",
            action_id=str(log.get("action_id") or "") or None,
        )
    execution.close_snapshot = skills_execution.close_snapshot
    execution.logs = dict(skills_execution.to_dict())
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
    )
    graph = candidate.get("plan")
    if graph is None or mode == "auto":
        graph = await task.async_plan(max_retries=int(candidate.get("max_retries", 3) or 3))
    task.validate(graph, strict_schema_version=True)
    graph_dict = graph.to_dict() if hasattr(graph, "to_dict") else dict(graph)
    await execution.emit_stream("route.dynamic_task.graph", graph_dict, route="dynamic_task", source="dynamic_task")

    compiled = task.compile(graph)
    dag_execution = compiled.create_execution(auto_close=False)
    stream = dag_execution.get_async_runtime_stream(timeout=None)

    async def runner():
        await dag_execution.async_start(candidate.get("graph_input", {"target": target}))
        return await dag_execution.async_close(timeout=candidate.get("timeout", 30))

    run_task = asyncio.create_task(runner())
    async for item in stream:
        await execution.bridge_task_dag_stream_item(item, route="dynamic_task")
    close_snapshot = await run_task
    task_result = close_snapshot.get("state", {}).get("task_dag_execution", close_snapshot)
    execution.close_snapshot = close_snapshot
    execution.logs = {"task_dag": close_snapshot}
    return task_result
