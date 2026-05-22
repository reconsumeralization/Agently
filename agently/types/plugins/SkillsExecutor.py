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
from typing import Any, Protocol, runtime_checkable

from agently.types.data import ActionResult, SkillContract, SkillExecutionPlan, SkillMode, SkillScope, SkillsPackRecord

# ── Protocol layout ──────────────────────────────────────────────────────────
# This file defines four protocols because the Skills Executor subsystem has two
# sides that must agree on a contract:
#
#   SkillsExecutor (plugin protocol)
#     Implemented by framework/third-party plugins (e.g. AgentlySkillsExecutor).
#     Owns skill-pack lifecycle (install/list/inspect/remove) and plan
#     resolution/execution.
#
#   SkillsPlanningContext / SkillsExecutionContext / SkillsRuntimeContext
#     Implemented by the Agent host. The Agent injects a context object at
#     planning time and execution time so the plugin can reach agent-owned
#     services (settings, action availability, model requests, action dispatch)
#     without coupling to a concrete Agent class.
#
# Every other file in types/plugins/ defines a single plugin protocol.
# SkillsExecutor is the exception because the context protocols are small and
# tightly coupled to the plugin contract; splitting them into separate files
# would create import indirection with no real decoupling benefit.
# ──────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class SkillsPlanningContext(Protocol):
    """Agent-owned services exposed to Skills Executor planning."""

    def get_setting(self, key: str, default: Any = None) -> Any: ...

    def action_available(self, action_id: str) -> bool: ...

    def can_auto_bind_bash_action(self, action_id: str) -> bool: ...

    def auto_bind_bash_action(self, action_id: str) -> None: ...

    async def async_request_model_plan(
        self,
        *,
        plan: SkillExecutionPlan,
        semantic_output_contract: dict[str, Any],
        output_schema: dict[str, Any],
        max_revisions: int,
    ) -> dict[str, Any]: ...


@runtime_checkable
class SkillsExecutionContext(Protocol):
    """Agent-owned services exposed to Skills Executor execution."""

    def get_setting(self, key: str, default: Any = None) -> Any: ...

    def action_available(self, action_id: str) -> bool: ...

    async def async_execute_action(
        self,
        action_id: str,
        kwargs: dict[str, Any],
        *,
        purpose: str,
        source_protocol: str,
    ) -> ActionResult: ...


@runtime_checkable
class SkillsRuntimeContext(SkillsPlanningContext, SkillsExecutionContext, Protocol):
    """Full Agent component adapter surface used by the builtin plugin."""

    agent: Any


@runtime_checkable
class SkillsExecutor(Protocol):
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

    def list_skills(self) -> list[dict[str, Any]]: ...

    def list_skills_packs(self) -> list[SkillsPackRecord]: ...

    def inspect_skills(self, skill_id: str) -> SkillContract: ...

    def inspect_skills_pack(self, skills_pack_id: str) -> SkillsPackRecord: ...

    def remove_skills(self, skill_id: str) -> dict[str, Any]: ...

    def remove_skills_pack(self, skills_pack_id: str, *, remove_skills: bool = False) -> dict[str, Any]: ...

    async def async_resolve_plan(
        self,
        *,
        context: SkillsPlanningContext,
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
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
    ) -> Any: ...
