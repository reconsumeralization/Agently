# Low-level AgentExecution stream-projection probe.
#
# This uses a local ModelRequester only to force a real provider-attempt replay
# through Agently. It is an infrastructure probe, not a model-quality or
# business-result example. The exercise proves that AgentExecution projects
# ModelRequestResult attempt facts as structured stream items instead of
# injecting the direct-request retry marker into its business delta stream.
#
# Expected key output:
# {
#   "statuses": ["failed", "completed"],
#   "status_paths": ["$status", "$status"],
#   "lineage_present": true,
#   "retry_boundary_ordered": true,
#   "retry_marker_in_execution_delta": false
# }

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from agently import Agently
from agently.core import PluginManager
from agently.core.model.AttemptRunner import core_attempt_runner_entrypoint
from agently.types.data import AgentlyRequestData, AttemptDecision, AttemptHandlers, AttemptState
from agently.utils import Settings


class ProjectionProbeRequester:
    name = "ProjectionProbeRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self, prompt: Any, settings: Any):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"prompt_text": self.prompt.to_text()},
            request_options={"stream": True},
            request_url="probe://agent-execution-status",
        )

    def build_request_handlers(self, _request_data: AgentlyRequestData) -> AttemptHandlers:
        async def execute(state: AttemptState) -> AsyncGenerator[tuple[str, str], None]:
            if state.attempt_index == 1:
                yield "message", "partial output"
                raise ConnectionError("simulated connection reset after first output")
            yield "message", "replacement output"

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
                if isinstance(data, dict) and data.get("status") == "failed" and data.get("retry"):
                    response_text = ""
                yield event, data
            elif event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text


def create_probe_agent():
    settings = Settings(name="ProjectionProbeSettings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="ProjectionProbePluginManager",
    )
    plugin_manager.register("ModelRequester", ProjectionProbeRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name="projection-probe")


async def main() -> None:
    execution = create_probe_agent().input("Run the AgentExecution status projection probe.")
    items = [item async for item in execution.get_async_generator(type="instant")]
    status_items = [item for item in items if item.path == "$status"]
    delta_items = [item for item in items if item.path == "model.delta"]
    lineage_keys = {"response_id", "request_run_id", "model_run_id", "attempt_index"}

    print(
        json.dumps(
            {
                "statuses": [str(item.value.get("status")) for item in status_items],
                "status_paths": [item.path for item in status_items],
                "lineage_present": all(
                    item.meta is not None and lineage_keys <= set(item.meta)
                    for item in [*status_items, *delta_items]
                ),
                "retry_boundary_ordered": (
                    bool(status_items and len(delta_items) == 2)
                    and items.index(delta_items[0]) < items.index(status_items[0]) < items.index(delta_items[1])
                ),
                "retry_marker_in_execution_delta": any(
                    "<$retry>" in str(item.delta or item.value) for item in delta_items
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
