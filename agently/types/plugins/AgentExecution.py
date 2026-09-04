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

import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from typing import Any, Literal, Protocol, TYPE_CHECKING, overload, runtime_checkable

from agently.types.data import (
    AgentlySpecificResultMessage,
    AgentArtifactHandler,
    AgentArtifactResult,
    AgentExecutionLineage,
    AgentExecutionLimits,
    AgentExecutionMeta,
    AgentExecutionEffort,
    AgentExecutionPatternInfo,
    AgentExecutionStreamData,
    AgentExecutionRecordPurpose,
    AgentExecutionRecordWrite,
    AgentExecutionStatus,
    AgentExecutionStrategy,
    AgentReviewHandler,
    AgentReviewResult,
    ContextBudget,
    ContextPackage,
    ContextReadIntent,
    OutputValidateHandler,
    RunContext,
    SkillMode,
)
from .AgentPattern import AgentPatternInput

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agently.core.application import AgentTask
    from agently.core.application.AgentExecution import (
        AgentExecutionContext,
        AgentExecutionResult,
        AgentExecutionStream,
    )
    from agently.core.context import TaskContext
    from agently.core.model import ModelRequest, Prompt
    from agently.core.TaskWorkspace import TaskWorkspace


@runtime_checkable
class AgentExecution(Protocol):
    """Response-style contract for one bounded Agent execution object."""

    id: str
    lineage: AgentExecutionLineage
    limits: AgentExecutionLimits
    options: Any
    effective_options: dict[str, Any]
    consumed_options: dict[str, Any]
    status: AgentExecutionStatus
    request: "ModelRequest"
    request_prompt: "Prompt"
    prompt: "Prompt"
    stream: "AgentExecutionStream"
    execution_context: "AgentExecutionContext"
    task_context: "TaskContext"
    task_workspace: "TaskWorkspace"
    record_store: Any
    task_refs: dict[str, Any]
    task_record: "AgentTask | None"
    pattern_info: AgentExecutionPatternInfo
    artifact_results: list[AgentArtifactResult]
    review_results: list[AgentReviewResult]
    result: object | None

    def __getattr__(self, name: str) -> Any: ...

    def input(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def info(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def output(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def ensure_long_output(self, enabled: bool = True) -> "AgentExecution": ...

    def instruct(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def set_execution_prompt(self, key: Any, value: Any, *, mappings: dict[str, Any] | None = None) -> "AgentExecution": ...

    def remove_execution_prompt(self, key: Any) -> "AgentExecution": ...

    def goal(
        self,
        goal: str | list[str] | tuple[str, ...] | set[str],
        success_criteria: str | list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> "AgentExecution": ...

    def goals(
        self,
        goal: str | list[str] | tuple[str, ...] | set[str],
        success_criteria: str | list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> "AgentExecution": ...

    @overload
    def pattern(
        self,
        pattern: Literal["request", "goal", "plan", "long_content"],
    ) -> "AgentExecution": ...

    @overload
    def pattern(self, pattern: AgentPatternInput) -> "AgentExecution": ...

    def pattern(self, pattern: AgentPatternInput) -> "AgentExecution": ...

    def review(self, handler: AgentReviewHandler | None = None) -> "AgentExecution": ...

    def verify(self, handler: AgentReviewHandler | None = None) -> "AgentExecution": ...

    def artifact(
        self,
        path: str | os.PathLike[str],
        handler: AgentArtifactHandler | None = None,
    ) -> "AgentExecution": ...

    @overload
    def effort(
        self,
        value: Literal["minimal", "low", "fast", "medium", "normal", "high", "max"] = "medium",
        **strategy: object,
    ) -> "AgentExecution": ...

    @overload
    def effort(
        self,
        value: AgentExecutionEffort = "medium",
        **strategy: object,
    ) -> "AgentExecution": ...

    def effort(
        self,
        value: AgentExecutionEffort = "medium",
        **strategy: object,
    ) -> "AgentExecution": ...

    @overload
    def strategy(
        self,
        value: Literal["auto", "direct", "task", "task_loop", "long_task", "flat", "taskboard"] | None = None,
        **options: object,
    ) -> "AgentExecution": ...

    @overload
    def strategy(
        self,
        value: AgentExecutionStrategy | None = None,
        **options: object,
    ) -> "AgentExecution": ...

    def strategy(
        self,
        value: AgentExecutionStrategy | None = None,
        **options: object,
    ) -> "AgentExecution": ...

    def create_execution(self, **kwargs: Any) -> "AgentExecution": ...

    def get_result(self) -> "AgentExecutionResult": ...

    def validate(self, handler: OutputValidateHandler) -> "AgentExecution": ...

    def create_dynamic_task(self, *args: Any, **kwargs: Any) -> Any: ...

    def use_skills(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
    ) -> "AgentExecution": ...

    def require_skills(
        self,
        skills: Any,
        *,
        auto_allow: bool = False,
    ) -> "AgentExecution": ...

    def use_skills_packs(
        self,
        skills_packs: Any,
        *,
        mode: SkillMode = "model_decision",
    ) -> "AgentExecution": ...

    async def async_prepare_task_context(self) -> "TaskContext": ...

    async def async_read_task_context(
        self,
        *,
        consumer_id: str,
        phase: str,
        intent: str | ContextReadIntent | None = None,
        budget: ContextBudget | None = None,
    ) -> ContextPackage: ...

    def run_skills_task(self, *args: Any, **kwargs: Any) -> Any: ...

    async def async_run_skills_task(self, *args: Any, **kwargs: Any) -> Any: ...

    async def select_route(self) -> tuple[str, dict[str, Any]]: ...

    async def emit_stream(self, *args: Any, **kwargs: Any) -> AgentExecutionStreamData: ...

    async def close_streams(self) -> None: ...

    def start(self, **kwargs: Any) -> Any: ...

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
        parent_run_context: Any = None,
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
        parent_run_context: Any = None,
    ) -> Any: ...

    async def async_get_full_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> Any: ...

    async def async_get_text(self, **kwargs: Any) -> str: ...

    async def async_get_meta(self) -> AgentExecutionMeta: ...

    async def async_streaming_print(self) -> None: ...

    async def async_record_data(
        self,
        *,
        purpose: AgentExecutionRecordPurpose = "process",
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
    ) -> AgentExecutionRecordWrite: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[str, AgentExecutionStreamData], None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[AgentlySpecificResultMessage, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["instant", "streaming_parse", "original"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[AgentExecutionStreamData, None]: ...

    @overload
    def get_async_generator(self, *args: Any, **kwargs: Any) -> AsyncGenerator[str, None]: ...

    def get_async_generator(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]: ...

    def get_data(self, **kwargs: Any) -> Any: ...

    def get_full_data(self, **kwargs: Any) -> Any: ...

    @overload
    def get_data_object(self) -> "BaseModel | None": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str],
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel | None": ...

    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel | None": ...

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel | None": ...

    def get_text(self, **kwargs: Any) -> str: ...

    def get_meta(self) -> AgentExecutionMeta: ...

    def streaming_print(self) -> None: ...

    def record_data(self, **kwargs: Any) -> AgentExecutionRecordWrite: ...

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[tuple[str, AgentExecutionStreamData], None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[AgentlySpecificResultMessage, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["instant", "streaming_parse", "original"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[AgentExecutionStreamData, None, None]: ...

    @overload
    def get_generator(self, *args: Any, **kwargs: Any) -> Generator[str, None, None]: ...

    def get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]: ...

    async def async_get_key_result(self, key: str, *, must_in_prompt: bool = False) -> object | None: ...

    def get_key_result(self, key: str, *, must_in_prompt: bool = False) -> object | None: ...

    async def async_wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ) -> AsyncGenerator[tuple[str, object], None]: ...

    def wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ) -> Generator[tuple[str, object], None, None]: ...

    def on_key(self, key: str, handler: Callable[[object], object | Awaitable[object]]) -> "AgentExecution": ...

    def when_key(self, key: str, handler: Callable[[object], object | Awaitable[object]]) -> "AgentExecution": ...

    async def async_start_waiter(self, *, must_in_prompt: bool = False) -> list[tuple[str, object, object]]: ...

    def start_waiter(self, *, must_in_prompt: bool = False) -> list[tuple[str, object, object]]: ...


@runtime_checkable
class AgentStepExecutor(Protocol):
    """Restricted adapter for asking an Agent to perform one task-step execution."""

    async def async_execute_step(
        self,
        *,
        lineage: AgentExecutionLineage | dict[str, Any] | None = None,
        limits: AgentExecutionLimits | dict[str, Any] | None = None,
    ) -> AgentExecutionMeta: ...
