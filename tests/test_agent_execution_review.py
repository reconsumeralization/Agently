import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agently import Agently
from agently.core import AgentVerificationError, PluginManager
from agently.types.data import AgentlyRequestData, AgentReviewContext
from agently.utils import Settings


class ReviewRequester:
    name = "ReviewRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    requests: list[dict[str, Any]] = []

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
        is_review = isinstance(request_input, dict) and "verification_is_required" in request_input
        payload = {
            "is_review": is_review,
            "input": request_input,
            "output": self.prompt.get("output"),
        }
        type(self).requests.append(payload)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data=payload,
            request_options={"stream": True},
            request_url="mock://agent-execution-review",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        if request_data.data["is_review"]:
            content = json.dumps(
                {
                    "passed": True,
                    "score": 0.92,
                    "summary": "The candidate meets the supplied contract.",
                    "issues": [],
                    "suggestions": ["Keep the answer concise."],
                }
            )
        else:
            content = "candidate-result"
        yield "message", content

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {"provider": "mock-agent-execution-review", "status": "completed"}


def create_review_agent(tmp_path, name: str):
    ReviewRequester.reset()
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-PluginManager")
    plugin_manager.register("ModelRequester", ReviewRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    ).use_task_workspace(tmp_path / name)


@pytest.mark.asyncio
async def test_advisory_review_records_failure_without_changing_result(tmp_path):
    agent = create_review_agent(tmp_path, "advisory-review")
    contexts: list[AgentReviewContext] = []

    def reviewer(result: Any, context: AgentReviewContext):
        contexts.append(context)
        assert result == "candidate-result"
        return {
            "passed": False,
            "score": 0.4,
            "summary": "Useful but incomplete.",
            "issues": ["Missing one requested detail."],
            "suggestions": ["Add that detail."],
        }

    execution = agent.input("Produce a concise result.").review(reviewer)

    assert await execution.async_get_data() == "candidate-result"
    assert execution.status == "success"
    assert len(contexts) == 1
    assert contexts[0].required is False
    assert execution.review_results[0]["passed"] is False
    assert execution.review_results[0]["required"] is False
    assert execution.diagnostics["review"] == {
        "declared": 1,
        "completed": 1,
        "passed": 0,
        "failed": 1,
        "required_failed": 0,
    }
    paths = [item.path for item in execution.stream.items]
    assert paths.index("review.started") < paths.index("review.completed") < paths.index("result")
    meta = await execution.async_get_meta()
    assert meta.get("reviews") == execution.review_results


@pytest.mark.asyncio
async def test_required_verification_blocks_terminal_success(tmp_path):
    agent = create_review_agent(tmp_path, "required-verification")
    execution = agent.input("Produce a result.").verify(lambda _result, _context: False)

    with pytest.raises(AgentVerificationError, match="failed required verification") as raised:
        await execution.async_get_data()

    assert raised.value.review["required"] is True
    assert raised.value.review["passed"] is False
    assert execution.status == "blocked"
    assert execution.result == "candidate-result"
    paths = [item.path for item in execution.stream.items]
    assert "verification.failed" in paths
    assert "result" not in paths
    meta = await execution.async_get_meta()
    assert meta["diagnostics"].get("review", {}).get("required_failed") == 1


@pytest.mark.asyncio
async def test_review_handlers_run_in_fluent_order_and_accept_async_handlers(tmp_path):
    agent = create_review_agent(tmp_path, "ordered-review")
    calls: list[tuple[int, bool]] = []

    def first(_result: Any, context: AgentReviewContext):
        calls.append((context.index, context.required))
        return True

    async def second(_result: Any, context: AgentReviewContext):
        calls.append((context.index, context.required))
        return {"passed": True, "summary": "Required check passed."}

    execution = agent.input("Produce a result.").review(first).verify(second)

    assert await execution.async_get_data() == "candidate-result"
    assert calls == [(1, False), (2, True)]
    assert [item["passed"] for item in execution.review_results] == [True, True]


@pytest.mark.asyncio
async def test_review_without_handler_uses_one_structured_model_request(tmp_path):
    agent = create_review_agent(tmp_path, "model-review")
    execution = agent.input("Produce a result.").review()

    assert await execution.async_get_data() == "candidate-result"
    assert [request["is_review"] for request in ReviewRequester.requests] == [False, True]
    assert execution.review_results[0]["source"] == "model"
    assert execution.review_results[0]["passed"] is True
    assert len(execution.logs["model_response_ids"]) == 2


@pytest.mark.asyncio
async def test_review_runs_after_successful_agent_task_route(tmp_path, monkeypatch):
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules import route_execution

    agent = create_review_agent(tmp_path, "agent-task-review")
    reviewed: list[Any] = []

    async def fake_agent_task_route(execution, _route_meta):
        execution.status = "success"
        return {"final_response": "task-result"}

    def reviewer(result: object, _context: AgentReviewContext) -> bool:
        reviewed.append(result)
        return True

    monkeypatch.setattr(route_execution, "run_agent_task_route", fake_agent_task_route)
    execution = agent.goal("Complete a bounded task.").strategy("flat").review(reviewer)

    assert await execution.async_get_data() == {"final_response": "task-result"}
    assert reviewed == [{"final_response": "task-result"}]
    assert execution.review_results[0]["passed"] is True


def test_review_rejects_non_callable_handler(tmp_path):
    agent = create_review_agent(tmp_path, "invalid-review")

    with pytest.raises(TypeError, match="callable or None"):
        agent.input("Produce a result.").review("not-callable")  # type: ignore[arg-type]
