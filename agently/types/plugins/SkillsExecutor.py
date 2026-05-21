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
from typing import Any, Protocol

from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode, SkillScope, SkillsPackRecord


class SkillsExecutorPlugin(Protocol):
    name: str
    DEFAULT_SETTINGS: dict[str, Any]

    def install_skills(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> SkillContract: ...

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
    ) -> SkillsPackRecord: ...

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
    ) -> SkillExecutionPlan: ...

    async def async_execute_plan(
        self,
        *,
        agent: Any,
        task: str,
        plan: SkillExecutionPlan,
    ) -> Any: ...
