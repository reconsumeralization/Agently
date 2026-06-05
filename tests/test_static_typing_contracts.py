from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from typing import TYPE_CHECKING, Any, cast

from typing_extensions import assert_type

from agently import (
    Agent,
    AgentExecutionStreamData as RootAgentExecutionStreamData,
    Agently,
    AgentlyModelResultEvent as RootAgentlyModelResultEvent,
    AgentlyModelResultMessage as RootAgentlyModelResultMessage,
    AgentlyModelResponseMessage as RootAgentlyModelResponseMessage,
    AgentlyOriginalResultPayload as RootAgentlyOriginalResultPayload,
    AgentlyOriginalResponsePayload as RootAgentlyOriginalResponsePayload,
    AgentlySpecificResultMessage as RootAgentlySpecificResultMessage,
    AgentlySpecificResponseMessage as RootAgentlySpecificResponseMessage,
    EventHook as RootEventHook,
    ModelStreamingHandler as RootModelStreamingHandler,
    RuntimeEvent as RootRuntimeEvent,
    RuntimeEventHook as RootRuntimeEventHook,
    SkillRuntimeStreamHandler as RootSkillRuntimeStreamHandler,
    SkillRuntimeStreamItem as RootSkillRuntimeStreamItem,
    StreamingData as RootStreamingData,
)
from agently.core import AgentTurn, BaseAgent, ModelResponseResult
from agently.types.data import (
    AgentExecutionStreamData,
    AgentlyModelResultEvent,
    AgentlyModelResultMessage,
    AgentlyModelResponseMessage,
    AgentlyOriginalResultPayload,
    AgentlyOriginalResponsePayload,
    AgentlySpecificResultMessage,
    AgentlySpecificResponseMessage,
    ModelStreamingHandler,
    SkillRuntimeStreamHandler,
    StreamingData,
)
from agently.types.plugins import AgentExecution, SkillsPlanningContext


def test_agent_turn_and_model_response_streaming_type_contracts():
    if TYPE_CHECKING:
        agent: BaseAgent = Agently.create_agent("typing-contract")

        assert_type(agent.input("hello"), AgentTurn)
        assert_type(agent.input("persistent", always=True), Agent)

        turn = agent.create_turn().input("hello").output({"reply": (str,)})
        assert_type(turn.get_generator(type="delta"), Generator[str, None, None])
        assert_type(turn.get_generator(type="instant"), Generator[StreamingData, None, None])
        assert_type(turn.get_generator(type="specific"), Generator[AgentlySpecificResultMessage, None, None])
        assert_type(turn.get_generator(type="all"), Generator[AgentlyModelResultMessage, None, None])
        assert_type(turn.get_generator(type="original"), Generator[AgentlyOriginalResultPayload, None, None])

        assert_type(turn.get_async_generator(type="delta"), AsyncGenerator[str, None])
        assert_type(turn.get_async_generator(type="instant"), AsyncGenerator[StreamingData, None])
        assert_type(turn.get_async_generator(type="specific"), AsyncGenerator[AgentlySpecificResultMessage, None])
        assert_type(turn.get_async_generator(type="all"), AsyncGenerator[AgentlyModelResultMessage, None])
        assert_type(turn.get_async_generator(type="original"), AsyncGenerator[AgentlyOriginalResultPayload, None])

        result: ModelResponseResult = agent.create_request().input("hello").get_result()
        assert_type(result.get_generator(type="instant"), Generator[StreamingData, None, None])
        assert_type(result.get_async_generator(type="specific"), AsyncGenerator[AgentlySpecificResultMessage, None])

        compat_result: ModelResponseResult = agent.create_request().input("hello").get_response()
        assert_type(compat_result.result.get_text(), str)


def test_public_handler_type_aliases():
    if TYPE_CHECKING:
        async def model_stream_handler(item: StreamingData) -> None:
            assert_type(item, StreamingData)

        def skills_stream_handler(item: dict[str, Any]) -> None:
            assert_type(item, dict[str, Any])

        model_handler: ModelStreamingHandler = model_stream_handler
        skills_handler: SkillRuntimeStreamHandler = skills_stream_handler


def test_agent_execution_stream_protocol_contract():
    if TYPE_CHECKING:
        execution = cast(AgentExecution, object())

        assert_type(execution.get_async_generator(type="instant"), AsyncGenerator[AgentExecutionStreamData, None])
        assert_type(execution.get_generator(type="instant"), Generator[AgentExecutionStreamData, None, None])


def test_skills_planning_context_model_stream_handler_contract():
    if TYPE_CHECKING:
        context = cast(SkillsPlanningContext, object())

        async def handler(item: StreamingData) -> None:
            assert_type(item, StreamingData)

        _result = context.async_request_model(prompt="hello", stream_handler=handler)


def test_common_types_are_available_from_package_root():
    if TYPE_CHECKING:
        assert_type(RootStreamingData(path="reply", value="ok"), StreamingData)
        assert_type(cast(RootAgentExecutionStreamData, object()), AgentExecutionStreamData)
        assert_type(cast(RootAgentlyModelResultEvent, "delta"), AgentlyModelResultEvent)
        assert_type(cast(RootAgentlyModelResultMessage, object()), AgentlyModelResultMessage)
        assert_type(cast(RootAgentlySpecificResultMessage, object()), AgentlySpecificResultMessage)
        assert_type(cast(RootAgentlyOriginalResultPayload, object()), AgentlyOriginalResultPayload)
        assert_type(cast(RootAgentlyModelResponseMessage, object()), AgentlyModelResponseMessage)
        assert_type(cast(RootAgentlySpecificResponseMessage, object()), AgentlySpecificResponseMessage)
        assert_type(cast(RootAgentlyOriginalResponsePayload, object()), AgentlyOriginalResponsePayload)
        assert_type(cast(RootModelStreamingHandler, object()), ModelStreamingHandler)
        assert_type(cast(RootSkillRuntimeStreamItem, object()), dict[str, Any])
        assert_type(cast(RootSkillRuntimeStreamHandler, object()), SkillRuntimeStreamHandler)
        assert_type(cast(RootRuntimeEvent, object()), RootRuntimeEvent)
        assert_type(cast(RootEventHook, object()), RootEventHook)
        assert_type(cast(RootRuntimeEventHook, object()), RootRuntimeEventHook)
