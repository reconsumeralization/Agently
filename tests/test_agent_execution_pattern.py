from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData, AgentReviewContext
from agently.types.plugins import AgentPatternContinuation
from agently.utils import Settings


class PatternRequester:
    name = "PatternRequester"
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
            request_url="mock://agent-execution-pattern",
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
        yield "meta", {"provider": "mock-agent-execution-pattern", "status": "completed"}


class WrappedPattern:
    name = "wrapped"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def __init__(self, *, plugin_manager, settings):
        self.plugin_manager = plugin_manager
        self.settings = settings

    async def run(self, _execution, run_default: AgentPatternContinuation):
        return f"wrapped:{await run_default()}"


class ConstantPattern:
    name = "constant-instance"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    async def run(self, execution, _run_default: AgentPatternContinuation):
        return {"input": execution.prompt_snapshot.get("input"), "source": "instance"}


class InputNamedPattern:
    """A Pattern name must stay isolated from Agent.input(...)."""

    name = "input"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    init_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.init_count = 0

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def __init__(self, *, plugin_manager, settings):
        type(self).init_count += 1
        self.plugin_manager = plugin_manager
        self.settings = settings

    async def run(self, execution, _run_default: AgentPatternContinuation):
        return f"pattern:{execution.prompt_snapshot.get('input')}"


class MissingRunPattern:
    name = "missing_run"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def __init__(self, *, plugin_manager, settings):
        self.plugin_manager = plugin_manager
        self.settings = settings


def create_pattern_agent(tmp_path: Path, name: str, *, register_pattern: bool = False):
    PatternRequester.reset()
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-PluginManager")
    plugin_manager.register("ModelRequester", PatternRequester, activate=True)
    if register_pattern:
        plugin_manager.register("AgentPattern", WrappedPattern, activate=False)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    ).use_task_workspace(tmp_path / name)


@pytest.mark.asyncio
async def test_default_request_pattern_preserves_simple_request_lifecycle(tmp_path):
    agent = create_pattern_agent(tmp_path, "default-pattern")
    execution = agent.input("hello")

    assert await execution.async_get_data() == "base-result"
    assert PatternRequester.requests == ["hello"]
    assert execution.pattern_info == {
        "name": "request",
        "source": "builtin",
        "selected_by": "default",
        "status": "completed",
        "used_default": True,
    }
    assert not any(item.path.startswith("pattern.") for item in execution.stream.items)
    assert "pattern" not in await execution.async_get_meta()


@pytest.mark.asyncio
async def test_standard_interaction_handler_is_idle_without_an_exchange(tmp_path):
    agent = create_pattern_agent(tmp_path, "idle-interaction")
    agent.settings.set("interaction.mode", "durable")
    calls: list[object] = []

    execution = (
        agent.interact(lambda exchange: calls.append(exchange))
        .input("hello")
        .create_execution(limits={"max_model_requests": 1})
    )

    assert await execution.async_get_data() == "base-result"
    assert calls == []
    assert execution.request.settings.get("interaction.mode") == "hot"
    assert agent.settings.get("interaction.mode") == "durable"
    assert not any(item.path.startswith("exchange.") for item in execution.stream.items)


def test_standard_interaction_handler_rejects_non_callable(tmp_path):
    agent = create_pattern_agent(tmp_path, "invalid-interaction")

    with pytest.raises(TypeError, match="must be callable"):
        agent.interact("console")  # type: ignore[arg-type]

    assert PatternRequester.requests == []


@pytest.mark.asyncio
async def test_explicit_request_pattern_emits_pattern_lifecycle(tmp_path):
    agent = create_pattern_agent(tmp_path, "explicit-request-pattern")
    execution = agent.input("hello").pattern("request")

    assert await execution.async_get_data() == "base-result"
    assert execution.pattern_info["selected_by"] == "pattern"
    assert (await execution.async_get_meta())["pattern"] == execution.pattern_info
    paths = [item.path for item in execution.stream.items]
    assert paths.index("pattern.started") < paths.index("route.selected") < paths.index("pattern.completed")


