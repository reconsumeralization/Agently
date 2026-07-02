# Low-level ModelRequest infrastructure probe.
#
# This example intentionally uses a local ModelRequester that simulates one
# transient stream failure. It does not test model quality or fake a business
# result: it proves the public $status and delta retry-marker replay boundaries
# through the real ModelRequest -> ModelRequestRunner -> ResponseParser pipeline.
#
# Expected key output:
# {
#   "statuses": [
#     {"status": "failed", "attempt_index": 1, "retry": true,
#      "reason": "simulated connection reset after first output"},
#     {"status": "completed", "attempt_index": 2, "retry": false}
#   ],
#   "delta_chunks": [
#     "{\"reply\": \"partial\"}",
#     "<$retry>simulated connection reset after first output</$retry>",
#     "{\"reply\": \"replacement\"}"
#   ],
#   "delta_replayed_text": "{\"reply\": \"replacement\"}",
#   "final": {"reply": "replacement"}
# }

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from agently import Agently
from agently.core.model.AttemptRunner import core_attempt_runner_entrypoint
from agently.types.data import AgentlyRequestData, AttemptDecision, AttemptHandlers, AttemptState


class StatusProbeRequester:
    name = "StatusProbeRequester"
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

    def generate_request_data(self) -> AgentlyRequestData:
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"prompt_text": self.prompt.to_text()},
            request_options={"stream": True},
            request_url="probe://status",
        )

    def build_request_handlers(self, _request_data: AgentlyRequestData) -> AttemptHandlers:
        async def execute(state: AttemptState):
            if state.attempt_index == 1:
                yield "message", '{"reply": "partial"}'
                raise ConnectionError("simulated connection reset after first output")
            yield "message", '{"reply": "replacement"}'

        async def handle_error(error: BaseException, _state: AttemptState) -> AttemptDecision:
            if isinstance(error, ConnectionError):
                return AttemptDecision.retry(allow_after_output_started=True)
            return AttemptDecision.yield_error(error)

        return AttemptHandlers(execute=execute, handle_error=handle_error)

    @core_attempt_runner_entrypoint
    async def request_model(self, request_data: AgentlyRequestData) -> AsyncGenerator[tuple[str, Any], None]:
        handlers = self.build_request_handlers(request_data)
        async for item in handlers.execute(AttemptState()):
            yield item

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ) -> AsyncGenerator[tuple[str, Any], None]:
        response_text = ""
        async for event, data in response_generator:
            if event == "status":
                if isinstance(data, dict) and data.get("status") == "failed" and data.get("retry") is True:
                    response_text = ""
                yield event, data
                continue
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text


async def main() -> None:
    Agently.plugin_manager.register("ModelRequester", StatusProbeRequester, activate=True)
    agent = Agently.create_agent()
    result = agent.create_request().input("Run the infrastructure status probe.").output(
        {"reply": (str, "Response text.", True)},
        format="json",
    ).get_result()

    statuses: list[dict[str, Any]] = []
    async for item in result.get_async_generator(type="instant"):
        if item.path == "$status":
            statuses.append(
                {
                    key: item.value[key]
                    for key in ("status", "attempt_index", "retry", "reason")
                    if key in item.value
                }
            )

    delta_result = agent.create_request().input("Run the infrastructure status probe.").output(
        {"reply": (str, "Response text.", True)},
        format="json",
    ).get_result()
    delta_chunks = [
        chunk
        async for chunk in delta_result.get_async_generator(type="delta")
    ]
    delta_replayed_text = ""
    for chunk in delta_chunks:
        if "<$retry>" in chunk:
            delta_replayed_text = ""
            continue
        delta_replayed_text += chunk

    print(
        json.dumps(
            {
                "statuses": statuses,
                "delta_chunks": delta_chunks,
                "delta_replayed_text": delta_replayed_text,
                "final": await result.async_get_data(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
