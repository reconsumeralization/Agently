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

from pathlib import Path
from typing import Any

from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode, SkillScope, SkillsPackRecord
from agently.utils import Settings

from .AgentlySkillsExecutor import SkillExecutor, SkillPlanner, SkillRegistry


class GlobalSkillsFacade:
    def __init__(self, settings: Settings):
        self.registry = SkillRegistry(settings)

    def install_skills(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> SkillContract:
        return self.registry.install_skills(source, source_type=source_type, trust_level=trust_level, update=update)

    def install_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        fetch: bool = False,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = True,
        discover: str = "auto",
        resolver_mode: str = "deterministic",
        resolver_agent: Any = None,
    ) -> SkillsPackRecord:
        return self.registry.install_skills_pack(
            source,
            name=name,
            skills_pack_id=skills_pack_id,
            fetch=fetch,
            source_type=source_type,
            trust_level=trust_level,
            update=update,
            discover=discover,
            resolver_mode=resolver_mode,
            resolver_agent=resolver_agent,
        )

    def list_skills(self) -> list[dict[str, Any]]:
        return self.registry.list_skills()

    def list_skills_packs(self) -> list[SkillsPackRecord]:
        return self.registry.list_skills_packs()

    def inspect_skills(self, skill_id: str) -> SkillContract:
        return self.registry.inspect_skills(skill_id)

    def inspect_skills_pack(self, skills_pack_id: str) -> SkillsPackRecord:
        return self.registry.inspect_skills_pack(skills_pack_id)

    def remove_skills(self, skill_id: str) -> dict[str, Any]:
        return self.registry.remove_skills(skill_id)

    def remove_skills_pack(self, skills_pack_id: str, *, remove_skills: bool = False) -> dict[str, Any]:
        return self.registry.remove_skills_pack(skills_pack_id, remove_skills=remove_skills)


class AgentlySkillsExecutor(GlobalSkillsFacade):
    name = "AgentlySkillsExecutor"
    DEFAULT_SETTINGS = {}

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def __init__(self, *, plugin_manager: Any = None, settings: Settings):
        super().__init__(settings)
        self.plugin_manager = plugin_manager

    async def async_resolve_plan(
        self,
        *,
        agent: Any,
        task: str | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        scope: SkillScope = "session",
        decision_handler: Any = None,
        semantic_outputs: Any = None,
        planner_mode: str = "auto",
        planner_max_revisions: int = 2,
    ) -> SkillExecutionPlan:
        return await SkillPlanner(self.registry).resolve(
            agent=agent,
            task=task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            scope=scope,
            decision_handler=decision_handler,
            semantic_outputs=semantic_outputs,
            planner_mode=planner_mode,
            planner_max_revisions=planner_max_revisions,
        )

    async def async_execute_plan(
        self,
        *,
        agent: Any,
        task: str,
        plan: SkillExecutionPlan,
    ):
        return await SkillExecutor(self.registry).execute(agent=agent, task=task, plan=plan)