@pytest.mark.asyncio
async def test_goal_selects_builtin_goal_pattern_without_a_pass_through_plugin(tmp_path, monkeypatch):
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules import route_execution

    agent = create_pattern_agent(tmp_path, "goal-pattern")

    async def fake_agent_task_route(execution, _route_meta):
        execution.status = "success"
        return {"final_response": "goal-complete"}

    monkeypatch.setattr(route_execution, "run_agent_task_route", fake_agent_task_route)
    execution = agent.goal("Complete the goal.", ["A result exists."]).strategy("flat")

    assert await execution.async_get_data() == {"final_response": "goal-complete"}
    assert execution.pattern_info == {
        "name": "goal",
        "source": "builtin",
        "selected_by": "goal",
        "status": "completed",
        "used_default": True,
    }
    paths = [item.path for item in execution.stream.items]
    assert not any(path.startswith("pattern.") for path in paths)
    assert "pattern" not in await execution.async_get_meta()


@pytest.mark.asyncio
async def test_registered_pattern_is_opt_in_and_method_name_is_isolated(tmp_path):
    agent = create_pattern_agent(tmp_path, "opt-in-pattern")
    InputNamedPattern.reset()
    agent.plugin_manager.register("AgentPattern", InputNamedPattern)

    ordinary = agent.input("ordinary")

    assert await ordinary.async_get_data() == "base-result"
    assert PatternRequester.requests == ["ordinary"]
    assert InputNamedPattern.init_count == 0
    assert callable(agent.input)
    assert callable(ordinary.input)
    assert not any(item.path.startswith("pattern.") for item in ordinary.stream.items)
    assert "pattern" not in await ordinary.async_get_meta()

    selected = agent.input("selected").pattern("input")

    assert await selected.async_get_data() == "pattern:selected"
    assert InputNamedPattern.init_count == 1
    assert PatternRequester.requests == ["ordinary"]
    assert selected.pattern_info["source"] == "plugin"


@pytest.mark.asyncio
async def test_invalid_registered_pattern_fails_before_model_dispatch(tmp_path):
    agent = create_pattern_agent(tmp_path, "invalid-plugin-pattern")
    agent.plugin_manager.register("AgentPattern", MissingRunPattern, activate=False)
    execution = agent.input("must-not-dispatch").pattern("missing_run")

    with pytest.raises(TypeError, match="must define callable run"):
        await execution.async_get_data()

    assert PatternRequester.requests == []


@pytest.mark.asyncio
async def test_named_pattern_plugin_wraps_default_route_once(tmp_path):
    agent = create_pattern_agent(tmp_path, "named-pattern", register_pattern=True)
    execution = agent.input("hello").pattern("wrapped")

    assert await execution.async_get_data() == "wrapped:base-result"
    assert PatternRequester.requests == ["hello"]
    assert execution.pattern_info["name"] == "wrapped"
    assert execution.pattern_info["source"] == "plugin"
    assert execution.pattern_info["used_default"] is True
    assert [item.path for item in execution.stream.items].count("route.selected") == 1


@pytest.mark.asyncio
async def test_direct_pattern_handler_can_own_result_without_replaying_root_request(tmp_path):
    agent = create_pattern_agent(tmp_path, "direct-pattern")

    async def direct(execution, _run_default: AgentPatternContinuation):
        return {"input": execution.prompt_snapshot.get("input"), "source": "handler"}

    execution = agent.input("pattern-input").pattern(direct)

    assert await execution.async_get_data() == {"input": "pattern-input", "source": "handler"}
    assert PatternRequester.requests == []
    assert execution.route_info["selected_route"] == "agent_pattern"
    assert execution.pattern_info["source"] == "handler"
    assert execution.pattern_info["used_default"] is False


@pytest.mark.asyncio
async def test_pattern_instance_consumes_existing_execution_input(tmp_path):
    agent = create_pattern_agent(tmp_path, "instance-pattern")
    execution = agent.input("instance-input").pattern(ConstantPattern())

    assert await execution.async_get_data() == {"input": "instance-input", "source": "instance"}
    assert execution.pattern_info["source"] == "instance"
    assert PatternRequester.requests == []


