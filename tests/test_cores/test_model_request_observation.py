import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently.builtins.plugins.ResponseParser.AgentlyResponseParser import AgentlyResponseParser
from agently.core import ModelRequest, ModelRequestResult, ModelResponseResult, PluginManager
from agently.core.model.AttemptRunner import core_attempt_runner_entrypoint
from agently.core.runtime.RuntimeEvents import (
    async_emit_action_flow_observation,
    async_emit_response_parser_observation,
)
from agently.types.data import AgentlyRequestData, AttemptDecision, AttemptHandlers, AttemptState, RunContext
from agently.types.data.event import normalize_triggerflow_event_type
from agently.utils import Settings


class MockObservationRequester:
    name = "MockObservationRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    attempts = 0

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls):
        cls.attempts = 0

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        type(self).attempts += 1
        prompt_object = self.prompt.to_prompt_object()
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "attempt": type(self).attempts,
                "messages": self.prompt.to_messages(),
                "prompt_text": self.prompt.to_text(),
                "output_format": prompt_object.output_format,
            },
            request_options={"stream": True},
            request_url="mock://observation-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        attempt = int(request_data.data.get("attempt", 1))
        output_format = str(request_data.data.get("output_format", "markdown"))
        if output_format == "json":
            if attempt == 1:
                yield "message", json.dumps({"summary": "retry me"}, ensure_ascii=False)
            else:
                yield "message", json.dumps({"summary": "all good", "reply": "done"}, ensure_ascii=False)
            return
        yield "message", "Morning briefing prepared.\nHighlight GPU demand.\n"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        for line in response_text.splitlines(keepends=True):
            if line:
                yield "delta", line
                await asyncio.sleep(0)
        yield "done", response_text
        yield "meta", {"provider": "mock-observation", "model": "mock-1"}


class MockThinkStructuredRequester:
    name = "MockThinkStructuredRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        prompt_object = self.prompt.to_prompt_object()
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "messages": self.prompt.to_messages(),
                "prompt_text": self.prompt.to_text(),
                "output_format": prompt_object.output_format,
            },
            request_options={"stream": True},
            request_url="mock://think-structured-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        del request_data
        yield "message", json.dumps(
            {
                "summary": "draft",
                "action_items": [{"owner": "张经理"}],
            },
            ensure_ascii=False,
        )
        yield "message", "\n"
        yield "message", json.dumps(
            {
                "summary": "启动用户反馈系统开发，暂缓数据导出功能；微服务改造需评估；4月底完成原型，6月底上线；下周提交项目计划。",
                "action_items": [
                    {
                        "task": "提交详细项目计划",
                        "owner": "张经理",
                        "deadline": "2024-03-22",
                    },
                    {
                        "task": "评估微服务改造可行性",
                        "owner": "张经理",
                        "deadline": "2024-03-29",
                    },
                ],
            },
            ensure_ascii=False,
        )

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = "<think>先草拟一个结构。"
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        response_text += "</think>"
        for line in response_text.splitlines(keepends=True):
            if line:
                yield "delta", line
                await asyncio.sleep(0)
        yield "done", response_text
        yield "meta", {"provider": "mock-observation", "model": "mock-think"}


class MockCompleteJsonRequester:
    name = "MockCompleteJsonRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    attempts = 0

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls):
        cls.attempts = 0

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        type(self).attempts += 1
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages()},
            request_options={"stream": True},
            request_url="mock://complete-json-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        del request_data
        yield "message", json.dumps({"summary": "ready", "reply": "done"}, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "delta", response_text
        yield "done", response_text


class MockSlowCancelableRequester:
    name = "MockSlowCancelableRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    canceled_attempts = 0

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls):
        cls.canceled_attempts = 0

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        prompt_object = self.prompt.to_prompt_object()
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "messages": self.prompt.to_messages(),
                "prompt_text": self.prompt.to_text(),
                "output_format": prompt_object.output_format,
            },
            request_options={"stream": True},
            request_url="mock://slow-cancelable-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        del request_data
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            type(self).canceled_attempts += 1
            raise
        yield "message", "unexpected"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        async for event, data in response_generator:
            yield event, data


