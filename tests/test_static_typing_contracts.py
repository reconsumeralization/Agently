from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args, get_origin, get_type_hints

import pytest
from typing_extensions import assert_type, get_overloads

import agently as agently_package
from agently import (
    Agent,
    AgentExecutionStreamData as RootAgentExecutionStreamData,
    Agently,
    AgentlyModelResultEvent as RootAgentlyModelResultEvent,
    AgentlyModelResultMessage as RootAgentlyModelResultMessage,
    AgentlyOriginalResultPayload as RootAgentlyOriginalResultPayload,
    AgentlySpecificResultMessage as RootAgentlySpecificResultMessage,
    EventHook as RootEventHook,
    ModelStreamingHandler as RootModelStreamingHandler,
    ResultContentType as RootResultContentType,
    RuntimeEvent as RootRuntimeEvent,
    RuntimeEventHook as RootRuntimeEventHook,
    SkillRuntimeStreamHandler as RootSkillRuntimeStreamHandler,
    SkillRuntimeStreamItem as RootSkillRuntimeStreamItem,
    StreamingData as RootStreamingData,
)
from agently.core import AgentExecutionResult, BaseAgent, ModelRequestResult, TaskWorkspace
from agently.types.data import (
    AgentArtifactContext,
    AgentArtifactHandler,
    AgentArtifactResult,
    AgentExecutionEffort,
    AgentInteractionHandler,
    AgentReviewContext,
    AgentReviewHandler,
    AgentExecutionStreamData,
    AgentExecutionStrategy,
    ExecutionExchangeView,
    AgentlyModelResultEvent,
    AgentlyModelResultMessage,
    AgentlyResultGenerator,
    AgentlyModelResponseMessage,
    AgentlyResponseGenerator,
    AgentlyOriginalResultPayload,
    AgentlyOriginalResponsePayload,
    AgentlySpecificResultMessage,
    AgentlySpecificResponseMessage,
    ModelStreamingHandler,
    ResponseContentType,
    ResultContentType,
    SkillRuntimeStreamHandler,
    StreamingData,
    TaskBoardGraph,
    TaskBoardRevision,
)
from agently.types.data import AgentExecutionName, AgentExecutionLineage, AgentExecutionLimits, RunContext
from agently.types.options import ExecutionOptions
from agently.types.plugins import (
    ActionExecutor,
    AgentExecution,
    ExecutionResourceProvider,
)


