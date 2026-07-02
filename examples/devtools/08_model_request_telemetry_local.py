import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from agently import Agently
from agently.core import ModelRequest, PluginManager
from agently.types.data import AgentlyRequestData
from agently.utils import Settings


class TelemetryMockRequester:
    name = "TelemetryMockRequester"
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
            data={"messages": self.prompt.to_messages(), "model": "mock-model"},
            request_options={"stream": True},
            request_url="mock://telemetry-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        del request_data
        yield "message", "Telemetry local response."

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        text = ""
        async for event, data in response_generator:
            if event == "message":
                text += str(data)
        yield "done", text
        yield "meta", {"provider": "mock-telemetry", "model": "mock-model", "usage": {"total_tokens": 12}}


def create_request() -> ModelRequest:
    settings = Settings(name="TelemetryExampleSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="TelemetryExamplePluginManager")
    plugin_manager.register("ModelRequester", TelemetryMockRequester, activate=True)
    return ModelRequest(
        plugin_manager,
        agent_name="telemetry-example-agent",
        agent_id="telemetry-example-agent",
        parent_settings=settings,
    )


async def run_once() -> str:
    request = create_request()
    request.input("Return the telemetry probe response.")
    response = request.get_response()
    return await response.async_get_text()


async def main():
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "examples.devtools.model_request_telemetry"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        text = await run_once()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    hookless_text = await run_once()
    telemetry_events = [
        event for event in captured if isinstance(event.payload, dict) and "model_request_telemetry" in event.payload
    ]
    telemetry_items = [event.payload["model_request_telemetry"] for event in telemetry_events]
    meta_telemetry = next(item for item in telemetry_items if item["event_kind"] == "model.meta")
    requesting_telemetry = next(item for item in telemetry_items if item["event_kind"] == "model.requesting")

    summary = {
        "text": text,
        "hookless_text": hookless_text,
        "event_types": [event.event_type for event in telemetry_events],
        "telemetry_count": len(telemetry_items),
        "attempt_indexes": sorted({item["attempt_index"] for item in telemetry_items}),
        "response_ids_linked": len({item["response_id"] for item in telemetry_items}) == 1,
        "request_url": requesting_telemetry["request_url"],
        "provider": meta_telemetry["provider"],
        "model": meta_telemetry["model"],
        "usage_total_tokens": meta_telemetry["usage"]["total_tokens"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output from a real local infrastructure-probe run:
# telemetry_count == 4
# event_types == ["model.request_started", "model.requesting", "model.completed", "model.meta"]
# attempt_indexes == [1]
# response_ids_linked == true
# request_url == "mock://telemetry-requester"
# provider == "mock-telemetry"
# model == "mock-model"
# usage_total_tokens == 12
# hookless_text == "Telemetry local response."
