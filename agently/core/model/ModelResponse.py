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

import inspect
import json
import uuid

from typing import Any, AsyncGenerator, Generator, Literal, TYPE_CHECKING, cast, overload

from agently.core.extension import ExtensionHandlers
from agently.core.runtime import bind_runtime_context, get_current_agent_execution_context
from agently.utils import DeprecationWarnings, Settings, DataFormatter

from .Prompt import Prompt
from .ModelResponseResult import DEFAULT_SPECIFIC_EVENTS, ModelResponseResult

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agently.core import PluginManager
    from agently.types.data import (
        AgentlyModelResultMessage,
        AgentlyOriginalResultPayload,
        AgentlySpecificResultMessage,
        InstantStreamingContentType,
        OutputValidateHandler,
        ResultContentType,
        RunContext,
        SpecificEvents,
        StreamingData,
    )
    from agently.types.plugins import ModelRequester


class ModelResponse:
    def __init__(
        self,
        agent_name: str,
        plugin_manager: "PluginManager",
        settings: Settings,
        prompt: Prompt,
        extension_handlers: ExtensionHandlers,
        *,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        agent_turn_run_context: "RunContext | None" = None,
        attempt_index: int = 1,
        warn_deprecated: bool = True,
    ):
        if warn_deprecated:
            DeprecationWarnings.warn_deprecated_once(
                "ModelResponse",
                "ModelResponse is deprecated and will be removed in Agently 4.2. "
                "Use ModelResponseResult returned by get_result() instead.",
                stacklevel=2,
            )
        self.agent_name = agent_name
        self.id = uuid.uuid4().hex
        self.attempt_index = attempt_index
        if run_context is not None:
            self.request_run_context = run_context
        else:
            from agently.types.data import RunContext

            self.request_run_context = RunContext.create(
                run_kind="request",
                parent=parent_run_context,
                agent_name=self.agent_name,
                response_id=self.id,
            )
        if self.request_run_context.response_id is None:
            self.request_run_context.response_id = self.id
        if self.request_run_context.agent_name is None:
            self.request_run_context.agent_name = self.agent_name
        self.run_context = self.request_run_context
        self.agent_turn_run_context = agent_turn_run_context
        self.model_run_context = self.request_run_context.create_child(
            run_kind="model_request",
            response_id=self.id,
            meta={
                "attempt_index": self.attempt_index,
            },
        )
        self.plugin_manager = plugin_manager
        settings_snapshot = settings.get()
        self.settings = Settings(settings_snapshot if isinstance(settings_snapshot, dict) else {})
        self.settings.set("$log.cancel_logs", False)
        prompt_snapshot = prompt.get()
        self.prompt = Prompt(
            self.plugin_manager,
            self.settings,
            prompt_dict=prompt_snapshot if isinstance(prompt_snapshot, dict) else {},
        )
        extension_handlers_snapshot = extension_handlers.get()
        self.extension_handlers = ExtensionHandlers(
            extension_handlers_snapshot if isinstance(extension_handlers_snapshot, dict) else {}
        )
        self.result = ModelResponseResult(
            self.agent_name,
            self.id,
            self.prompt,
            self._get_response_generator(),
            self.plugin_manager,
            self.settings,
            self.extension_handlers,
            request_run_context=self.request_run_context,
            model_run_context=self.model_run_context,
            attempt_index=self.attempt_index,
        )

    def cancel_logs(self):
        self.settings.set("$log.cancel_logs", True)

    def get_meta(self):
        return self.result.get_meta()

    async def async_get_meta(self):
        return await self.result.async_get_meta()

    def get_text(self) -> str:
        return self.result.get_text()

    async def async_get_text(self) -> str:
        return await self.result.async_get_text()

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
        _retry_count: int = 0,
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
        _retry_count: int = 0,
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
        _retry_count: int = 0,
    ) -> Any:
        return self.result.get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            _retry_count=_retry_count,
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
        _retry_count: int = 0,
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
        _retry_count: int = 0,
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
        _retry_count: int = 0,
    ) -> Any:
        return await self.result.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            _retry_count=_retry_count,
        )

    @overload
    def get_data_object(self) -> "BaseModel | None": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel | None": ...

    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ):
        return self.result.get_data_object(
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    @overload
    async def async_get_data_object(self) -> "BaseModel | None": ...

    @overload
    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel": ...

    @overload
    async def async_get_data_object(
        self,
        *,
        ensure_keys: None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel | None": ...

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ):
        return await self.result.async_get_data_object(
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    @overload
    def get_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["StreamingData", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyModelResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlySpecificResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyOriginalResultPayload", None, None]: ...

    @overload
    def get_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator: ...

    def get_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator:
        return cast(Any, self.result).get_generator(type=type, content=content, specific=specific)

    @overload
    def get_async_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["StreamingData", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyModelResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlySpecificResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyOriginalResultPayload", None]: ...

    @overload
    def get_async_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator: ...

    def get_async_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator:
        return cast(Any, self.result).get_async_generator(type=type, content=content, specific=specific)

    def _build_prompt_payload(self) -> dict[str, Any]:
        prompt_snapshot = self.prompt.to_serializable_prompt_data()
        prompt_object = self.prompt.to_prompt_object()
        prompt_messages = self.prompt.to_messages(rich_content=bool(prompt_object.attachment))
        prompt_text = self.prompt.to_text()
        return {
            "prompt": prompt_snapshot,
            "prompt_messages": DataFormatter.sanitize(prompt_messages),
            "prompt_text": prompt_text,
            "output_format": prompt_object.output_format,
            "ensure_all_keys": getattr(prompt_object, "ensure_all_keys", False),
            "has_tools": bool(prompt_object.tools),
            "chat_history_length": len(prompt_object.chat_history),
            "attachment_count": len(prompt_object.attachment),
        }

    def _build_request_payload(self, request_data: Any):
        request_data_dict = DataFormatter.sanitize(request_data.model_dump())
        request_detail = {
            "data": request_data_dict["data"] if "data" in request_data_dict else None,
            "request_options": request_data_dict["request_options"] if "request_options" in request_data_dict else None,
            "request_url": request_data_dict["request_url"] if "request_url" in request_data_dict else None,
            "stream": request_data_dict["stream"] if "stream" in request_data_dict else None,
        }
        return {
            "request": request_detail,
            "request_text": json.dumps(request_detail, indent=2, ensure_ascii=False)
            .replace("\\n", "\n")
            .replace("\\\"", "\""),
        }

    def _build_full_provider_request_data(self, request_data: Any) -> dict[str, Any]:
        data = DataFormatter.to_str_key_dict(
            getattr(request_data, "data", {}),
            value_format="serializable",
            default_value={},
        )
        options = DataFormatter.to_str_key_dict(
            getattr(request_data, "request_options", {}),
            value_format="serializable",
            default_value={},
        )
        data.update(options)
        return data

    async def _get_response_generator(self) -> AsyncGenerator["AgentlyModelResultMessage", None]:
        from agently.base import async_emit_runtime

        with bind_runtime_context(
            parent_run_context=self.request_run_context,
            request_run_context=self.request_run_context,
            model_run_context=self.model_run_context,
            agent_turn_run_context=self.agent_turn_run_context,
            settings=self.settings,
        ):
            await async_emit_runtime(
                {
                    "event_type": "request.started",
                    "source": "ModelResponse",
                    "message": f"Starting request for agent '{ self.agent_name }'.",
                    "payload": {
                        "agent_name": self.agent_name,
                        "response_id": self.id,
                        "attempt_index": self.attempt_index,
                    },
                    "run": self.request_run_context,
                }
            )
            try:
                ModelRequester = cast(
                    type["ModelRequester"],
                    self.plugin_manager.get_plugin(
                        "ModelRequester",
                        str(self.settings["plugins.ModelRequester.activate"]),
                    ),
                )
                request_prefixes = self.extension_handlers.get("request_prefixes", [])
                for prefix in request_prefixes:
                    if inspect.iscoroutinefunction(prefix):
                        await prefix(self.prompt, self.settings)
                    elif inspect.isfunction(prefix):
                        prefix(self.prompt, self.settings)
                await async_emit_runtime(
                    {
                        "event_type": "model.request_started",
                        "source": "ModelResponse",
                        "message": f"Starting model request attempt #{ self.attempt_index } for agent '{ self.agent_name }'.",
                        "payload": {
                            "agent_name": self.agent_name,
                            "response_id": self.id,
                            "request_run_id": self.request_run_context.run_id,
                            "attempt_index": self.attempt_index,
                        },
                        "run": self.model_run_context,
                    }
                )
                prompt_payload = self._build_prompt_payload()
                await async_emit_runtime(
                    {
                        "event_type": "prompt.built",
                        "source": "ModelResponse",
                        "message": f"Prompt built for model request attempt #{ self.attempt_index }.",
                        "payload": {
                            "agent_name": self.agent_name,
                            "response_id": self.id,
                            "attempt_index": self.attempt_index,
                            **prompt_payload,
                        },
                        "run": self.model_run_context,
                    }
                )
                model_requester = ModelRequester(self.prompt, self.settings)
                request_data = model_requester.generate_request_data()
                request_payload = self._build_request_payload(request_data)
                await async_emit_runtime(
                    {
                        "event_type": "model.requesting",
                        "source": "ModelResponse",
                        "message": f"Sending model request for agent '{ self.agent_name }'.",
                        "payload": {
                            "agent_name": self.agent_name,
                            "response_id": self.id,
                            "attempt_index": self.attempt_index,
                            "request_run_id": self.request_run_context.run_id,
                            **request_payload,
                        },
                        "run": self.model_run_context,
                    }
                )
                consume_model_request = getattr(
                    get_current_agent_execution_context(),
                    "consume_model_request",
                    None,
                )
                if callable(consume_model_request):
                    consume_model_request(
                        response_id=self.id,
                        run_id=self.model_run_context.run_id,
                    )
                build_request_handlers = getattr(model_requester, "build_request_handlers", None)
                if callable(build_request_handlers):
                    from agently.core.model.AttemptRunner import AttemptRunner, is_core_attempt_runner_entrypoint
                    from agently.types.data import AttemptHandlers, AttemptObservation, AttemptState

                    if is_core_attempt_runner_entrypoint(getattr(model_requester, "request_model", None)):
                        handlers = cast(AttemptHandlers, build_request_handlers(request_data))
                        full_request_data = self._build_full_provider_request_data(request_data)

                        async def observe_attempt(observation: AttemptObservation, state: AttemptState) -> None:
                            if handlers.on_observation is not None:
                                result = handlers.on_observation(observation, state)
                                if inspect.isawaitable(result):
                                    await result
                            if observation.kind == "error_yielded":
                                from agently.core.runtime.RuntimeEvents import async_emit_model_requester_error

                                await async_emit_model_requester_error(
                                    observation.data.get("error"),
                                    source=str(getattr(model_requester, "name", ModelRequester.name)),
                                    request_data=full_request_data,
                                )

                        response_generator = AttemptRunner(
                            AttemptHandlers(
                                execute=handlers.execute,
                                handle_error=handlers.handle_error,
                                on_observation=observe_attempt,
                                is_output_started=handlers.is_output_started,
                            )
                        ).run_stream()
                    else:
                        response_generator = model_requester.request_model(request_data)
                else:
                    response_generator = model_requester.request_model(request_data)
                broadcast_generator = model_requester.broadcast_response(response_generator)
                broadcast_prefixes = self.extension_handlers.get("broadcast_prefixes", [])
                broadcast_suffixes = self.extension_handlers.get("broadcast_suffixes", {})
                for prefix in broadcast_prefixes:
                    if inspect.iscoroutinefunction(prefix):
                        result = await prefix(
                            self.result.full_result_data,
                            self.settings,
                        )
                        if result is not None:
                            yield result
                    elif inspect.isgeneratorfunction(prefix):
                        for result in prefix(
                            self.result.full_result_data,
                            self.settings,
                        ):
                            if result is not None:
                                yield result
                    elif inspect.isasyncgenfunction(prefix):
                        async for result in prefix(
                            self.result.full_result_data,
                            self.settings,
                        ):
                            if result is not None:
                                yield result
                    elif inspect.isfunction(prefix):
                        result = prefix(
                            self.result.full_result_data,
                            self.settings,
                        )
                        if result is not None:
                            yield result
                async for event, data in broadcast_generator:
                    yield event, data
                    suffixes = broadcast_suffixes[event] if event in broadcast_suffixes else []
                    for suffix in suffixes:
                        if inspect.iscoroutinefunction(suffix):
                            result = await suffix(
                                event,
                                data,
                                self.result.full_result_data,
                                self.settings,
                            )
                            if result is not None:
                                yield result
                        elif inspect.isgeneratorfunction(suffix):
                            for result in suffix(
                                event,
                                data,
                                self.result.full_result_data,
                                self.settings,
                            ):
                                if result is not None:
                                    yield result
                        elif inspect.isasyncgenfunction(suffix):
                            async for result in suffix(
                                event,
                                data,
                                self.result.full_result_data,
                                self.settings,
                            ):
                                if result is not None:
                                    yield result
                        elif inspect.isfunction(suffix):
                            result = suffix(
                                event,
                                data,
                                self.result.full_result_data,
                                self.settings,
                            )
                            if result is not None:
                                yield result
                await async_emit_runtime(
                    {
                        "event_type": "request.completed",
                        "source": "ModelResponse",
                        "message": f"Request completed for agent '{ self.agent_name }'.",
                        "payload": {
                            "agent_name": self.agent_name,
                            "response_id": self.id,
                            "attempt_index": self.attempt_index,
                        },
                        "run": self.request_run_context,
                    }
                )
                if self.agent_turn_run_context is not None:
                    await async_emit_runtime(
                        {
                            "event_type": "agent_turn.completed",
                            "source": "ModelResponse",
                            "message": f"Agent turn completed for '{ self.agent_name }'.",
                            "payload": {
                                "agent_name": self.agent_name,
                                "response_id": self.id,
                                "request_run_id": self.request_run_context.run_id,
                                "attempt_count": self.attempt_index,
                            },
                            "run": self.agent_turn_run_context,
                        }
                    )
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                is_side_channel = bool(
                    self.settings.get("runtime.side_channel", False)
                    or self.settings.get("model_request.side_channel", False)
                )
                failure_level = "WARNING" if is_side_channel else "ERROR"
                model_failure_event = (
                    "model.side_channel_request_failed" if is_side_channel else "model.request_failed"
                )
                request_failure_event = (
                    "request.side_channel_failed" if is_side_channel else "request.failed"
                )
                await async_emit_runtime(
                    {
                        "event_type": model_failure_event,
                        "source": "ModelResponse",
                        "level": failure_level,
                        "message": (
                            f"Side-channel model request failed for agent '{ self.agent_name }'."
                            if is_side_channel
                            else f"Model request failed for agent '{ self.agent_name }'."
                        ),
                        "payload": {
                            "agent_name": self.agent_name,
                            "response_id": self.id,
                            "attempt_index": self.attempt_index,
                            "request_run_id": self.request_run_context.run_id,
                            "side_channel": is_side_channel,
                        },
                        "error": error,
                        "run": self.model_run_context,
                    }
                )
                await async_emit_runtime(
                    {
                        "event_type": request_failure_event,
                        "source": "ModelResponse",
                        "level": failure_level,
                        "message": (
                            f"Side-channel request failed for agent '{ self.agent_name }'."
                            if is_side_channel
                            else f"Request failed for agent '{ self.agent_name }'."
                        ),
                        "payload": {
                            "agent_name": self.agent_name,
                            "response_id": self.id,
                            "attempt_index": self.attempt_index,
                            "side_channel": is_side_channel,
                        },
                        "error": error,
                        "run": self.request_run_context,
                    }
                )
                if self.agent_turn_run_context is not None:
                    await async_emit_runtime(
                        {
                            "event_type": "agent_turn.failed",
                            "source": "ModelResponse",
                            "level": "ERROR",
                            "message": f"Agent turn failed for '{ self.agent_name }'.",
                            "payload": {
                                "agent_name": self.agent_name,
                                "response_id": self.id,
                                "request_run_id": self.request_run_context.run_id,
                                "attempt_count": self.attempt_index,
                            },
                            "error": error,
                            "run": self.agent_turn_run_context,
                        }
                    )
                raise
