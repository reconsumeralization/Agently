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

    if execution.limits.get("allow_create_task") is False:
        reason = "AgentExecution limits disallow task creation (allow_create_task=False)."
        execution.status = "blocked"
        execution.close_snapshot = {"status": "blocked", "route": "agent_task", "reason": reason}
        execution.diagnostics.setdefault("limit_events", []).append(
            {"limit_name": "allow_create_task", "limit_value": False, "reason": reason}
        )
        await execution.emit_stream(
            "route.agent_task.blocked",
            {"reason": reason, "limit_name": "allow_create_task"},
            route="agent_task",
            source="agent_execution",
            meta={"status": "blocked"},
        )
        return {
            "status": "blocked",
            "accepted": False,
            "artifact_status": "blocked",
            "reason": reason,
        }

    task_options = execution.task_strategy_options()
    resume_task_id = task_options.get("resume_task_id")
    if resume_task_id is None and task_options.get("resume"):
        resume_task_id = task_options.get("task_id") or execution.lineage.get("task_id")
    task = getattr(execution, "task_record", None)
    if not isinstance(task, AgentTask) and resume_task_id is not None:
        task = await AgentTask.async_resume(
            execution.agent,
            str(resume_task_id),
            workspace=cast(Any, task_options.get("workspace")),
        )
        execution.task_record = task

    if isinstance(task, AgentTask):
        goal = task.goal
        success_criteria = list(task.success_criteria)
    else:
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

    effort_strategy = execution.effective_options.get("effort_strategy")
    effort_strategy = dict(effort_strategy) if isinstance(effort_strategy, dict) else {}
    max_iterations = task_options.get("max_iterations")
    if max_iterations is None and effort_strategy:
        max_iterations = effort_strategy.get("max_iterations")
        execution.record_consumed_option(
            "effort.max_iterations",
            max_iterations,
            owner="AgentTaskLoop",
        )
    agent_task_options = dict(task_options.get("options") or {})
    if effort_strategy:
        agent_task_options.setdefault("agent_task", {})
        if isinstance(agent_task_options["agent_task"], dict):
            agent_task_options["agent_task"].setdefault("effort", effort_strategy)
    required_actions = execution.required_action_ids()
    required_skills = execution.required_skill_ids()
    if required_actions or required_skills:
        constraints = dict(agent_task_options.get("capability_constraints") or {})
        if required_actions:
            constraints.setdefault("actions", {})
            if isinstance(constraints["actions"], dict):
                constraints["actions"]["required"] = required_actions
        if required_skills:
            constraints.setdefault("skills", {})
            if isinstance(constraints["skills"], dict):
                constraints["skills"]["required"] = required_skills
        agent_task_options["capability_constraints"] = constraints

    # Planner-visibility snapshot (BUG_FIX_AGENT_TASK_SKILL_GUIDANCE_BYPASS_SPEC
    # 4.1): adapt the route planner's skill candidate summary into a sanitized,
    # inert snapshot and pass it into AgentTask options. AgentTask reads only
    # this snapshot; it never imports HybridRoutePlanner or holds the execution
    # draft. Computed once here, at task construction, from the top-level routing
    # execution. The caller's explicitly supplied snapshot, if any, wins.
    if "available_skills" not in agent_task_options:
        candidate_snapshot = _planner_skill_candidate_snapshot(execution)
        if candidate_snapshot:
            agent_task_options["available_skills"] = candidate_snapshot

    if not isinstance(task, AgentTask):
        task = AgentTask(
            execution.agent,
            goal=goal,
            success_criteria=success_criteria,
            workspace=task_options.get("workspace"),
            max_iterations=int(max_iterations or 3),
            verify=cast(Any, task_options.get("verify", "before_done")),
            recall_profile=str(task_options.get("recall_profile", "auto")),
            context_budget=cast(Any, task_options.get("context_budget")),
            limits=cast(Any, task_options.get("limits", execution.limits)),
            options=cast(Any, agent_task_options),
            task_id=cast(Any, task_options.get("task_id") or execution.lineage.get("task_id")),
        )
    # Advanced/test step-stage override channel. Callers may set an explicit
    # `execution._agent_task_step_overrides = {"_request_plan": ..., ...}` before
    # running to drive the plan/execute/verify stages deterministically. This is
    # an intentional, documented seam (not a public API): only the named stage
    # handlers are applied, and nothing is read in normal goal-pursuit runs.
    step_overrides = getattr(execution, "_agent_task_step_overrides", None)
    if isinstance(step_overrides, dict):
        for stage_name in ("_request_plan", "_execute_step", "_request_verification"):
            handler = step_overrides.get(stage_name)
            if callable(handler):
                setattr(task, stage_name, handler)
    execution.task_record = task
    execution.task_refs = {
        "task_id": task.id,
        "strategy": route_meta.get("strategy") or execution.strategy_name or "task",
        "resume": bool(resume_task_id is not None or task_options.get("resume")),
        "resumed_from_iteration": getattr(task, "_resumed_from_iteration", 0),
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


def _planner_skill_candidate_snapshot(execution: "AgentExecution") -> list[dict[str, Any]]:
    """Sanitized planner-facing skill candidate snapshot (inert data only).

    Adapts the route planner's `skill_candidate_summary()` into the typed
    `PlannerSkillCandidate` shape (id + mode + best-effort decision-card
    description). Descriptions are a best-effort enrichment read from the agent's
    installed-skill records; any failure degrades to an empty description rather
    than raising, so planner visibility never depends on registry availability.
    """
    try:
        summary = execution.skill_candidate_summary()
    except Exception:
        return []
    if not isinstance(summary, dict):
        return []

    descriptions = _installed_skill_descriptions(execution)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mode in ("model_decision", "required"):
        for selector in summary.get(f"{mode}_skills", []) or []:
            skill_id = _skill_id_from_selector(selector)
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            candidates.append(
                {
                    "id": skill_id,
                    "mode": mode,
                    "description": descriptions.get(skill_id, ""),
                }
            )
    return candidates


def _skill_id_from_selector(selector: Any) -> str:
    if isinstance(selector, dict):
        for key in ("id", "skill_id", "name", "source"):
            value = selector.get(key)
            if value:
                return str(value).strip()
        return ""
    return str(selector or "").strip()


def _installed_skill_descriptions(execution: "AgentExecution") -> dict[str, str]:
    skills_executor = getattr(getattr(execution, "agent", None), "skills_executor", None)
    list_skills = getattr(skills_executor, "list_skills", None)
    if not callable(list_skills):
        return {}
    try:
        records = list_skills()
    except Exception:
        return {}
    descriptions: dict[str, str] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        skill_id = str(record.get("skill_id") or "").strip()
        if skill_id:
            descriptions[skill_id] = str(record.get("description") or "").strip()
    return descriptions
