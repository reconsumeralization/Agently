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
import warnings

from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode, SkillsPackRecord
from agently.types.plugins import SkillsEffortStrategyHandler, SkillsExecutionContext, SkillsExecutor, SkillsPlanningContext
from agently.utils import Settings

from .modules.effort_strategies import BUILTIN_EFFORT_STRATEGY_NAMES
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
        self._effort_strategy_handlers: dict[str, SkillsEffortStrategyHandler] = {}

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
        ref: str | None = None,
        subpath: str | None = None,
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
            ref=ref,
            subpath=subpath,
            source_type=source_type,
            trust_level=trust_level,
            update=update,
            discover=discover,
            resolver_mode=resolver_mode,
            resolver_agent=resolver_agent,
        )

    def discover_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        fetch: bool = True,
        ref: str | None = None,
        subpath: str | None = None,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> dict[str, Any]:
        return self.registry.discover_skills_pack(
            source,
            name=name,
            skills_pack_id=skills_pack_id,
            fetch=fetch,
            ref=ref,
            subpath=subpath,
            source_type=source_type,
            trust_level=trust_level,
            update=update,
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

    def read_resource(self, skill_id: str, path: str, *, max_bytes: int = 262144) -> str:
        return self.registry.read_resource(skill_id, path, max_bytes=max_bytes)

    # ── Runtime strategy extension ────────────────────────────────────────

    def register_effort_strategy(
        self,
        name: str,
        handler: SkillsEffortStrategyHandler,
        *,
        replace: bool = False,
    ) -> "AgentlySkillsExecutor":
        strategy_name = str(name or "").strip()
        if not strategy_name:
            raise ValueError("Skills effort strategy name cannot be empty.")
        if not callable(handler):
            raise TypeError("Skills effort strategy handler must be callable.")
        if (
            strategy_name in self._effort_strategy_handlers
            or strategy_name in BUILTIN_EFFORT_STRATEGY_NAMES
        ) and not replace:
            raise ValueError(f"Skills effort strategy '{ strategy_name }' is already registered.")
        self._effort_strategy_handlers[strategy_name] = handler
        return self

    def unregister_effort_strategy(self, name: str) -> bool:
        strategy_name = str(name or "").strip()
        return self._effort_strategy_handlers.pop(strategy_name, None) is not None

    def list_effort_strategies(self) -> list[str]:
        return sorted({*BUILTIN_EFFORT_STRATEGY_NAMES, *self._effort_strategy_handlers})

    # ── Plan / Execute ─────────────────────────────────────────────────────

    async def async_resolve_plan(
        self,
        *,
        context: SkillsPlanningContext,
        task: str | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] = "auto",
    ) -> SkillExecutionPlan:
        if output is not None and semantic_outputs is not None:
            raise ValueError("Use either output= or semantic_outputs= for Skills planning, not both.")
        if semantic_outputs is not None:
            warnings.warn(
                "semantic_outputs= is deprecated for Skills planning; use output= instead.",
                DeprecationWarning,
                stacklevel=2,
            )
        return await SkillPlanner(self.registry).resolve(
            context=context,
            task=task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            semantic_outputs=output if output is not None else semantic_outputs,
            output_format=output_format,
        )

    async def async_execute_plan(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "auto"] | None = None,
        effort: str | None = None,
    ):
        return await SkillExecutor(
            self.registry,
            effort_strategy_handlers=self._effort_strategy_handlers,
        ).execute(
            context=context,
            task=task,
            plan=plan,
            output_format=output_format,
            effort=effort,
        )