def test_agent_execution_and_model_response_streaming_type_contracts():
    if TYPE_CHECKING:
        agent: BaseAgent = Agently.create_agent("typing-contract")
        full_agent: Agent = Agently.create_agent("typing-fluent-contract")

        def sample_action(value: str) -> str:
            return value

        assert_type(agent.input("hello"), AgentExecution)
        assert_type(agent.input("persistent", always=True), Agent)
        assert_type(agent.input("hello").get_result(), AgentExecutionResult)

        execution = agent.create_execution().input("hello").output({"reply": (str,)})
        assert_type(execution, AgentExecution)
        assert_type(agent.input("hello").info("context"), AgentExecution)
        assert_type(execution.info("updated context"), AgentExecution)
        assert_type(agent.input("next turn").output({"reply": (str,)}), AgentExecution)
        assert_type(execution.input("reuse draft").output({"reply": (str,)}), AgentExecution)
        assert_type(execution.ensure_long_output(), AgentExecution)
        assert_type(execution.auto_continue(), AgentExecution)
        assert_type(execution.auto_continue(False).input("updated draft"), AgentExecution)
        assert_type(execution.artifact("result.txt"), AgentExecution)
        assert_type(agent.goal("ship", ["tests pass"]), AgentExecution)
        assert_type(agent.goal("explain", turn_on_long_task=False), AgentExecution)
        assert_type(agent.goals(["explain"], turn_on_long_task=False), AgentExecution)
        assert_type(agent.goals(["ship", "document"], ("tests pass",)), AgentExecution)
        assert_type(execution.goal(["ship", "document"], ("tests pass",)), AgentExecution)
        assert_type(execution.goal("explain", turn_on_long_task=False), AgentExecution)
        assert_type(execution.goals("explain", turn_on_long_task=False), AgentExecution)
        assert_type(agent.create_execution("plan"), AgentExecution)
        assert_type(agent.create_execution("long_content"), AgentExecution)
        assert_type(agent.create_execution("custom"), AgentExecution)
        assert_type(agent.interact(lambda _exchange: "answer"), AgentExecution)
        assert_type(execution.interact(lambda _exchange: {"answer": "value"}), AgentExecution)
        assert_type(execution.review(), AgentExecution)
        assert_type(execution.review(rules=["Check evidence."], on_fail="block"), AgentExecution)
        assert_type(execution.effort("high"), AgentExecution)
        assert_type(execution.effort({"name": "high", "planning": {"depth": "deep"}}), AgentExecution)
        assert_type(execution.effort("team_profile"), AgentExecution)
        assert_type(execution.strategy("taskboard"), AgentExecution)
        assert_type(execution.strategy("custom_strategy"), AgentExecution)
        assert_type(full_agent.use_actions(sample_action), AgentExecution)
        assert_type(full_agent.use_action(actions=sample_action), AgentExecution)
        assert_type(full_agent.use_tools(sample_action), AgentExecution)
        assert_type(full_agent.use_tool(tools=sample_action), AgentExecution)
        assert_type(full_agent.require_actions(sample_action), AgentExecution)
        assert_type(full_agent.use_actions(sample_action, always=True), Agent)
        assert_type(full_agent.use_action(sample_action, always=True), Agent)
        assert_type(full_agent.require_actions(sample_action, always=True), Agent)
        assert_type(execution.use_actions(sample_action), AgentExecution)
        assert_type(execution.use_action(actions=sample_action), AgentExecution)
        assert_type(execution.use_tools(sample_action), AgentExecution)
        assert_type(execution.use_tool(tools=sample_action), AgentExecution)
        assert_type(execution.require_actions(sample_action), AgentExecution)
        assert_type(full_agent.use_skills("writer"), AgentExecution)
        assert_type(full_agent.require_skills("writer"), AgentExecution)
        assert_type(full_agent.use_skills_packs("writing"), AgentExecution)
        assert_type(full_agent.use_skills("writer", always=True), Agent)
        assert_type(full_agent.require_skills("writer", always=True), Agent)
        assert_type(full_agent.use_skills_packs("writing", always=True), Agent)
        assert_type(execution.use_skills("writer"), AgentExecution)
        assert_type(execution.require_skills("writer"), AgentExecution)
        assert_type(execution.use_skills_packs("writing"), AgentExecution)
        assert_type(full_agent.set_action_loop(planning_protocol="programmatic"), Agent)
        assert_type(
            full_agent.register_action(
                name="sample_action",
                desc="Return the supplied value.",
                kwargs={"value": str},
                func=sample_action,
                concurrency_mode="parallel",
            ),
            Agent,
        )
        assert_type(execution.get_generator(), Generator[str, None, None])
        assert_type(execution.get_generator(type="delta"), Generator[str, None, None])
        assert_type(execution.get_generator(type="instant"), Generator[AgentExecutionStreamData, None, None])
        assert_type(execution.get_generator(type="specific"), Generator[AgentlySpecificResultMessage, None, None])
        assert_type(execution.get_generator(type="all"), Generator[tuple[str, AgentExecutionStreamData], None, None])
        assert_type(execution.get_generator(type="original"), Generator[AgentExecutionStreamData, None, None])

        assert_type(execution.get_async_generator(), AsyncGenerator[str, None])
        assert_type(execution.get_async_generator(type="delta"), AsyncGenerator[str, None])
        assert_type(execution.get_async_generator(type="instant"), AsyncGenerator[AgentExecutionStreamData, None])
        assert_type(execution.get_async_generator(type="specific"), AsyncGenerator[AgentlySpecificResultMessage, None])
        assert_type(execution.get_async_generator(type="all"), AsyncGenerator[tuple[str, AgentExecutionStreamData], None])
        assert_type(execution.get_async_generator(type="original"), AsyncGenerator[AgentExecutionStreamData, None])
        assert_type(execution.streaming_print(), None)

        result: ModelRequestResult = agent.create_request().input("hello").get_result()
        assert_type(result.get_generator(type="instant"), Generator[StreamingData, None, None])
        assert_type(result.get_async_generator(type="specific"), AsyncGenerator[AgentlySpecificResultMessage, None])

        compat_result: ModelRequestResult = agent.create_request().input("hello").get_response()
        assert_type(compat_result.get_data(ensure_keys=["reply"]), dict[str, Any])
        assert_type(compat_result.get_text(), str)
        assert_type(compat_result.result.get_text(), str)

        execution_result = execution.get_result()
        assert_type(execution_result.get_data(ensure_keys=["reply"]), dict[str, Any])
        assert_type(execution_result.get_text(), str)
        assert_type(execution_result.get_generator(type="instant"), Generator[AgentExecutionStreamData, None, None])
        assert_type(execution_result.get_async_generator(type="instant"), AsyncGenerator[AgentExecutionStreamData, None])


