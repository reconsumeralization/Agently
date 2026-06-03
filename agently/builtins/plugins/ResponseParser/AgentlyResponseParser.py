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

import asyncio
import contextlib
import warnings

from typing import TYPE_CHECKING, Any, AsyncGenerator, Generator, Literal, Mapping, cast
from pydantic import BaseModel

from agently.types.plugins import ResponseParser
from agently.types.data import StreamingData
from agently.utils import (
    DataPathBuilder,
    DataFormatter,
    StateDataNamespace,
    GeneratorConsumer,
    FunctionShifter,
    StreamingJSONParser,
    DeprecationWarnings,
)

from agently.builtins.plugins.ResponseParser.modules.json_output import (
    parse_json_output,
)
from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
    FlatMarkdownStreamingParser,
    parse_flat_markdown_output,
)
from agently.builtins.plugins.ResponseParser.modules.hybrid import (
    HybridStreamingParser,
    parse_hybrid_output,
)

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import AgentlyModelResult, AgentlyResponseGenerator, RunContext, SerializableMapping, SpecificEvents
    from agently.utils import Settings

DEFAULT_SPECIFIC_EVENTS = cast(
    "SpecificEvents",
    ["reasoning_delta", "delta", "reasoning_done", "done", "tool_calls"],
)


class AgentlyResponseParser(ResponseParser):
    name = "AgentlyResponseParser"
    DEFAULT_SETTINGS = {
        "$global": {
            "response": {
                "streaming_parse": False,
                "streaming_parse_path_style": "dot",
            },
        },
    }

    def __init__(
        self,
        agent_name: str,
        response_id: str,
        prompt: "Prompt",
        response_generator: "AgentlyResponseGenerator",
        settings: "Settings",
        run_context: "RunContext | None" = None,
    ):
        self.agent_name = agent_name
        self.response_id = response_id
        self.response_generator = response_generator
        self.settings = settings
        self.run_context = run_context
        self.plugin_settings = StateDataNamespace(self.settings, f"plugins.ResponseParser.{ self.name }")
        self.full_result_data: AgentlyModelResult = {
            "result_consumer": None,
            "meta": {},
            "original_delta": [],
            "original_done": {},
            "text_result": "",
            "cleaned_result": "",
            "parsed_result": None,
            "result_object": None,
            "errors": [],
            "extra": {},
        }
        self._prompt_object = prompt.to_prompt_object()
        self._OutputModel = prompt.to_output_model() if self._prompt_object.output_format in ("json", "flat_markdown", "hybrid") else None
        self._response_consumer: GeneratorConsumer | None = None
        self._consumer_lock = asyncio.Lock()
        self._final_json_parse_result: tuple[str | None, Any, BaseModel | None, bool] | None = None
        self._runtime_observations: list[dict[str, Any]] = []

        self._streaming_canceled = False

        self.get_meta = FunctionShifter.syncify(self.async_get_meta)
        self.get_text = FunctionShifter.syncify(self.async_get_text)
        self.get_data = FunctionShifter.syncify(self.async_get_data)
        self.get_data_object = FunctionShifter.syncify(self.async_get_data_object)

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def _build_result_object(self, parsed: Any) -> BaseModel | None:
        try:
            if self._OutputModel:
                return self._OutputModel.model_validate(parsed)
        except Exception:
            return None
        return None

    def _parse_json_output(self, text: str) -> tuple[str | None, Any, BaseModel | None, bool]:
        return parse_json_output(
            text,
            self._prompt_object.output,
            self._build_result_object,
        )

    def _record_runtime_observation(
        self,
        kind: str,
        *,
        message: str,
        level: str = "INFO",
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._runtime_observations.append(
            {
                "kind": kind,
                "level": level,
                "message": message,
                "payload": payload or {},
                "error": error,
            }
        )

    def drain_runtime_observations(self) -> list[dict[str, Any]]:
        observations = self._runtime_observations
        self._runtime_observations = []
        return observations

    async def _handle_done_event(self, data: Any, buffer: str) -> None:
        self.full_result_data["text_result"] = str(data)
        if self._prompt_object.output_format == "json":
            self._final_json_parse_result = self._parse_json_output(str(data))
            completed, parsed, result_object, repaired = self._final_json_parse_result
            if parsed is not None:
                self.full_result_data["cleaned_result"] = completed
                self.full_result_data["parsed_result"] = parsed
                self.full_result_data["result_object"] = result_object
                self._record_runtime_observation(
                    "completed",
                    message="Model response parsed as JSON output.",
                    payload={
                        "result": DataFormatter.sanitize(parsed),
                        "raw_text": str(data),
                        "cleaned_text": completed,
                        "repaired": repaired,
                        "streamed_text": buffer,
                        "format": "json",
                    },
                )
            else:
                self.full_result_data["cleaned_result"] = completed
                self.full_result_data["parsed_result"] = None
                self.full_result_data["result_object"] = None
                self._record_runtime_observation(
                    "parse_failed",
                    level="WARNING",
                    message="Can not parse JSON output from model response.",
                    payload={
                        "result": str(data),
                        "cleaned_text": completed,
                        "streamed_text": buffer,
                        "format": "json",
                    },
                )
            return

        if self._prompt_object.output_format == "flat_markdown":
            parsed = parse_flat_markdown_output(str(data), self._prompt_object.output or {})
            if parsed is not None:
                result_object = self._build_result_object(parsed)
                self.full_result_data["parsed_result"] = parsed
                self.full_result_data["result_object"] = result_object
                self.full_result_data["text_result"] = str(data)
                self._record_runtime_observation(
                    "completed",
                    message="Model response parsed as flat_markdown output.",
                    payload={
                        "result": DataFormatter.sanitize(parsed),
                        "raw_text": str(data),
                        "streamed_text": buffer,
                        "format": "flat_markdown",
                    },
                )
            else:
                self.full_result_data["parsed_result"] = None
                self.full_result_data["result_object"] = None
                self.full_result_data["text_result"] = str(data)
                self._record_runtime_observation(
                    "parse_failed",
                    level="WARNING",
                    message="Can not parse flat_markdown output from model response.",
                    payload={
                        "result": str(data),
                        "streamed_text": buffer,
                        "format": "flat_markdown",
                    },
                )
            return

        if self._prompt_object.output_format == "hybrid":
            parsed = parse_hybrid_output(str(data), self._prompt_object.output or {})
            if parsed is not None:
                result_object = self._build_result_object(parsed)
                self.full_result_data["parsed_result"] = parsed
                self.full_result_data["result_object"] = result_object
                self.full_result_data["text_result"] = str(data)
                self._record_runtime_observation(
                    "completed",
                    message="Model response parsed as hybrid output.",
                    payload={
                        "result": DataFormatter.sanitize(parsed),
                        "raw_text": str(data),
                        "streamed_text": buffer,
                        "format": "hybrid",
                    },
                )
            else:
                self.full_result_data["parsed_result"] = None
                self.full_result_data["result_object"] = None
                self.full_result_data["text_result"] = str(data)
                self._record_runtime_observation(
                    "parse_failed",
                    level="WARNING",
                    message="Can not parse hybrid output from model response.",
                    payload={
                        "result": str(data),
                        "streamed_text": buffer,
                        "format": "hybrid",
                    },
                )
            return

        if (
            isinstance(data, list)
            and isinstance(data[0], dict)
            and "object" in data[0]
            and data[0]["object"] == "embedding"
        ):
            data = [item["embedding"] for item in data]
        self.full_result_data["parsed_result"] = data
        if self.settings.get("$log.cancel_logs") is not True:
            self._record_runtime_observation(
                "completed",
                message="Model response parsing completed.",
                payload={
                    "result": DataFormatter.sanitize(data),
                    "raw_text": str(data),
                    "streamed_text": buffer,
                    "format": self._prompt_object.output_format,
                },
            )

    async def _flush_streaming_json_events(self, streaming_json_parser: StreamingJSONParser) -> AsyncGenerator[StreamingData, None]:
        if self._prompt_object.output_format != "json":
            return

        parsed_result = self.full_result_data["parsed_result"]
        if parsed_result is None:
            return

        async for streaming_data in streaming_json_parser.flush_final_data(parsed_result):
            yield streaming_data

    async def _ensure_consumer(self):
        if self._response_consumer is None:
            async with self._consumer_lock:
                if self._response_consumer is None:
                    self._response_consumer = GeneratorConsumer(self._extract())

    async def _wait_for_consumer_result(self):
        await self._ensure_consumer()
        consumer = cast(GeneratorConsumer, self._response_consumer)
        try:
            await consumer.get_result()
        except asyncio.CancelledError:
            await consumer.close()
            raise

    @staticmethod
    def _normalize_error_event(data: Any) -> Exception:
        if isinstance(data, Exception):
            return data
        return RuntimeError(str(data) if data is not None else "Model response stream emitted an error.")

    async def _extract(self):
        buffer = ""
        stream_chunk_index = 0
        try:
            async for item in self.response_generator:
                try:
                    event, data = item
                except:
                    warnings.warn(f"\n⚠️ Incorrect response data from Agently Response Generator: { item }")
                    continue
                if event == "done":
                    await self._handle_done_event(data, buffer)
                    yield event, data
                    continue
                yield event, data
                match event:
                    case "original_delta":
                        self.full_result_data["original_delta"].append(data)
                    case "delta":
                        buffer += str(data)
                        stream_chunk_index += 1
                        if self.settings.get("$log.cancel_logs") is not True:
                            self._record_runtime_observation(
                                "streaming",
                                level="DEBUG",
                                message=str(data),
                                payload={
                                    "delta": str(data),
                                    "chunk_index": stream_chunk_index,
                                },
                            )
                        elif self._streaming_canceled is False:
                            self._record_runtime_observation(
                                "streaming_canceled",
                                level="INFO",
                                message=f"Streaming logs canceled for response '{ self.response_id }'.",
                            )
                            self._streaming_canceled = True
                    case "original_done":
                        self.full_result_data["original_done"] = data
                    case "meta":
                        if isinstance(data, Mapping):
                            self.full_result_data["meta"].update(dict(data))
                            self._record_runtime_observation(
                                "meta",
                                message="Model response meta updated.",
                                payload={"meta": dict(data)},
                            )
                    case "error":
                        error = self._normalize_error_event(data)
                        self.full_result_data["errors"].append(error)
                        self._record_runtime_observation(
                            "failed",
                            level="ERROR",
                            message="Model response stream emitted an error.",
                            error=error,
                        )
                        raise error
        finally:
            if hasattr(self.response_generator, "aclose"):
                with contextlib.suppress(RuntimeError):
                    await self.response_generator.aclose()

    async def async_get_meta(self) -> "SerializableMapping":
        await self._wait_for_consumer_result()
        return self.full_result_data["meta"]

    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', "all"] | None = "parsed",
        content: Literal['original', 'parsed', "all"] | None = "parsed",
    ) -> Any:
        await self._wait_for_consumer_result()
        if type is None and content is not None:
            DeprecationWarnings.warn_deprecated_once(
                "AgentlyResponseParser.async_get_data.content",
                "Parameter `content` in method .async_get_data() is  deprecated and will be removed in future version, please use parameter `type` instead.",
                stacklevel=2,
            )
            type = content
        match type:
            case "original":
                return self.full_result_data["original_done"].copy()
            case "parsed":
                parsed = self.full_result_data["parsed_result"]
                return parsed.copy() if hasattr(parsed, "copy") else parsed  # type: ignore
            case "all":
                return self.full_result_data.copy()

    async def async_get_data_object(self) -> BaseModel | None:
        if self._prompt_object.output_format not in ("json", "flat_markdown", "hybrid"):
            raise TypeError(
                "Error: Cannot build an output model for a non-structure output.\n"
                f"Output Format: { self._prompt_object.output_format }\n"
                f"Output Prompt: { self._prompt_object.output }"
            )
        await self._wait_for_consumer_result()
        return self.full_result_data["result_object"]

    async def async_get_text(self) -> str:
        await self._wait_for_consumer_result()
        return self.full_result_data["text_result"]

    async def get_async_generator(
        self,
        type: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        content: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator:
        await self._ensure_consumer()
        consumer = cast(GeneratorConsumer, self._response_consumer)
        parsed_generator = consumer.get_async_generator()
        _streaming_parse_path_style = self.settings.get("response.streaming_parse_path_style", "dot")
        streaming_json_parser = None
        streaming_flat_markdown_parser = None
        streaming_hybrid_parser = None
        if type in ("instant", "streaming_parse"):
            if self._prompt_object.output_format == "json":
                streaming_json_parser = StreamingJSONParser(self._prompt_object.output)
            elif self._prompt_object.output_format == "flat_markdown":
                streaming_flat_markdown_parser = FlatMarkdownStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "hybrid":
                streaming_hybrid_parser = HybridStreamingParser(self._prompt_object.output or {})
        if type is None and content is not None:
            DeprecationWarnings.warn_deprecated_once(
                "AgentlyResponseParser.get_async_generator.content",
                "Parameter `content` in method .get_async_generator() is  deprecated and will be removed in future version, please use parameter `type` instead.",
                stacklevel=2,
            )
            type = content
        try:
            async for event, data in parsed_generator:
                match type:
                    case "all":
                        yield event, data
                    case "delta":
                        if event == "delta":
                            yield data
                    case "specific":
                        if specific is None:
                            specific = ["delta"]
                        elif isinstance(specific, str):
                            specific = [specific]
                        if event in specific:
                            yield event, data
                    case "instant" | "streaming_parse":
                        if streaming_json_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_json_parser.parse_chunk(str(data)):
                                    if _streaming_parse_path_style == "slash":
                                        streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                    yield streaming_data
                            if event == "tool_calls":
                                yield StreamingData(path="$tool_calls", value=data)
                            elif event == "done":
                                async for streaming_data in self._flush_streaming_json_events(streaming_json_parser):
                                    if _streaming_parse_path_style == "slash":
                                        streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                    yield streaming_data
                        if streaming_flat_markdown_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_flat_markdown_parser.parse_chunk(str(data)):
                                    if _streaming_parse_path_style == "slash":
                                        streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                    yield streaming_data
                            elif event == "done":
                                async for streaming_data in streaming_flat_markdown_parser.flush():
                                    if _streaming_parse_path_style == "slash":
                                        streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                    yield streaming_data
                        if streaming_hybrid_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_hybrid_parser.parse_chunk(str(data)):
                                    if _streaming_parse_path_style == "slash":
                                        streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                    yield streaming_data
                            elif event == "done":
                                async for streaming_data in streaming_hybrid_parser.flush():
                                    if _streaming_parse_path_style == "slash":
                                        streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                    yield streaming_data
                    case "original":
                        if event.startswith("original"):
                            yield data
        except asyncio.CancelledError:
            await consumer.close()
            raise

    def get_generator(
        self,
        type: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        content: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator:
        asyncio.run(self._ensure_consumer())
        parsed_generator = cast(GeneratorConsumer, self._response_consumer).get_generator()
        _streaming_parse_path_style = self.settings.get("response.streaming_parse_path_style", "dot")
        streaming_json_parser = None
        streaming_flat_markdown_parser = None
        streaming_hybrid_parser = None
        if type in ("instant", "streaming_parse"):
            if self._prompt_object.output_format == "json":
                streaming_json_parser = StreamingJSONParser(self._prompt_object.output)
            elif self._prompt_object.output_format == "flat_markdown":
                streaming_flat_markdown_parser = FlatMarkdownStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "hybrid":
                streaming_hybrid_parser = HybridStreamingParser(self._prompt_object.output or {})
        if type is None and content is not None:
            DeprecationWarnings.warn_deprecated_once(
                "AgentlyResponseParser.get_generator.content",
                "Parameter `content` in method .get_generator() is  deprecated and will be removed in future version, please use parameter `type` instead.",
                stacklevel=2,
            )
            type = content
        for event, data in parsed_generator:
            match type:
                case "all":
                    yield event, data
                case "delta":
                    if event == "delta":
                        yield data
                case "specific":
                    if specific is None:
                        specific = ["delta"]
                    elif isinstance(specific, str):
                        specific = [specific]
                    if event in specific:
                        yield event, data
                case "instant" | "streaming_parse":
                    if streaming_json_parser is not None:
                        if event == "delta":
                            for streaming_data in FunctionShifter.syncify_async_generator(
                                streaming_json_parser.parse_chunk(str(data))
                            ):
                                if _streaming_parse_path_style == "slash":
                                    streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                yield streaming_data
                        if event == "tool_calls":
                            yield StreamingData(path="$tool_calls", value=data)
                        elif event == "done":
                            for streaming_data in FunctionShifter.syncify_async_generator(
                                self._flush_streaming_json_events(streaming_json_parser)
                            ):
                                if _streaming_parse_path_style == "slash":
                                    streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                yield streaming_data
                    if streaming_flat_markdown_parser is not None:
                        if event == "delta":
                            for streaming_data in FunctionShifter.syncify_async_generator(
                                streaming_flat_markdown_parser.parse_chunk(str(data))
                            ):
                                if _streaming_parse_path_style == "slash":
                                    streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                yield streaming_data
                        elif event == "done":
                            for streaming_data in FunctionShifter.syncify_async_generator(
                                streaming_flat_markdown_parser.flush()
                            ):
                                if _streaming_parse_path_style == "slash":
                                    streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
                                yield streaming_data
                case "original":
                    if event.startswith("original"):
                        yield data
