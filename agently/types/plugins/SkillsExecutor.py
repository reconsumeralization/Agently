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

"""Skills Executor plugin protocol surfaces.

This module groups four Protocol declarations because they form one tightly
coupled boundary, even though only one is a plugin-manager-managed protocol:

- ``SkillsExecutor`` is the plugin protocol implemented by framework and
  third-party Skills Executor plugins. ``PluginManager`` resolves it from
  ``builtins.plugins.SkillsExecutor``.
- ``SkillsPlanningContext`` / ``SkillsExecutionContext`` /
  ``SkillsRuntimeContext`` are SPI surfaces implemented by the Agent and
  injected at planning / execution time. They are not plugins; they describe
  what the Agent gives the plugin.

Splitting these into separate files would obscure that injection direction and
duplicate type imports. New context Protocols that follow the same SPI shape
should be added here; new plugin Protocols belong in their own file.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from agently.types.data import SkillContract, SkillExecutionPlan, SkillMode, SkillsPackRecord


@runtime_checkable
class SkillsPlanningContext(Protocol):
    """Agent-owned services exposed to Skills Executor planning."""

    def get_setting(self, key: str, default: Any = None) -> Any: ...

    async def async_request_model(
        self,
        *,
        prompt: Any,
        model_key: str | None = None,
        output_schema: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] = "auto",
        ensure_keys: list[str] | None = None,
        max_retries: int = 3,
        stream_handler: Callable[[Any], Awaitable[None] | None] | None = None,
    ) -> Any: ...


@runtime_checkable
class SkillsExecutionContext(SkillsPlanningContext, Protocol):
    """Agent-owned services exposed to Skills Executor execution."""

    async def async_emit_runtime_stream(self, item: dict[str, Any]) -> None: ...

    # ── Acting surface (bound only when granted by the plan) ──

    async def async_call_tool(self, name: str, /, **kwargs: Any) -> Any: ...
    async def async_call_action(self, name: str, /, **kwargs: Any) -> Any: ...
    async def async_execute_action_specs(
        self,
        action_specs: list[dict[str, Any]],
        *,
        concurrency: int | None = None,
    ) -> list[dict[str, Any]]: ...

    # ── Progressive disclosure over resource_index ──

    async def async_read_resource(
        self, *, skill_id: str, path: str, max_bytes: int = 262144
    ) -> str: ...

    # ── Controlled side effects; None when not granted ──

    @property
    def execution_environment(self) -> Any | None: ...


@runtime_checkable
class SkillsRuntimeContext(SkillsExecutionContext, Protocol):
    """Full Agent component adapter surface used by the builtin plugin."""

    agent: Any


@runtime_checkable
class SkillsEffortStrategyHandler(Protocol):
    """Callable protocol for application-defined Skills effort strategies.

    Handlers are invoked after Skills planning and capability mounting. They
    receive the Agent runtime context, selected Skills plan, requested task, the
    resolved effort config, and the requested output format. A handler may
    request models, call Actions/MCP through the context, emit runtime stream
    items, and return the final Skill execution output. Returning a
    SkillExecution-like object is also accepted by the builtin implementation.
    """

    def __call__(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        effort: str | None = None,
        effort_config: dict[str, Any] | None = None,
    ) -> Awaitable[Any] | Any: ...


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

    def configure(
        self,
        *,
        registry_root: str | Path | None = None,
        allowed_trust_levels: list[str] | None = None,
    ) -> "SkillsExecutor": ...

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
    ) -> SkillsPackRecord: ...

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
    ) -> dict[str, Any]: ...

    def list_skills(self) -> list[dict[str, Any]]: ...

    def list_skills_packs(self) -> list[SkillsPackRecord]: ...

    def inspect_skills(self, skill_id: str) -> SkillContract: ...

    def inspect_skills_pack(self, skills_pack_id: str) -> SkillsPackRecord: ...

    def read_resource(self, skill_id: str, path: str, *, max_bytes: int = 262144) -> str: ...

    def remove_skills(self, skill_id: str) -> dict[str, Any]: ...

    def remove_skills_pack(self, skills_pack_id: str, *, remove_skills: bool = False) -> dict[str, Any]: ...

    def register_effort_strategy(
        self,
        name: str,
        handler: SkillsEffortStrategyHandler,
        *,
        replace: bool = False,
    ) -> "SkillsExecutor": ...

    def unregister_effort_strategy(self, name: str) -> bool: ...

    def list_effort_strategies(self) -> list[str]: ...

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
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] = "auto",
    ) -> SkillExecutionPlan: ...

    async def async_execute_plan(
        self,
        *,
        context: SkillsExecutionContext,
        task: str,
        plan: SkillExecutionPlan,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        effort: str | None = None,
    ) -> Any: ...