def test_public_handler_type_aliases():
    if TYPE_CHECKING:
        from agently.builtins.plugins.AgentExecution import (
            AgentExecution as BundledExecution,
            RequestExecution, LongTaskExecution, PlanExecution, LongContentExecution,
        )
        concrete = BundledExecution(Agently.create_agent())
        plugin_contract: AgentExecution = concrete
        producer_contracts: list[AgentExecution] = [
            RequestExecution(Agently.create_agent()),
            LongTaskExecution(Agently.create_agent()),
            PlanExecution(Agently.create_agent()),
            LongContentExecution(Agently.create_agent()),
        ]

        def review_handler(_result: Any, _context: AgentReviewContext) -> bool:
            return True

        def artifact_handler(_result: object, _context: AgentArtifactContext) -> str:
            return "artifact"

        async def interaction_handler(exchange: ExecutionExchangeView) -> object:
            assert_type(exchange, ExecutionExchangeView)
            return {"answer": "provided"}

        async def model_stream_handler(item: StreamingData) -> None:
            assert_type(item, StreamingData)

        def skills_stream_handler(item: dict[str, Any]) -> None:
            assert_type(item, dict[str, Any])

        model_handler: ModelStreamingHandler = model_stream_handler
        skills_handler: SkillRuntimeStreamHandler = skills_stream_handler
        agent_review_handler: AgentReviewHandler = review_handler
        agent_artifact_handler: AgentArtifactHandler = artifact_handler
        agent_interaction_handler: AgentInteractionHandler = interaction_handler
        agent: BaseAgent = Agently.create_agent("typing-handler-contract")
        assert callable(model_handler)
        assert callable(skills_handler)
        assert callable(agent_review_handler)
        assert callable(agent_artifact_handler)
        assert callable(agent_interaction_handler)


def test_handler_context_members_are_concretely_typed():
    if TYPE_CHECKING:
        review_context = cast(AgentReviewContext, object())
        artifact_context = cast(AgentArtifactContext, object())

        assert_type(review_context.execution, AgentExecution)
        assert_type(review_context.task_workspace, TaskWorkspace)
        assert_type(review_context.artifact_refs, tuple[AgentArtifactResult, ...])
        assert_type(artifact_context.execution, AgentExecution)
        assert_type(artifact_context.task_workspace, TaskWorkspace)