class MockHandlerDrivenRequester:
    name = "MockHandlerDrivenRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"prompt_text": self.prompt.to_text()},
            request_options={"stream": True},
            request_url="mock://handler-driven-requester",
        )

    def build_request_handlers(self, request_data: AgentlyRequestData):
        async def execute(state: AttemptState):
            prompt_text = str(request_data.data.get("prompt_text", ""))
            if "after-output retry" in prompt_text:
                if state.attempt_index == 1:
                    yield "message", '{"reply": "partial'
                    raise RuntimeError("handler stream broke")
                yield "message", json.dumps({"reply": "done"}, ensure_ascii=False)
                return
            if "fail" in prompt_text:
                raise RuntimeError("handler provider failed")
            yield "message", "handler output"

        async def handle_error(error: BaseException, state: AttemptState):
            if str(error) == "handler stream broke":
                return AttemptDecision.retry(reason="transient_stream_error", allow_after_output_started=True)
            return AttemptDecision.yield_error(error)

        return AttemptHandlers(execute=execute, handle_error=handle_error)

    @core_attempt_runner_entrypoint
    async def request_model(self, request_data: AgentlyRequestData):
        handlers = self.build_request_handlers(request_data)
        async for item in handlers.execute(AttemptState()):
            yield item

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "error":
                yield event, data
                continue
            if event == "status":
                if isinstance(data, dict) and data.get("status") == "failed" and data.get("retry") is True:
                    response_text = ""
                yield event, data
                continue
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        if response_text:
            yield "done", response_text


