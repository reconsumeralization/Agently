import json
from collections.abc import AsyncGenerator
from typing import Any

from agently import Agently
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData
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
        payload = {
            "attempt": type(self).attempts,
            "input": self.prompt.get("input"),
            "system": self.prompt.get("system"),
            "chat_history_count": len(chat_history) if isinstance(chat_history, list) else 1,
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
                "reply": (
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


def _create_test_agent(name: str = "agent-execution-compatibility"):
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", MockAgentExecutionCompatibilityRequester, activate=True)
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


def test_completed_agent_execution_reconfiguration_forks_fresh_execution():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("completed-execution-fork")
    execution = agent.system("Reply briefly.").output({"reply": (str,)}, format="json")

    first = execution.input("first").start()["reply"]
    second = execution.input("second").start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    assert second == "attempt=2; input=second; history=0"
    assert [request["input"] for request in MockAgentExecutionCompatibilityRequester.requests] == [
        "first",
        "second",
    ]


def test_completed_agent_execution_reconfiguration_preserves_validation_handlers():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("completed-execution-validator-fork")
    validated_replies: list[str] = []

    def record_validation(value: dict[str, Any], _context: Any) -> bool:
        validated_replies.append(str(value.get("reply")))
        return True

    execution = (
        agent.system("Reply briefly.")
        .output({"reply": (str,)}, format="json")
        .validate(record_validation)
    )

    first = execution.input("first").start()["reply"]
    second = execution.input("second").start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    assert second == "attempt=2; input=second; history=0"
    assert validated_replies == [first, second]


def test_completed_agent_execution_reconfiguration_uses_current_agent_chat_history():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("completed-execution-history-fork")
    execution = agent.system("Reply with history.").output({"reply": (str,)}, format="json")
    chat_history: list[dict[str, str]] = []

    first = execution.set_chat_history(chat_history).input("first").start()["reply"]
    chat_history.extend(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": first},
        ]
    )
    second = execution.set_chat_history(chat_history).input("second").start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    assert second == "attempt=2; input=second; history=2"