def _literal_values(annotation: object) -> set[object]:
    values: set[object] = set()
    if get_origin(annotation) is Literal:
        values.update(get_args(annotation))
    for item in get_args(annotation):
        if get_origin(item) is Literal:
            values.update(get_args(item))
    return values


def test_agent_execution_choice_aliases_keep_builtin_editor_candidates():
    assert _literal_values(AgentExecutionName) == {
        "auto",
        "request",
        "long_task",
        "plan",
        "long_content",
    }
    assert _literal_values(AgentExecutionStrategy) == {
        "auto",
        "direct",
        "task",
        "task_loop",
        "long_task",
        "flat",
        "taskboard",
    }
    assert _literal_values(AgentExecutionEffort) == {
        "minimal",
        "low",
        "fast",
        "medium",
        "normal",
        "high",
        "max",
    }

    expected_builtin_hints = {
        "effort": {"minimal", "low", "fast", "medium", "normal", "high", "max"},
        "strategy": {"auto", "direct", "task", "task_loop", "long_task", "flat", "taskboard"},
    }
    name_overload = get_overloads(BaseAgent.create_execution)[0]
    assert _literal_values(get_type_hints(name_overload, localns=globals())["name"]) == {
        "auto", "request", "long_task", "plan", "long_content",
    }
    for owner in (BaseAgent, AgentExecution):
        for method_name, expected in expected_builtin_hints.items():
            overloads = get_overloads(getattr(owner, method_name))
            assert len(overloads) == 2
            parameter_name = "value"
            builtin_hint = get_type_hints(
                overloads[0],
                localns={"AgentExecution": AgentExecution},
            )[parameter_name]
            assert _literal_values(builtin_hint) == expected


def test_agent_task_execution_hint_is_a_closed_host_choice():
    execution_hint = get_type_hints(
        BaseAgent.create_task,
        localns={"AgentExecution": AgentExecution},
    )["execution"]
    assert _literal_values(execution_hint) == {
        "auto",
        "flat",
        "taskboard",
        "default",
        "automatic",
        "linear",
        "react",
        "flat_react",
        "task_board",
        "board",
        "taskboard_evidenceview",
    }
    assert str not in get_args(execution_hint)


def _pyright_command() -> list[str] | None:
    executable = shutil.which("pyright")
    if executable is not None:
        return [executable]
    if importlib.util.find_spec("pyright") is not None:
        return [sys.executable, "-m", "pyright"]
    return None


