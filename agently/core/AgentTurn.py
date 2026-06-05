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
from typing import Any, AsyncGenerator, Generator, TYPE_CHECKING, Literal, cast, overload

from agently.core.model import _UNSET, _resolve_quick_prompt_input
from agently.core.model.AttachmentInput import ImageDetail, build_image_attachment
from agently.core.model.ModelResponseResult import DEFAULT_SPECIFIC_EVENTS
from agently.utils import FunctionShifter

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.core.model import ModelRequest, ModelResponse, ModelResponseResult
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        AgentExecutionMode,
        AgentlyModelResponseMessage,
        AgentlyOriginalResponsePayload,
        AgentlySpecificResponseMessage,
        InstantStreamingContentType,
        OutputValidateHandler,
        PromptStandardSlot,
        ResponseContentType,
        RunContext,
        SkillRuntimeStreamHandler,
        SpecificEvents,
        StreamingData,
        TaskDAG,
    )
    from agently.types.options import ExecutionOptions


class AgentTurn:
    """Request-scoped prompt draft for one Agent turn."""

    def __init__(self, agent: "BaseAgent", *, request: "ModelRequest | None" = None):
        self._agent = agent
        self.request = request if request is not None else agent.create_request()
        self.request_prompt = self.request.prompt
        self.prompt = self.request_prompt

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    @property
    def id(self) -> str:
        return self._agent.id

    @property
    def name(self) -> str:
        return self._agent.name

    @property
    def plugin_manager(self):
        return self._agent.plugin_manager

    @property
    def settings(self):
        return self._agent.settings

    @property
    def agent_prompt(self):
        return self._agent.agent_prompt

    @property
    def extension_handlers(self):
        return self._agent.extension_handlers

    def _snapshot_request_prompt(self) -> dict[str, Any]:
        prompt_snapshot = self.request.prompt.get()
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, dict) else {}

    def set_turn_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: Any,
        *,
        mappings: dict[str, Any] | None = None,
    ):
        self.request.prompt.set(key, value, mappings=mappings)
        return self

    def set_request_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: Any,
        *,
        mappings: dict[str, Any] | None = None,
    ):
        return self.set_turn_prompt(key, value, mappings=mappings)

    def remove_request_prompt(self, key: "PromptStandardSlot | str"):
        self.request.prompt.set(key, None)
        return self

    def validate(self, handler: "OutputValidateHandler"):
        self.request.validate(handler)
        return self

    def system(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False):
        if always:
            self._agent.system(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("system", prompt, mappings=mappings)
        return self

    def rule(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False):
        if always:
            self._agent.rule(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("instruct", ["{system.rule} ARE IMPORTANT RULES YOU SHALL FOLLOW!"])
            self.request.prompt.set("system.rule", prompt, mappings=mappings)
        return self

    def role(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self._agent.role(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("instruct", ["YOU MUST REACT AND RESPOND AS {system.role}!"])
            self.request.prompt.set("system.your_role", prompt, mappings=mappings)
        return self

    def user_info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self._agent.user_info(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("instruct", ["{system.user_info} IS IMPORTANT INFORMATION ABOUT USER!"])
            self.request.prompt.set("system.user_info", prompt, mappings=mappings)
        return self

    def input(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self._agent.input(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("input", prompt, mappings=mappings)
        return self

    def info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self._agent.info(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("info", prompt, mappings=mappings)
        return self

    def instruct(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self._agent.instruct(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("instruct", prompt, mappings=mappings)
        return self

    def examples(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self._agent.examples(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("examples", prompt, mappings=mappings)
        return self

    def output(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ):
        if always:
            self._agent.output(prompt, mappings=mappings, always=True, format=format)
        else:
            self.request.prompt.set("output", prompt, mappings=mappings)
            if format is not None:
                self.request.prompt.set("output_format", format)
        return self

    def attachment(
        self,
        prompt: list[dict[str, Any]],
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        if always:
            self._agent.attachment(prompt, mappings=mappings, always=True)
        else:
            self.request_prompt.set("attachment", prompt, mappings=mappings)
        return self

    def image(
        self,
        *,
        question: str,
        file: str | os.PathLike[str] | None = None,
        url: str | None = None,
        files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
        urls: list[str] | tuple[str, ...] | None = None,
        detail: ImageDetail | None = None,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        attachment = build_image_attachment(
            question=question,
            file=file,
            url=url,
            files=files,
            urls=urls,
            detail=detail,
        )
        if always:
            self._agent.image(
                question=question,
                file=file,
                url=url,
                files=files,
                urls=urls,
                detail=detail,
                mappings=mappings,
                always=True,
            )
        else:
            self.request_prompt.set("attachment", attachment, mappings=mappings)
        return self

    def options(self, options: dict[str, Any], *, always: bool = False):
        if always:
            self._agent.options(options, always=True)
        else:
            self.request.prompt.set("options", options)
        return self

    def use_dynamic_task(self, *args: Any, **kwargs: Any):
        self._agent.use_dynamic_task(*args, **kwargs)
        return self

    def _skills_prompt_defaults(
        self,
        task: str | None,
        *,
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ):
        if output is not None and semantic_outputs is not None:
            raise ValueError("Use either output= or semantic_outputs= for Skills execution, not both.")
        prompt_defaults = self._agent._dynamic_task_prompt_defaults(
            task,
            prompt_snapshot=self._snapshot_request_prompt(),
        )
        resolved_task = task if task is not None and prompt_defaults["target"] is None else prompt_defaults["target"]
        if not resolved_task:
            raise ValueError("Skills execution requires task=... or a configured agent.input(...).")
        explicit_output = output if output is not None else semantic_outputs
        resolved_output = explicit_output if explicit_output is not None else prompt_defaults["output_schema"]
        resolved_format = output_format or cast(Any, prompt_defaults["output_format"]) or "auto"
        return str(resolved_task), resolved_output, resolved_format

    async def async_resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: Any = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ):
        task, output, output_format = self._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        return await cast(Any, self._agent).async_resolve_skills_plan(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            output=output,
            output_format=output_format,
        )

    def resolve_skills_plan(self, *args: Any, **kwargs: Any):
        return FunctionShifter.syncify(self.async_resolve_skills_plan)(*args, **kwargs)

    async def async_run_skills_task(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: Any = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        stream_handler: "SkillRuntimeStreamHandler | None" = None,
        effort: str | None = None,
    ):
        task, output, output_format = self._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        return await cast(Any, self._agent).async_run_skills_task(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            output=output,
            output_format=output_format,
            stream_handler=stream_handler,
            effort=effort,
        )

    def run_skills_task(self, *args: Any, **kwargs: Any):
        return FunctionShifter.syncify(self.async_run_skills_task)(*args, **kwargs)

    def create_dynamic_task(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("_prompt_snapshot", self._snapshot_request_prompt())
        return self._agent.create_dynamic_task(*args, **kwargs)

    def create_execution(
        self,
        *,
        mode: "AgentExecutionMode | str" = "one_turn",
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: "ExecutionOptions | dict[str, Any] | None" = None,
        parent_run_context: "RunContext | None" = None,
    ):
        plugin_name = str(self.settings.get("plugins.AgentOrchestrator.activate", "AgentlyAgentOrchestrator"))
        plugin_class = cast(Any, self.plugin_manager.get_plugin("AgentOrchestrator", plugin_name))
        orchestrator = plugin_class(plugin_manager=self.plugin_manager, settings=self.settings)
        return orchestrator.create_execution(
            self,
            mode=mode,
            lineage=lineage,
            limits=limits,
            options=options,
            parent_run_context=parent_run_context,
        )

    def start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        return self.create_execution(parent_run_context=parent_run_context).get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    async def async_start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ):
        return await self.create_execution(parent_run_context=parent_run_context).async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    def get_response(self, *, parent_run_context: "RunContext | None" = None) -> "ModelResponse":
        turn_run_context = self._agent._create_agent_turn_run_context(parent_run_context=parent_run_context)
        self._agent._emit_agent_turn_started(turn_run_context)
        return self.request.get_response(parent_run_context=turn_run_context)

    def get_result(self, *, parent_run_context: "RunContext | None" = None) -> "ModelResponseResult":
        return self.get_response(parent_run_context=parent_run_context).result

    def get_meta(self, *, parent_run_context: "RunContext | None" = None):
        return self.get_response(parent_run_context=parent_run_context).get_meta()

    async def async_get_meta(self, *, parent_run_context: "RunContext | None" = None):
        return await self.get_response(parent_run_context=parent_run_context).async_get_meta()

    def get_text(self, *, parent_run_context: "RunContext | None" = None):
        return self.get_response(parent_run_context=parent_run_context).get_text()

    async def async_get_text(self, *, parent_run_context: "RunContext | None" = None):
        return await self.get_response(parent_run_context=parent_run_context).async_get_text()

    @overload
    def get_data(
        self,
        *,
        type: Literal['parsed'],
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> dict[str, Any]: ...

    @overload
    def get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any: ...

    def get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return self.get_response(parent_run_context=parent_run_context).get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    @overload
    async def async_get_data(
        self,
        *,
        type: Literal['parsed'],
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> dict[str, Any]: ...

    @overload
    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any: ...

    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await self.get_response(parent_run_context=parent_run_context).async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    def get_data_object(self, *args: Any, parent_run_context: "RunContext | None" = None, **kwargs: Any):
        return self.get_response(parent_run_context=parent_run_context).get_data_object(*args, **kwargs)

    async def async_get_data_object(self, *args: Any, parent_run_context: "RunContext | None" = None, **kwargs: Any):
        return await self.get_response(parent_run_context=parent_run_context).async_get_data_object(*args, **kwargs)

    @overload
    def get_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["StreamingData", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["AgentlyModelResponseMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["AgentlySpecificResponseMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["original"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["AgentlyOriginalResponsePayload", None, None]: ...

    @overload
    def get_generator(
        self,
        type: "ResponseContentType | None" = None,
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator: ...

    def get_generator(
        self,
        type: "ResponseContentType | None" = None,
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator:
        return self.get_response(parent_run_context=parent_run_context).get_generator(
            type=type,
            content=content,
            specific=specific,
        )

    @overload
    def get_async_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["StreamingData", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["AgentlyModelResponseMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["AgentlySpecificResponseMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["original"],
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["AgentlyOriginalResponsePayload", None]: ...

    @overload
    def get_async_generator(
        self,
        type: "ResponseContentType | None" = None,
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator: ...

    def get_async_generator(
        self,
        type: "ResponseContentType | None" = None,
        content: "ResponseContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator:
        return self.get_response(parent_run_context=parent_run_context).get_async_generator(
            type=type,
            content=content,
            specific=specific,
        )

    def get_prompt_text(self):
        return self.request_prompt.to_text()[6:][:-11]
