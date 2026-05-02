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

import json
from typing import TYPE_CHECKING, Any, AsyncGenerator, Literal, cast
from typing_extensions import TypedDict

from httpx import AsyncClient, HTTPStatusError, RequestError
from httpx_sse import SSEError

from agently.types.data import AgentlyRequestData, SerializableValue
from agently.utils import DataFormatter, SettingsNamespace

from .OpenAIResponsesCompatible import OpenAIResponsesCompatible

if TYPE_CHECKING:
    from agently.core.Prompt import Prompt
    from agently.types.data import AgentlyRequestDataDict, AgentlyResponseGenerator
    from agently.utils import Settings


class AnthropicCompatibleSettings(TypedDict, total=False):
    model: str
    timeout_mode: Literal["http", "first_token"]
    client_options: dict[str, "SerializableValue"]
    headers: dict[str, "SerializableValue"]
    proxy: str
    request_options: dict[str, "SerializableValue"]
    base_url: str
    full_url: str
    auth: "SerializableValue"
    stream: bool
    rich_content: bool
    strict_role_orders: bool
    anthropic_version: str
    anthropic_beta: str | list[str]
    max_tokens: int


class AnthropicCompatible(OpenAIResponsesCompatible):
    name = "AnthropicCompatible"

    DEFAULT_SETTINGS = {
        "$mappings": {
            "path_mappings": {
                "AnthropicCompatible": "plugins.ModelRequester.AnthropicCompatible",
                "Anthropic": "plugins.ModelRequester.AnthropicCompatible",
                "Claude": "plugins.ModelRequester.AnthropicCompatible",
            },
        },
        "model": None,
        "default_model": "claude-sonnet-4-20250514",
        "timeout_mode": "first_token",
        "client_options": {},
        "headers": {},
        "proxy": None,
        "request_options": {},
        "base_url": "https://api.anthropic.com/v1",
        "full_url": None,
        "auth": None,
        "stream": True,
        "rich_content": True,
        "strict_role_orders": False,
        "anthropic_version": "2023-06-01",
        "anthropic_beta": None,
        "max_tokens": 4096,
        "timeout": {
            "connect": 30.0,
            "read": 600.0,
            "write": 30.0,
            "pool": 30.0,
        },
    }

    def __init__(
        self,
        prompt: "Prompt",
        settings: "Settings",
    ):
        from agently.base import event_center

        self.prompt = prompt
        self.settings = settings
        self.plugin_settings = SettingsNamespace(self.settings, f"plugins.ModelRequester.{ self.name }")
        self._emitter = event_center.create_emitter(self.name)

        if self.prompt["attachment"]:
            self.plugin_settings["rich_content"] = True

    @staticmethod
    def _content_text_block(text: Any) -> dict[str, Any]:
        return {"type": "text", "text": str(text)}

    @classmethod
    def _normalize_content_part(cls, part: Any) -> dict[str, Any]:
        if isinstance(part, str):
            return cls._content_text_block(part)
        if not isinstance(part, dict):
            return cls._content_text_block(part)

        part_type = part.get("type")
        if part_type in ("text", "input_text"):
            return {"type": "text", "text": str(part.get("text", ""))}
        if part_type == "image_url":
            image_value = part.get("image_url")
            if isinstance(image_value, dict):
                if isinstance(image_value.get("url"), str) and image_value["url"]:
                    return {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": image_value["url"],
                        },
                    }
            elif isinstance(image_value, str) and image_value:
                return {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": image_value,
                    },
                }
            raise TypeError(
                f"Plugin Name: { cls.name }\n"
                f"Error: Anthropic image_url content requires a non-empty URL.\n"
                f"Content: { part }"
            )
        if part_type == "input_image":
            image_url = part.get("image_url")
            if isinstance(image_url, str) and image_url:
                return {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": image_url,
                    },
                }
            raise TypeError(
                f"Plugin Name: { cls.name }\n"
                f"Error: Anthropic input_image content currently requires 'image_url'.\n"
                f"Content: { part }"
            )
        if part_type == "image":
            source = part.get("source")
            if isinstance(source, dict):
                return {
                    "type": "image",
                    "source": source,
                }
        if part_type == "tool_result":
            content = part.get("content", "")
            normalized_content = content
            if isinstance(content, list):
                normalized_content = [cls._normalize_content_part(item) for item in content]
            return {
                "type": "tool_result",
                "tool_use_id": str(part.get("tool_use_id", "")),
                "content": normalized_content,
                **({"is_error": bool(part.get("is_error"))} if "is_error" in part else {}),
            }
        if part_type == "tool_use":
            return {
                "type": "tool_use",
                "id": str(part.get("id", "")),
                "name": str(part.get("name", "")),
                "input": part.get("input", {}),
            }
        if part_type == "thinking":
            return {
                "type": "thinking",
                "thinking": str(part.get("thinking", "")),
                **({"signature": str(part.get("signature"))} if part.get("signature") else {}),
            }
        raise TypeError(
            f"Plugin Name: { cls.name }\n"
            f"Error: Unsupported rich content type for Anthropic Messages input: '{ part_type }'\n"
            f"Content: { part }"
        )

    @classmethod
    def _normalize_message_content(cls, raw_content: Any) -> list[dict[str, Any]] | str:
        if isinstance(raw_content, str):
            return raw_content
        if isinstance(raw_content, list):
            return [cls._normalize_content_part(part) for part in raw_content]
        if isinstance(raw_content, dict):
            return [cls._normalize_content_part(raw_content)]
        return str(raw_content)

    def _build_system_and_messages(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        messages = self.prompt.to_messages(
            rich_content=True,
            strict_role_orders=bool(self.plugin_settings.get("strict_role_orders", False)),
        )
        system_blocks: list[dict[str, Any]] = []
        anthropic_messages: list[dict[str, Any]] = []

        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "user"))
            content = self._normalize_message_content(message.get("content", ""))
            if role == "system":
                if isinstance(content, str):
                    system_blocks.append(self._content_text_block(content))
                else:
                    system_blocks.extend(content)
                continue
            if role not in ("user", "assistant"):
                role = "user"
            anthropic_messages.append({"role": role, "content": content})
        return system_blocks, anthropic_messages

    @classmethod
    def _normalize_explicit_tool(cls, tool: Any) -> dict[str, Any] | None:
        if not isinstance(tool, dict):
            return None
        if tool.get("type") == "function":
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                return None
            return {
                "name": name,
                "description": str(tool.get("description", "")),
                "input_schema": cast(dict[str, Any], tool.get("parameters", {"type": "object", "properties": {}})),
                **(
                    {"eager_input_streaming": bool(tool.get("eager_input_streaming"))}
                    if "eager_input_streaming" in tool
                    else {}
                ),
            }
        if isinstance(tool.get("name"), str) and tool["name"]:
            normalized = dict(tool)
            if "parameters" in normalized and "input_schema" not in normalized:
                normalized["input_schema"] = normalized.pop("parameters")
            return normalized
        return dict(tool)

    @classmethod
    def _convert_prompt_tools(cls, prompt_tools: Any) -> list[dict[str, Any]]:
        if not isinstance(prompt_tools, list):
            return []
        result: list[dict[str, Any]] = []
        for tool in prompt_tools:
            if not isinstance(tool, dict):
                continue
            name = cls._tool_name(tool)
            if name is None:
                continue
            description = tool.get("desc", tool.get("description", ""))
            input_schema = cls._kwargs_to_json_schema(cast(dict[str, Any] | None, tool.get("kwargs")))
            result.append(
                {
                    "name": name,
                    "description": str(description) if description is not None else "",
                    "input_schema": input_schema,
                    "eager_input_streaming": bool(tool.get("eager_input_streaming", False)),
                }
            )
        return result

    @classmethod
    def _merge_tools(cls, auto_tools: list[dict[str, Any]], explicit_tools: Any) -> list[dict[str, Any]]:
        if not isinstance(explicit_tools, list) or len(explicit_tools) == 0:
            return auto_tools

        merged_named: dict[str, dict[str, Any]] = {}
        ordered: list[dict[str, Any]] = []
        for tool in auto_tools:
            tool_name = tool.get("name")
            if isinstance(tool_name, str) and tool_name:
                merged_named[tool_name] = tool
            else:
                ordered.append(tool)

        explicit_non_named: list[dict[str, Any]] = []
        for tool in explicit_tools:
            normalized = cls._normalize_explicit_tool(tool)
            if not isinstance(normalized, dict):
                continue
            tool_name = normalized.get("name")
            if isinstance(tool_name, str) and tool_name:
                merged_named[tool_name] = normalized
            else:
                explicit_non_named.append(normalized)

        merged_tools = list(merged_named.values())
        merged_tools.extend(ordered)
        merged_tools.extend(explicit_non_named)
        return merged_tools

    def generate_request_data(self) -> "AgentlyRequestData":
        agently_request_dict: AgentlyRequestDataDict = {
            "client_options": {},
            "headers": {},
            "data": {},
            "request_options": {},
            "request_url": "",
        }

        system_blocks, anthropic_messages = self._build_system_and_messages()
        agently_request_dict["data"] = {"messages": anthropic_messages}
        if len(system_blocks) > 0:
            agently_request_dict["data"]["system"] = system_blocks

        headers: dict[str, str] = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("headers"),
            value_format="str",
            default_value={},
        )
        headers.update(
            {
                "Connection": "close",
                "anthropic-version": str(self.plugin_settings.get("anthropic_version", "2023-06-01")),
            }
        )
        anthropic_beta = self.plugin_settings.get("anthropic_beta", None)
        if isinstance(anthropic_beta, list):
            beta_items = [
                str(item).strip()
                for item in anthropic_beta
                if item is not None and str(item).strip() and str(item).strip() != "None"
            ]
            if len(beta_items) > 0:
                headers["anthropic-beta"] = ",".join(beta_items)
        elif isinstance(anthropic_beta, str) and anthropic_beta.strip() and anthropic_beta.strip() != "None":
            headers["anthropic-beta"] = anthropic_beta.strip()
        agently_request_dict["headers"] = headers

        client_options = DataFormatter.to_str_key_dict(self.plugin_settings.get("client_options"), default_value={})
        proxy = self.plugin_settings.get("proxy", None)
        if proxy:
            client_options.update({"proxy": proxy})
        client_options.update({"timeout": self._get_http_timeout()})
        agently_request_dict["client_options"] = client_options

        legacy_options = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("options"),
            value_format="serializable",
            default_value={},
        )
        request_options = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("request_options"),
            value_format="serializable",
            default_value={},
        )
        request_options = {**legacy_options, **request_options}
        request_options_in_prompt = self.prompt.get("options", {})
        if request_options_in_prompt:
            request_options.update(request_options_in_prompt)
            request_options = DataFormatter.to_str_key_dict(
                request_options,
                value_format="serializable",
                default_value={},
            )
        max_tokens_config = self.plugin_settings.get("max_tokens", 4096)
        max_tokens = int(max_tokens_config) if isinstance(max_tokens_config, (int, float, str)) else 4096

        request_options.update(
            {
                "model": self.plugin_settings.get(
                    "model",
                    self.plugin_settings.get("default_model", "claude-sonnet-4-20250514"),
                ),
                "max_tokens": max_tokens,
            }
        )

        is_stream = self.plugin_settings.get("stream")
        request_options.update({"stream": True if is_stream is None else bool(is_stream)})

        auto_tools = self._convert_prompt_tools(self.prompt.to_prompt_object().tools)
        merged_tools = self._merge_tools(auto_tools, request_options.get("tools"))
        if len(merged_tools) > 0:
            request_options["tools"] = merged_tools

        agently_request_dict["request_options"] = request_options

        full_url = self.plugin_settings.get("full_url")
        base_url = str(self.plugin_settings.get("base_url"))
        base_url = base_url[:-1] if base_url[-1] == "/" else base_url
        agently_request_dict["request_url"] = str(full_url) if isinstance(full_url, str) else f"{ base_url }/messages"

        return AgentlyRequestData(**agently_request_dict)

    def _build_headers_with_auth(self, request_data: "AgentlyRequestData") -> dict[str, Any]:
        auth = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("auth", "None"),
            value_format="serializable",
            default_key="api_key",
        )
        api_key = self.plugin_settings.get("api_key", None)
        auth_api_key = auth.get("api_key", "None")
        if api_key is not None and auth_api_key == "None":
            auth["api_key"] = str(api_key)
            auth_api_key = auth["api_key"]

        headers_with_auth = request_data.headers.copy()
        if "headers" in auth and isinstance(auth["headers"], dict):
            headers_with_auth.update(
                DataFormatter.to_str_key_dict(
                    auth["headers"],
                    value_format="str",
                    default_value={},
                )
            )
        if "body" in auth and isinstance(auth["body"], dict):
            request_data.data.update(**auth["body"])
        if auth_api_key != "None":
            headers_with_auth["x-api-key"] = str(auth_api_key)
        return headers_with_auth

    @staticmethod
    def _extract_text_from_content_blocks(content_blocks: Any) -> str:
        if not isinstance(content_blocks, list):
            return ""
        texts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        return "".join(texts)

    @staticmethod
    def _extract_reasoning_from_content_blocks(content_blocks: Any) -> str:
        if not isinstance(content_blocks, list):
            return ""
        reasoning: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                reasoning.append(str(block.get("thinking", "")))
        return "".join(reasoning)

    @staticmethod
    def _map_finish_reason(stop_reason: Any) -> str:
        if stop_reason == "tool_use":
            return "tool_calls"
        if stop_reason == "max_tokens":
            return "length"
        return "stop"

    async def request_model(self, request_data: "AgentlyRequestData") -> AsyncGenerator[tuple[str, Any], None]:
        headers_with_auth = self._build_headers_with_auth(request_data)
        full_request_data = DataFormatter.to_str_key_dict(
            request_data.data,
            value_format="serializable",
            default_value={},
        )
        full_request_data.update(request_data.request_options)

        if request_data.stream:
            client_options = request_data.client_options.copy()
            if self._should_use_first_token_timeout(request_data):
                client_options.update({"timeout": self._get_http_timeout(disable_read=True)})

            async with AsyncClient(**client_options) as client:
                client.headers.update(headers_with_auth)
                try:
                    sse_generator = await self._aiter_sse_with_retry(
                        client,
                        "POST",
                        request_data.request_url,
                        json=full_request_data,
                        headers=headers_with_auth,
                    )
                    if self._should_use_first_token_timeout(request_data):
                        sse_generator = self._aiter_with_first_token_timeout(
                            sse_generator,
                            timeout_seconds=self._get_first_token_timeout_seconds(),
                        )
                    async for sse in sse_generator:
                        yield sse.event, sse.data
                except SSEError:
                    response = await client.post(
                        request_data.request_url,
                        json=full_request_data,
                        headers=headers_with_auth,
                    )
                    if response.status_code >= 400:
                        error = RequestError(
                            f"Status Code: { response.status_code }\n"
                            f"Detail: { response.text }\n"
                            f"Request Data: { full_request_data }"
                        )
                        await self._emitter.async_error(
                            error,
                            event_type="model.requester.error",
                            payload={"request_data": full_request_data},
                        )
                        yield "error", error
                    else:
                        yield "message", response.content.decode()
                except HTTPStatusError as e:
                    await self._emitter.async_error(
                        "Error: HTTP Status Error\n"
                        f"Detail: { e.response.status_code } - { e.response.text }\n"
                        f"Request Data: { full_request_data }",
                        event_type="model.requester.error",
                        payload={"request_data": full_request_data},
                    )
                    yield "error", e
                except TimeoutError as e:
                    await self._emitter.async_error(
                        "Error: Timeout Error\n" f"Detail: { e }\n" f"Request Data: { full_request_data }",
                        event_type="model.requester.error",
                        payload={"request_data": full_request_data},
                    )
                    yield "error", e
                except RequestError as e:
                    await self._emitter.async_error(
                        "Error: Request Error\n" f"Detail: { e }\n" f"Request Data: { full_request_data }",
                        event_type="model.requester.error",
                        payload={"request_data": full_request_data},
                    )
                    yield "error", e
                except Exception as e:
                    await self._emitter.async_error(
                        "Error: Unknown Error\n" f"Detail: { e }\n" f"Request Data: { full_request_data }",
                        event_type="model.requester.error",
                        payload={"request_data": full_request_data},
                    )
                    yield "error", e
                finally:
                    await client.aclose()
            return

        async with AsyncClient(**request_data.client_options) as client:
            client.headers.update(headers_with_auth)
            try:
                response = await client.post(
                    request_data.request_url,
                    json=full_request_data,
                )
                if response.status_code >= 400:
                    error = RequestError(
                        f"Status Code: { response.status_code }\n"
                        f"Detail: { response.text }\n"
                        f"Request Data: { full_request_data }"
                    )
                    await self._emitter.async_error(
                        error,
                        event_type="model.requester.error",
                        payload={"request_data": full_request_data},
                    )
                    yield "error", error
                else:
                    yield "message", response.content.decode()
            except HTTPStatusError as e:
                await self._emitter.async_error(
                    "Error: HTTP Status Error\n"
                    f"Detail: { e.response.status_code } - { e.response.text }\n"
                    f"Request Data: { full_request_data }",
                    event_type="model.requester.error",
                    payload={"request_data": full_request_data},
                )
                yield "error", e
            except RequestError as e:
                await self._emitter.async_error(
                    "Error: Request Error\n" f"Detail: { e }\n" f"Request Data: { full_request_data }",
                    event_type="model.requester.error",
                    payload={"request_data": full_request_data},
                )
                yield "error", e
            except Exception as e:
                await self._emitter.async_error(
                    "Error: Unknown Error\n" f"Detail: { e }\n" f"Request Data: { full_request_data }",
                    event_type="model.requester.error",
                    payload={"request_data": full_request_data},
                )
                yield "error", e
            finally:
                await client.aclose()

    async def broadcast_response(self, response_generator: AsyncGenerator) -> "AgentlyResponseGenerator":
        meta: dict[str, Any] = {}
        content_buffer = ""
        reasoning_buffer = ""
        message_record: dict[str, Any] = {}
        content_blocks: dict[int, dict[str, Any]] = {}
        tool_call_states: dict[str, dict[str, Any]] = {}
        completed = False
        saw_any_event = False

        def get_tool_state(tool_use_id: str, index: int):
            return tool_call_states.setdefault(
                tool_use_id,
                self._create_tool_call_state(tool_use_id, index),
            )

        async for event, message in response_generator:
            if event == "error":
                yield "error", message
                continue

            saw_any_event = True
            if not isinstance(message, str):
                message = json.dumps(message, ensure_ascii=False)
            yield "original_delta", message
            loaded_message = json.loads(message)
            payload_type = str(loaded_message.get("type", event))

            if payload_type == "message":
                if isinstance(loaded_message, dict):
                    message_record.update(loaded_message)
                completed = True
                break

            if payload_type == "message_start":
                message_payload = loaded_message.get("message", loaded_message)
                if isinstance(message_payload, dict):
                    message_record.update(message_payload)
                    if "id" in message_payload:
                        meta["id"] = message_payload["id"]
                    if "model" in message_payload:
                        meta["model"] = message_payload["model"]
                    if "role" in message_payload:
                        meta["role"] = message_payload["role"]
                    if "usage" in message_payload:
                        meta["usage"] = message_payload["usage"]
                continue

            if payload_type == "content_block_start":
                block = loaded_message.get("content_block")
                index = loaded_message.get("index", 0)
                if not isinstance(block, dict) or not isinstance(index, int):
                    continue
                content_blocks[index] = dict(block)
                if block.get("type") == "tool_use":
                    tool_use_id = str(block.get("id", ""))
                    if tool_use_id:
                        tool_state = get_tool_state(tool_use_id, index)
                        tool_state["name"] = str(block.get("name", ""))
                        tool_state["arguments"] = ""
                continue

            if payload_type == "content_block_delta":
                index = loaded_message.get("index", 0)
                delta = loaded_message.get("delta", {})
                if not isinstance(index, int) or not isinstance(delta, dict):
                    continue
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = str(delta.get("text", ""))
                    content_buffer += text
                    block = content_blocks.setdefault(index, {"type": "text", "text": ""})
                    block["text"] = str(block.get("text", "")) + text
                    yield "delta", text
                    continue
                if delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking", ""))
                    reasoning_buffer += thinking
                    block = content_blocks.setdefault(index, {"type": "thinking", "thinking": ""})
                    block["thinking"] = str(block.get("thinking", "")) + thinking
                    yield "reasoning_delta", thinking
                    continue
                if delta_type == "signature_delta":
                    block = content_blocks.setdefault(index, {"type": "thinking"})
                    block["signature"] = str(delta.get("signature", ""))
                    continue
                if delta_type == "input_json_delta":
                    block = content_blocks.setdefault(index, {"type": "tool_use", "input": {}})
                    partial_json = str(delta.get("partial_json", ""))
                    accumulated = str(block.get("_partial_json", "")) + partial_json
                    block["_partial_json"] = accumulated
                    tool_use_id = str(block.get("id", ""))
                    if tool_use_id:
                        tool_state = get_tool_state(tool_use_id, index)
                        tool_state["name"] = str(block.get("name", tool_state.get("name", "")))
                        tool_state["arguments"] = str(tool_state.get("arguments", "")) + partial_json
                        yield "tool_calls", self._emit_tool_call_chunk(tool_state, arguments_delta=partial_json)
                    continue
                continue

            if payload_type == "content_block_stop":
                index = loaded_message.get("index", 0)
                if not isinstance(index, int):
                    continue
                block = content_blocks.get(index)
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    partial_json = block.pop("_partial_json", "")
                    if isinstance(partial_json, str) and partial_json.strip():
                        try:
                            block["input"] = json.loads(partial_json)
                        except json.JSONDecodeError:
                            block["input"] = {"raw_arguments": partial_json}
                continue

            if payload_type == "message_delta":
                delta = loaded_message.get("delta", {})
                if isinstance(delta, dict):
                    if "stop_reason" in delta:
                        message_record["stop_reason"] = delta["stop_reason"]
                    if "stop_sequence" in delta:
                        message_record["stop_sequence"] = delta["stop_sequence"]
                if isinstance(loaded_message.get("usage"), dict):
                    message_record["usage"] = loaded_message["usage"]
                continue

            if payload_type == "message_stop":
                completed = True
                break

        if not saw_any_event:
            return

        if len(content_blocks) > 0:
            message_record["content"] = [content_blocks[index] for index in sorted(content_blocks.keys())]

        if not completed and "content" not in message_record:
            message_record["content"] = []

        for index, block in enumerate(cast(list[dict[str, Any]], message_record.get("content", []))):
            if block.get("type") != "tool_use":
                continue
            tool_use_id = str(block.get("id", ""))
            if not tool_use_id:
                continue
            tool_state = get_tool_state(tool_use_id, index)
            tool_state["name"] = str(block.get("name", tool_state.get("name", "")))
            arguments = block.get("input", {})
            if isinstance(arguments, dict):
                arguments_text = json.dumps(arguments, ensure_ascii=False)
            else:
                arguments_text = str(arguments)
            if not tool_state.get("any_argument_delta_emitted"):
                yield "tool_calls", self._emit_tool_call_chunk(tool_state, arguments_delta=arguments_text)
            elif not tool_state.get("name_emitted") and tool_state.get("name"):
                yield "tool_calls", self._emit_tool_call_chunk(tool_state, arguments_delta=None, name_only=True)

        done_content = self._extract_text_from_content_blocks(message_record.get("content", []))
        if done_content == "":
            done_content = content_buffer
        reasoning_done = self._extract_reasoning_from_content_blocks(message_record.get("content", []))
        if reasoning_done == "":
            reasoning_done = reasoning_buffer

        yield "done", done_content
        yield "reasoning_done", reasoning_done
        yield "original_done", message_record

        if "id" in message_record:
            meta["id"] = message_record["id"]
        if "model" in message_record:
            meta["model"] = message_record["model"]
        if "role" in message_record:
            meta["role"] = message_record["role"]
        if "usage" in message_record:
            meta["usage"] = message_record["usage"]
        meta["finish_reason"] = self._map_finish_reason(message_record.get("stop_reason"))
        yield "meta", meta