def test_unknown_finite_choices_are_rejected_by_pyright():
    pyright_command = _pyright_command()
    if pyright_command is None:
        pytest.skip("pyright executable is not installed in this test environment")
    fixture_dir = Path(__file__).parent / "typing_fixtures"
    completed = subprocess.run(
        [
            *pyright_command,
            "--pythonpath",
            sys.executable,
            "--project",
            str(fixture_dir / "pyrightconfig.json"),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode != 0
    assert "agent_execution_finite_invalid.py:5" in output
    assert "reportArgumentType" in output
    assert "parallel" in output
    assert "release_4_1_4_8_finite_invalid.py:9" in output
    assert "unknown_protocol" in output
    assert "release_4_1_4_8_finite_invalid.py:15" in output
    assert "serial" in output


def test_changed_runtime_protocols_are_publicly_typed():
    if TYPE_CHECKING:
        action_executor = cast(ActionExecutor, object())
        resource_provider = cast(ExecutionResourceProvider, object())

        assert_type(action_executor, ActionExecutor)
        assert_type(resource_provider, ExecutionResourceProvider)


def test_agent_execution_stream_protocol_contract():
    if TYPE_CHECKING:
        execution = cast(AgentExecution, object())

        assert_type(execution.info("context"), AgentExecution)
        assert_type(execution.get_async_generator(), AsyncGenerator[str, None])
        assert_type(execution.get_async_generator(type="instant"), AsyncGenerator[AgentExecutionStreamData, None])
        assert_type(execution.get_generator(), Generator[str, None, None])
        assert_type(execution.get_generator(type="instant"), Generator[AgentExecutionStreamData, None, None])


def test_common_types_are_available_from_package_root():
    if TYPE_CHECKING:
        assert_type(RootStreamingData(path="reply", value="ok"), StreamingData)
        assert_type(cast(RootAgentExecutionStreamData, object()), AgentExecutionStreamData)
        assert_type(cast(RootAgentlyModelResultEvent, "delta"), AgentlyModelResultEvent)
        assert_type(cast(RootAgentlyModelResultMessage, object()), AgentlyModelResultMessage)
        assert_type(cast(RootAgentlySpecificResultMessage, object()), AgentlySpecificResultMessage)
        assert_type(cast(RootAgentlyOriginalResultPayload, object()), AgentlyOriginalResultPayload)
        assert_type(cast(RootResultContentType, "all"), ResultContentType)
        assert_type(cast(RootModelStreamingHandler, object()), ModelStreamingHandler)
        assert_type(cast(RootSkillRuntimeStreamItem, object()), dict[str, Any])
        assert_type(cast(RootSkillRuntimeStreamHandler, object()), SkillRuntimeStreamHandler)
        assert_type(cast(RootRuntimeEvent, object()), RootRuntimeEvent)
        assert_type(cast(RootEventHook, object()), RootEventHook)
        assert_type(cast(RootRuntimeEventHook, object()), RootRuntimeEventHook)


def test_response_named_aliases_stay_in_typed_data_namespace_only():
    assert not hasattr(agently_package, "AgentlyModelResponseMessage")
    assert not hasattr(agently_package, "AgentlySpecificResponseMessage")
    assert not hasattr(agently_package, "AgentlyResponseGenerator")
    assert not hasattr(agently_package, "ResponseContentType")

    if TYPE_CHECKING:
        assert_type(cast(AgentlyModelResponseMessage, object()), AgentlyModelResultMessage)
        assert_type(cast(AgentlySpecificResponseMessage, object()), AgentlySpecificResultMessage)
        assert_type(cast(AgentlyOriginalResponsePayload, object()), AgentlyOriginalResultPayload)
        assert_type(cast(AgentlyResponseGenerator, object()), AgentlyResultGenerator)
        assert_type(cast(ResponseContentType, "all"), ResultContentType)


def test_advanced_agent_execution_types_do_not_expand_the_package_root():
    assert not hasattr(agently_package, "AgentPatternName")
    assert not hasattr(agently_package, "AgentExecutionStrategy")
    assert not hasattr(agently_package, "AgentExecutionEffort")
    assert not hasattr(agently_package, "AgentArtifactResult")
    assert not hasattr(agently_package, "AgentInteractionHandler")


def test_task_board_public_update_methods_accept_dict_payloads():
    if TYPE_CHECKING:
        revision = TaskBoardRevision.create(
            board_id="typing-task-board",
            graph={
                "graph_id": "typing-task-board-graph",
                "cards": [{"id": "collect", "objective": "Collect facts."}],
            },
        )

        next_revision = revision.next_revision(
            {
                "graph_id": "typing-task-board-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect facts."},
                    {"id": "final", "objective": "Write final answer.", "depends_on": ["collect"]},
                ],
            },
            card_results={"collect": {"card_id": "collect", "status": "completed"}},
        )
        assert_type(next_revision, TaskBoardRevision)

        graph = TaskBoardGraph.from_value(
            {
                "graph_id": "typing-task-board-graph",
                "cards": [{"id": "collect", "objective": "Collect facts."}],
            }
        )
        assert_type(
            graph.with_cards(
                [
                    {"id": "collect", "objective": "Collect facts."},
                    {"id": "final", "objective": "Write final answer.", "depends_on": ["collect"]},
                ]
            ),
            TaskBoardGraph,
        )
