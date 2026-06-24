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

import json
from typing import Any, TYPE_CHECKING, cast

from agently.utils import DataFormatter

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent


class HybridRoutePlanner:
    """Candidate-driven route planner for one Agent execution."""

    def __init__(self, agent: "BaseAgent", *, prompt_snapshot: dict[str, Any] | None = None, execution: Any = None):
        self.agent = agent
        self.prompt_snapshot = dict(prompt_snapshot or {})
        self.execution = execution

    def task_target(self) -> str:
        value = self.prompt_snapshot.get("input")
        if isinstance(value, str) and value.strip():
            return value
        if value is not None:
            return json.dumps(DataFormatter.sanitize(value), ensure_ascii=False)
        return "Agent task"

    def dynamic_task_candidates(self) -> list[dict[str, Any]]:
        agent_candidates = list(getattr(self.agent, "_dynamic_task_candidates", []) or [])
        local_candidates = list(getattr(self.execution, "local_dynamic_task_candidates", []) or [])
        return [*agent_candidates, *local_candidates]

    def action_candidates(self) -> list[dict[str, Any]]:
        action = getattr(self.agent, "action", None)
        if action is None:
            return []
        try:
            candidates = list(action.get_action_list(tags=[f"agent-{ self.agent.name }"]))
        except Exception:
            return []
        local_ids = set(getattr(self.execution, "local_action_ids", []) or [])
        if local_ids:
            candidates = [
                candidate
                for candidate in candidates
                if str(candidate.get("action_id") or candidate.get("name") or "") in local_ids
            ]
        recall_records = getattr(self.execution.execution_context, "scoped_action_artifact_recall_records", None)
        if callable(recall_records):
            candidates = action._with_action_artifact_recall_action(candidates, recall_records())
        return candidates

    def skill_candidate_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"model_decision": False, "required": False}
        for mode in ("model_decision", "required"):
            collect_skills = getattr(self.agent, "_collect_skill_selectors", None)
            collect_packs = getattr(self.agent, "_collect_skills_pack_selectors", None)
            try:
                raw_skills = collect_skills(skills=None, mode=mode) if callable(collect_skills) else []
                raw_packs = collect_packs(skills_packs=None, mode=mode) if callable(collect_packs) else []
            except Exception:
                raw_skills, raw_packs = [], []
            skills = list(raw_skills) if isinstance(raw_skills, (list, tuple, set)) else []
            packs = list(raw_packs) if isinstance(raw_packs, (list, tuple, set)) else []
            for item in getattr(self.execution, "local_skill_selectors", []) or []:
                if item.get("mode") == mode:
                    skills.append(item.get("selector"))
            for item in getattr(self.execution, "local_skills_pack_selectors", []) or []:
                if item.get("mode") == mode:
                    packs.append(item.get("selector"))
            summary[mode] = bool(skills or packs)
            summary[f"{ mode }_skills"] = skills
            summary[f"{ mode }_skills_packs"] = packs
        return summary

    def route_policy(self) -> dict[str, Any]:
        execution = self.execution
        if execution is None:
            return {}
        for source_name in ("options", "effective_options"):
            source = getattr(execution, source_name, None)
            if isinstance(source, dict):
                policy = source.get("route_policy")
                if isinstance(policy, dict):
                    return policy
        return {}

    def allowed_routes(self) -> set[str] | None:
        policy = self.route_policy()
        raw_routes = policy.get("allowed_routes")
        if raw_routes is None:
            raw_routes = policy.get("allowed")
        if raw_routes is None:
            raw_routes = policy.get("routes")
        forced_route = policy.get("force_route") or policy.get("required_route")
        if forced_route is not None:
            raw_routes = [forced_route]
        if raw_routes is None:
            return None
        if isinstance(raw_routes, str):
            candidates = [raw_routes]
        elif isinstance(raw_routes, (list, tuple, set)):
            candidates = [str(item) for item in raw_routes]
        else:
            return None
        routes = {route.strip() for route in candidates if route.strip()}
        return routes or None

    def route_allowed(self, route: str) -> bool:
        allowed_routes = self.allowed_routes()
        return allowed_routes is None or route in allowed_routes

    def on_violation(self) -> str:
        mode = str(self.route_policy().get("on_violation") or "fallback").strip().lower()
        return mode if mode in {"fallback", "block"} else "fallback"

    async def select_route(self) -> tuple[str, dict[str, Any]]:
        dynamic_candidates = self.dynamic_task_candidates()
        submitted_dynamic_candidates = [
            candidate for candidate in dynamic_candidates if str(candidate.get("mode") or "auto") == "submitted"
        ]
        if submitted_dynamic_candidates and self.route_allowed("dynamic_task"):
            return "dynamic_task", {"candidate": submitted_dynamic_candidates[-1], "selected_by": "deterministic"}

        skills = self.skill_candidate_summary()
        if skills["required"] and self.route_allowed("skills"):
            return "skills", {
                "mode": "required",
                "selected_by": "deterministic",
                "skills": skills.get("required_skills", []),
                "skills_packs": skills.get("required_skills_packs", []),
            }

        optional_candidates = []
        if dynamic_candidates and self.route_allowed("dynamic_task"):
            optional_candidates.append({"route": "dynamic_task", "candidate": dynamic_candidates[-1]})
        if skills["model_decision"] and self.route_allowed("skills"):
            optional_candidates.append({
                "route": "skills",
                "mode": "model_decision",
                "skills": skills.get("model_decision_skills", []),
                "skills_packs": skills.get("model_decision_skills_packs", []),
            })
        action_candidates = self.action_candidates()
        if action_candidates and self.route_allowed("model_request"):
            optional_candidates.append({"route": "model_request", "with_actions": True})

        if len(optional_candidates) > 1:
            return await self._select_ambiguous_route(optional_candidates)
        if optional_candidates:
            selected = optional_candidates[0]
            route = str(selected.get("route"))
            meta = {key: value for key, value in selected.items() if key != "route"}
            meta["selected_by"] = "single_candidate"
            return route, meta

        if self.route_allowed("model_request"):
            return "model_request", {}

        # No candidate satisfied the route policy. on_violation="block" surfaces
        # the violation as an explicit blocked route (the AgentTask loop turns it
        # into a replan signal); the default "fallback" runs model_request with a
        # recorded warning to preserve backward-compatible behavior.
        violation_meta = {
            "route_policy": DataFormatter.sanitize(self.route_policy()),
            "allowed_routes": sorted(self.allowed_routes() or []),
            "route_policy_warning": "No allowed route candidate was available for the route policy.",
        }
        if self.on_violation() == "block":
            return "route_policy_blocked", {"selected_by": "route_policy_violation", **violation_meta}
        return "model_request", {"selected_by": "route_policy_fallback", **violation_meta}

    async def _select_ambiguous_route(self, candidates: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        request_factory = getattr(self.agent, "create_temp_request", None)
        if callable(request_factory):
            try:
                result = await (
                    cast(Any, request_factory())
                    .input(
                        {
                            "task": self.task_target(),
                            "route_candidates": DataFormatter.sanitize(candidates),
                            "route_policy": (
                                "Choose exactly one route. Prefer dynamic_task for multi-step explicit DAG work, "
                                "skills for installed domain Skill behavior, and model_request when direct model "
                                "reasoning with available actions is sufficient."
                            ),
                        }
                    )
                    .output(
                        {
                            "selected_route": (str, "one of: dynamic_task, skills, model_request", True),
                            "reason": (str, "concise business reason for the route choice"),
                        },
                        format="json",
                    )
                    .async_start(max_retries=2, raise_ensure_failure=False)
                )
                selected_route = str(_safe_get(result, "selected_route") or "").strip()
                for candidate in candidates:
                    if selected_route == candidate.get("route"):
                        meta = {key: value for key, value in candidate.items() if key != "route"}
                        meta["selected_by"] = "model"
                        meta["route_choice_reason"] = _safe_get(result, "reason")
                        return selected_route, meta
            except Exception:
                pass
        # Deterministic fallback when the model route choice fails: prefer the
        # lowest-cost route rather than candidate construction order (which always
        # put dynamic_task, the most expensive route, first).
        fallback_priority = {"model_request": 0, "skills": 1, "dynamic_task": 2}
        candidate = min(
            candidates,
            key=lambda item: fallback_priority.get(str(item.get("route")), 99),
        )
        route = str(candidate.get("route"))
        meta = {key: value for key, value in candidate.items() if key != "route"}
        meta["selected_by"] = "fallback"
        meta["route_choice_reason"] = "Model route choice failed; selected the lowest-cost allowed route."
        return route, meta

    def build_route_plan(self, *, execution_id: str, route: str, route_meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "execution_id": execution_id,
            "selected_route": route,
            "route_meta": DataFormatter.sanitize(route_meta),
            "candidates": {
                "actions": self.action_candidates(),
                "skills": self.skill_candidate_summary(),
                "dynamic_task": self.dynamic_task_candidates(),
            },
        }


def _safe_get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None
