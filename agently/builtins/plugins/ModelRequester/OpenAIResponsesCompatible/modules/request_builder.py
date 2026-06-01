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

import json
from typing import TYPE_CHECKING, Any, Literal, cast, get_args, get_origin

from agently.types.data import AgentlyRequestData, AgentlyRequestDataDict
from agently.utils import DataFormatter

if TYPE_CHECKING:
    from httpx import Timeout


class OpenAIResponsesCompatibleRequestBuilderMixin:
    name: str
    plugin_settings: Any
    prompt: Any

    if TYPE_CHECKING:
        def _get_http_timeout(self, *, disable_read: bool = False) -> "Timeout": ...

    @staticmethod
    def _build_simple_type_schema(type_name: str) -> dict[str, Any]:
        normalized = type_name.strip()
        scalar_mapping = {
            "str": "string",
            "string": "string",
            "int": "integer",
            "integer": "integer",
            "float": "number",
            "number": "number",
            "bool": "boolean",
            "boolean": "boolean",
            "None": "null",
            "none": "null",
        }
        if normalized in scalar_mapping:
            return {"type": scalar_mapping[normalized]}
        if normalized in {"Any", "any", "unknown", "object"}:
            return {}
        if normalized.startswith("list[") and normalized.endswith("]"):
            item_type = normalized[5:-1].strip()
            return {"type": "array", "items": OpenAIResponsesCompatibleRequestBuilderMixin._build_simple_type_schema(item_type)}
        if normalized.startswith("dict[") and normalized.endswith("]"):
            return {"type": "object"}
        if normalized.startswith("Literal[") and normalized.endswith("]"):
            literal_body = normalized[len("Literal[") : -1]
            values = [item.strip() for item in literal_body.split(",") if item.strip()]
            enum_values = []
            for value in values:
                try:
                    enum_values.append(json.loads(value))
                except Exception:
                    enum_values.append(value.strip("'\""))
            schema: dict[str, Any] = {"enum": enum_values}
            if len(enum_values) > 0:
                if all(isinstance(item, str) for item in enum_values):
                    schema["type"] = "string"
                elif all(isinstance(item, bool) for item in enum_values):
                    schema["type"] = "boolean"
                elif all(isinstance(item, int) and not isinstance(item, bool) for item in enum_values):
                    schema["type"] = "integer"
                elif all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in enum_values):
                    schema["type"] = "number"
            return schema
        if "|" in normalized:
            options = [part.strip() for part in normalized.split("|") if part.strip()]
            return {"anyOf": [OpenAIResponsesCompatibleRequestBuilderMixin._build_simple_type_schema(option) for option in options]}
        return {}

    @classmethod
    def _annotation_to_schema(cls, annotation: Any) -> dict[str, Any]:
        if annotation is None:
            return {"type": "null"}
        if isinstance(annotation, dict):
            return cls._kwargs_to_json_schema(cast(dict[str, Any], annotation))
        if isinstance(annotation, str):
            return cls._build_simple_type_schema(annotation)

        origin = get_origin(annotation)
        if origin is list:
            args = get_args(annotation)
            item_annotation = args[0] if args else Any
            return {"type": "array", "items": cls._annotation_to_schema(item_annotation)}
        if origin is dict:
            return {"type": "object"}
        if origin is Literal:
            literal_values = list(get_args(annotation))
            schema: dict[str, Any] = {"enum": literal_values}
            if len(literal_values) > 0:
                first_value = literal_values[0]
                if isinstance(first_value, str):
                    schema["type"] = "string"
                elif isinstance(first_value, bool):
                    schema["type"] = "boolean"
                elif isinstance(first_value, int) and not isinstance(first_value, bool):
                    schema["type"] = "integer"
                elif isinstance(first_value, float):
                    schema["type"] = "number"
            return schema
        if origin is not None:
            return cls._build_simple_type_schema(str(DataFormatter.sanitize(annotation)))

        if isinstance(annotation, type):
            base_mapping = {
                str: {"type": "string"},
                int: {"type": "integer"},
                float: {"type": "number"},
                bool: {"type": "boolean"},
            }
            if annotation in base_mapping:
                return base_mapping[annotation].copy()
            if hasattr(annotation, "model_json_schema"):
                try:
                    return cast(dict[str, Any], annotation.model_json_schema())
                except Exception:
                    return {"type": "object"}
        sanitized = DataFormatter.sanitize(annotation)
        if isinstance(sanitized, dict):
            return cls._kwargs_to_json_schema(cast(dict[str, Any], sanitized))
        if isinstance(sanitized, str):
            return cls._build_simple_type_schema(sanitized)
        return {}

    @classmethod
    def _kwargs_to_json_schema(cls, kwargs_schema: dict[str, Any] | None) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        additional_properties: bool | dict[str, Any] = False
        if not isinstance(kwargs_schema, dict) or len(kwargs_schema) == 0:
            return {"type": "object", "properties": {}}

        for key, raw_value in kwargs_schema.items():
            if key == "<*>":
                wildcard_value = raw_value
                wildcard_annotation = wildcard_value[0] if isinstance(wildcard_value, tuple) and wildcard_value else wildcard_value
                wildcard_schema = cls._annotation_to_schema(wildcard_annotation)
                additional_properties = wildcard_schema if len(wildcard_schema) > 0 else True
                continue

            annotation = raw_value
            description = None
            if isinstance(raw_value, tuple):
                annotation = raw_value[0] if len(raw_value) > 0 else Any
                if len(raw_value) > 1 and isinstance(raw_value[1], str) and raw_value[1]:
                    description = raw_value[1]
            schema = cls._annotation_to_schema(annotation)
            if description:
                schema = schema.copy()
                schema["description"] = description
            properties[str(key)] = schema

        result: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if additional_properties is not False:
            result["additionalProperties"] = additional_properties
        else:
            result["additionalProperties"] = False
        return result

    @classmethod
    def _tool_name(cls, tool: dict[str, Any]) -> str | None:
        name = tool.get("name", tool.get("action_id"))
        return str(name) if isinstance(name, str) and name.strip() else None
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
            parameters = cls._kwargs_to_json_schema(cast(dict[str, Any] | None, tool.get("kwargs")))
            result.append(
                {
                    "type": "function",
                    "name": name,
                    "description": str(description) if description is not None else "",
                    "parameters": parameters,
                    "strict": False,
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
            if not isinstance(tool, dict):
                continue
            tool_name = tool.get("name")
            if tool.get("type") == "function" and isinstance(tool_name, str) and tool_name:
                merged_named[tool_name] = tool
            else:
                explicit_non_named.append(tool)

        merged_tools = list(merged_named.values())
        merged_tools.extend(ordered)
        merged_tools.extend(explicit_non_named)
        return merged_tools

    @staticmethod
    def _message_text_to_content(text: Any) -> dict[str, Any]:
        return {"type": "input_text", "text": str(text)}

    @classmethod
    def _normalize_content_part(cls, part: Any) -> dict[str, Any]:
        if isinstance(part, str):
            return cls._message_text_to_content(part)
        if not isinstance(part, dict):
            return cls._message_text_to_content(part)

        part_type = part.get("type")
        if part_type == "text":
            return {"type": "input_text", "text": str(part.get("text", ""))}
        if part_type == "image_url":
            image_value = part.get("image_url")
            mapped: dict[str, Any] = {"type": "input_image"}
            if isinstance(image_value, dict):
                if "url" in image_value:
                    mapped["image_url"] = image_value["url"]
                if "detail" in image_value:
                    mapped["detail"] = image_value["detail"]
                if "file_id" in image_value:
                    mapped["file_id"] = image_value["file_id"]
            else:
                mapped["image_url"] = image_value
            return mapped
        if part_type in ("input_text", "input_image", "input_file"):
            mapped = dict(part)
            if mapped.get(str(part_type)) is None:
                del mapped[str(part_type)]
            return mapped
        raise TypeError(
            f"Plugin Name: { cls.name }\n"
            f"Error: Unsupported rich content type for Responses input: '{ part_type }'\n"
            f"Content: { part }"
        )

    def _build_input_items(self) -> list[dict[str, Any]]:
        messages = self.prompt.to_messages(
            rich_content=True,
            strict_role_orders=bool(self.plugin_settings.get("strict_role_orders", True)),
        )
        input_items: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "user"))
            raw_content = message.get("content", "")
            if isinstance(raw_content, list):
                content = [self._normalize_content_part(part) for part in raw_content]
            else:
                content = [self._normalize_content_part(raw_content)]
            input_items.append({"type": "message", "role": role, "content": content})
        return input_items

    def generate_request_data(self) -> "AgentlyRequestData":
        agently_request_dict: AgentlyRequestDataDict = {
            "client_options": {},
            "headers": {},
            "data": {},
            "request_options": {},
            "request_url": "",
        }

        agently_request_dict["data"] = {
            "input": self._build_input_items(),
        }

        headers: dict[str, str] = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("headers"),
            value_format="str",
            default_value={},
        )
        headers.update({"Connection": "close"})
        agently_request_dict["headers"] = headers

        client_options = DataFormatter.to_str_key_dict(self.plugin_settings.get("client_options"), default_value={})
        client_options.setdefault("trust_env", False)
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

        request_options.update(
            {
                "model": self.plugin_settings.get(
                    "model",
                    self.plugin_settings.get("default_model", "gpt-5.5"),
                )
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
        agently_request_dict["request_url"] = str(full_url) if isinstance(full_url, str) else f"{ base_url }/responses"

        return AgentlyRequestData(**agently_request_dict)