def _create_request():
    settings = Settings(name="ObservationTestSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="ObservationTestPluginManager")
    plugin_manager.register("ModelRequester", MockObservationRequester, activate=True)
    return ModelRequest(
        plugin_manager,
        agent_name="observation-agent",
        agent_id="agent-observation",
        parent_settings=settings,
    )


def _create_agent():
    settings = Settings(name="ObservationTestAgentSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="ObservationTestAgentPluginManager")
    plugin_manager.register("ModelRequester", MockObservationRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="observation-agent",
    )


def _create_slow_agent():
    settings = Settings(name="SlowObservationTestAgentSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="SlowObservationTestAgentPluginManager")
    plugin_manager.register("ModelRequester", MockSlowCancelableRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="slow-observation-agent",
    )


def _create_handler_driven_agent():
    settings = Settings(name="HandlerDrivenAgentSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="HandlerDrivenAgentPluginManager")
    plugin_manager.register("ModelRequester", MockHandlerDrivenRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="handler-driven-agent",
    )


def _create_think_structured_request():
    settings = Settings(name="ThinkStructuredTestSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="ThinkStructuredPluginManager")
    plugin_manager.register("ModelRequester", MockThinkStructuredRequester, activate=True)
    return ModelRequest(
        plugin_manager,
        agent_name="think-structured-agent",
        agent_id="agent-think-structured",
        parent_settings=settings,
    )


def _create_complete_json_request():
    settings = Settings(name="CompleteJsonTestSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="CompleteJsonPluginManager")
    plugin_manager.register("ModelRequester", MockCompleteJsonRequester, activate=True)
    return ModelRequest(
        plugin_manager,
        agent_name="complete-json-agent",
        agent_id="agent-complete-json",
        parent_settings=settings,
    )


def _create_handler_driven_request():
    settings = Settings(name="HandlerDrivenTestSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="HandlerDrivenPluginManager")
    plugin_manager.register("ModelRequester", MockHandlerDrivenRequester, activate=True)
    return ModelRequest(
        plugin_manager,
        agent_name="handler-driven-agent",
        agent_id="agent-handler-driven",
        parent_settings=settings,
    )


@pytest.mark.asyncio
async def test_model_request_events_include_prompt_and_child_run_lineage():
    MockObservationRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        workflow_run = RunContext.create(
            run_kind="workflow_execution",
            agent_name="workflow-agent",
            execution_id="execution-observation",
            meta={"flow_name": "observation-flow"},
        )
        request = _create_request()
        request.input("Summarize the morning operations notes.")
        request.instruct("Focus on GPU cloud demand and operational risk.")

        response = request.get_response(parent_run_context=workflow_run)
        text = await response.async_get_text()

        assert "Morning briefing prepared." in text
        assert response.run_context is not None
        assert response.model_run_context is not None
        request_run = response.run_context
        model_run = response.model_run_context
        assert request_run.parent_run_id == workflow_run.run_id
        assert model_run.parent_run_id == request_run.run_id
        assert model_run.run_kind == "model_request"

        request_events = [event for event in captured if event.run and event.run.run_id == request_run.run_id]
        model_events = [
            event for event in captured if event.run and event.run.run_id == model_run.run_id
        ]

        assert [event.event_type for event in request_events if event.event_type.startswith("request.")] == [
            "request.started",
            "request.completed",
        ]
        assert [event.event_type for event in model_events] == [
            "model.request_started",
            "prompt.built",
            "model.requesting",
            "model.streaming",
            "model.streaming",
            "model.completed",
            "model.meta",
        ]

        prompt_event = next(event for event in model_events if event.event_type == "prompt.built")
        assert "Summarize the morning operations notes." in str(prompt_event.payload["prompt"]["input"])
        assert "GPU cloud demand" in str(prompt_event.payload["prompt_text"])

        requesting_event = next(event for event in model_events if event.event_type == "model.requesting")
        assert requesting_event.payload["request"]["request_url"] == "mock://observation-requester"
        assert requesting_event.payload["attempt_index"] == 1

        started_event = next(event for event in model_events if event.event_type == "model.request_started")
        started_telemetry = started_event.payload["model_request_telemetry"]
        assert started_telemetry["event_kind"] == "model.request_started"
        assert started_telemetry["response_id"] == response.response_id
        assert started_telemetry["attempt_index"] == 1
        assert started_telemetry["request_run_id"] == request_run.run_id
        assert started_telemetry["model_run_id"] == model_run.run_id
        assert started_telemetry["provider_family"] == "MockObservationRequester"

        requesting_telemetry = requesting_event.payload["model_request_telemetry"]
        assert requesting_telemetry["event_kind"] == "model.requesting"
        assert requesting_telemetry["request_url"] == "mock://observation-requester"

        meta_event = next(event for event in model_events if event.event_type == "model.meta")
        assert meta_event.payload["meta"]["provider"] == "mock-observation"
        assert meta_event.payload["meta"]["model"] == "mock-1"
        meta_telemetry = meta_event.payload["model_request_telemetry"]
        assert meta_telemetry["event_kind"] == "model.meta"
        assert meta_telemetry["provider"] == "mock-observation"
        assert meta_telemetry["model"] == "mock-1"
    finally:
        Agently.event_center.unregister_hook(hook_name)


def test_get_result_is_primary_model_request_facade():
    request = _create_request()
    request.input("Summarize the morning operations notes.")

    result = request.get_result()

    assert isinstance(result, ModelRequestResult)
    assert ModelResponseResult is ModelRequestResult
    assert result.result is result
    assert result.id == result.response_id


def test_get_response_returns_result_compatible_facade():
    request = _create_request()
    request.input("Summarize the morning operations notes.")

    result = request.get_response()

    assert isinstance(result, ModelRequestResult)
    assert result.result is result


@pytest.mark.asyncio
async def test_legacy_model_requester_without_handlers_still_streams():
    MockObservationRequester.reset()
    request = _create_request()
    request.input("Legacy requester still works.")

    response = request.get_response()
    assert await response.async_get_text() == "Morning briefing prepared.\nHighlight GPU demand.\n"


@pytest.mark.asyncio
async def test_handler_driven_model_requester_streams_through_core_attempt_runner():
    request = _create_handler_driven_request()
    request.input("ok")

    response = request.get_response()
    assert await response.async_get_text() == "handler output"


@pytest.mark.asyncio
async def test_handler_driven_after_output_retry_emits_status_and_replays_cleanly():
    captured = []
    hook_name = "test_model_request_observation.status_capture"
    Agently.event_center.register_hook(lambda event: captured.append(event), hook_name=hook_name)
    request = _create_handler_driven_request()
    try:
        request.input("after-output retry")
        request.output({"reply": (str, "Final reply.", True)}, format="json")

        response = request.get_response()
        all_events = [item async for item in response.get_async_generator(type="all")]

        status_events = [item for item in all_events if item[0] == "status"]
        assert len(status_events) == 2
        failed_status = status_events[0][1]
        assert failed_status["status"] == "failed"
        assert failed_status["response_id"] == response.response_id
        assert failed_status["attempt_index"] == 1
        assert failed_status["next_attempt_index"] == 2
        assert failed_status["retry"] is True
        assert failed_status["reason"] == "handler stream broke"
        assert failed_status["error_type"] == "RuntimeError"
        assert status_events[1][1]["status"] == "completed"
        done_events = [item for item in all_events if item[0] == "done"]
        assert done_events[-1][1] == '{"reply": "done"}'

        status_runtime_events = [event for event in captured if event.event_type == "model.status"]
        assert len(status_runtime_events) == 2
        assert status_runtime_events[0].payload["response_id"] == response.response_id
        assert status_runtime_events[0].payload["reason"] == "handler stream broke"
    finally:
        Agently.event_center.unregister_hook(hook_name)

    parsed_request = _create_handler_driven_request()
    parsed_request.input("after-output retry")
    parsed_request.output({"reply": (str, "Final reply.", True)}, format="json")

    data = await parsed_request.get_response().async_get_data()
    assert data == {"reply": "done"}

    instant_request = _create_handler_driven_request()
    instant_request.input("after-output retry")
    instant_request.output({"reply": (str, "Final reply.", True)}, format="json")

    instant_items = [item async for item in instant_request.get_response().get_async_generator(type="instant")]
    instant_statuses = [item for item in instant_items if item.path == "$status"]
    assert len(instant_statuses) == 2
    assert instant_statuses[0].value["status"] == "failed"
    assert instant_statuses[0].value["next_attempt_index"] == 2
    assert instant_statuses[-1].value["status"] == "completed"

    delta_request = _create_handler_driven_request()
    delta_request.input("after-output retry")
    delta_request.output({"reply": (str, "Final reply.", True)}, format="json")

    delta_chunks = [
        item
        async for item in delta_request.get_response().get_async_generator(type="delta")
    ]
    assert delta_chunks == [
        '{"reply": "partial',
        "<$retry>handler stream broke</$retry>",
        '{"reply": "done"}',
    ]

    rendered_text = ""
    for chunk in delta_chunks:
        if "<$retry>" in chunk:
            rendered_text = ""
            continue
        rendered_text += chunk
    assert rendered_text == '{"reply": "done"}'


@pytest.mark.asyncio
async def test_agent_execution_projects_model_status_and_lineage_for_plain_delta_replay():
    execution = _create_handler_driven_agent().input("after-output retry")

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    status_items = [item for item in stream_items if item.path == "$status"]
    delta_items = [item for item in stream_items if item.path == "model.delta"]

    assert [item.value["status"] for item in status_items] == ["failed", "completed"]
    assert status_items[0].value["retry"] is True
    assert status_items[0].value["next_attempt_index"] == 2
    assert [item.delta for item in delta_items] == ['{"reply": "partial', '{"reply": "done"}']
    assert stream_items.index(status_items[0]) > stream_items.index(delta_items[0])
    assert stream_items.index(status_items[0]) < stream_items.index(delta_items[1])

    model_items = [*status_items, *delta_items]
    response_ids = {item.meta["response_id"] for item in model_items if item.meta is not None}
    request_run_ids = {item.meta["request_run_id"] for item in model_items if item.meta is not None}
    model_run_ids = {item.meta["model_run_id"] for item in model_items if item.meta is not None}
    assert len(response_ids) == len(request_run_ids) == len(model_run_ids) == 1
    assert all(item.source == "model_request" and item.route == "model_request" for item in model_items)


def test_delta_retry_marker_escapes_provider_reason():
    assert AgentlyResponseParser._format_delta_retry_marker(
        {
            "status": "failed",
            "retry": True,
            "reason": "peer <closed> & sent </$retry>",
        }
    ) == "<$retry>peer &lt;closed&gt; &amp; sent &lt;/$retry&gt;</$retry>"


@pytest.mark.asyncio
async def test_handler_driven_provider_error_becomes_core_runtime_event():
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.handler_error_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        request = _create_handler_driven_request()
        request.input("fail")
        response = request.get_response()

        with pytest.raises(RuntimeError, match="handler provider failed"):
            await response.async_get_text()

        requester_errors = [event for event in captured if event.event_type == "model.requester.error"]
        assert len(requester_errors) == 1
        assert requester_errors[0].source == "MockHandlerDrivenRequester"
        assert requester_errors[0].error is not None
        assert requester_errors[0].error.message == "handler provider failed"
        requester_error_telemetry = requester_errors[0].payload["model_request_telemetry"]
        assert requester_error_telemetry["event_kind"] == "model.requester.error"
        assert requester_error_telemetry["response_id"] == response.response_id
        assert requester_error_telemetry["attempt_index"] == 1
        assert requester_error_telemetry["error"]["message"] == "handler provider failed"
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_model_request_retry_creates_multiple_attempt_runs():
    MockObservationRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.retry_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        request = _create_request()
        request.input("Return a structured operations update.")
        request.output(
            {
                "summary": (str,),
                "reply": (str,),
            },
            format="json",
        )

        response = request.get_response()
        data = await response.async_get_data(ensure_keys=["reply"], max_retries=1)

        assert data["reply"] == "done"

        attempt_start_events = [event for event in captured if event.event_type == "model.request_started"]
        assert len(attempt_start_events) == 2
        assert [event.payload["attempt_index"] for event in attempt_start_events] == [1, 2]
        assert [
            event.payload["model_request_telemetry"]["attempt_index"] for event in attempt_start_events
        ] == [1, 2]
        assert len(
            {
                event.payload["model_request_telemetry"]["telemetry_key"]
                for event in attempt_start_events
            }
        ) == 2
        assert len({event.run.run_id for event in attempt_start_events if event.run is not None}) == 2
        assert response.run_context is not None
        request_run = response.run_context
        assert all(
            event.run and event.run.parent_run_id == request_run.run_id for event in attempt_start_events
        )

        retry_event = next(event for event in captured if event.event_type == "model.retrying")
        assert retry_event.payload["next_attempt_index"] == 2
        assert retry_event.run is not None
        assert retry_event.run.run_id == request_run.run_id

        completed_events = [event for event in captured if event.event_type == "model.completed"]
        assert len(completed_events) == 2
        final_completed_event = completed_events[-1]
        assert final_completed_event.payload["result"] == {
            "summary": "all good",
            "reply": "done",
        }
        assert final_completed_event.payload["raw_text"] == '{"summary": "all good", "reply": "done"}'
        assert final_completed_event.payload["cleaned_text"] == '{"summary": "all good", "reply": "done"}'
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_model_request_telemetry_dedupes_same_kind_for_same_attempt():
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.telemetry_dedupe_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        run = RunContext.create(
            run_kind="model_request",
            agent_name="dedupe-agent",
            response_id="response-dedupe",
            meta={"attempt_index": 1},
        )
        await async_emit_response_parser_observation(
            {
                "kind": "meta",
                "source": "TestParser",
                "payload": {"meta": {"provider": "mock-provider", "model": "mock-model", "usage": {"total_tokens": 3}}},
            },
            agent_name="dedupe-agent",
            response_id="response-dedupe",
            run=run,
        )
        await async_emit_response_parser_observation(
            {
                "kind": "meta",
                "source": "TestParser",
                "payload": {"meta": {"provider": "mock-provider", "model": "mock-model", "usage": {"total_tokens": 3}}},
            },
            agent_name="dedupe-agent",
            response_id="response-dedupe",
            run=run,
        )

        meta_events = [event for event in captured if event.event_type == "model.meta"]
        assert len(meta_events) == 2
        assert "model_request_telemetry" in meta_events[0].payload
        assert meta_events[0].payload["model_request_telemetry"]["telemetry_key"] == (
            "response-dedupe:1:model.meta"
        )
        assert "model_request_telemetry" not in meta_events[1].payload
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_auto_format_parse_failure_degrades_to_json_and_preserves_all_shape():
    MockObservationRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.auto_degradation_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        request = _create_request()
        request.input("Return a structured operations update.")
        request.output(
            {
                "summary": (str,),
                "reply": (str,),
            },
            format="auto",
        )

        response = request.get_response()
        all_data = await response.async_get_data(type="all", max_retries=1)

        assert all_data["parsed_result"] == {"summary": "all good", "reply": "done"}
        assert all_data["extra"]["output_format"] == "json"
        assert MockObservationRequester.attempts == 2

        retry_event = next(event for event in captured if event.event_type == "model.retrying")
        assert retry_event.payload["retry_reason"] == "format_degradation"
        assert retry_event.payload["from_output_format"] == "xml_field"
        assert retry_event.payload["to_output_format"] == "json"
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_get_data_all_checks_ensure_keys_against_parsed_result():
    MockCompleteJsonRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.all_ensure_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        request = _create_complete_json_request()
        request.input("Return a complete update.")
        request.output(
            {
                "summary": (str,),
                "reply": (str,),
            },
            format="json",
        )

        all_data = await request.get_response().async_get_data(type="all", max_retries=1)

        assert all_data["parsed_result"] == {"summary": "ready", "reply": "done"}
        assert MockCompleteJsonRequester.attempts == 1
        assert [event for event in captured if event.event_type == "model.retrying"] == []
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_model_request_ensure_keys_prefers_complete_json_after_think_block():
    request = _create_think_structured_request()
    request.output(
        {
            "summary": (str, "会议核心结论，100字以内"),
            "action_items": [
                {
                    "task": (str, "待办事项描述"),
                    "owner": (str, "负责人"),
                    "deadline": (str, "截止日期"),
                }
            ],
        },
        format="json",
    )

    response = request.get_response()
    data = await response.async_get_data(
        ensure_keys=["summary", "action_items[*].task", "action_items[*].owner"],
        max_retries=0,
    )

    assert data["summary"].startswith("启动用户反馈系统开发")
    assert data["action_items"][0]["task"] == "提交详细项目计划"
    assert data["action_items"][0]["owner"] == "张经理"


@pytest.mark.asyncio
async def test_agent_execution_wraps_request_and_model_request_runs():
    MockObservationRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.agent_execution_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        workflow_run = RunContext.create(
            run_kind="workflow_execution",
            agent_name="workflow-agent",
            execution_id="execution-agent-execution",
            meta={"flow_name": "agent-execution-flow"},
        )
        agent = _create_agent()
        execution = agent.input("Summarize the morning operations notes.")
        execution.instruct("Focus on GPU cloud demand and operational risk.")

        text = await execution.async_get_text(parent_run_context=workflow_run)

        assert "Morning briefing prepared." in text

        execution_events = [event for event in captured if event.run and event.run.run_kind == "agent_execution"]
        assert [event.event_type for event in execution_events] == [
            "agent_execution.started",
            "agent_execution.completed",
        ]

        execution_run = execution_events[0].run
        assert execution_run is not None
        assert execution_run.parent_run_id == workflow_run.run_id

        request_events = [
            event
            for event in captured
            if event.run and event.run.run_kind == "request" and event.run.parent_run_id == execution_run.run_id
        ]
        assert [event.event_type for event in request_events if event.event_type.startswith("request.")] == [
            "request.started",
            "request.completed",
        ]

        model_start_event = next(event for event in captured if event.event_type == "model.request_started")
        assert model_start_event.run is not None
        assert model_start_event.run.parent_run_id == request_events[0].run.run_id
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_tool_runtime_uses_action_runs_under_request_scope():
    MockObservationRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.tool_action_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        agent = _create_agent()

        agent.tool.register(
            name="lookup_signal",
            desc="lookup external signal",
            kwargs={"topic": (str, "signal topic")},
            func=lambda topic: f"signal:{ topic }",
            tags=[f"agent-{ agent.name }"],
        )

        async def fake_plan_handler(
            context,
            request,
        ):
            _ = request
            round_index = context.get("round_index", 0)
            if round_index == 0:
                return {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "gather_market_signal",
                            "tool_name": "lookup_signal",
                            "tool_kwargs": {"topic": "gpu"},
                            "todo_suggestion": "respond",
                        }
                    ],
                }
            return {
                "next_action": "response",
                "execution_commands": [],
            }

        async def fake_execution_handler(
            context,
            request,
        ):
            _ = context
            tool_commands = request.get("action_calls", [])
            return [
                {
                    "purpose": str(tool_commands[0].get("purpose", "unknown")),
                    "tool_name": str(tool_commands[0].get("tool_name", "unknown")),
                    "kwargs": tool_commands[0].get("tool_kwargs", {}),
                    "todo_suggestion": str(tool_commands[0].get("todo_suggestion", "")),
                    "success": True,
                    "result": {"signal": "gpu-demand-rising"},
                    "error": "",
                }
            ]

        agent.register_tool_plan_analysis_handler(fake_plan_handler)
        agent.register_tool_execution_handler(fake_execution_handler)
        agent.tool.tag(["lookup_signal"], f"agent-{ agent.name }")
        execution = agent.input("Need a briefing with external signal.")
        await execution.async_get_text()

        request_run = next(event.run for event in captured if event.event_type == "request.started")
        action_loop_start = next(event for event in captured if event.event_type == "action.loop_started")
        assert action_loop_start.run is not None
        assert request_run is not None
        assert action_loop_start.run.parent_run_id == request_run.run_id
        assert action_loop_start.run.run_kind == "action_loop"

        tool_loop_start = next(event for event in captured if event.event_type == "tool.loop_started")
        assert tool_loop_start.run is not None
        assert tool_loop_start.run.run_id == action_loop_start.run.run_id
        assert tool_loop_start.meta.get("compat_event_alias") is True
        assert tool_loop_start.meta.get("compat_alias_for") == "action.loop_started"
        assert tool_loop_start.meta.get("primary_event_id") == action_loop_start.event_id

        action_events = [
            event
            for event in captured
            if event.event_type in {"action.started", "action.completed", "action.failed"}
        ]
        assert [event.event_type for event in action_events] == [
            "action.started",
            "action.completed",
        ]
        action_run = action_events[0].run
        assert action_run is not None
        assert action_run.run_kind == "action"
        assert action_run.parent_run_id == action_loop_start.run.run_id
        assert action_run.meta.get("action_type") == "tool"
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_action_flow_reports_approval_required_without_failed_event():
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.action_approval_required_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        agent = Agently.create_agent("action-flow-approval-event-agent")

        async def fake_plan_handler(context, request):
            _ = request
            if context.get("round_index", 0) == 0:
                return {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "validate shell command",
                            "action_id": "run_shell",
                            "action_input": {"cmd": "python legacy_script.py", "workdir": "."},
                        }
                    ],
                }
            return {"next_action": "response", "execution_commands": []}

        async def fake_execution_handler(context, request):
            _ = context
            command = request["action_calls"][0]
            return [
                {
                    "purpose": command["purpose"],
                    "action_id": command["action_id"],
                    "kwargs": command["action_input"],
                    "status": "approval_required",
                    "success": False,
                    "result": None,
                    "error": "workdir_not_allowed",
                    "approval": {"required": True, "reason": "workdir_not_allowed"},
                }
            ]

        await Agently.action_flow.async_run(
            action=agent.action,
            prompt=agent.request.prompt,
            settings=agent.settings,
            action_list=[{"name": "run_shell", "desc": "Run shell command.", "kwargs": {}}],
            agent_name=agent.name,
            planning_handler=fake_plan_handler,
            execution_handler=fake_execution_handler,
            max_rounds=2,
            runtime_observation_handler=async_emit_action_flow_observation,
        )

        action_event_types = [event.event_type for event in captured if event.event_type.startswith("action.")]
        assert "action.approval_required" in action_event_types
        assert "action.failed" not in action_event_types
        approval_event = next(event for event in captured if event.event_type == "action.approval_required")
        assert approval_event.level == "WARNING"
        assert approval_event.payload["record"]["status"] == "approval_required"
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_trigger_flow_runtime_context_auto_inherits_parent_run_for_agent_and_request():
    MockObservationRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.trigger_flow_runtime_context_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        flow = TriggerFlow(name="runtime-context-auto-parent")

        async def run_inside_flow(data: TriggerFlowRuntimeData):
            agent = _create_agent()
            execution = agent.input("Summarize the runtime context flow.")
            request = _create_request()
            request.input("Provide a direct request summary.")
            agent_text = await execution.async_get_text()
            request_text = await request.async_get_text()
            return {
                "agent_text": agent_text,
                "request_text": request_text,
            }

        flow.to(run_inside_flow).end()

        result = await flow.async_start("start")
        final = result.get("$final_result", result)

        assert "Morning briefing prepared." in final["agent_text"]
        assert "Morning briefing prepared." in final["request_text"]

        workflow_start = next(
            event
            for event in captured
            if normalize_triggerflow_event_type(event.event_type) == "triggerflow.execution_started"
        )
        workflow_run = workflow_start.run
        assert workflow_run is not None

        chunk_start = next(
            event
            for event in captured
            if event.event_type == "chunk.started"
            and event.run is not None
            and event.run.meta.get("chunk_name") == "run_inside_flow"
        )
        chunk_run = chunk_start.run
        assert chunk_run is not None
        assert chunk_run.parent_run_id == workflow_run.run_id

        agent_execution_start = next(event for event in captured if event.event_type == "agent_execution.started")
        assert agent_execution_start.run is not None
        assert agent_execution_start.run.parent_run_id == chunk_run.run_id

        request_starts = [event for event in captured if event.event_type == "request.started"]
        assert len(request_starts) >= 2
        parent_ids = {event.run.parent_run_id for event in request_starts if event.run is not None}
        assert chunk_run.run_id in parent_ids
        assert agent_execution_start.run.run_id in parent_ids
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_nested_subflow_helper_calls_auto_inherit_runtime_context():
    MockObservationRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.nested_subflow_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        sub_flow = TriggerFlow(name="daily-news-summary-sub-flow")

        async def summarize_candidate(data: TriggerFlowRuntimeData):
            async def helper():
                agent = _create_agent()
                execution = agent.input("Summarize candidate news.")
                request = _create_request()
                request.input("Summarize direct request in subflow.")
                agent_text = await execution.async_get_text()
                request_text = await request.async_get_text()
                return {
                    "agent_text": agent_text,
                    "request_text": request_text,
                }

            return await helper()

        sub_flow.to(summarize_candidate).end()

        flow = TriggerFlow(name="daily-news-root-flow")
        flow.to_sub_flow(sub_flow, capture={"input": "value"}, write_back={"value": "result"}).end()

        result = await flow.async_start("topic")
        final = result.get("$final_result", result)

        assert "Morning briefing prepared." in final["agent_text"]
        assert "Morning briefing prepared." in final["request_text"]

        workflow_runs = [
            event.run
            for event in captured
            if normalize_triggerflow_event_type(event.event_type) == "triggerflow.execution_started"
            and event.run is not None
        ]

        root_workflow_run = next(run for run in workflow_runs if run.meta.get("flow_name") == "daily-news-root-flow")
        subflow_workflow_run = next(
            run for run in workflow_runs if run.meta.get("flow_name") == "daily-news-summary-sub-flow"
        )

        subflow_parent_chunk = next(
            event.run
            for event in captured
            if event.event_type == "chunk.started"
            and event.run is not None
            and event.run.run_id == subflow_workflow_run.parent_run_id
        )
        assert subflow_parent_chunk is not None
        assert subflow_parent_chunk.parent_run_id == root_workflow_run.run_id

        summarize_chunk = next(
            event.run
            for event in captured
            if event.event_type == "chunk.started"
            and event.run is not None
            and event.run.meta.get("chunk_name") == "summarize_candidate"
        )
        assert summarize_chunk is not None
        assert summarize_chunk.parent_run_id == subflow_workflow_run.run_id

        agent_execution_run = next(
            event.run for event in captured if event.event_type == "agent_execution.started" and event.run is not None
        )
        assert agent_execution_run is not None
        assert agent_execution_run.parent_run_id == summarize_chunk.run_id

        request_starts = [
            event.run for event in captured if event.event_type == "request.started" and event.run is not None
        ]
        parent_ids = {run.parent_run_id for run in request_starts}
        assert summarize_chunk.run_id in parent_ids
        assert agent_execution_run.run_id in parent_ids

        model_request_runs = [
            event.run for event in captured if event.event_type == "model.request_started" and event.run is not None
        ]
        assert len(model_request_runs) >= 2
        assert all(run.parent_run_id in {request.run_id for request in request_starts} for run in model_request_runs)
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_trigger_flow_failure_cancels_sibling_model_request_and_emits_cancelled_status():
    MockSlowCancelableRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.sibling_cancel_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        flow = TriggerFlow(name="sibling-cancel-flow")

        async def slow_branch(data: TriggerFlowRuntimeData):
            del data
            agent = _create_slow_agent()
            execution = agent.input("Wait for sibling cancellation.")
            return await execution.async_get_text()

        async def fail_branch(data: TriggerFlowRuntimeData):
            del data
            await asyncio.sleep(0.05)
            raise RuntimeError("branch boom")

        flow.batch(slow_branch, fail_branch).end()

        with pytest.raises(RuntimeError, match="branch boom"):
            await flow.async_start("start")

        for _ in range(20):
            if MockSlowCancelableRequester.canceled_attempts >= 1 and any(
                event.event_type == "model.status" and event.payload.get("status") == "cancelled"
                for event in captured
            ):
                break
            await asyncio.sleep(0.01)

        event_types = [event.event_type for event in captured]
        cancelled_statuses = [
            event for event in captured if event.event_type == "model.status" and event.payload.get("status") == "cancelled"
        ]
        assert len(cancelled_statuses) == 1
        assert cancelled_statuses[0].payload["retry"] is False
        assert "model.request_failed" not in event_types
        assert "request.failed" not in event_types
        assert "chunk.failed" in event_types
        assert normalize_triggerflow_event_type("triggerflow.execution_failed") in {
            normalize_triggerflow_event_type(event_type) for event_type in event_types
        }
        assert MockSlowCancelableRequester.canceled_attempts >= 1
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_trigger_flow_for_each_failure_waits_for_sibling_cleanup():
    MockSlowCancelableRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_observation.for_each_cancel_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        flow = TriggerFlow(name="for-each-cancel-flow")

        async def prepare_items(data: TriggerFlowRuntimeData):
            del data
            return ["slow", "fail"]

        async def analyze_item(data: TriggerFlowRuntimeData):
            if data.value == "slow":
                agent = _create_slow_agent()
                execution = agent.input("Wait for for_each sibling cancellation.")
                return await execution.async_get_text()
            await asyncio.sleep(0.05)
            raise RuntimeError("for_each branch boom")

        flow.to(prepare_items).for_each(concurrency=2).to(analyze_item).end_for_each().end()

        with pytest.raises(RuntimeError, match="for_each branch boom"):
            await flow.async_start("start")

        event_types = [event.event_type for event in captured]
        cancelled_statuses = [
            event for event in captured if event.event_type == "model.status" and event.payload.get("status") == "cancelled"
        ]
        assert len(cancelled_statuses) == 1
        assert cancelled_statuses[0].payload["retry"] is False
        assert "model.request_failed" not in event_types
        assert "request.failed" not in event_types
        assert "chunk.failed" in event_types
        assert normalize_triggerflow_event_type("triggerflow.execution_failed") in {
            normalize_triggerflow_event_type(event_type) for event_type in event_types
        }
        assert MockSlowCancelableRequester.canceled_attempts >= 1
    finally:
        Agently.event_center.unregister_hook(hook_name)
