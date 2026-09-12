from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agently import Agently
from agently.core import ModelRequest, PluginManager
from agently.core.model.OutputObservationPolicy import OutputObservationPolicy
from agently.types.data import AgentlyRequestData
from agently.utils import Settings


class _SensitiveOutputRequester:
    name = "SensitiveOutputRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    outputs: list[str] = []
    attempts = 0

    def __init__(self, prompt: Any, settings: Any) -> None:
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls, outputs: list[str]) -> None:
        cls.outputs = list(outputs)
        cls.attempts = 0

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    def generate_request_data(self) -> AgentlyRequestData:
        type(self).attempts += 1
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"attempt": type(self).attempts},
            request_options={"stream": True},
            request_url="mock://sensitive-output",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        attempt = int(request_data.data.get("attempt", 1))
        yield "message", type(self).outputs[attempt - 1]

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        midpoint = max(1, len(response_text) // 2)
        for chunk in (response_text[:midpoint], response_text[midpoint:]):
            if chunk:
                yield "delta", chunk
                await asyncio.sleep(0)
        yield "done", response_text


class _SensitiveReasoningRequester(_SensitiveOutputRequester):
    name = "SensitiveReasoningRequester"
    reasoning = ""

    @classmethod
    def reset(cls, outputs: list[str], *, reasoning: str) -> None:
        super().reset(outputs)
        cls.reasoning = reasoning

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        midpoint = max(1, len(type(self).reasoning) // 2)
        yield "reasoning_delta", type(self).reasoning[:midpoint]
        yield "reasoning_delta", type(self).reasoning[midpoint:]
        yield "reasoning_done", type(self).reasoning
        yield "delta", response_text
        yield "done", response_text


def _create_request(
    requester_cls: type[_SensitiveOutputRequester] = _SensitiveOutputRequester,
) -> ModelRequest:
    settings = Settings(name="SensitiveOutputSettings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="SensitiveOutputPluginManager",
    )
    plugin_manager.register(
        "ModelRequester",
        requester_cls,
        activate=True,
    )
    return ModelRequest(
        plugin_manager,
        agent_name="sensitive-output-agent",
        parent_settings=settings,
    )


def _event_text(event: Any) -> str:
    return json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


@pytest.mark.asyncio
async def test_sensitive_output_policy_redacts_streaming_and_completed_events() -> None:
    marker = "SECRET_PROGRAM_SUCCESS_MARKER"
    _SensitiveOutputRequester.reset(
        [
            json.dumps(
                {
                    "next_action": "execute",
                    "description": "safe decision",
                    "program": f"return {marker!r}",
                }
            )
        ]
    )
    request = _create_request()
    request._set_output_observation_policy(sensitive_paths=["program"])
    request.output(
        {
            "next_action": (str,),
            "description": (str,),
            "program": (str,),
        },
        format="json",
    )
    captured: list[Any] = []
    hook_name = f"sensitive-output-success-{uuid.uuid4().hex}"
    Agently.event_center.register_hook(
        lambda event: captured.append(event),
        hook_name=hook_name,
    )
    try:
        data = await request.async_get_data()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert marker in data["program"]
    output_events = [event for event in captured if event.event_type in {"model.streaming", "model.completed"}]
    assert output_events
    assert all(marker not in _event_text(event) for event in output_events)
    streaming_events = [event for event in output_events if event.event_type == "model.streaming"]
    assert streaming_events
    assert all(event.payload["delta"]["redacted"] is True for event in streaming_events)
    completed = next(event for event in output_events if event.event_type == "model.completed")
    assert completed.payload["result"]["next_action"] == "execute"
    assert completed.payload["result"]["description"] == "safe decision"
    assert completed.payload["result"]["program"]["redacted"] is True
    for field in ("raw_text", "cleaned_text", "streamed_text"):
        assert completed.payload[field]["redacted"] is True
    assert completed.payload["estimated_output_chars"] > 0
    assert completed.payload["estimated_output_source"] == ("sensitive_output_observation.raw_text")


@pytest.mark.asyncio
async def test_sensitive_output_policy_redacts_validation_and_retry_events() -> None:
    rejected_marker = "SECRET_PROGRAM_REJECTED_MARKER"
    accepted_marker = "SECRET_PROGRAM_ACCEPTED_MARKER"
    _SensitiveOutputRequester.reset(
        [
            json.dumps(
                {
                    "description": "draft",
                    "program": f"return {rejected_marker!r}",
                }
            ),
            json.dumps(
                {
                    "description": "accepted",
                    "program": f"return {accepted_marker!r}",
                }
            ),
        ]
    )
    request = _create_request()
    request._set_output_observation_policy(sensitive_paths=["program"])
    request.output(
        {"description": (str,), "program": (str,)},
        format="json",
    ).validate(lambda result, _context: result["description"] == "accepted")
    captured: list[Any] = []
    hook_name = f"sensitive-output-retry-{uuid.uuid4().hex}"
    Agently.event_center.register_hook(
        lambda event: captured.append(event),
        hook_name=hook_name,
    )
    try:
        data = await request.async_get_data(max_retries=1)
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert accepted_marker in data["program"]
    observed = [
        event
        for event in captured
        if event.event_type in {"model.validation_failed", "model.retrying", "model.completed"}
    ]
    assert observed
    assert all(rejected_marker not in _event_text(event) for event in observed)
    assert all(accepted_marker not in _event_text(event) for event in observed)
    validation = next(event for event in observed if event.event_type == "model.validation_failed")
    retrying = next(event for event in observed if event.event_type == "model.retrying")
    assert validation.payload["response_text"]["redacted"] is True
    assert validation.payload["reason"]["redacted"] is True
    assert retrying.payload["response_text"]["redacted"] is True
    assert retrying.payload["validation_reason"]["redacted"] is True


@pytest.mark.asyncio
async def test_sensitive_output_policy_redacts_parse_failed_events() -> None:
    marker = "SECRET_PROGRAM_PARSE_FAILURE_MARKER"
    _SensitiveOutputRequester.reset([f"not-json {marker}"])
    request = _create_request()
    request._set_output_observation_policy(sensitive_paths=["program"])
    request.output({"program": (str,)}, format="json")
    captured: list[Any] = []
    hook_name = f"sensitive-output-parse-{uuid.uuid4().hex}"
    Agently.event_center.register_hook(
        lambda event: captured.append(event),
        hook_name=hook_name,
    )
    try:
        text = await request.async_get_text()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert marker in text
    parse_failed = next(event for event in captured if event.event_type == "model.parse_failed")
    assert marker not in _event_text(parse_failed)
    assert parse_failed.payload["result"]["redacted"] is True
    assert parse_failed.payload["streamed_text"]["redacted"] is True


def test_sensitive_output_policy_validates_and_normalizes_paths() -> None:
    request = _create_request()

    request._set_output_observation_policy(sensitive_paths=["program", "$.program", "/nested/token"])

    assert request.settings.get("model_request.output_observation.sensitive_paths") == ["program", "nested.token"]
    with pytest.raises(TypeError):
        request._set_output_observation_policy(sensitive_paths="program")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        request._set_output_observation_policy(sensitive_paths=[""])


def test_sensitive_output_policy_redacts_reasoning_observations() -> None:
    marker = "SECRET_PROGRAM_REASONING_MARKER"
    policy = OutputObservationPolicy(("program",))

    delta = policy.project_parser_observation(
        {
            "kind": "reasoning_delta",
            "message": marker,
            "payload": {"delta": marker},
        }
    )
    completed = policy.project_parser_observation(
        {
            "kind": "reasoning_completed",
            "message": "reasoning completed",
            "payload": {"reasoning": marker},
        }
    )

    assert marker not in json.dumps(delta, ensure_ascii=False)
    assert marker not in json.dumps(completed, ensure_ascii=False)
    assert delta["payload"]["delta"]["redacted"] is True
    assert completed["payload"]["reasoning"]["redacted"] is True

    failed = policy.project_parser_observation(
        {
            "kind": "failed",
            "message": "model failed",
            "payload": {},
            "error": RuntimeError(marker),
        }
    )
    assert marker not in json.dumps(failed, ensure_ascii=False)
    assert failed["error"] is None
    assert failed["payload"]["error_observation_redaction"]["redacted"] is True


@pytest.mark.asyncio
async def test_sensitive_output_policy_redacts_published_reasoning_events() -> None:
    marker = "SECRET_PROGRAM_PUBLISHED_REASONING_MARKER"
    _SensitiveReasoningRequester.reset(
        [
            json.dumps(
                {
                    "description": "safe decision",
                    "program": "return {'ok': True}",
                }
            )
        ],
        reasoning=f"I will emit program {marker}",
    )
    request = _create_request(_SensitiveReasoningRequester)
    request._set_output_observation_policy(sensitive_paths=["program"])
    request.output(
        {"description": (str,), "program": (str,)},
        format="json",
    )
    captured: list[Any] = []
    hook_name = f"sensitive-output-reasoning-{uuid.uuid4().hex}"
    Agently.event_center.register_hook(
        lambda event: captured.append(event),
        hook_name=hook_name,
    )
    try:
        data = await request.async_get_data()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert data["description"] == "safe decision"
    reasoning_events = [
        event for event in captured if event.event_type in {"model.reasoning.delta", "model.reasoning.completed"}
    ]
    assert reasoning_events
    assert all(marker not in _event_text(event) for event in reasoning_events)
    assert all(
        event.payload.get("delta", event.payload.get("reasoning"))["redacted"] is True for event in reasoning_events
    )
