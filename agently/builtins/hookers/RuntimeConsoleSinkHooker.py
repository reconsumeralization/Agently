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
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from agently.types.data.event import (
    get_triggerflow_event_aliases,
    normalize_triggerflow_event_type,
)
from agently.types.plugins import EventHooker
from agently.utils import DataFormatter, Settings

if TYPE_CHECKING:
    from agently.types.data import RuntimeEvent


RuntimeLogProfile: TypeAlias = Literal["off", "simple", "detail"]


_VALID_RUNTIME_LOG_PROFILES = frozenset({"off", "simple", "detail"})
_ALWAYS_VISIBLE_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
_CONSOLE_EVENT_FAMILIES = frozenset({"model", "tool", "triggerflow"})
_RUNTIME_PRINT_EVENTS = frozenset({"runtime.print"})
_SIMPLE_EVENT_TYPES = {
    "model": frozenset(
        {
            "model.requesting",
            "model.completed",
            "model.parse_failed",
            "model.retrying",
            "model.requester.error",
            "model.streaming_canceled",
        }
    ),
    "tool": frozenset(
        {
            "tool.loop_started",
            "tool.loop_completed",
            "tool.loop_failed",
        }
    ),
    "triggerflow": frozenset(
        {
            "triggerflow.execution_started",
            "triggerflow.execution_completed",
            "triggerflow.execution_failed",
            "triggerflow.execution_resumed",
            "triggerflow.interrupt_raised",
        }
    ),
}
_FAMILY_SETTINGS_KEYS = {
    "model": "runtime.show_model_logs",
    "tool": "runtime.show_tool_logs",
    "triggerflow": "runtime.show_trigger_flow_logs",
    "runtime": "runtime.show_runtime_logs",
}


def normalize_runtime_log_profile(value: Any, *, default: RuntimeLogProfile | str = "off") -> RuntimeLogProfile | str:
    if isinstance(value, bool):
        return "simple" if value else "off"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _VALID_RUNTIME_LOG_PROFILES:
            return normalized  # type: ignore[return-value]
        if normalized in {"true", "on"}:
            return "simple"
        if normalized in {"false", "none", "quiet"}:
            return "off"
        if normalized in {"summary", "verbose", "detailed"}:
            return "simple" if normalized == "summary" else "detail"
    return default


def coerce_runtime_log_profile(value: Any) -> RuntimeLogProfile:
    normalized = normalize_runtime_log_profile(value, default="")
    if normalized:
        return cast(RuntimeLogProfile, normalized)
    raise ValueError(
        '`debug` only accepts False | True | "simple" | "detail" | "off".'
    )


def resolve_runtime_event_family(event_type: str | None) -> str:
    if isinstance(event_type, str):
        if event_type.startswith("model."):
            return "model"
        if event_type.startswith("tool."):
            return "tool"
        if any(alias.startswith("triggerflow.") for alias in get_triggerflow_event_aliases(event_type)):
            return "triggerflow"
    return "runtime"


def resolve_runtime_log_profile(settings: Settings, event_type: str | None) -> RuntimeLogProfile:
    family = resolve_runtime_event_family(event_type)
    key = _FAMILY_SETTINGS_KEYS[family]
    return cast(RuntimeLogProfile, normalize_runtime_log_profile(settings.get(key, "off")))


def is_simple_runtime_event(event: "RuntimeEvent") -> bool:
    family = resolve_runtime_event_family(event.event_type)
    if family not in _SIMPLE_EVENT_TYPES:
        return event.event_type in _RUNTIME_PRINT_EVENTS
    event_type = event.event_type
    if family == "triggerflow":
        event_type = normalize_triggerflow_event_type(event.event_type)
    return event_type in _SIMPLE_EVENT_TYPES[family]


def should_render_console_event(event: "RuntimeEvent", settings: Settings) -> bool:
    family = resolve_runtime_event_family(event.event_type)
    if family not in _CONSOLE_EVENT_FAMILIES:
        return False
    profile = resolve_runtime_log_profile(settings, event.event_type)
    if profile == "off":
        return False
    if profile == "detail":
        return True
    return is_simple_runtime_event(event)


