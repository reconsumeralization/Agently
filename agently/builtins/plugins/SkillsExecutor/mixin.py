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

from typing import Any, Callable

from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode, SkillScope
from agently.utils import FunctionShifter

from .AgentlySkillsExecutor import (
    SkillExecution,
    SkillRegistry,
    _copy_public,
    _ensure_dict,
    _ensure_list,
    _matches_skills_pack_selector,
    _matches_selector,
)


class AgentSkillsMixin:
    def _init_skills(self, skills_executor: Any):
        self.skills_executor = skills_executor
        self.skills_registry = getattr(skills_executor, "registry", skills_executor)
        self.__session_skill_selectors: list[Any] = []
        self.__request_skill_selectors: list[Any] = []
        self.__session_skills_pack_selectors: list[Any] = []
        self.__request_skills_pack_selectors: list[Any] = []
        self.__skill_decision_handler: Callable[..., Any] | None = None
        self.__skill_execution_logs: list[Any] = []

    def use_skills(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
    ):
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        target = self.__request_skill_selectors if scope == "request" else self.__session_skill_selectors
        for item in _ensure_list(skills):
            target.append({"selector": _copy_public(item), "mode": mode, "scope": scope})
        return self

    def use_skills_packs(
        self,
        skills_packs: Any,
        *,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
    ):
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        target = self.__request_skills_pack_selectors if scope == "request" else self.__session_skills_pack_selectors
        for item in _ensure_list(skills_packs):
            target.append({"selector": _copy_public(item), "mode": mode, "scope": scope})
        return self

    async def async_resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
        semantic_outputs: Any = None,
        planner_mode: str = "auto",
        planner_max_revisions: int = 2,
    ) -> SkillExecutionPlan:
        selectors = self._collect_skill_selectors(skills=skills, mode=mode)
        skills_pack_selectors = self._collect_skills_pack_selectors(skills_packs=skills_packs, mode=mode)
        if hasattr(self.skills_executor, "async_resolve_plan"):
            return await self.skills_executor.async_resolve_plan(
                agent=self,
                task=task,
                skills=selectors,
                skills_packs=skills_pack_selectors,
                mode=mode,
                scope=scope,
                decision_handler=self.__skill_decision_handler,
                semantic_outputs=semantic_outputs,
                planner_mode=planner_mode,
                planner_max_revisions=planner_max_revisions,
            )
        from .AgentlySkillsExecutor import SkillPlanner

        return await SkillPlanner(self.skills_registry).resolve(
            agent=self,
            task=task,
            skills=selectors,
            skills_packs=skills_pack_selectors,
            mode=mode,
            scope=scope,
            decision_handler=self.__skill_decision_handler,
            semantic_outputs=semantic_outputs,
            planner_mode=planner_mode,
            planner_max_revisions=planner_max_revisions,
        )

    def resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
        semantic_outputs: Any = None,
        planner_mode: str = "auto",
        planner_max_revisions: int = 2,
    ) -> SkillExecutionPlan:
        return FunctionShifter.syncify(self.async_resolve_skills_plan)(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            scope=scope,
            semantic_outputs=semantic_outputs,
            planner_mode=planner_mode,
            planner_max_revisions=planner_max_revisions,
        )

    async def async_run_skills_task(
        self,
        task: str,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "execution",
        semantic_outputs: Any = None,
        planner_mode: str = "auto",
        planner_max_revisions: int = 2,
    ) -> SkillExecution:
        plan = await self.async_resolve_skills_plan(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            scope=scope,
            semantic_outputs=semantic_outputs,
            planner_mode=planner_mode,
            planner_max_revisions=planner_max_revisions,
        )
        execution = await self.async_execute_skills_plan(task, plan=plan)
        self.__skill_execution_logs.append(execution.to_dict())
        return execution

    def run_skills_task(
        self,
        task: str,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "execution",
        semantic_outputs: Any = None,
        planner_mode: str = "auto",
        planner_max_revisions: int = 2,
    ) -> SkillExecution:
        return FunctionShifter.syncify(self.async_run_skills_task)(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            scope=scope,
            semantic_outputs=semantic_outputs,
            planner_mode=planner_mode,
            planner_max_revisions=planner_max_revisions,
        )

    def set_skills_decision_handler(self, handler: Callable[..., Any] | None):
        self.__skill_decision_handler = handler
        return self

    async def async_execute_skills_plan(
        self,
        task: str,
        *,
        plan: SkillExecutionPlan,
    ) -> SkillExecution:
        if hasattr(self.skills_executor, "async_execute_plan"):
            return await self.skills_executor.async_execute_plan(agent=self, task=task, plan=plan)
        from .AgentlySkillsExecutor import SkillExecutor

        return await SkillExecutor(self.skills_registry).execute(agent=self, task=task, plan=plan)

    def get_skills_execution_logs(self) -> list[dict[str, Any]]:
        return _copy_public(self.__skill_execution_logs)

    def _collect_skill_selectors(self, *, skills: Any, mode: SkillMode) -> list[Any]:
        selectors = []
        if skills is not None:
            selectors.extend(_ensure_list(skills))
        for item in [*self.__session_skill_selectors, *self.__request_skill_selectors]:
            if _ensure_dict(item).get("mode", "model_decision") == mode:
                selectors.append(_ensure_dict(item).get("selector"))
        return selectors

    def _collect_skills_pack_selectors(self, *, skills_packs: Any, mode: SkillMode) -> list[Any]:
        selectors = []
        if skills_packs is not None:
            selectors.extend(_ensure_list(skills_packs))
        for item in [*self.__session_skills_pack_selectors, *self.__request_skills_pack_selectors]:
            if _ensure_dict(item).get("mode", "model_decision") == mode:
                selectors.append(_ensure_dict(item).get("selector"))
        return selectors

    async def _apply_skill_cards_to_prompt(self, prompt: Any):
        selectors = self._collect_skill_selectors(skills=None, mode="model_decision")
        skills_pack_selectors = self._collect_skills_pack_selectors(skills_packs=None, mode="model_decision")
        if not selectors and not skills_pack_selectors:
            return
        cards = []
        guidance = []
        settings = getattr(self, "settings")
        include_guidance = bool(settings.get("skills.prompt.include_primary_guidance", True))
        max_guidance_chars = int(settings.get("skills.prompt.max_guidance_chars_per_skill", 6000) or 6000)
        for record in self.skills_registry.list_skills():
            contract = self.skills_registry.inspect_skills(str(record["skill_id"]))
            if any(_matches_selector(contract, selector) for selector in selectors) or any(
                _matches_skills_pack_selector(contract, selector) for selector in skills_pack_selectors
            ):
                cards.append(contract.get("card", {}))
                if include_guidance:
                    guidance.extend(self._collect_prompt_guidance(contract, max_chars=max_guidance_chars))
        if not cards:
            return
        prompt_mode = str(settings.get("skills.prompt.mode", settings.get("agent.auto_orchestration.skills_prompt_mode", "route_owned")))
        if prompt_mode == "route_owned":
            prompt.append(
                "info",
                {
                    "skill_candidates": cards,
                    "skill_instruction": (
                        "These skills are route candidates for Agent auto-orchestration. "
                        "Do not claim that a Skill was executed unless the selected route provides skill execution logs."
                    ),
                },
            )
            return
        payload = {
            "skill_cards": cards,
            "skill_instruction": (
                "These skills are optional behavior-loop candidates. "
                "Use them only when they fit the task; otherwise answer normally."
            ),
        }
        if guidance:
            payload["skill_guidance"] = guidance
        prompt.append("info", payload)

    def _clear_request_skill_selectors(self):
        self.__request_skill_selectors = []
        self.__request_skills_pack_selectors = []

    def _collect_prompt_guidance(self, contract: SkillContract, *, max_chars: int) -> list[dict[str, Any]]:
        assets = _ensure_dict(contract.get("assets"))
        guidance_assets = []
        for asset in _ensure_list(assets.get("guidance_assets")):
            asset_data = _ensure_dict(asset)
            content = str(asset_data.get("content") or "")
            if not content.strip():
                continue
            trimmed = content[:max_chars]
            guidance_assets.append(
                {
                    "skill_id": str(contract.get("skill_id", "")),
                    "asset_id": str(asset_data.get("asset_id") or "guidance"),
                    "title": str(asset_data.get("title") or contract.get("card", {}).get("display_name", "")),
                    "content": trimmed,
                    "truncated": len(content) > len(trimmed),
                }
            )
            break
        return guidance_assets
