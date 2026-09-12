from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData, AgentReviewContext
from agently.builtins.plugins.AgentExecution import AgentExecution, ProductionOptions, RequestExecution
from agently.utils import Settings


class ProducerRequester:
    name = "ProducerRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    requests: list[object] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls) -> None:
        cls.requests = []

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        request_input = self.prompt.get("input")
        type(self).requests.append(request_input)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"input": request_input},
            request_options={"stream": True},
            request_url="mock://agent-execution-producer",
        )

    async def request_model(self, _request_data: AgentlyRequestData):
        yield "message", "base-result"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {"provider": "mock-agent-execution-producer", "status": "completed"}


class WrappedExecution(RequestExecution):
    name = "wrapped"

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        route, result = await super()._async_produce(options)
        return route, f"wrapped:{result}"


class ConstantExecution(AgentExecution):
    name = "constant"
    producer_route = "constant"

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        return "constant", {"input": self.prompt_snapshot.get("input"), "source": "producer"}


if TYPE_CHECKING:
    from agently.types.plugins import AgentExecution as ExecutionProtocol
    _custom_contracts: list[ExecutionProtocol] = [
        WrappedExecution(Agently.create_agent()), ConstantExecution(Agently.create_agent()),
    ]


def create_producer_agent(tmp_path: Path, name: str):
    ProducerRequester.reset()
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-Plugins")
    plugin_manager.register("ModelRequester", ProducerRequester)
    plugin_manager.register("AgentExecution", WrappedExecution, activate=False)
    plugin_manager.register("AgentExecution", ConstantExecution, activate=False)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name).use_task_workspace(tmp_path / name)


@pytest.mark.asyncio
async def test_default_request_preserves_one_run_and_no_extra_model_pass(tmp_path):
    import asyncio

    agent = create_producer_agent(tmp_path, "ordinary")
    execution = agent.input("hello")
    captured = execution.get_result()
    results = await asyncio.gather(
        execution.async_run(), execution.async_start(), captured.async_get_data(),
    )
    assert results == ["base-result"] * 3
    assert ProducerRequester.requests == ["hello"]
    assert (await execution.async_get_meta())["plugin"] == "auto"
    assert not hasattr(execution, "pattern")
    assert not hasattr(agent, "pattern")
    assert not any(item.path.startswith("pattern.") for item in execution.stream.items)


@pytest.mark.asyncio
async def test_standard_interaction_handler_is_idle_without_an_exchange(tmp_path):
    agent = create_producer_agent(tmp_path, "idle-interaction")
    agent.settings.set("interaction.mode", "durable")
    calls = []
    execution = agent.interact(lambda exchange: calls.append(exchange)).input("hello")
    assert await execution.async_get_data() == "base-result"
    assert calls == []
    assert execution.request.settings.get("interaction.mode") == "hot"
    assert agent.settings.get("interaction.mode") == "durable"


def test_standard_interaction_handler_rejects_non_callable(tmp_path):
    agent = create_producer_agent(tmp_path, "invalid-interaction")
    with pytest.raises(TypeError, match="must be callable"):
        agent.interact("console")  # type: ignore[arg-type]
    assert ProducerRequester.requests == []


@pytest.mark.asyncio
async def test_named_producer_reuses_default_without_new_execution_identity(tmp_path):
    agent = create_producer_agent(tmp_path, "wrapped")
    execution = agent.create_execution("wrapped").input("hello")
    identity = execution.id
    # Check exact runtime identity without asking the checker to intersect a
    # recursive structural protocol with an exact class test.
    assert type(cast(object, execution)) is WrappedExecution
    assert await execution.async_get_data() == "wrapped:base-result"
    assert execution.id == identity
    assert ProducerRequester.requests == ["hello"]
    assert [item.path for item in execution.stream.items].count("route.selected") == 1


@pytest.mark.asyncio
async def test_producer_can_own_result_without_dispatching_root_request(tmp_path):
    agent = create_producer_agent(tmp_path, "constant")
    execution = agent.create_execution("constant").input("source")
    assert type(cast(object, execution)) is ConstantExecution
    assert await execution.async_get_data() == {"input": "source", "source": "producer"}
    assert ProducerRequester.requests == []
    assert execution.route_info["selected_route"] == "constant"


@pytest.mark.asyncio
async def test_producer_uses_existing_output_contract(tmp_path):
    agent = create_producer_agent(tmp_path, "output")
    seen = []

    class StructuredExecution(ConstantExecution):
        name = "structured"
        async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
            seen.append(self.prompt_snapshot["output"])
            return "constant", {"answer": "produced"}

    agent.plugin_manager.register("AgentExecution", StructuredExecution, activate=False)
    execution = agent.create_execution("structured").input("source").output({"answer": (str, "Final answer")})
    assert await execution.async_get_data() == {"answer": "produced"}
    assert seen[0]["answer"][0] is str
    assert ProducerRequester.requests == []


@pytest.mark.asyncio
async def test_producer_result_flows_through_artifact_then_review(tmp_path):
    agent = create_producer_agent(tmp_path, "terminal")
    observed = []
    def reviewer(result: object, context: AgentReviewContext) -> bool:
        observed.append((result, tuple(context.artifact_refs)))
        return True
    execution = agent.create_execution("wrapped").input("hello").review(reviewer).artifact("wrapped.txt")
    assert await execution.async_get_data() == "wrapped:base-result"
    assert observed == [("wrapped:base-result", tuple(execution.artifact_results))]
    path = execution.task_workspace.resolve_file_path(execution.artifact_results[0]["path"])
    assert path.read_text() == "wrapped:base-result"
    paths = [item.path for item in execution.stream.items]
    assert paths.index("artifact.completed") < paths.index("review.started")


@pytest.mark.asyncio
async def test_public_async_run_override_is_not_hidden_by_instance_aliases(tmp_path):
    agent = create_producer_agent(tmp_path, "override")
    entered = []
    class OverrideExecution(ConstantExecution):
        name = "override"
        async def async_run(self, **kwargs):
            entered.append("run")
            return await super().async_run(**kwargs)
    agent.plugin_manager.register("AgentExecution", OverrideExecution, activate=False)
    execution = agent.create_execution("override").input("hello")
    assert "run" not in vars(execution)
    assert "async_run" not in vars(execution)
    assert await execution.async_get_data() == {"input": "hello", "source": "producer"}
    assert entered == ["run"]


@pytest.mark.asyncio
async def test_failed_producer_is_not_silently_dispatched_again(tmp_path):
    agent = create_producer_agent(tmp_path, "failed")
    calls = []
    class FailedExecution(ConstantExecution):
        name = "failed"
        async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
            calls.append("produce")
            raise ValueError("producer failed")
    agent.plugin_manager.register("AgentExecution", FailedExecution, activate=False)
    execution = agent.create_execution("failed")
    for _ in range(2):
        with pytest.raises(ValueError, match="producer failed"):
            await execution.async_get_data()
    assert calls == ["produce"]
    assert execution.status == "error"
    assert ProducerRequester.requests == []
