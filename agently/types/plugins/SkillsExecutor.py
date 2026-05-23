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

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode, SkillsPackRecord


@runtime_checkable
class SkillsPlanningContext(Protocol):
    """Agent-owned services exposed to Skills Executor planning."""

    def get_setting(self, key: str, default: Any = None) -> Any: ...

    async def async_request_model(
        self,
        *,
        prompt: Any,
        output_schema: Any = None,
        ensure_keys: list[str] | None = None,
        max_retries: int = 3,
        stream_handler: Callable[[Any], Awaitable[None] | None] | None = None,
    ) -> Any: ...


@runtime_checkable
class SkillsExecutionContext(SkillsPlanningContext, Protocol):
    """Agent-owned services exposed to Skills Executor execution."""

    async def async_emit_runtime_stream(self, item: dict[str, Any]) -> None: ...


@runtime_checkable
class SkillsRuntimeContext(SkillsExecutionContext, Protocol):
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
        semantic_outputs: Any = None,
    ) -> SkillExecutionPlan: ...

    async def async_execute_plan(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
    ) -> Any: ...
