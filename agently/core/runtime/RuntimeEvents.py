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

from typing import Any

from agently.types.data import ErrorInfo, RuntimeEvent

__all__ = [
    "RuntimeEvent",
    "async_emit_action_flow_observation",
    "async_emit_model_requester_error",
    "async_emit_response_parser_observation",
    "async_emit_session_observation",
    "emit_session_observation",
]


def _normalize_error(error: Any) -> tuple[ErrorInfo, BaseException | None]:
    if isinstance(error, BaseException):
        return ErrorInfo.from_exception(error), error
    if isinstance(error, ErrorInfo):
        return error, None
    if isinstance(error, dict):
        try:
            return ErrorInfo.model_validate(error), None
        except Exception:
            pass
    final_error = RuntimeError(str(error))
    return ErrorInfo.from_exception(final_error), final_error


async def async_emit_model_requester_error(
    error: Any,
    *,
    source: str,
    request_data: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit the official provider request error RuntimeEvent from core."""

    from agently.base import async_emit_runtime, settings

    event_payload: dict[str, Any] = {}
    if payload:
        event_payload.update(payload)
    if request_data is not None:
        event_payload.setdefault("request_data", request_data)
    error_info, raiseable_error = _normalize_error(error)
    await async_emit_runtime(
        {
            "event_type": "model.requester.error",
            "source": source,
            "level": "ERROR",
            "message": error_info.message,
            "payload": event_payload,
            "error": error_info,
        }
    )
    if settings.get("runtime.raise_error") and raiseable_error is not None:
        raise raiseable_error


async def async_emit_response_parser_observation(
    observation: dict[str, Any],
    *,
    agent_name: str,
    response_id: str,
    run: Any = None,
) -> None:
    """Map response parser observations to official RuntimeEvent records."""

    from agently.base import async_emit_runtime

    kind = str(observation.get("kind", ""))
    event_types = {
        "completed": "model.completed",
        "parse_failed": "model.parse_failed",
        "streaming": "model.streaming",
        "streaming_canceled": "model.streaming_canceled",
        "meta": "model.meta",
        "failed": "model.failed",
    }
    event_type = event_types.get(kind)
    if event_type is None:
        return
    payload = observation.get("payload")
    resolved_payload = dict(payload) if isinstance(payload, dict) else {}
    resolved_payload.setdefault("agent_name", agent_name)
    resolved_payload.setdefault("response_id", response_id)
    await async_emit_runtime(
        {
            "event_type": event_type,
            "source": str(observation.get("source") or "AgentlyResponseParser"),
            "level": observation.get("level", "INFO"),
            "message": observation.get("message"),
            "payload": resolved_payload,
            "error": observation.get("error"),
            "run": run,
        }
    )


async def async_emit_action_flow_observation(observation: dict[str, Any]) -> None:
    """Map ActionFlow observations to official RuntimeEvent records."""

    from agently.base import async_emit_runtime
    from agently.types.data import ObservationEvent

    kind = str(observation.get("kind", ""))
    event_types = {
        "loop_started": "action.loop_started",
        "plan_ready": "action.plan_ready",
        "loop_failed": "action.loop_failed",
        "loop_completed": "action.loop_completed",
        "action_started": "action.started",
        "action_completed": "action.completed",
        "action_failed": "action.failed",
    }
    event_type = event_types.get(kind)
    if event_type is None:
        return

    payload = observation.get("payload")
    resolved_payload = dict(payload) if isinstance(payload, dict) else {}

    error = observation.get("error")
    primary_event = ObservationEvent(
        event_type=event_type,
        source=str(observation.get("source") or "ActionFlow"),
        level=observation.get("level", "INFO"),
        message=observation.get("message"),
        payload=resolved_payload,
        error=ErrorInfo.from_exception(error) if isinstance(error, BaseException) else error,
        run=observation.get("run"),
    )
    await async_emit_runtime(primary_event)

    tool_aliases = {
        "action.loop_started": "tool.loop_started",
        "action.plan_ready": "tool.plan_ready",
        "action.loop_failed": "tool.loop_failed",
        "action.loop_completed": "tool.loop_completed",
    }
    if observation.get("compat_event_family", "tool") != "tool" or event_type not in tool_aliases:
        return
    await async_emit_runtime(
        ObservationEvent(
            event_type=tool_aliases[event_type],
            source=primary_event.source,
            level=primary_event.level,
            message=observation.get("compat_message") or primary_event.message,
            payload=primary_event.payload,
            error=primary_event.error,
            run=primary_event.run,
            meta={
                "compat_event_alias": True,
                "compat_alias_for": primary_event.event_type,
                "primary_event_id": primary_event.event_id,
                "compat_family": "tool",
            },
        )
    )


def emit_session_observation(observation: dict[str, Any]) -> None:
    """Map session observations to official RuntimeEvent records."""

    from agently.base import emit_runtime

    event = _build_session_event(observation)
    if event is not None:
        emit_runtime(event)


async def async_emit_session_observation(observation: dict[str, Any]) -> None:
    """Map session observations to official async RuntimeEvent records."""

    from agently.base import async_emit_runtime

    event = _build_session_event(observation)
    if event is not None:
        await async_emit_runtime(event)


def _build_session_event(observation: dict[str, Any]):
    from agently.types.data import ObservationEvent

    kind = str(observation.get("kind", ""))
    event_types = {
        "activated": "session.activated",
        "deactivated": "session.deactivated",
        "applied_to_request": "session.applied_to_request",
        "context_appended": "session.context_appended",
    }
    event_type = event_types.get(kind)
    if event_type is None:
        return None
    payload = observation.get("payload")
    resolved_payload = dict(payload) if isinstance(payload, dict) else {}
    error = observation.get("error")
    return ObservationEvent(
        event_type=event_type,
        source=str(observation.get("source") or "SessionExtension"),
        level=observation.get("level", "INFO"),
        message=observation.get("message"),
        payload=resolved_payload,
        error=ErrorInfo.from_exception(error) if isinstance(error, BaseException) else error,
        run=observation.get("run"),
    )
