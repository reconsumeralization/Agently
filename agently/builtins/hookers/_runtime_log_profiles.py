from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from agently.types.data.event import (
    get_triggerflow_event_aliases,
    normalize_triggerflow_event_type,
)

if TYPE_CHECKING:
    from agently.types.data import RuntimeEvent
    from agently.utils import Settings


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
        return normalized
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


def resolve_runtime_log_profile(settings: "Settings", event_type: str | None) -> RuntimeLogProfile:
    family = resolve_runtime_event_family(event_type)
    key = _FAMILY_SETTINGS_KEYS[family]
    return normalize_runtime_log_profile(settings.get(key, "off"))


def is_simple_runtime_event(event: "RuntimeEvent") -> bool:
    family = resolve_runtime_event_family(event.event_type)
    if family not in _SIMPLE_EVENT_TYPES:
        return event.event_type in _RUNTIME_PRINT_EVENTS
    event_type = event.event_type
    if family == "triggerflow":
        event_type = normalize_triggerflow_event_type(event.event_type)
    return event_type in _SIMPLE_EVENT_TYPES[family]


def should_render_console_event(event: "RuntimeEvent", settings: "Settings") -> bool:
    family = resolve_runtime_event_family(event.event_type)
    if family not in _CONSOLE_EVENT_FAMILIES:
        return False
    profile = resolve_runtime_log_profile(settings, event.event_type)
    if profile == "off":
        return False
    if profile == "detail":
        return True
    return is_simple_runtime_event(event)


def should_render_storage_event(event: "RuntimeEvent", settings: "Settings") -> bool:
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