def should_render_storage_event(event: "RuntimeEvent", settings: Settings) -> bool:
    if event.event_type in _RUNTIME_PRINT_EVENTS:
        return True

    family = resolve_runtime_event_family(event.event_type)
    profile = resolve_runtime_log_profile(settings, event.event_type)

    if family in _CONSOLE_EVENT_FAMILIES:
        if profile == "off":
            return event.level in _ALWAYS_VISIBLE_LEVELS
        return False

    if event.level in _ALWAYS_VISIBLE_LEVELS:
        return True

    if profile == "detail":
        return True

    return False


COLORS = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "gray": 90,
}


def color_text(text: str, color: str | None = None, bold: bool = False, underline: bool = False) -> str:
    codes = []
    if bold:
        codes.append("1")
    if underline:
        codes.append("4")
    if color and color in COLORS:
        codes.append(str(COLORS[color]))
    if not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def _stringify_payload(payload: Any, *, indent: int | None = None) -> str:
    if payload is None:
        return ""
    sanitized = DataFormatter.sanitize(payload)
    try:
        return json.dumps(sanitized, ensure_ascii=False, indent=indent)
    except TypeError:
        return str(sanitized)


def _payload_value(event: "RuntimeEvent", key: str, default: Any = None) -> Any:
    if isinstance(event.payload, dict):
        return event.payload.get(key, default)
    return default


def _event_detail(event: "RuntimeEvent", *, pretty_payload: bool = False) -> str:
    if event.message:
        return event.message
    if event.error is not None:
        return event.error.message
    return _stringify_payload(event.payload, indent=2 if pretty_payload else None)


def _resolve_agent_name(event: "RuntimeEvent") -> str | None:
    agent_name = _payload_value(event, "agent_name")
    if isinstance(agent_name, str) and agent_name:
        return agent_name
    if event.run is not None and event.run.agent_name:
        return event.run.agent_name
    meta_agent_name = event.meta.get("agent_name")
    return str(meta_agent_name) if isinstance(meta_agent_name, str) and meta_agent_name else None


def _resolve_response_id(event: "RuntimeEvent") -> str | None:
    response_id = _payload_value(event, "response_id")
    if isinstance(response_id, str) and response_id:
        return response_id
    if event.run is not None and event.run.response_id:
        return event.run.response_id
    return None


def _resolve_execution_id(event: "RuntimeEvent") -> str | None:
    execution_id = event.meta.get("execution_id")
    if isinstance(execution_id, str) and execution_id:
        return execution_id
    if event.run is not None and event.run.execution_id:
        return event.run.execution_id
    return None


def _render_block(header: str, stage: str, detail: str, *, detail_color: str = "gray", end: str = "\n"):
    header_text = color_text(header, color="blue", bold=True)
    stage_label = color_text("Stage:", color="cyan", bold=True)
    stage_value = color_text(stage, color="yellow", underline=True)
    detail_label = color_text("Detail:", color="cyan", bold=True)
    detail_text = color_text(detail, color=detail_color)
    print(f"{header_text}\n{stage_label} {stage_value}\n{detail_label}\n{detail_text}", end=end, flush=True)


def _render_line(prefix: str, detail: str, *, color: str = "gray"):
    title = color_text(prefix, color="yellow", bold=True)
    body = color_text(detail, color=color)
    print(f"{title} {body}")


