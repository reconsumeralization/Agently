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

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Callable, cast

from agently.core import BaseAgent
from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode
from agently.types.plugins import SkillsExecutor
from agently.utils import FunctionShifter
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list
from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.planner import (
    _matches_selector,
    _matches_skills_pack_selector,
)

from ._SkillsContext import create_agent_skills_runtime_context

if TYPE_CHECKING:
    from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.executor import SkillExecution
    from agently.core import Prompt
    from agently.utils import Settings


class SkillsExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from agently.base import skills_executor

        self.skills_executor = cast(SkillsExecutor, skills_executor)

        self.__session_skill_selectors: list[Any] = []
        self.__session_skills_pack_selectors: list[Any] = []
        self.__skill_execution_logs: list[Any] = []

        request_prefixes = self.extension_handlers.get("request_prefixes", [])
        if not isinstance(request_prefixes, list):
            request_prefixes = []
        self.extension_handlers.set("request_prefixes", [self.__request_prefix, *request_prefixes])
        self.extension_handlers.append("finally", self.__finally)

    # ── User-facing API ─────────────────────────────────────────────────────

    def use_skills(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
    ):
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        for item in _ensure_list(skills):
            self.__session_skill_selectors.append({"selector": _copy_public(item), "mode": mode})
        return self

    def use_skills_packs(
        self,
        skills_packs: Any,
        *,
        mode: SkillMode = "model_decision",
    ):
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be one of: 'model_decision', 'required'.")
        for item in _ensure_list(skills_packs):
            self.__session_skills_pack_selectors.append({"selector": _copy_public(item), "mode": mode})
        return self

    async def async_resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        semantic_outputs: Any = None,
    ) -> SkillExecutionPlan:
        selectors = self._collect_skill_selectors(skills=skills, mode=mode)
        skills_pack_selectors = self._collect_skills_pack_selectors(skills_packs=skills_packs, mode=mode)
        context = create_agent_skills_runtime_context(self)
        return await self.skills_executor.async_resolve_plan(
            context=context,
            task=task,
            skills=selectors,
            skills_packs=skills_pack_selectors,
            mode=mode,
            semantic_outputs=semantic_outputs,
        )

    def resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        semantic_outputs: Any = None,
    ) -> SkillExecutionPlan:
        return FunctionShifter.syncify(self.async_resolve_skills_plan)(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            semantic_outputs=semantic_outputs,
        )

    async def async_run_skills_task(
        self,
        task: str,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        semantic_outputs: Any = None,
        stream_handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> "SkillExecution":
        plan = await self.async_resolve_skills_plan(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            semantic_outputs=semantic_outputs,
        )
        execution = await self.async_execute_skills_plan(
            task,
            plan=plan,
            stream_handler=stream_handler,
        )
        self.__skill_execution_logs.append(execution.to_dict())
        return execution

    def run_skills_task(
        self,
        task: str,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        semantic_outputs: Any = None,
        stream_handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> "SkillExecution":
        return FunctionShifter.syncify(self.async_run_skills_task)(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            semantic_outputs=semantic_outputs,
            stream_handler=stream_handler,
        )

    async def async_execute_skills_plan(
        self,
        task: str,
        *,
        plan: SkillExecutionPlan,
        stream_handler: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> "SkillExecution":
        context = create_agent_skills_runtime_context(self, runtime_stream_handler=stream_handler)
        return await self.skills_executor.async_execute_plan(
            context=context,
            task=task,
            plan=plan,
        )

    def get_skills_execution_logs(self) -> list[dict[str, Any]]:
        return _copy_public(self.__skill_execution_logs)

    # ── Selector collection ─────────────────────────────────────────────────

    def _collect_skill_selectors(self, *, skills: Any, mode: SkillMode) -> list[Any]:
        selectors = []
        if skills is not None:
            selectors.extend(_ensure_list(skills))
        for item in self.__session_skill_selectors:
            if _ensure_dict(item).get("mode", "model_decision") == mode:
                selectors.append(_ensure_dict(item).get("selector"))
        return selectors

    def _collect_skills_pack_selectors(self, *, skills_packs: Any, mode: SkillMode) -> list[Any]:
        selectors = []
        if skills_packs is not None:
            selectors.extend(_ensure_list(skills_packs))
        for item in self.__session_skills_pack_selectors:
            if _ensure_dict(item).get("mode", "model_decision") == mode:
                selectors.append(_ensure_dict(item).get("selector"))
        return selectors

    # ── Prompt injection ────────────────────────────────────────────────────

    async def _apply_skill_cards_to_prompt(self, prompt: "Prompt"):
        selectors = self._collect_skill_selectors(skills=None, mode="model_decision")
        skills_pack_selectors = self._collect_skills_pack_selectors(skills_packs=None, mode="model_decision")
        if not selectors and not skills_pack_selectors:
            return
        cards = []
        guidance = []
        settings = getattr(self, "settings")
        include_guidance = bool(settings.get("skills.prompt.include_primary_guidance", True))
        max_guidance_chars = int(settings.get("skills.prompt.max_guidance_chars_per_skill", 6000) or 6000)
        for record in self.skills_executor.list_skills():
            contract = self.skills_executor.inspect_skills(str(record["skill_id"]))
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
        return

    def _collect_prompt_guidance(self, contract: SkillContract, *, max_chars: int) -> list[dict[str, Any]]:
        guidance = _ensure_dict(contract.get("guidance"))
        content = str(guidance.get("content") or "")
        if not content.strip():
            return []
        trimmed = content[:max_chars]
        return [{
            "skill_id": str(contract.get("skill_id", "")),
            "path": str(guidance.get("path") or "SKILL.md"),
            "title": str(contract.get("card", {}).get("display_name", "")),
            "content": trimmed,
            "truncated": len(content) > len(trimmed),
        }]

    # ── Extension handlers ──────────────────────────────────────────────────

    async def __request_prefix(self, prompt: "Prompt", _settings: "Settings"):
        await self._apply_skill_cards_to_prompt(prompt)

    async def __finally(self, *_):
        self._clear_request_skill_selectors()
