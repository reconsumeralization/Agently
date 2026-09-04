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
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from agently.types.data.event import (
    get_triggerflow_event_aliases,
    normalize_triggerflow_event_type,
)
from agently.types.plugins import EventHooker
from agently.utils import DataFormatter, Settings

if TYPE_CHECKING:
    from agently.types.data import ObservationEvent


RuntimeLogProfile: TypeAlias = Literal["off", "simple", "detail"]


_VALID_RUNTIME_LOG_PROFILES = frozenset({"off", "simple", "detail"})
_ALWAYS_VISIBLE_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
_VALIDATION_CONTEXT_MAX_CHARS = 500
_VALIDATION_TRACEBACK_MAX_CHARS = 2000
_VALIDATION_TRACEBACK_MAX_LINES = 8
_SIMPLE_PROMPT_MAX_CHARS = 2000
_DETAIL_PROMPT_MAX_CHARS = 16000
_SIMPLE_ACTION_PREVIEW_MAX_CHARS = 500
_CONSOLE_STREAM_BUFFER_MAX_CHARS = 65536
_CONSOLE_DEFERRED_DETAIL_MAX_CHARS = 16000
_CONSOLE_DEFERRED_TOTAL_MAX_CHARS = 131072
_CONSOLE_DEFERRED_MAX_ENTRIES = 128
_CONSOLE_EVENT_FAMILIES = frozenset({"model", "action", "triggerflow", "runtime"})
_RUNTIME_PRINT_EVENTS = frozenset({"runtime.print"})
_SIMPLE_AGENT_EXECUTION_STREAM_KINDS = frozenset(
    {
        "action_observation",
        "phase",
        "progress",
        "taskboard_control_request",
        "task_workspace_artifact_draft",
        "task_workspace_artifact_draft_public_replay_marker",
        "task_workspace_artifact_draft_retry",
    }
)
_SIMPLE_EVENT_TYPES = {
    "model": frozenset(
        {
            "model.requesting",
            "model.streaming",
            "model.completed",
            "model.failed",
            "model.parse_failed",
            "model.request_failed",
            "model.retrying",
            "model.requester.error",
            "model.streaming_canceled",
            "model.validation_error",
            "model.validation_failed",
        }
    ),
    "action": frozenset(
        {
            "action.loop_started",
            "action.loop_completed",
            "action.loop_failed",
            "action.started",
            "action.completed",
            "action.approval_required",
            "action.blocked",
            "action.failed",
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
    "runtime": frozenset(
        {
            "agent_execution.started",
            "agent_execution.completed",
            "agent_execution.failed",
            "agent_execution.cancelled",
            "agent_execution.stream",
            "execution_resource.ensuring",
            "execution_resource.probed",
            "execution_resource.progress",
            "execution_resource.ready",
            "execution_resource.unhealthy",
            "execution_resource.approval_required",
            "execution_resource.failed",
            "prompt.built",
            "runtime.print",
        }
    ),
}
_FAMILY_SETTINGS_KEYS = {
    "model": "runtime.show_model_logs",
    "action": "runtime.show_action_logs",
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
    raise ValueError('`debug` only accepts False | True | "simple" | "detail" | "off".')


def resolve_runtime_event_family(event_type: str | None) -> str:
    if isinstance(event_type, str):
        if event_type.startswith("model."):
            return "model"
        if event_type.startswith(("action.", "tool.")):
            return "action"
        if any(alias.startswith("triggerflow.") for alias in get_triggerflow_event_aliases(event_type)):
            return "triggerflow"
    return "runtime"


def _payload_value(event: "ObservationEvent", key: str, default: Any = None) -> Any:
    if isinstance(event.payload, dict):
        return event.payload.get(key, default)
    return default


def _model_request_role(event: "ObservationEvent") -> str:
    run = event.run
    if run is None or not isinstance(run.meta, Mapping):
        return ""
    return str(run.meta.get("model_request_role") or "")


def _settings_layer_value(settings: Settings, key: str) -> Any:
    value = settings.get(key, None, inherit=False)
    if value is not None:
        return value
    parent = getattr(settings, "parent", None)
    if parent is not None:
        return _settings_layer_value(parent, key)
    return None


def _resolve_action_log_setting(settings: Settings) -> Any:
    current: Settings | None = settings
    while current is not None:
        action_value = current.get("runtime.show_action_logs", None, inherit=False)
        if action_value is not None:
            return action_value
        tool_value = current.get("runtime.show_tool_logs", None, inherit=False)
        if tool_value is not None:
            return tool_value
        current = getattr(current, "parent", None)
    return "off"


def resolve_runtime_log_profile(settings: Settings, event_type: str | None) -> RuntimeLogProfile:
    family = resolve_runtime_event_family(event_type)
    if family == "action":
        return cast(RuntimeLogProfile, normalize_runtime_log_profile(_resolve_action_log_setting(settings)))
    key = _FAMILY_SETTINGS_KEYS[family]
    return cast(RuntimeLogProfile, normalize_runtime_log_profile(_settings_layer_value(settings, key)))


def is_simple_runtime_event(event: "ObservationEvent") -> bool:
    family = resolve_runtime_event_family(event.event_type)
    if family not in _SIMPLE_EVENT_TYPES:
        return event.event_type in _RUNTIME_PRINT_EVENTS
    event_type = event.event_type
    if family == "triggerflow":
        event_type = normalize_triggerflow_event_type(event.event_type)
    if event_type == "agent_execution.stream":
        stream_kind = _payload_value(event, "stream_kind")
        return isinstance(stream_kind, str) and stream_kind in _SIMPLE_AGENT_EXECUTION_STREAM_KINDS
    if event_type == "agent_execution.stream.delta":
        if _is_nested_model_stream_projection(event):
            return False
        if _payload_value(event, "stream_kind") == "progress_delta":
            return True
        if _payload_value(event, "source") == "model_request":
            if _is_nested_run_event(event):
                return False
            path = str(_payload_value(event, "path") or "")
            return "original_delta" not in path
        return False
    return event_type in _SIMPLE_EVENT_TYPES[family]


def _is_empty_reasoning_completion(event: "ObservationEvent") -> bool:
    if event.event_type != "model.reasoning.completed":
        return False
    reasoning = _payload_value(event, "reasoning")
    chunk_count = _payload_value(event, "chunk_count")
    return reasoning in (None, "") and chunk_count in (None, 0)


def _is_runtime_progress_projection(event: "ObservationEvent") -> bool:
    if event.event_type not in {"agent_execution.stream", "agent_execution.stream.delta"}:
        return False
    path = str(_payload_value(event, "path") or "")
    return _payload_value(event, "stream_kind") == "runtime_progress" or path.startswith(
        "runtime.progress."
    ) or ".runtime.progress." in path


def _is_nested_child_execution_leaf(event: "ObservationEvent") -> bool:
    if _payload_value(event, "stream_kind") != "child_execution":
        return False
    path = str(_payload_value(event, "path") or "")
    marker = ".execution."
    if marker not in path:
        return False
    relative_path = path.split(marker, 1)[1]
    if relative_path in {"route.selected", "context.package"}:
        return False
    return "." in relative_path or "[" in relative_path


def _is_detail_console_event(event: "ObservationEvent") -> bool:
    """Select human-meaningful diagnostics without changing EventCenter facts."""
    if event.event_type in {"request.started", "request.completed", "model.reasoning.delta"}:
        return False
    if event.event_type == "model.status" and _payload_value(event, "status") == "completed":
        return False
    if _is_empty_reasoning_completion(event):
        return False
    if event.event_type in {"agent_execution.stream", "agent_execution.stream.delta"}:
        if _is_runtime_progress_projection(event):
            return False
        if _payload_value(event, "stream_kind") == "heartbeat":
            return False
        if _is_nested_child_execution_leaf(event):
            return False
        if (
            event.event_type == "agent_execution.stream"
            and _payload_value(event, "path") == "result"
            and _payload_value(event, "source") == "agent_execution"
            and _payload_value(event, "route") == "model_request"
        ):
            return False
    return True


def should_render_console_event(event: "ObservationEvent", settings: Settings) -> bool:
    if (
        _is_compat_alias_event(event)
        and resolve_runtime_log_profile(settings, event.meta.get("compat_alias_for")) != "off"
    ):
        return False
    family = resolve_runtime_event_family(event.event_type)
    if family not in _CONSOLE_EVENT_FAMILIES:
        return False
    profile = resolve_runtime_log_profile(settings, event.event_type)
    if profile == "off":
        return False
    if _is_runtime_progress_projection(event):
        return False
    if event.level in _ALWAYS_VISIBLE_LEVELS:
        return True
    if (
        profile == "simple"
        and _model_request_role(event) == "action_planning"
        and (family == "model" or event.event_type == "prompt.built")
    ):
        return False
    if profile == "detail":
        return _is_detail_console_event(event)
    return is_simple_runtime_event(event)


def should_render_storage_event(event: "ObservationEvent", settings: Settings) -> bool:
    if _is_compat_alias_event(event):
        return False
    if event.event_type in _RUNTIME_PRINT_EVENTS:
        return resolve_runtime_log_profile(settings, event.event_type) == "off"

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


def _compact_single_line(text: str, *, max_chars: int = 4000) -> str:
    compacted = " ".join(text.split())
    if len(compacted) <= max_chars:
        return compacted
    return f"{compacted[: max_chars - 3]}..."


def _event_detail(event: "ObservationEvent", *, pretty_payload: bool = False) -> str:
    if event.message:
        return event.message
    if event.error is not None:
        return event.error.message
    return _stringify_payload(event.payload, indent=2 if pretty_payload else None)


def _resolve_agent_name(event: "ObservationEvent") -> str | None:
    agent_name = _payload_value(event, "agent_name")
    if isinstance(agent_name, str) and agent_name:
        return agent_name
    if event.run is not None and event.run.agent_name:
        return event.run.agent_name
    meta_agent_name = event.meta.get("agent_name")
    return str(meta_agent_name) if isinstance(meta_agent_name, str) and meta_agent_name else None


def _resolve_response_id(event: "ObservationEvent") -> str | None:
    response_id = _payload_value(event, "response_id")
    if isinstance(response_id, str) and response_id:
        return response_id
    if event.run is not None and event.run.response_id:
        return event.run.response_id
    return None


def _resolve_execution_id(event: "ObservationEvent") -> str | None:
    execution_id = event.meta.get("execution_id")
    if isinstance(execution_id, str) and execution_id:
        return execution_id
    if event.run is not None and event.run.execution_id:
        return event.run.execution_id
    return None


def _model_request_detail(event: "ObservationEvent", *, indent: int | None = None) -> str:
    request_text = _payload_value(event, "request_text")
    if isinstance(request_text, str) and request_text:
        return request_text if indent is not None else _compact_single_line(request_text)
    request = _payload_value(event, "request")
    if request is not None:
        return _stringify_payload(request, indent=indent)
    return ""


def _model_request_summary(event: "ObservationEvent") -> str:
    request = _payload_value(event, "request")
    request = request if isinstance(request, dict) else {}
    data = request.get("data")
    data = data if isinstance(data, dict) else {}
    options = request.get("request_options")
    options = options if isinstance(options, dict) else {}
    provider = _payload_value(event, "provider_family")
    model = options.get("model", data.get("model"))
    stream = request.get("stream", options.get("stream", data.get("stream")))
    endpoint = request.get("request_url")
    parts = []
    if provider not in (None, ""):
        parts.append(f"provider={provider}")
    if model not in (None, ""):
        parts.append(f"model={model}")
    if stream is not None:
        parts.append(f"stream={str(bool(stream)).lower()}")
    if endpoint not in (None, ""):
        parts.append(f"endpoint={endpoint}")
    return " ".join(parts) or event.message or "Sending model request."


def _prompt_detail(event: "ObservationEvent", profile: "RuntimeLogProfile") -> str:
    prompt_text = _payload_value(event, "prompt_text")
    if isinstance(prompt_text, str) and prompt_text:
        return _bounded_head(
            prompt_text,
            max_chars=_DETAIL_PROMPT_MAX_CHARS if profile == "detail" else _SIMPLE_PROMPT_MAX_CHARS,
        )
    prompt = _payload_value(event, "prompt")
    if prompt is not None:
        return _bounded_head(
            _stringify_payload(prompt, indent=2),
            max_chars=_DETAIL_PROMPT_MAX_CHARS if profile == "detail" else _SIMPLE_PROMPT_MAX_CHARS,
        )
    return event.message or "Prompt built."


def _model_status_detail(event: "ObservationEvent") -> str:
    status = str(_payload_value(event, "status") or "unknown")
    attempt = _payload_value(event, "attempt_index")
    retry = _payload_value(event, "retry")
    reason = _payload_value(event, "reason")
    parts = [f"status={status}"]
    if attempt is not None:
        parts.append(f"attempt={attempt}")
    if retry is not None:
        parts.append(f"retry={str(bool(retry)).lower()}")
    if reason not in (None, ""):
        parts.append(f"reason={reason}")
    return " ".join(parts)


def _model_reasoning_summary(event: "ObservationEvent") -> str:
    reasoning = _payload_value(event, "reasoning")
    chunk_count = _payload_value(event, "chunk_count")
    chars = len(reasoning) if isinstance(reasoning, str) else 0
    return f"Provider reasoning captured: chunks={chunk_count or 0} chars={chars}."


def _model_result_detail(
    event: "ObservationEvent",
    *,
    indent: int | None = None,
    full: bool = False,
) -> str:
    keys = ("result", "raw_text", "cleaned_text") if indent is not None else ("raw_text", "cleaned_text", "result")
    for key in keys:
        value = _payload_value(event, key)
        if value is None:
            continue
        if isinstance(value, str):
            return value if full or indent is not None else _compact_single_line(value)
        return _stringify_payload(value, indent=indent)
    return ""


def _action_simple_detail(event: "ObservationEvent") -> str:
    lines = [event.message] if event.message else []
    command = _payload_value(event, "command")
    command = command if isinstance(command, dict) else {}
    record = _payload_value(event, "record")
    record = record if isinstance(record, dict) else {}
    purpose = command.get("purpose", record.get("purpose"))
    if purpose in (None, "") and event.run is not None:
        purpose = event.run.meta.get("purpose")
    if purpose not in (None, ""):
        lines.append(f"Purpose: {_compact_single_line(str(purpose), max_chars=240)}")
    if event.event_type == "action.started":
        arguments = command.get("args", command.get("kwargs", command.get("arguments")))
        if arguments not in (None, "", {}, []):
            lines.append(
                "Arguments: "
                + _compact_single_line(
                    _stringify_payload(arguments),
                    max_chars=_SIMPLE_ACTION_PREVIEW_MAX_CHARS,
                )
            )
    elif event.event_type in {
        "action.completed",
        "action.failed",
        "action.blocked",
        "action.approval_required",
    }:
        result = record.get("data", record.get("result", record.get("output")))
        if result not in (None, "", {}, []):
            if isinstance(result, Mapping):
                result = {
                    str(key): value
                    for key, value in result.items()
                    if str(key) not in {"meta", "model_digest", "artifacts"}
                }
            lines.append(
                "Result: "
                + _compact_single_line(
                    _stringify_payload(result),
                    max_chars=_SIMPLE_ACTION_PREVIEW_MAX_CHARS,
                )
            )
        refs = record.get("artifact_refs")
        if refs not in (None, "", [], {}):
            lines.append(
                "Refs: "
                + _compact_single_line(
                    _stringify_payload(refs),
                    max_chars=_SIMPLE_ACTION_PREVIEW_MAX_CHARS,
                )
            )
    return "\n".join(lines) or _resolve_action_stage(event)


def _execution_resource_label(value: Any, *, provider: bool = False) -> str:
    normalized = str(value or "").strip()
    known = {
        "code_execution": "Code execution",
        "docker": "Docker",
        "gvisor": "gVisor",
        "trusted_local": "Trusted local process",
    }
    if normalized in known:
        return known[normalized]
    if not normalized:
        return "Environment"
    words = normalized.replace("_", " ").replace("-", " ")
    return words.title() if provider else words.capitalize()


def _execution_resource_image(event: "ObservationEvent") -> str:
    for key in ("image", "runtime_image", "image_preparation"):
        value = _payload_value(event, key)
        if isinstance(value, Mapping):
            value = value.get("image")
        if value not in (None, ""):
            return str(value)
    return ""


def _execution_resource_simple_detail(event: "ObservationEvent") -> str:
    provider_id = str(_payload_value(event, "provider_id") or "")
    provider = _execution_resource_label(provider_id, provider=True)
    kind_id = str(_payload_value(event, "kind") or "")
    kind = _execution_resource_label(kind_id)
    environment_name = f"{kind} environment" if kind_id else "Execution environment"
    phase = str(_payload_value(event, "phase") or "")
    image = _execution_resource_image(event)

    if event.event_type == "execution_resource.ensuring":
        purpose = f" for {kind.lower()}" if kind_id else ""
        message = f"Looking for an available environment{purpose}."
    elif event.event_type == "execution_resource.probed":
        message = f"{provider} passed the environment checks."
    elif event.event_type == "execution_resource.ready":
        message = f"{environment_name} is ready"
        if provider_id:
            message += f" with {provider}"
        if image:
            message += f" using {image}"
        message += "."
    elif event.event_type == "execution_resource.failed":
        message = f"Could not prepare the {environment_name.lower()}."
    elif phase == "image_pull_started":
        message = f"Downloading Docker image {image}. This may take a few minutes on the first run."
    elif phase == "image_pull_completed":
        message = f"Docker image {image} was downloaded successfully."
    elif phase == "image_pull_failed":
        message = f"Docker image {image} could not be downloaded."
    elif phase == "image_inspection":
        message = f"Checking whether Docker image {image} is available locally."
    elif phase == "image_ready":
        message = f"Docker image {image} is ready."
    else:
        message = event.message or "Preparing the execution environment."

    lines = [message]
    reason = _payload_value(event, "reason")
    if reason not in (None, ""):
        reason_text = str(reason)
        if " " not in reason_text:
            reason_text = reason_text.replace("_", " ")
        lines.append(f"Reason: {_compact_single_line(reason_text, max_chars=500)}")
    suggestion = _payload_value(event, "suggestion")
    if suggestion not in (None, ""):
        lines.append(f"Next step: {_compact_single_line(str(suggestion), max_chars=700)}")
    error_code = _payload_value(event, "error_code")
    if error_code not in (None, ""):
        lines.append(f"Diagnostic code: {error_code}")
    return "\n".join(lines)


def _docker_pull_progress_detail(event: "ObservationEvent") -> str | None:
    line = str(_payload_value(event, "line") or event.message or "").strip()
    if not line:
        return None
    if line.startswith("Status:") or line.startswith("docker.io/"):
        return None
    if line.startswith("Digest:"):
        return f"Verified image {line.lower()}."

    item, separator, state = line.partition(": ")
    if not separator:
        return _compact_single_line(line, max_chars=500)
    if state.startswith("Pulling from "):
        return f"Downloading from {state.removeprefix('Pulling from ')}."

    layer = item[:12]
    state_labels = {
        "Pulling fs layer": "Downloading layer",
        "Waiting": "Waiting to download layer",
        "Verifying Checksum": "Verifying layer",
        "Download complete": "Downloaded layer",
        "Extracting": "Preparing layer",
        "Pull complete": "Prepared layer",
        "Already exists": "Layer already available",
    }
    label = state_labels.get(state)
    if label:
        return f"{label} {layer}."
    return _compact_single_line(line, max_chars=500)


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _bounded_head(text: str, *, max_chars: int) -> str:
    marker = "... [truncated]"
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - len(marker)]}{marker}"


def _bounded_traceback_tail(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-_VALIDATION_TRACEBACK_MAX_LINES:])
    marker = "[truncated] ...\n"
    if len(tail) <= _VALIDATION_TRACEBACK_MAX_CHARS:
        return tail
    return f"{marker}{tail[-(_VALIDATION_TRACEBACK_MAX_CHARS - len(marker)):]}"


