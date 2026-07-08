import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData, ChatMessageDict
from agently.utils import Settings


class MockAgentExecutionCompatibilityRequester:
    name = "MockAgentExecutionCompatibilityRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    attempts = 0
    requests: list[dict[str, Any]] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls):
        cls.attempts = 0
        cls.requests = []

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        type(self).attempts += 1
        chat_history = self.prompt.get("chat_history", []) or []
        output_prompt = self.prompt.get("output", {}) or {}
        output_keys = list(output_prompt) if isinstance(output_prompt, dict) else []
        payload = {
            "attempt": type(self).attempts,
            "input": self.prompt.get("input"),
            "system": self.prompt.get("system"),
            "chat_history_count": len(chat_history) if isinstance(chat_history, list) else 1,
            "output_key": output_keys[0] if output_keys else "reply",
        }
        type(self).requests.append(payload)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data=payload,
            request_options={"stream": True},
            request_url="mock://agent-execution-compatibility",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        yield "message", json.dumps(
            {
                request_data.data["output_key"]: (
                    f"attempt={ request_data.data['attempt'] }; "
                    f"input={ request_data.data['input'] }; "
                    f"history={ request_data.data['chat_history_count'] }"
                )
            },
            ensure_ascii=False,
        )

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {"provider": "mock-agent-execution-compatibility"}


class MockAgentExecutionSpecificStreamRequester(MockAgentExecutionCompatibilityRequester):
    name = "MockAgentExecutionSpecificStreamRequester"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        async for _event, _data in response_generator:
            pass
        yield "reasoning_delta", "thinking"
        yield "delta", "answer"
        yield "tool_calls", [{"id": "call-1", "name": "lookup_policy"}]
        yield "done", "answer"
        yield "meta", {"provider": "mock-agent-execution-specific-stream"}


class MockAgentExecutionOriginalDeltaRequester(MockAgentExecutionCompatibilityRequester):
    name = "MockAgentExecutionOriginalDeltaRequester"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        async for _event, _data in response_generator:
            pass
        yield "original_delta", '{"provider":"raw"}'
        yield "delta", "answer"
        yield "done", "answer"


def _create_test_agent(name: str = "agent-execution-compatibility"):
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", MockAgentExecutionCompatibilityRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


def _create_specific_stream_test_agent(name: str = "agent-execution-specific-stream"):
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", MockAgentExecutionSpecificStreamRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


def _create_original_delta_test_agent(name: str = "agent-execution-original-delta"):
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", MockAgentExecutionOriginalDeltaRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


def test_agent_quick_prompt_start_creates_isolated_execution_each_turn():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("quick-prompt-isolation")
    agent.system("Reply briefly.", always=True)

    replies = [
        agent.input(f"turn-{index}").output({"reply": (str,)}, format="json").start()["reply"]
        for index in range(3)
    ]

    assert replies == [
        "attempt=1; input=turn-0; history=0",
        "attempt=2; input=turn-1; history=0",
        "attempt=3; input=turn-2; history=0",
    ]
    assert [request["input"] for request in MockAgentExecutionCompatibilityRequester.requests] == [
        "turn-0",
        "turn-1",
        "turn-2",
    ]


def test_completed_agent_execution_reconfiguration_requires_new_execution():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("completed-execution-one-run")
    agent.system("Reply briefly.", always=True)

    execution = agent.input("first").output({"reply": (str,)}, format="json")
    first = execution.start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    with pytest.raises(RuntimeError, match="one independent run"):
        execution.input("second")

    second = agent.input("second").output({"reply": (str,)}, format="json").start()["reply"]
    assert second == "attempt=2; input=second; history=0"
    assert [request["input"] for request in MockAgentExecutionCompatibilityRequester.requests] == [
        "first",
        "second",
    ]


def test_completed_agent_execution_create_execution_returns_clean_fresh_execution():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("completed-execution-create-new")
    agent.system("Reply briefly.", always=True)

    execution = agent.input("first").output({"reply": (str,)}, format="json")
    first = execution.start()["reply"]
    fresh_execution = execution.create_execution()
    second = fresh_execution.input("second").output({"reply": (str,)}, format="json").start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    assert second == "attempt=2; input=second; history=0"


