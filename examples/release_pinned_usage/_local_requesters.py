from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from agently import Agently
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData
from agently.utils import Settings


class PinnedUsageStructuredRequester:
    name = "PinnedUsageStructuredRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    attempts = 0
    requests: list[dict[str, Any]] = []

    def __init__(self, prompt: Any, settings: Any) -> None:
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls) -> None:
        cls.attempts = 0
        cls.requests = []

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        type(self).attempts += 1
        output_prompt = self.prompt.get("output", {}) or {}
        output_keys = list(output_prompt) if isinstance(output_prompt, dict) else []
        payload = {
            "attempt": type(self).attempts,
            "input": self.prompt.get("input"),
            "output_key": output_keys[0] if output_keys else "reply",
        }
        type(self).requests.append(payload)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data=payload,
            request_options={"stream": True},
            request_url="pinned-usage://structured",
        )

    async def request_model(self, request_data: AgentlyRequestData) -> AsyncGenerator[tuple[str, Any], None]:
        yield "message", json.dumps(
            {
                request_data.data["output_key"]: (
                    f"attempt={request_data.data['attempt']}; "
                    f"input={request_data.data['input']}"
                )
            },
            ensure_ascii=False,
        )

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ) -> AsyncGenerator[tuple[str, Any], None]:
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text
        yield "meta", {"provider": "pinned-usage-structured"}


class PinnedUsageSpecificStreamRequester(PinnedUsageStructuredRequester):
    name = "PinnedUsageSpecificStreamRequester"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ) -> AsyncGenerator[tuple[str, Any], None]:
        async for _event, _data in response_generator:
            pass
        yield "reasoning_delta", "thinking"
        yield "delta", "answer"
        yield "tool_calls", [{"id": "call-1", "name": "lookup_policy"}]
        yield "done", "answer"
        yield "meta", {"provider": "pinned-usage-specific-stream"}


def create_structured_agent(name: str):
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-PluginManager")
    plugin_manager.register("ModelRequester", PinnedUsageStructuredRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def create_specific_stream_agent(name: str):
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-PluginManager")
    plugin_manager.register("ModelRequester", PinnedUsageSpecificStreamRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)
