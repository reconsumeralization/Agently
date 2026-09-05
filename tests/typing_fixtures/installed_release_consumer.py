from __future__ import annotations

from typing import TYPE_CHECKING

from typing_extensions import assert_type

from agently import Agent, Agently, __version__
from agently.types.data import AgentArtifactContext, AgentReviewContext
from agently.types.plugins import AgentExecution


def lookup(value: str) -> str:
    return value


agent: Agent = Agently.create_agent("installed-typing-smoke")
execution = agent.input("check").info("installed wheel").use_action(lookup)

assert_type(__version__, str)
assert_type(execution, AgentExecution)
assert_type(execution.use_tool(lookup), AgentExecution)
assert_type(execution.require_actions(lookup), AgentExecution)
assert_type(execution.use_skills("writer"), AgentExecution)
assert_type(execution.pattern("plan"), AgentExecution)
assert_type(execution.review(), AgentExecution)
assert_type(execution.verify(), AgentExecution)
assert_type(execution.artifact("report.md"), AgentExecution)


if TYPE_CHECKING:
    def review_handler(_result: object, context: AgentReviewContext) -> bool:
        assert_type(context.execution, AgentExecution)
        return True

    def artifact_handler(_result: object, context: AgentArtifactContext) -> str:
        assert_type(context.execution, AgentExecution)
        return "rendered"

    assert_type(agent.use_actions(lookup, always=True), Agent)
    assert_type(agent.require_skills("writer", always=True), Agent)
    assert_type(execution.review(review_handler), AgentExecution)
    assert_type(execution.artifact("report.md", artifact_handler), AgentExecution)