def test_agent_quick_prompt_uses_current_agent_chat_history():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("quick-prompt-history")
    agent.system("Reply with history.", always=True)
    chat_history: list[ChatMessageDict] = []

    agent.set_chat_history(chat_history)
    first = agent.input("first").output({"reply": (str,)}, format="json").start()["reply"]
    chat_history.extend(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": first},
        ]
    )
    agent.set_chat_history(chat_history)
    second = agent.input("second").output({"reply": (str,)}, format="json").start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    assert second == "attempt=2; input=second; history=2"


def test_agent_execution_get_data_object_returns_model_object():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-data-object")

    result_object = (
        agent.input("object")
        .output({"reply": (str,)}, format="json")
        .get_data_object(ensure_keys=["reply"])
    )

    assert result_object is not None
    assert result_object.model_dump()["reply"] == "attempt=1; input=object; history=0"


def test_agent_execution_result_get_data_object_returns_model_object():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-result-data-object")
    execution_result = agent.input("object").output({"reply": (str,)}, format="json").get_result()

    result_object = execution_result.get_data_object(ensure_keys=["reply"])

    assert result_object is not None
    assert result_object.model_dump()["reply"] == "attempt=1; input=object; history=0"


def test_agent_execution_key_waiter_facade_uses_execution_prompt():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-key-waiter")

    execution = agent.input("key").output({"reply": (str,)}, format="json")

    assert execution.get_key_result("reply") == "attempt=1; input=key; history=0"

    waiter_execution = agent.input("wait").output({"reply": (str,)}, format="json")
    assert list(waiter_execution.wait_keys(["reply"])) == [
        ("reply", "attempt=2; input=wait; history=0")
    ]

    handler_execution = agent.input("handler").output({"reply": (str,)}, format="json")
    handled = handler_execution.when_key("reply", lambda value: str(value).upper()).start_waiter()
    assert handled == [
        (
            "reply",
            "attempt=3; input=handler; history=0",
            "ATTEMPT=3; INPUT=HANDLER; HISTORY=0",
        )
    ]


def test_agent_execution_key_waiter_result_key_ignores_terminal_execution_result():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-key-waiter-result-key")

    handler_execution = agent.input("handler").output({"result": (str,)}, format="json")
    handled = handler_execution.when_key("result", lambda value: value).start_waiter()

    assert handled == [
        (
            "result",
            "attempt=1; input=handler; history=0",
            "attempt=1; input=handler; history=0",
        )
    ]


def test_agent_execution_prompt_text_is_available_before_and_after_start():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-prompt-text")
    agent.system("Reply briefly.", always=True)

    execution = agent.input("inspect prompt").output({"reply": (str,)}, format="json")
    before = execution.get_prompt_text()
    execution.start()
    after = execution.get_prompt_text()

    assert "inspect prompt" in before
    assert "inspect prompt" in after


def test_agent_get_prompt_text_reports_execution_prompt_boundary():
    agent = _create_test_agent("agent-prompt-boundary")

    agent.input("discarded execution")

    with pytest.raises(RuntimeError, match="AgentExecution"):
        agent.get_prompt_text()


def test_agent_execution_specific_stream_yields_event_tuples():
    MockAgentExecutionSpecificStreamRequester.reset()
    agent = _create_specific_stream_test_agent()

    events = list(agent.input("stream").get_generator(type="specific"))

    assert events == [
        ("reasoning_delta", "thinking"),
        ("delta", "answer"),
        ("tool_calls", [{"id": "call-1", "name": "lookup_policy"}]),
        ("done", "answer"),
    ]


def test_agent_execution_streaming_print_uses_execution_delta_stream(capsys):
    MockAgentExecutionSpecificStreamRequester.reset()
    agent = _create_specific_stream_test_agent("execution-streaming-print")

    agent.input("stream").streaming_print()

    output = capsys.readouterr().out
    assert "answer" in output


def test_agent_execution_delta_stream_filters_original_provider_delta():
    MockAgentExecutionOriginalDeltaRequester.reset()
    agent = _create_original_delta_test_agent()

    deltas = list(agent.input("stream").get_generator(type="delta"))
    original_events = list(
        agent.input("stream").get_generator(
            type="specific",
            specific=["original_delta"],
        )
    )

    assert deltas == ["answer"]
    assert original_events == [("original_delta", '{"provider":"raw"}')]


def test_agent_execution_instant_stream_preserves_full_data_snapshot():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-instant-full-data")

    items = list(agent.input("snapshot").output({"reply": (str,)}, format="json").get_generator(type="instant"))
    completed_reply_items = [
        item
        for item in items
        if item.path == "reply" and item.is_complete
    ]

    assert completed_reply_items
    assert completed_reply_items[-1].full_data