def _model_validation_detail(event: "ObservationEvent", profile: RuntimeLogProfile) -> str:
    validator = _payload_value(event, "validator_name")
    validator = validator.strip() if isinstance(validator, str) and validator.strip() else "unknown validator"
    reason = _payload_value(event, "reason")
    reason = (
        reason.strip() if isinstance(reason, str) and reason.strip() else (event.message or "Output validation failed.")
    )

    attempt_index = _non_negative_int(_payload_value(event, "attempt_index"))
    max_retries = _non_negative_int(_payload_value(event, "max_retries"))
    attempt = ""
    if attempt_index is not None and attempt_index > 0 and max_retries is not None:
        attempt = f" (attempt {attempt_index}/{max_retries + 1})"

    error_type = event.error.type if event.error is not None else _payload_value(event, "error_kind")
    if isinstance(error_type, str) and error_type:
        error_message = event.error.message if event.error is not None else reason
        lines = [f"{validator} raised {error_type}: {error_message}{attempt}"]
    else:
        lines = [f"{validator}: {reason}{attempt}"]

    stop = _payload_value(event, "stop") is True
    no_retry = _payload_value(event, "no_retry") is True
    if stop or no_retry:
        flags = ", ".join(flag for flag, enabled in (("stop=true", stop), ("no_retry=true", no_retry)) if enabled)
        lines.append(f"[Decision]: no retry ({flags})")

    context = _payload_value(event, "validation_payload")
    if profile == "detail" and isinstance(context, dict) and context:
        context_text = _bounded_head(_stringify_payload(context), max_chars=_VALIDATION_CONTEXT_MAX_CHARS)
        lines.append(f"[Context]: {context_text}")

    if profile == "detail" and event.error is not None and event.error.traceback:
        traceback_tail = _bounded_traceback_tail(event.error.traceback)
        if traceback_tail:
            lines.append(f"[Traceback]:\n{traceback_tail}")
    return "\n".join(lines)


