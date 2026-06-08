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

from typing import Any, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .execution import AgentExecution


async def run_agent_task_route(execution: "AgentExecution", route_meta: dict[str, Any]) -> Any:
    from agently.core.application import AgentTask

    task_options = execution.task_strategy_options()
    generated_before = list(getattr(execution, "generated_success_criteria", []) or [])
    goal = execution.task_goal()
    success_criteria = execution.task_success_criteria()
    generated_after = list(getattr(execution, "generated_success_criteria", []) or [])
    if generated_after and generated_after != generated_before:
        await execution.emit_stream(
            "success_criteria.generated",
            {"goal": goal, "success_criteria": generated_after},
            route="agent_task",
            source="agent_execution",
        )

    task = AgentTask(
        execution.agent,
        goal=goal,
        success_criteria=success_criteria,
        workspace=task_options.get("workspace"),
        max_iterations=int(task_options.get("max_iterations", 3) or 3),
        verify=cast(Any, task_options.get("verify", "before_done")),
        recall_profile=str(task_options.get("recall_profile", "software_dev")),
        context_budget=cast(Any, task_options.get("context_budget")),
        limits=cast(Any, task_options.get("limits", execution.limits)),
        options=cast(Any, task_options.get("options") or {}),
        task_id=cast(Any, task_options.get("task_id") or execution.lineage.get("task_id")),
    )
    for name, value in getattr(execution, "__dict__", {}).items():
        if name.startswith("_") and name in {"_execute_step", "_request_plan", "_request_verification"}:
            setattr(task, name, value)
    execution.task_record = task
    execution.task_refs = {
        "task_id": task.id,
        "strategy": route_meta.get("strategy") or execution.strategy_name or "task",
    }
    await execution.emit_stream(
        "agent_task.created",
        {"task_id": task.id, "goal": goal, "success_criteria": success_criteria},
        route="agent_task",
        source="agent_execution",
        task_id=task.id,
    )

    async for item in task.get_async_generator():
        await execution.stream.bridge_agent_task_item(item, route="agent_task")

    task_meta = await task.async_meta()
    execution.task_refs.update(
        {
            "status": task.status,
            "workspace_refs": task_meta.get("workspace_refs", {}),
        }
    )
    execution.logs["route_logs"] = {"agent_task": task_meta}
    execution.close_snapshot = {
        "status": task.status,
        "route": "agent_task",
        "task": task_meta,
    }
    if isinstance(task_meta.get("workspace_refs"), dict):
        execution.workspace_refs["agent_task"] = task_meta["workspace_refs"]
    execution.status = "success" if task.status == "completed" else str(task.status)
    return task.result
