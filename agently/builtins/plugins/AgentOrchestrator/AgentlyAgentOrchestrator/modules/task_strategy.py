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

from collections.abc import Sequence
from typing import Any, TYPE_CHECKING, cast

from agently.utils import DataFormatter

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

    # Planner capability visibility (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
    # adapt the route planner's action / skill / skill-pack / dynamic-task
    # candidates into one sanitized, inert capability snapshot and pass it into
    # AgentTask options. AgentTask reads only this snapshot; it never imports
    # HybridRoutePlanner or holds the execution draft. Computed once here, at task
    # construction, from the top-level routing execution. A caller-supplied
    # snapshot, if any, wins.
    if "planner_capabilities" not in agent_task_options:
        capability_snapshot = _planner_capability_snapshot(execution)
        if capability_snapshot:
            agent_task_options["planner_capabilities"] = capability_snapshot
    prompt_snapshot = getattr(execution, "prompt_snapshot", {})
    if isinstance(prompt_snapshot, dict) and prompt_snapshot:
        agent_task_options.setdefault(
            "execution_prompt_snapshot",
            DataFormatter.sanitize(dict(prompt_snapshot)),
        )

    if not isinstance(task, AgentTask):
        task = AgentTask(
            execution.agent,
            goal=goal,
            success_criteria=success_criteria,
            workspace=task_options.get("workspace"),
            max_iterations=int(max_iterations or 3),
            verify=cast(Any, task_options.get("verify", "before_done")),
            context_profile=str(task_options.get("context_profile", "auto")),
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


def _planner_capability_snapshot(execution: "AgentExecution") -> list[dict[str, Any]]:
    """Sanitized planner-facing capability snapshot (inert data only).

    Adapts the route planner's action / skill / skill-pack / dynamic-task
    candidates into one list of `PlannerCapabilityCandidate` dicts. Each entry
    carries inert data only (id + kind + route + guidance_access + description),
    so AgentTask never reaches back into the route planner. Any per-source
    failure degrades to skipping that source rather than raising, so planner
    visibility never depends on a single producer's availability.
    """
    capabilities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(candidate_id: str, kind: str, route: str, guidance_access: str, *, mode: str = "", description: str = "") -> None:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id or (candidate_id, kind) in seen:
            return
        seen.add((candidate_id, kind))
        entry: dict[str, Any] = {
            "id": candidate_id,
            "kind": kind,
            "route": route,
            "guidance_access": guidance_access,
            "description": str(description or "").strip(),
        }
        if mode:
            entry["mode"] = mode
        capabilities.append(entry)

    # Actions -> model_request route, no model-facing guidance beyond their spec.
    try:
        for action in execution.action_candidates() or []:
            if not isinstance(action, dict):
                continue
            add(
                action.get("action_id") or action.get("name") or "",
                "action",
                "model_request",
                "none",
                description=str(action.get("desc") or action.get("description") or ""),
            )
    except Exception:
        pass

    # Skills / skill packs -> skills route; their guidance (SKILL.md) reaches the
    # model only when that route runs.
    try:
        summary = execution.skill_candidate_summary()
    except Exception:
        summary = None
    if isinstance(summary, dict):
        descriptions = _installed_skill_descriptions(execution)
        for mode in ("model_decision", "required"):
            for selector in summary.get(f"{mode}_skills", []) or []:
                skill_id = _capability_id_from_selector(selector)
                add(skill_id, "skill", "skills", "route_context", mode=mode, description=descriptions.get(skill_id, ""))
            for selector in summary.get(f"{mode}_skills_packs", []) or []:
                pack_id = _capability_id_from_selector(selector)
                add(pack_id, "skill_pack", "skills", "route_context", mode=mode, description=descriptions.get(pack_id, ""))

    # DynamicTask / DAG candidates -> dynamic_task route.
    try:
        for candidate in execution.dynamic_task_candidates() or []:
            if not isinstance(candidate, dict):
                continue
            add(
                candidate.get("name") or candidate.get("id") or "",
                "dynamic_task",
                "dynamic_task",
                "none",
                mode=str(candidate.get("mode") or ""),
                description=str(candidate.get("description") or ""),
            )
    except Exception:
        pass

    return capabilities


def _capability_id_from_selector(selector: Any) -> str:
    if isinstance(selector, dict):
        for key in ("id", "skill_id", "skills_pack_id", "name", "source"):
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
    if not isinstance(records, Sequence):
        return {}
    descriptions: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        skill_id = str(record.get("skill_id") or "").strip()
        if skill_id:
            descriptions[skill_id] = str(record.get("description") or "").strip()
    return descriptions