def _model_retry_detail(event: "ObservationEvent", profile: RuntimeLogProfile) -> str:
    retry_count = _payload_value(event, "retry_count")
    if _payload_value(event, "retry_reason") != "validate":
        if profile == "simple":
            retry_label = f" (retry={retry_count})" if retry_count is not None else ""
            return f"{event.message or 'Model response retrying.'}{retry_label}"
        response_text = _payload_value(event, "response_text")
        return f"[Response]: {response_text}\n[Retried Times]: {retry_count}"

    attempt_index = _non_negative_int(_payload_value(event, "attempt_index"))
    next_attempt_index = _non_negative_int(_payload_value(event, "next_attempt_index"))
    if profile == "simple" and next_attempt_index is not None and next_attempt_index > 0:
        return f"Validation retry -> attempt {next_attempt_index}"
    if (
        profile == "detail"
        and attempt_index is not None
        and attempt_index > 0
        and next_attempt_index is not None
        and next_attempt_index > 0
    ):
        return f"Validation retry: attempt {attempt_index} -> {next_attempt_index}"
    return event.message or "Output validation failed. Preparing retry."


def _resolve_tool_stage(event: "ObservationEvent") -> str:
    stage_mapping = {
        "tool.loop_started": "Started",
        "tool.loop_completed": "Completed",
        "tool.loop_failed": "Failed",
        "tool.plan_ready": "Plan Ready",
    }
    if event.event_type in stage_mapping:
        return stage_mapping[event.event_type]
    success = _payload_value(event, "success", None)
    if isinstance(success, bool):
        return "Completed" if success else "Failed"
    if event.level in ("ERROR", "CRITICAL"):
        return "Failed"
    if event.level == "WARNING":
        return "Warning"
    return "Info"


