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
from typing import Any, TYPE_CHECKING

from agently.utils import DataFormatter

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent


class HybridRoutePlanner:
    """Candidate-driven route planner for one Agent execution."""

    def __init__(self, agent: "BaseAgent"):
        self.agent = agent

    def task_target(self) -> str:
        try:
            value = self.agent.request.prompt.get("input", default=None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value
        if value is not None:
            return json.dumps(DataFormatter.sanitize(value), ensure_ascii=False)
        return "Agent task"

    def dynamic_task_candidates(self) -> list[dict[str, Any]]:
        return list(getattr(self.agent, "_dynamic_task_candidates", []) or [])

    def action_candidates(self) -> list[dict[str, Any]]:
        action = getattr(self.agent, "action", None)
        if action is None:
            return []
        try:
            return list(action.get_action_list(tags=[f"agent-{ self.agent.name }"]))
        except Exception:
            return []

    def skill_candidate_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"model_decision": False, "required": False}
        for mode in ("model_decision", "required"):
            collect_skills = getattr(self.agent, "_collect_skill_selectors", None)
            collect_packs = getattr(self.agent, "_collect_skills_pack_selectors", None)
            try:
                skills = collect_skills(skills=None, mode=mode) if callable(collect_skills) else []
                packs = collect_packs(skills_packs=None, mode=mode) if callable(collect_packs) else []
            except Exception:
                skills, packs = [], []
            summary[mode] = bool(skills or packs)
            summary[f"{ mode }_skills"] = skills
            summary[f"{ mode }_skills_packs"] = packs
        return summary

    async def select_route(self) -> tuple[str, dict[str, Any]]:
        dynamic_candidates = self.dynamic_task_candidates()
        if dynamic_candidates:
            return "dynamic_task", {"candidate": dynamic_candidates[-1]}

        skills = self.skill_candidate_summary()
        if skills["required"]:
            return "skills", {"mode": "required"}
        if skills["model_decision"]:
            return "skills", {"mode": "model_decision"}

        if self.action_candidates():
            return "model_request", {"with_actions": True}
        return "model_request", {}

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