@pytest.mark.asyncio
async def test_pattern_consumes_existing_output_contract_without_a_second_input_api(tmp_path):
    agent = create_pattern_agent(tmp_path, "pattern-output-contract")
    observed: list[object] = []

    def owns_request(execution, _run_default: AgentPatternContinuation):
        observed.append(execution.prompt_snapshot.get("output"))
        return {"answer": "pattern-result"}

    execution = (
        agent.input("pattern-input")
        .output({"answer": (str, "final answer")})
        .pattern(owns_request)
    )

    assert await execution.async_get_data() == {"answer": "pattern-result"}
    assert isinstance(observed[0], dict)
    assert observed[0]["answer"][0] is str  # type: ignore[index]


@pytest.mark.asyncio
async def test_pattern_result_flows_through_artifact_then_review(tmp_path):
    agent = create_pattern_agent(tmp_path, "pattern-terminal-pipeline", register_pattern=True)
    observed: list[tuple[object, tuple[object, ...]]] = []

    def reviewer(result: object, context: AgentReviewContext) -> bool:
        observed.append((result, tuple(context.artifact_refs)))
        return True

    execution = (
        agent.input("hello")
        .pattern("wrapped")
        .review(reviewer)
        .artifact("wrapped.txt")
    )

    assert await execution.async_get_data() == "wrapped:base-result"
    assert observed == [("wrapped:base-result", tuple(execution.artifact_results))]
    artifact_path = execution.task_workspace.resolve_file_path(execution.artifact_results[0]["path"])
    assert artifact_path.read_text() == "wrapped:base-result"
    paths = [item.path for item in execution.stream.items]
    assert paths.index("pattern.completed") < paths.index("artifact.completed") < paths.index("review.started")


@pytest.mark.asyncio
async def test_pattern_default_continuation_is_strictly_one_use(tmp_path):
    agent = create_pattern_agent(tmp_path, "one-use-pattern")

    async def invalid(_execution, run_default: AgentPatternContinuation):
        await run_default()
        try:
            await run_default()
        except RuntimeError:
            return "pattern-tried-to-hide-duplicate"

    execution = agent.input("hello").pattern(invalid)

    with pytest.raises(RuntimeError, match="at most once"):
        await execution.async_get_data()

    assert PatternRequester.requests == ["hello"]
    assert execution.status == "error"
    assert execution.pattern_info["status"] == "failed"
    assert "pattern.failed" in [item.path for item in execution.stream.items]


@pytest.mark.asyncio
async def test_pattern_selection_replaces_instead_of_implicitly_chaining(tmp_path):
    agent = create_pattern_agent(tmp_path, "single-pattern")
    calls: list[str] = []

    def first(_execution, _run_default: AgentPatternContinuation):
        calls.append("first")
        return "first"

    def second(_execution, _run_default: AgentPatternContinuation):
        calls.append("second")
        return "second"

    execution = agent.input("hello").pattern(first).pattern(second)

    assert await execution.async_get_data() == "second"
    assert calls == ["second"]


@pytest.mark.asyncio
async def test_unknown_named_pattern_fails_closed(tmp_path):
    agent = create_pattern_agent(tmp_path, "unknown-pattern")
    execution = agent.input("hello").pattern("missing")

    with pytest.raises(ValueError, match="is not registered"):
        await execution.async_get_data()

    assert execution.status == "error"
    assert execution.pattern_info["status"] == "failed"
    assert "pattern.failed" in [item.path for item in execution.stream.items]


def test_pattern_rejects_invalid_selection(tmp_path):
    agent = create_pattern_agent(tmp_path, "invalid-pattern")

    with pytest.raises(ValueError, match="non-empty"):
        agent.input("hello").pattern("")
    with pytest.raises(TypeError, match="registered name"):
        agent.input("hello").pattern(42)  # type: ignore[arg-type]