def _is_compat_alias_event(event: "ObservationEvent") -> bool:
    return event.meta.get("compat_event_alias") is True


def _is_tool_loop_event(event: "ObservationEvent") -> bool:
    return event.event_type in {"tool.loop_started", "tool.loop_completed", "tool.loop_failed", "tool.plan_ready"}


def _is_action_loop_event(event: "ObservationEvent") -> bool:
    return event.event_type in {
        "action.loop_started",
        "action.loop_completed",
        "action.loop_failed",
        "action.plan_ready",
    }


def _resolve_tool_name(event: "ObservationEvent") -> str | None:
    for key in ("tool_name", "action_name"):
        value = _payload_value(event, key)
        if isinstance(value, str) and value:
            return value

    record = _payload_value(event, "record")
    if isinstance(record, dict):
        for key in ("tool_name", "action_name", "action_id"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value

    command = _payload_value(event, "command")
    if isinstance(command, dict):
        for key in ("tool_name", "action_name", "action_id"):
            value = command.get(key)
            if isinstance(value, str) and value:
                return value

    if event.run is not None:
        value = event.run.meta.get("action_name")
        if isinstance(value, str) and value:
            return value

    return None


def _resolve_action_name(event: "ObservationEvent") -> str:
    action_name = _payload_value(event, "action_name")
    if isinstance(action_name, str) and action_name:
        return action_name
    record = _payload_value(event, "record")
    if isinstance(record, dict):
        for key in ("action_name", "action_id", "tool_name"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
    command = _payload_value(event, "command")
    if isinstance(command, dict):
        for key in ("action_name", "action_id", "tool_name"):
            value = command.get(key)
            if isinstance(value, str) and value:
                return value
    if event.run is not None:
        action_name = event.run.meta.get("action_name")
        if isinstance(action_name, str) and action_name:
            return action_name
    return "unknown"


def _resolve_action_type(event: "ObservationEvent") -> str | None:
    action_type = _payload_value(event, "action_type")
    if isinstance(action_type, str) and action_type:
        return action_type
    if event.run is not None:
        action_type = event.run.meta.get("action_type")
        if isinstance(action_type, str) and action_type:
            return action_type
    return None


def _resolve_action_stage(event: "ObservationEvent") -> str:
    stage_mapping = {
        "action.loop_started": "Started",
        "action.loop_completed": "Completed",
        "action.loop_failed": "Failed",
        "action.plan_ready": "Plan Ready",
        "action.started": "Started",
        "action.completed": "Completed",
        "action.approval_required": "Approval Required",
        "action.blocked": "Blocked",
        "action.failed": "Failed",
    }
    if event.event_type in stage_mapping:
        return stage_mapping[event.event_type]
    return _resolve_tool_stage(event)


def _resolve_agent_execution_stage(event: "ObservationEvent") -> str:
    stage_mapping = {
        "agent_execution.started": "Started",
        "agent_execution.completed": "Completed",
        "agent_execution.failed": "Failed",
        "agent_execution.cancelled": "Cancelled",
        "agent_execution.stream": "Process",
        "agent_execution.stream.delta": "Streaming",
    }
    if event.event_type in stage_mapping:
        return stage_mapping[event.event_type]
    if event.level in ("ERROR", "CRITICAL"):
        return "Failed"
    if event.level == "WARNING":
        return "Warning"
    return "Info"


def _agent_execution_stream_detail(event: "ObservationEvent", profile: "RuntimeLogProfile") -> str:
    if profile == "detail":
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        diagnostic = {
            key: payload[key]
            for key in (
                "path",
                "source",
                "route",
                "stage_id",
                "task_id",
                "action_id",
                "graph_id",
                "stream_kind",
                "execution_strategy",
                "effective_execution_strategy",
                "delta",
                "value",
            )
            if payload.get(key) is not None
        }
        return _stringify_payload(diagnostic, indent=2) or _event_detail(event, pretty_payload=True)

    stream_kind = _payload_value(event, "stream_kind")
    path = _payload_value(event, "path")
    value = _payload_value(event, "value")
    delta = _payload_value(event, "delta")
    status_parts: list[str] = []
    if isinstance(stream_kind, str) and stream_kind:
        status_parts.append(f"kind={stream_kind}")
    if isinstance(path, str) and path:
        status_parts.append(f"path={path}")
    prefix = " ".join(status_parts)

    content = value if value is not None else delta
    if stream_kind == "phase" and isinstance(value, Mapping):
        diagnostics = value.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
        phase = value.get("phase") or diagnostics.get("phase")
        status = value.get("status")
        iteration = value.get("iteration")
        summary_parts = [f"phase={phase}"] if phase not in (None, "") else []
        if status not in (None, ""):
            summary_parts.append(f"status={status}")
        if iteration is not None:
            summary_parts.append(f"iteration={iteration}")
        if value.get("detail") not in (None, ""):
            summary_parts.append(_compact_single_line(str(value["detail"]), max_chars=240))
        content_text = " ".join(summary_parts) or _compact_single_line(_stringify_payload(value))
    elif stream_kind == "progress" and isinstance(value, Mapping) and value.get("message"):
        content_text = _compact_single_line(str(value["message"]))
    elif content is None:
        content_text = event.message or ""
    elif isinstance(content, str):
        content_text = _compact_single_line(content)
    else:
        content_text = _stringify_payload(content)
    if prefix and content_text:
        return f"{prefix}\n{content_text}"
    return prefix or content_text or event.message or event.event_type


def _is_model_stream_projection(event: "ObservationEvent") -> bool:
    """Identify AgentExecution records already represented by model events."""
    if event.event_type not in {"agent_execution.stream", "agent_execution.stream.delta"}:
        return False
    source = _payload_value(event, "source")
    stream_kind = _payload_value(event, "stream_kind")
    return (
        source == "model_request"
        or _is_nested_model_stream_projection(event)
        or (
            event.event_type == "agent_execution.stream.delta"
            and stream_kind == "child_execution"
            and _payload_value(event, "delta") is not None
        )
        or stream_kind == "progress_delta"
    )


def _is_nested_model_stream_projection(event: "ObservationEvent") -> bool:
    if _payload_value(event, "stream_kind") != "child_execution":
        return False
    payload_meta = _payload_value(event, "meta")
    if not isinstance(payload_meta, Mapping):
        item = _payload_value(event, "item")
        payload_meta = item.get("meta") if isinstance(item, Mapping) else None
    if not isinstance(payload_meta, Mapping):
        return False
    child_meta = payload_meta.get("child_meta")
    child_source = payload_meta.get("child_source")
    if not child_source and isinstance(child_meta, Mapping):
        child_source = child_meta.get("source")
    return child_source == "model_request"


def _is_nested_run_event(event: "ObservationEvent") -> bool:
    run = event.run
    if run is None or not run.parent_run_id:
        return False
    return bool(run.root_run_id and run.run_id != run.root_run_id)


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


@dataclass
class _BufferedModelStream:
    header: str
    detail_color: str
    chunks: list[str] = field(default_factory=list)
    buffered_chars: int = 0
    overflowed_chars: int = 0
    completed_event: Any = None

    def append(self, value: Any) -> None:
        text = value if isinstance(value, str) else str(value)
        remaining = max(0, _CONSOLE_STREAM_BUFFER_MAX_CHARS - self.buffered_chars)
        if remaining:
            retained = text[:remaining]
            self.chunks.append(retained)
            self.buffered_chars += len(retained)
        self.overflowed_chars += max(0, len(text) - remaining)

    def retained_text(self) -> str:
        return "".join(self.chunks)


@dataclass
class _DeferredConsoleEntry:
    header: str
    stage: str
    detail: str
    detail_color: str = "gray"


class RuntimeConsoleSinkHooker(EventHooker):
    name = "RuntimeConsoleSinkHooker"
    event_types = None
    delivery_policy = {
        "mode": "raw",
        "dispatch": "await",
    }

    _streaming_key: tuple[str, ...] | None = None
    _foreground_model_key: tuple[str, str] | None = None
    _opened_model_streams: set[tuple[str, str]] = set()
    _background_model_streams: OrderedDict[tuple[str, str], _BufferedModelStream] = OrderedDict()
    _final_materialization_model_streams: set[tuple[str, str]] = set()
    _streamed_model_responses: set[tuple[str, str]] = set()
    _expected_streaming_model_responses: set[tuple[str, str]] = set()
    _streamed_agent_execution_paths: set[tuple[str, str, str]] = set()
    _silent_model_stream_resumes: set[tuple[str, str]] = set()
    _deferred_console_entries: list[_DeferredConsoleEntry] = []
    _deferred_console_chars: int = 0
    _deferred_console_omitted_entries: int = 0
    _deferred_console_omitted_chars: int = 0

    @staticmethod
    def _on_register():
        RuntimeConsoleSinkHooker._streaming_key = None
        RuntimeConsoleSinkHooker._foreground_model_key = None
        RuntimeConsoleSinkHooker._opened_model_streams = set()
        RuntimeConsoleSinkHooker._background_model_streams = OrderedDict()
        RuntimeConsoleSinkHooker._final_materialization_model_streams = set()
        RuntimeConsoleSinkHooker._streamed_model_responses = set()
        RuntimeConsoleSinkHooker._expected_streaming_model_responses = set()
        RuntimeConsoleSinkHooker._streamed_agent_execution_paths = set()
        RuntimeConsoleSinkHooker._silent_model_stream_resumes = set()
        RuntimeConsoleSinkHooker._deferred_console_entries = []
        RuntimeConsoleSinkHooker._deferred_console_chars = 0
        RuntimeConsoleSinkHooker._deferred_console_omitted_entries = 0
        RuntimeConsoleSinkHooker._deferred_console_omitted_chars = 0

    @staticmethod
    def _on_unregister():
        RuntimeConsoleSinkHooker._streaming_key = None
        RuntimeConsoleSinkHooker._foreground_model_key = None
        RuntimeConsoleSinkHooker._opened_model_streams = set()
        RuntimeConsoleSinkHooker._background_model_streams = OrderedDict()
        RuntimeConsoleSinkHooker._final_materialization_model_streams = set()
        RuntimeConsoleSinkHooker._streamed_model_responses = set()
        RuntimeConsoleSinkHooker._expected_streaming_model_responses = set()
        RuntimeConsoleSinkHooker._streamed_agent_execution_paths = set()
        RuntimeConsoleSinkHooker._silent_model_stream_resumes = set()
        RuntimeConsoleSinkHooker._deferred_console_entries = []
        RuntimeConsoleSinkHooker._deferred_console_chars = 0
        RuntimeConsoleSinkHooker._deferred_console_omitted_entries = 0
        RuntimeConsoleSinkHooker._deferred_console_omitted_chars = 0

    @staticmethod
    def _close_stream_if_needed():
        if RuntimeConsoleSinkHooker._streaming_key is not None:
            print()
            RuntimeConsoleSinkHooker._streaming_key = None
        if RuntimeConsoleSinkHooker._foreground_model_key is not None:
            RuntimeConsoleSinkHooker._silent_model_stream_resumes.discard(
                RuntimeConsoleSinkHooker._foreground_model_key
            )

    @staticmethod
    def _has_active_model_streams() -> bool:
        return (
            RuntimeConsoleSinkHooker._foreground_model_key is not None
            or bool(RuntimeConsoleSinkHooker._background_model_streams)
        )

    @staticmethod
    def _is_actionable_event(event: "ObservationEvent") -> bool:
        if event.level in _ALWAYS_VISIBLE_LEVELS:
            return True
        event_type = event.event_type.lower()
        return any(
            marker in event_type
            for marker in (
                "approval_required",
                ".blocked",
                ".cancelled",
                ".canceled",
                ".failed",
                ".error",
                ".unhealthy",
                "interrupt_raised",
            )
        )

    @staticmethod
    def _is_model_request_execution_diagnostic(event: "ObservationEvent") -> bool:
        if event.event_type != "agent_execution.stream":
            return False
        path = str(_payload_value(event, "path") or "")
        route = str(_payload_value(event, "route") or "")
        return route == "model_request" or path in {"route.selected", "context.package"}

    @staticmethod
    def _defer_console_block(
        header: str,
        stage: str,
        detail: str,
        *,
        detail_color: str = "gray",
    ) -> None:
        detail_text = detail if isinstance(detail, str) else str(detail)
        original_chars = len(detail_text)
        if original_chars > _CONSOLE_DEFERRED_DETAIL_MAX_CHARS:
            omitted = original_chars - _CONSOLE_DEFERRED_DETAIL_MAX_CHARS
            detail_text = (
                detail_text[:_CONSOLE_DEFERRED_DETAIL_MAX_CHARS]
                + f"\n... [{omitted} diagnostic characters omitted by ConsoleSink]"
            )

        entry_chars = len(header) + len(stage) + len(detail_text)
        if (
            len(RuntimeConsoleSinkHooker._deferred_console_entries) >= _CONSOLE_DEFERRED_MAX_ENTRIES
            or RuntimeConsoleSinkHooker._deferred_console_chars + entry_chars
            > _CONSOLE_DEFERRED_TOTAL_MAX_CHARS
        ):
            RuntimeConsoleSinkHooker._deferred_console_omitted_entries += 1
            RuntimeConsoleSinkHooker._deferred_console_omitted_chars += original_chars
            return

        RuntimeConsoleSinkHooker._deferred_console_entries.append(
            _DeferredConsoleEntry(
                header=header,
                stage=stage,
                detail=detail_text,
                detail_color=detail_color,
            )
        )
        RuntimeConsoleSinkHooker._deferred_console_chars += entry_chars

    @staticmethod
    def _flush_deferred_console_blocks(*, force: bool = False) -> None:
        if RuntimeConsoleSinkHooker._has_active_model_streams() and not force:
            return
        entries = RuntimeConsoleSinkHooker._deferred_console_entries
        omitted_entries = RuntimeConsoleSinkHooker._deferred_console_omitted_entries
        omitted_chars = RuntimeConsoleSinkHooker._deferred_console_omitted_chars
        if not entries and not omitted_entries:
            return

        RuntimeConsoleSinkHooker._close_stream_if_needed()
        _render_line(
            "[Deferred diagnostics]",
            "Request and process details collected during streaming follow.",
        )
        for entry in entries:
            _render_block(
                f"{entry.header} [Deferred]",
                entry.stage,
                entry.detail,
                detail_color=entry.detail_color,
            )
        if omitted_entries:
            _render_line(
                "[Deferred diagnostics]",
                (
                    f"{omitted_entries} additional diagnostic event(s) "
                    f"({omitted_chars} source characters) were omitted by ConsoleSink limits."
                ),
            )

        RuntimeConsoleSinkHooker._deferred_console_entries = []
        RuntimeConsoleSinkHooker._deferred_console_chars = 0
        RuntimeConsoleSinkHooker._deferred_console_omitted_entries = 0
        RuntimeConsoleSinkHooker._deferred_console_omitted_chars = 0

    @staticmethod
    def _present_block(
        header: str,
        stage: str,
        detail: str,
        *,
        detail_color: str = "gray",
        defer: bool = False,
    ) -> None:
        if defer:
            RuntimeConsoleSinkHooker._defer_console_block(
                header,
                stage,
                detail,
                detail_color=detail_color,
            )
            return
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        _render_block(header, stage, detail, detail_color=detail_color)

    @staticmethod
    def _render_stream_delta(
        *,
        stream_key: tuple[str, ...],
        header: str,
        delta: Any,
        detail_color: str,
    ) -> None:
        delta_text = delta if isinstance(delta, str) else str(delta)
        if RuntimeConsoleSinkHooker._streaming_key == stream_key:
            print(color_text(delta_text, color=detail_color), end="", flush=True)
            return
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        _render_block(header, "Streaming", delta_text, detail_color=detail_color, end="")
        RuntimeConsoleSinkHooker._streaming_key = stream_key

    @staticmethod
    def _render_model_stream_delta(
        *,
        model_stream_key: tuple[str, str],
        header: str,
        delta: Any,
        detail_color: str,
    ) -> None:
        if model_stream_key in RuntimeConsoleSinkHooker._final_materialization_model_streams:
            return
        if RuntimeConsoleSinkHooker._foreground_model_key is None:
            RuntimeConsoleSinkHooker._foreground_model_key = model_stream_key

        if RuntimeConsoleSinkHooker._foreground_model_key != model_stream_key:
            buffered = RuntimeConsoleSinkHooker._background_model_streams.get(model_stream_key)
            if buffered is None:
                buffered = _BufferedModelStream(header=header, detail_color=detail_color)
                RuntimeConsoleSinkHooker._background_model_streams[model_stream_key] = buffered
                RuntimeConsoleSinkHooker._close_stream_if_needed()
                _render_line(
                    f"{header} [Background]",
                    "Another model response is running in the background; details will follow after streaming.",
                )
                if (
                    RuntimeConsoleSinkHooker._foreground_model_key
                    in RuntimeConsoleSinkHooker._opened_model_streams
                ):
                    RuntimeConsoleSinkHooker._silent_model_stream_resumes.add(
                        RuntimeConsoleSinkHooker._foreground_model_key
                    )
            buffered.append(delta)
            return

        stream_key = ("model", *model_stream_key)
        if model_stream_key not in RuntimeConsoleSinkHooker._opened_model_streams:
            RuntimeConsoleSinkHooker._render_stream_delta(
                stream_key=stream_key,
                header=header,
                delta=delta,
                detail_color=detail_color,
            )
            RuntimeConsoleSinkHooker._opened_model_streams.add(model_stream_key)
            return
        if RuntimeConsoleSinkHooker._streaming_key != stream_key:
            if model_stream_key in RuntimeConsoleSinkHooker._silent_model_stream_resumes:
                RuntimeConsoleSinkHooker._silent_model_stream_resumes.discard(model_stream_key)
                print(
                    color_text(delta if isinstance(delta, str) else str(delta), color=detail_color),
                    end="",
                    flush=True,
                )
                RuntimeConsoleSinkHooker._streaming_key = stream_key
                return
            RuntimeConsoleSinkHooker._close_stream_if_needed()
            continuation = color_text(f"{header} [Streaming continues]", color="yellow", bold=True)
            print(f"{continuation} ", end="", flush=True)
            RuntimeConsoleSinkHooker._streaming_key = stream_key
        print(color_text(delta if isinstance(delta, str) else str(delta), color=detail_color), end="", flush=True)

    @staticmethod
    def _promote_next_model_stream() -> None:
        while RuntimeConsoleSinkHooker._background_model_streams:
            model_stream_key, buffered = RuntimeConsoleSinkHooker._background_model_streams.popitem(last=False)
            if buffered.completed_event is not None:
                event, profile = buffered.completed_event
                RuntimeConsoleSinkHooker._render_model_event_now(
                    event,
                    profile,
                    force_completed_result=True,
                )
                RuntimeConsoleSinkHooker._streamed_model_responses.discard(model_stream_key)
                RuntimeConsoleSinkHooker._expected_streaming_model_responses.discard(model_stream_key)
                RuntimeConsoleSinkHooker._opened_model_streams.discard(model_stream_key)
                continue

            if buffered.overflowed_chars:
                RuntimeConsoleSinkHooker._foreground_model_key = model_stream_key
                RuntimeConsoleSinkHooker._final_materialization_model_streams.add(model_stream_key)
                RuntimeConsoleSinkHooker._close_stream_if_needed()
                _render_line(
                    f"{buffered.header} [Full output pending]",
                    (
                        "The live replay buffer filled while this response was in the background; "
                        "its complete result will be shown when generation finishes."
                    ),
                )
                return

            RuntimeConsoleSinkHooker._foreground_model_key = model_stream_key
            retained = buffered.retained_text()
            RuntimeConsoleSinkHooker._render_stream_delta(
                stream_key=("model", *model_stream_key),
                header=buffered.header,
                delta=retained,
                detail_color=buffered.detail_color,
            )
            RuntimeConsoleSinkHooker._opened_model_streams.add(model_stream_key)
            return
        RuntimeConsoleSinkHooker._foreground_model_key = None

    @staticmethod
    def _finish_foreground_model_stream(model_stream_key: tuple[str, str]) -> None:
        if RuntimeConsoleSinkHooker._foreground_model_key != model_stream_key:
            return
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        RuntimeConsoleSinkHooker._opened_model_streams.discard(model_stream_key)
        RuntimeConsoleSinkHooker._silent_model_stream_resumes.discard(model_stream_key)
        RuntimeConsoleSinkHooker._streamed_model_responses.discard(model_stream_key)
        RuntimeConsoleSinkHooker._expected_streaming_model_responses.discard(model_stream_key)
        RuntimeConsoleSinkHooker._final_materialization_model_streams.discard(model_stream_key)
        RuntimeConsoleSinkHooker._foreground_model_key = None
        RuntimeConsoleSinkHooker._promote_next_model_stream()
        RuntimeConsoleSinkHooker._flush_deferred_console_blocks()

    @staticmethod
    def _render_model_event_now(
        event: "ObservationEvent",
        profile: "RuntimeLogProfile",
        *,
        force_completed_result: bool = False,
        defer: bool = False,
    ) -> None:
        agent_name = _resolve_agent_name(event) or event.source
        response_id = _resolve_response_id(event)
        response_label = f"[ModelRequest] [Agent-{ agent_name }]"
        if response_id:
            response_label = f"{ response_label } - [Response-{ response_id }]"

        model_stream_key = (str(agent_name or ""), str(response_id or ""))
        if event.event_type == "model.streaming":
            delta = _payload_value(event, "delta", event.message or "")
            RuntimeConsoleSinkHooker._streamed_model_responses.add(model_stream_key)
            RuntimeConsoleSinkHooker._render_model_stream_delta(
                model_stream_key=model_stream_key,
                header=response_label,
                delta=delta,
                detail_color="green",
            )
            return

        stage_mapping = {
            "model.requesting": "Requesting",
            "model.completed": "Done",
            "model.failed": "Failed",
            "model.parse_failed": "Parse Failed",
            "model.request_failed": "Request Failed",
            "model.retrying": "Retrying",
            "model.streaming_canceled": "Streaming Canceled",
            "model.requester.error": "Requester Error",
            "model.validation_error": "Validation Error",
            "model.validation_failed": "Validation Failed",
        }
        detail = event.message or ""
        if profile == "simple":
            if event.event_type in {"model.validation_error", "model.validation_failed"}:
                detail = _model_validation_detail(event, profile)
            elif event.event_type == "model.retrying":
                detail = _model_retry_detail(event, profile)
            elif event.event_type == "model.requesting":
                detail = _model_request_summary(event)
                request = _payload_value(event, "request")
                if isinstance(request, Mapping) and request.get("stream") is True:
                    RuntimeConsoleSinkHooker._expected_streaming_model_responses.add(model_stream_key)
            elif event.event_type == "model.completed":
                if force_completed_result:
                    detail = (
                        _model_result_detail(event, full=True)
                        or event.message
                        or stage_mapping.get(event.event_type, event.event_type)
                    )
                elif model_stream_key in RuntimeConsoleSinkHooker._streamed_model_responses:
                    detail = "Model response completed."
                else:
                    detail = (
                        _model_result_detail(event, full=True)
                        or event.message
                        or stage_mapping.get(event.event_type, event.event_type)
                    )
            elif event.error is not None:
                detail = event.error.message
            else:
                detail = event.message or stage_mapping.get(event.event_type, event.event_type)
        elif event.event_type == "model.requesting":
            detail = _model_request_detail(event, indent=2)
        elif event.event_type == "model.completed":
            detail = _model_result_detail(event, indent=2) or detail
        elif event.event_type in {"model.validation_error", "model.validation_failed"}:
            detail = _model_validation_detail(event, profile)
        elif event.event_type == "model.retrying":
            detail = _model_retry_detail(event, profile)
        elif event.event_type == "model.status":
            detail = _model_status_detail(event)
        elif event.event_type == "model.reasoning.completed":
            detail = _model_reasoning_summary(event)
        elif event.event_type == "model.meta":
            detail = _stringify_payload(event.payload, indent=2)
        elif event.error is not None:
            detail = event.error.message
        if not detail:
            detail = _stringify_payload(event.payload, indent=2)
        detail_color = "red" if event.level in ("WARNING", "ERROR", "CRITICAL") else "gray"
        RuntimeConsoleSinkHooker._present_block(
            response_label,
            stage_mapping.get(event.event_type, event.event_type),
            detail,
            detail_color=detail_color,
            defer=defer,
        )
        if event.event_type == "model.completed":
            RuntimeConsoleSinkHooker._streamed_model_responses.discard(model_stream_key)
            RuntimeConsoleSinkHooker._expected_streaming_model_responses.discard(model_stream_key)

    @staticmethod
    def _handle_model_event(event: "ObservationEvent", profile: "RuntimeLogProfile") -> None:
        agent_name = _resolve_agent_name(event) or event.source
        response_id = _resolve_response_id(event)
        model_stream_key = (str(agent_name or ""), str(response_id or ""))

        if event.event_type == "model.streaming":
            RuntimeConsoleSinkHooker._render_model_event_now(event, profile)
            return

        request = _payload_value(event, "request")
        expects_stream = (
            event.event_type == "model.requesting"
            and isinstance(request, Mapping)
            and request.get("stream") is True
        )
        if expects_stream:
            RuntimeConsoleSinkHooker._expected_streaming_model_responses.add(model_stream_key)

        background = RuntimeConsoleSinkHooker._background_model_streams.get(model_stream_key)
        status = str(_payload_value(event, "status") or "").lower()
        terminal_failure = event.event_type in {
            "model.failed",
            "model.request_failed",
            "model.streaming_canceled",
        } or (event.event_type == "model.status" and status in {"cancelled", "failed"})
        if background is not None:
            if terminal_failure or event.level in {"WARNING", "ERROR", "CRITICAL"}:
                RuntimeConsoleSinkHooker._render_model_event_now(event, profile)
                if terminal_failure:
                    RuntimeConsoleSinkHooker._background_model_streams.pop(model_stream_key, None)
                    RuntimeConsoleSinkHooker._opened_model_streams.discard(model_stream_key)
                    RuntimeConsoleSinkHooker._streamed_model_responses.discard(model_stream_key)
                    RuntimeConsoleSinkHooker._expected_streaming_model_responses.discard(model_stream_key)
                    RuntimeConsoleSinkHooker._final_materialization_model_streams.discard(model_stream_key)
                return
            if event.event_type == "model.completed":
                background.completed_event = (event, profile)
            else:
                RuntimeConsoleSinkHooker._render_model_event_now(event, profile, defer=True)
            return

        is_foreground = RuntimeConsoleSinkHooker._foreground_model_key == model_stream_key
        actionable = RuntimeConsoleSinkHooker._is_actionable_event(event) or terminal_failure
        defer = (
            not actionable
            and event.event_type != "model.completed"
            and (
                RuntimeConsoleSinkHooker._has_active_model_streams()
                or (
                    profile == "detail"
                    and event.event_type in {"model.request_started", "model.requesting"}
                    and (event.event_type == "model.request_started" or expects_stream)
                )
            )
        )
        if (
            profile == "detail"
            and event.event_type == "model.requesting"
            and not expects_stream
            and not RuntimeConsoleSinkHooker._has_active_model_streams()
        ):
            RuntimeConsoleSinkHooker._flush_deferred_console_blocks(force=True)
        RuntimeConsoleSinkHooker._render_model_event_now(
            event,
            profile,
            force_completed_result=(
                event.event_type == "model.completed"
                and model_stream_key in RuntimeConsoleSinkHooker._final_materialization_model_streams
            ),
            defer=defer,
        )
        if is_foreground and (event.event_type == "model.completed" or terminal_failure):
            RuntimeConsoleSinkHooker._finish_foreground_model_stream(model_stream_key)
        elif (
            (event.event_type == "model.completed" or terminal_failure)
            and not RuntimeConsoleSinkHooker._has_active_model_streams()
        ):
            RuntimeConsoleSinkHooker._flush_deferred_console_blocks()

    @staticmethod
    def _handle_agent_execution_event(
        event: "ObservationEvent",
        profile: "RuntimeLogProfile",
        *,
        model_profile: "RuntimeLogProfile | None" = None,
    ):
        effective_model_profile = profile if model_profile is None else model_profile
        if _is_model_stream_projection(event):
            if effective_model_profile == "detail":
                return
            if (
                effective_model_profile == "simple"
                and _payload_value(event, "stream_kind") != "progress_delta"
            ):
                return
        execution_id = _resolve_execution_id(event)
        prefix = "[AgentExecution]"
        if execution_id:
            prefix = f"{ prefix } [Execution-{ execution_id }]"
        stage = _resolve_agent_execution_stage(event)
        if event.event_type == "agent_execution.stream.delta":
            path = _payload_value(event, "path")
            delta = _payload_value(event, "delta")
            if delta is not None:
                if _payload_value(event, "stream_kind") == "progress_delta":
                    RuntimeConsoleSinkHooker._streamed_agent_execution_paths.add(
                        ("agent_execution", str(execution_id or ""), str(path or ""))
                    )
                RuntimeConsoleSinkHooker._render_stream_delta(
                    stream_key=("agent_execution", str(execution_id or ""), str(path or "")),
                    header=prefix,
                    delta=delta,
                    detail_color="green" if profile == "detail" else "gray",
                )
                return
            stage = "Process"

        if profile == "simple" and event.event_type == "agent_execution.stream":
            path = _payload_value(event, "path")
            completed_progress_key = (
                "agent_execution",
                str(execution_id or ""),
                f"{path}.message",
            )
            if (
                _payload_value(event, "stream_kind") == "progress"
                and completed_progress_key in RuntimeConsoleSinkHooker._streamed_agent_execution_paths
            ):
                RuntimeConsoleSinkHooker._close_stream_if_needed()
                RuntimeConsoleSinkHooker._streamed_agent_execution_paths.discard(completed_progress_key)
                return

        if event.event_type in {"agent_execution.stream", "agent_execution.stream.delta"}:
            detail = _agent_execution_stream_detail(event, profile)
        else:
            detail = (event.message or stage) if profile == "simple" else _event_detail(event, pretty_payload=True)
        detail_color = "red" if stage in ("Failed", "Warning") else "gray"
        defer = (
            not RuntimeConsoleSinkHooker._is_actionable_event(event)
            and (
                RuntimeConsoleSinkHooker._has_active_model_streams()
                or (
                    profile == "detail"
                    and RuntimeConsoleSinkHooker._is_model_request_execution_diagnostic(event)
                )
            )
        )
        RuntimeConsoleSinkHooker._present_block(
            prefix,
            stage,
            detail,
            detail_color=detail_color,
            defer=defer,
        )
        if (
            event.event_type
            in {"agent_execution.completed", "agent_execution.failed", "agent_execution.cancelled"}
            and not RuntimeConsoleSinkHooker._has_active_model_streams()
        ):
            RuntimeConsoleSinkHooker._flush_deferred_console_blocks()

    @staticmethod
    def _handle_tool_event(event: "ObservationEvent", profile: "RuntimeLogProfile"):
        agent_name = _resolve_agent_name(event)
        tool_name = _resolve_tool_name(event)
        header = "[ToolLoop]" if _is_tool_loop_event(event) else f"[Tool-{ tool_name or 'unknown' }]"
        if agent_name:
            header = f"[Agent-{ agent_name }] - { header }"
        stage = _resolve_tool_stage(event)
        if profile == "simple":
            detail = _action_simple_detail(event)
        else:
            detail = _stringify_payload(event.payload, indent=2) or _event_detail(event, pretty_payload=True)
        detail_color = "red" if stage in ("Failed", "Warning") else "gray"
        RuntimeConsoleSinkHooker._present_block(
            header,
            stage,
            detail,
            detail_color=detail_color,
            defer=(
                RuntimeConsoleSinkHooker._has_active_model_streams()
                and not RuntimeConsoleSinkHooker._is_actionable_event(event)
            ),
        )

    @staticmethod
    def _handle_action_event(event: "ObservationEvent", profile: "RuntimeLogProfile"):
        action_name = _resolve_action_name(event)
        action_type = _resolve_action_type(event)
        agent_name = _resolve_agent_name(event)
        header = "[ActionLoop]" if _is_action_loop_event(event) else f"[Action-{ action_name }]"
        if action_type and not _is_action_loop_event(event):
            header = f"{ header } [type={ action_type }]"
        if agent_name:
            header = f"[Agent-{ agent_name }] - { header }"
        stage = _resolve_action_stage(event)
        if profile == "simple":
            detail = _action_simple_detail(event)
        else:
            detail = _stringify_payload(event.payload, indent=2) or _event_detail(event, pretty_payload=True)
        detail_color = "red" if stage in ("Failed", "Warning") else "gray"
        RuntimeConsoleSinkHooker._present_block(
            header,
            stage,
            detail,
            detail_color=detail_color,
            defer=(
                RuntimeConsoleSinkHooker._has_active_model_streams()
                and not RuntimeConsoleSinkHooker._is_actionable_event(event)
            ),
        )

    @staticmethod
    def _handle_trigger_flow_event(event: "ObservationEvent", profile: "RuntimeLogProfile"):
        execution_id = _resolve_execution_id(event)
        prefix = "[TriggerFlow]"
        if execution_id:
            prefix = f"{ prefix } [Execution-{ execution_id }]"
        detail = event.message or event.event_type if profile == "simple" else _event_detail(event, pretty_payload=True)
        color = (
            "red" if event.level in ("WARNING", "ERROR", "CRITICAL") else "yellow" if event.level == "DEBUG" else "gray"
        )
        if (
            RuntimeConsoleSinkHooker._has_active_model_streams()
            and not RuntimeConsoleSinkHooker._is_actionable_event(event)
        ):
            RuntimeConsoleSinkHooker._defer_console_block(prefix, event.event_type, detail, detail_color=color)
            return
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        _render_line(prefix, detail, color=color)

    @staticmethod
    def _handle_execution_resource_event(event: "ObservationEvent", profile: "RuntimeLogProfile"):
        provider_id = str(_payload_value(event, "provider_id") or "")
        kind = str(_payload_value(event, "kind") or "")
        phase = str(_payload_value(event, "phase") or "")
        image = _execution_resource_image(event)
        if phase.startswith("image_"):
            label = f"Docker image {image}" if image else "Docker image"
        elif provider_id:
            label = _execution_resource_label(provider_id, provider=True)
        else:
            label = _execution_resource_label(kind)
        header = f"[Environment] [{label}]"
        stage_mapping = {
            "execution_resource.ensuring": "Checking",
            "execution_resource.probed": "Available",
            "execution_resource.progress": "Preparing",
            "execution_resource.ready": "Ready",
            "execution_resource.unhealthy": "Unhealthy",
            "execution_resource.approval_required": "Approval Required",
            "execution_resource.failed": "Failed",
        }
        stage = stage_mapping.get(event.event_type, event.event_type)
        if phase == "image_pull_started":
            stage = "Downloading"
        elif phase == "image_pull_completed":
            stage = "Downloaded"
        elif phase == "image_pull_failed":
            stage = "Download Failed"
        elif phase == "image_inspection":
            stage = "Checking Image"
        elif phase == "image_ready":
            stage = "Image Ready"

        if profile == "simple" and phase in {"image_inspection", "image_ready"}:
            return
        if profile == "simple" and phase == "image_pull_progress":
            detail = _docker_pull_progress_detail(event)
            if detail:
                if RuntimeConsoleSinkHooker._has_active_model_streams():
                    RuntimeConsoleSinkHooker._defer_console_block(header, stage, detail)
                else:
                    RuntimeConsoleSinkHooker._close_stream_if_needed()
                    _render_line(header, detail)
            return

        readable_detail = _execution_resource_simple_detail(event)
        if profile == "detail":
            diagnostics = _stringify_payload(event.payload, indent=2)
            detail = readable_detail
            if diagnostics:
                detail = f"{detail}\nDiagnostics:\n{diagnostics}"
        else:
            detail = readable_detail
        detail_color = "red" if event.level in ("WARNING", "ERROR", "CRITICAL") else "gray"
        RuntimeConsoleSinkHooker._present_block(
            header,
            stage,
            detail,
            detail_color=detail_color,
            defer=(
                RuntimeConsoleSinkHooker._has_active_model_streams()
                and not RuntimeConsoleSinkHooker._is_actionable_event(event)
            ),
        )

    @staticmethod
    def _handle_generic_event(event: "ObservationEvent", profile: "RuntimeLogProfile"):
        if event.event_type == "prompt.built":
            agent_name = _resolve_agent_name(event) or event.source
            response_id = _resolve_response_id(event)
            header = f"[ModelRequest] [Agent-{agent_name}]"
            if response_id:
                header = f"{header} - [Response-{response_id}]"
            RuntimeConsoleSinkHooker._present_block(
                header,
                "Prompt",
                _prompt_detail(event, profile),
                defer=(profile == "detail" or RuntimeConsoleSinkHooker._has_active_model_streams()),
            )
            return
        detail = _event_detail(event, pretty_payload=True)
        prefix = f"[{ event.source }] [{ event.event_type }]"
        color = "gray"
        if event.level in ("WARNING", "ERROR", "CRITICAL"):
            color = "red"
        elif event.level == "INFO":
            color = "green"
        if (
            RuntimeConsoleSinkHooker._has_active_model_streams()
            and not RuntimeConsoleSinkHooker._is_actionable_event(event)
        ):
            RuntimeConsoleSinkHooker._defer_console_block(prefix, event.event_type, detail, detail_color=color)
            return
        RuntimeConsoleSinkHooker._close_stream_if_needed()
        _render_line(prefix, detail, color=color)

    @staticmethod
    async def handler(event: "ObservationEvent"):
        from agently.base import settings
        from agently.core.runtime.RuntimeContext import get_current_settings

        current_settings = get_current_settings()
        active_settings = current_settings if current_settings is not None else settings
        if not should_render_console_event(event, active_settings):
            return
        profile = resolve_runtime_log_profile(active_settings, event.event_type)
        family = resolve_runtime_event_family(event.event_type)
        if family == "model":
            RuntimeConsoleSinkHooker._handle_model_event(event, profile)
            return
        if family == "triggerflow":
            RuntimeConsoleSinkHooker._handle_trigger_flow_event(event, profile)
            return
        if event.event_type.startswith("agent_execution."):
            RuntimeConsoleSinkHooker._handle_agent_execution_event(
                event,
                profile,
                model_profile=resolve_runtime_log_profile(active_settings, "model.streaming"),
            )
            return
        if event.event_type.startswith("execution_resource."):
            RuntimeConsoleSinkHooker._handle_execution_resource_event(event, profile)
            return
        if event.event_type.startswith("action."):
            RuntimeConsoleSinkHooker._handle_action_event(event, profile)
            return
        if event.event_type.startswith("tool."):
            RuntimeConsoleSinkHooker._handle_tool_event(event, profile)
            return
        RuntimeConsoleSinkHooker._handle_generic_event(event, profile)
