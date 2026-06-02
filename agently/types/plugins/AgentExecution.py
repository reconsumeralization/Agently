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

from collections.abc import AsyncGenerator, Generator
from typing import Any, Literal, Protocol, runtime_checkable

from agently.types.data import (
    AgentExecutionLineage,
    AgentExecutionLimits,
    AgentExecutionMeta,
    AgentExecutionMode,
    AgentExecutionStreamData,
    AgentExecutionWorkspaceRecord,
    OutputValidateHandler,
)


@runtime_checkable
class AgentExecution(Protocol):
    """Response-style contract for one bounded Agent execution object."""

    id: str
    mode: AgentExecutionMode
    lineage: AgentExecutionLineage
    limits: AgentExecutionLimits
    options: dict[str, Any]
    effective_options: dict[str, Any]
    consumed_options: dict[str, Any]
    status: str

    async def async_start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> Any: ...

    async def async_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> Any: ...

    async def async_get_text(self) -> str: ...

    async def async_get_meta(self) -> AgentExecutionMeta: ...

    async def async_record_workspace(
        self,
        *,
        collection: str = "observations",
        kind: str | None = "agent_execution_observation",
        content: Any = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        checkpoint: bool = False,
        checkpoint_state: dict[str, Any] | None = None,
        checkpoint_step_id: str | None = None,
        profile: str = "fast",
    ) -> AgentExecutionWorkspaceRecord: ...

    async def get_async_generator(
        self,
        type: Literal["instant", "streaming_parse", "all"] | str | None = "instant",
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[AgentExecutionStreamData | tuple[str, AgentExecutionStreamData], None]: ...

    def get_data(self, **kwargs: Any) -> Any: ...

    def get_text(self) -> str: ...

    def get_meta(self) -> AgentExecutionMeta: ...

    def record_workspace(self, **kwargs: Any) -> AgentExecutionWorkspaceRecord: ...

    def get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]: ...


@runtime_checkable
class AgentStepExecutor(Protocol):
    """Restricted adapter for asking an Agent to perform one task-step execution."""

    async def async_execute_step(
        self,
        *,
        lineage: AgentExecutionLineage | dict[str, Any] | None = None,
        limits: AgentExecutionLimits | dict[str, Any] | None = None,
    ) -> AgentExecutionMeta: ...
