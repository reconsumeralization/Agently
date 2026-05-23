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
from typing import Any, Literal

from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode, SkillsPackRecord
from agently.types.plugins import SkillsExecutionContext, SkillsExecutor, SkillsPlanningContext
from agently.utils import Settings

from .modules.executor import SkillExecutor
from .modules.planner import SkillPlanner
from .modules.registry import SkillRegistry


class AgentlySkillsExecutor(SkillsExecutor):
    name = "AgentlySkillsExecutor"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self, *, plugin_manager: Any = None, settings: Settings):
        self.plugin_manager = plugin_manager
        self.settings = settings
        self.registry = SkillRegistry(settings)

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def configure(
        self,
        *,
        registry_root: str | Path | None = None,
        allowed_trust_levels: list[str] | None = None,
    ) -> "AgentlySkillsExecutor":
        if registry_root is not None:
            self.settings.set("skills.registry.root", str(registry_root))
        if allowed_trust_levels is not None:
            self.settings._set_item_by_dot_path("skills.allowed_trust_levels", list(allowed_trust_levels), cover=True)
        return self

    # ── Registry delegation ────────────────────────────────────────────────

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

    # ── Plan / Execute ─────────────────────────────────────────────────────

    async def async_resolve_plan(
        self,
        *,
        context: SkillsPlanningContext,
        task: str | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] = "auto",
    ) -> SkillExecutionPlan:
        return await SkillPlanner(self.registry).resolve(
            context=context,
            task=task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )

    async def async_execute_plan(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] | None = None,
    ):
        return await SkillExecutor(self.registry).execute(
            context=context,
            task=task,
            plan=plan,
            output_format=output_format,
        )