class RuntimeConsoleSinkHooker(EventHooker):
    name = "RuntimeConsoleSinkHooker"
    event_types = None

    _streaming_key: tuple[str | None, str | None] | None = None

    @staticmethod
    def _on_register():
        RuntimeConsoleSinkHooker._streaming_key = None

    @staticmethod
    def _on_unregister():
        RuntimeConsoleSinkHooker._streaming_key = None

    @staticmethod
    def _close_stream_if_needed():
        if RuntimeConsoleSinkHooker._streaming_key is not None:
            print()
            RuntimeConsoleSinkHooker._streaming_key = None

    @staticmethod
    def _handle_model_event(event: "RuntimeEvent", profile: "RuntimeLogProfile"):
        agent_name = _resolve_agent_name(event) or event.source
        response_id = _resolve_response_id(event)
        response_label = f"[Agent-{ agent_name }]"
        if response_id:
            response_label = f"{ response_label } - [Response-{ response_id }]"

        if event.event_type == "model.streaming" and profile == "detail":
            delta = _payload_value(event, "delta", event.message or "")
            if not isinstance(delta, str):
                delta = str(delta)
            stream_key = (agent_name, response_id)
            if RuntimeConsoleSinkHooker._streaming_key == stream_key:
                print(color_text(delta, color="gray"), end="", flush=True)
                return
            RuntimeConsoleSinkHooker._close_stream_if_needed()
            _render_block(response_label, "Streaming", delta, detail_color="green", end="")
            RuntimeConsoleSinkHooker._streaming_key = stream_key
            return

        RuntimeConsoleSinkHooker._close_stream_if_needed()

        stage_mapping = {
            "model.requesting": "Requesting",
            "model.completed": "Done",
            "model.parse_failed": "Parse Failed",
            "model.retrying": "Retrying",
            "model.streaming_canceled": "Streaming Canceled",
            "model.requester.error": "Requester Error",
        }
        detail = event.message or ""
        if profile == "simple":
            if event.event_type == "model.retrying":
                retry_count = _payload_value(event, "retry_count")
                retry_label = f" (retry={ retry_count })" if retry_count is not None else ""
                detail = f"{ event.message or 'Model response retrying.' }{ retry_label }"
            elif event.error is not None:
                detail = event.error.message
            else:
                detail = event.message or stage_mapping.get(event.event_type, event.event_type)
        elif event.event_type == "model.requesting":
            request_text = _payload_value(event, "request_text")
            detail = str(request_text) if request_text else _stringify_payload(_payload_value(event, "request"), indent=2)
        elif event.event_type == "model.completed":
            detail = _stringify_payload(_payload_value(event, "result"), indent=2) or detail
        elif event.event_type == "model.retrying":
            response_text = _payload_value(event, "response_text")
            retry_count = _payload_value(event, "retry_count")
            detail = f"[Response]: { response_text }\n[Retried Times]: { retry_count }"
        elif event.error is not None:
            detail = event.error.message
        if not detail:
            detail = _stringify_payload(event.payload, indent=2)
        detail_color = "red" if event.level in ("WARNING", "ERROR", "CRITICAL") else "gray"
        _render_block(response_label, stage_mapping.get(event.event_type, event.event_type), detail, detail_color=detail_color)

    @staticmethod
    def _handle_tool_event(event: "RuntimeEvent", profile: "RuntimeLogProfile"):
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        tool_name = _payload_value(event, "tool_name", "unknown")
        agent_name = _resolve_agent_name(event)
        header = f"[Tool-{ tool_name }]"
        if agent_name:
            header = f"[Agent-{ agent_name }] - { header }"
        stage = "Completed" if _payload_value(event, "success", False) else "Failed"
        if profile == "simple":
            detail = event.message or stage
        else:
            detail = _stringify_payload(event.payload, indent=2) or _event_detail(event, pretty_payload=True)
        detail_color = "gray" if stage == "Completed" else "red"
        _render_block(header, stage, detail, detail_color=detail_color)

    @staticmethod
    def _handle_trigger_flow_event(event: "RuntimeEvent", profile: "RuntimeLogProfile"):
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        execution_id = _resolve_execution_id(event)
        prefix = "[TriggerFlow]"
        if execution_id:
            prefix = f"{ prefix } [Execution-{ execution_id }]"
        detail = event.message or event.event_type if profile == "simple" else _event_detail(event, pretty_payload=True)
        _render_line(prefix, detail, color="yellow" if event.level == "DEBUG" else "gray")

    @staticmethod
    def _handle_generic_event(event: "RuntimeEvent"):
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        detail = _event_detail(event, pretty_payload=True)
        prefix = f"[{ event.source }] [{ event.event_type }]"
        color = "gray"
        if event.level in ("WARNING", "ERROR", "CRITICAL"):
            color = "red"
        elif event.level == "INFO":
            color = "green"
        _render_line(prefix, detail, color=color)

    @staticmethod
    async def handler(event: "RuntimeEvent"):
        from agently.base import settings

        if not should_render_console_event(event, settings):
            return
        profile = resolve_runtime_log_profile(settings, event.event_type)
        family = resolve_runtime_event_family(event.event_type)
        if family == "model":
            RuntimeConsoleSinkHooker._handle_model_event(event, profile)
            return
        if family == "tool":
            RuntimeConsoleSinkHooker._handle_tool_event(event, profile)
            return
        if family == "triggerflow":
            RuntimeConsoleSinkHooker._handle_trigger_flow_event(event, profile)
            return
        RuntimeConsoleSinkHooker._handle_generic_event(event)
