import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agently import Agently
from agently.core import AgentReviewError, PluginManager
from agently.types.data import AgentlyRequestData, AgentReviewContext
from agently.utils import Settings


class ReviewRequester:
    name = "ReviewRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    requests: list[dict[str, Any]] = []
    review_payload: dict[str, Any] | None = None

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls) -> None:
        cls.requests = []
        cls.review_payload = None

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        request_input = self.prompt.get("input")
        is_review = isinstance(self.prompt.get("info"), dict) and "review_rules" in self.prompt.get("info")
        payload = {
            "is_review": is_review,
            "input": request_input,
            "output": self.prompt.get("output"),
            "info": self.prompt.get("info"),
            "prompt": self.prompt.get(),
            "prompt_text": self.prompt.to_text(),
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
                type(self).review_payload or {
                    "passed": True,
                    "quality_level": "strong",
                    "checks": [{"rule_key": key, "status": "satisfied", "evidence": "Protocol fixture."}
                               for key in request_data.data["info"]["review_rules"]],
                    "summary": "The candidate meets the supplied contract.",
                    "issues": [],
                    "overall_suggestions": ["Keep the answer concise."],
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
@pytest.mark.parametrize("input_value", [None, "Source excerpt"])
async def test_semantic_goal_reaches_producer_and_review_once(tmp_path, input_value):
    agent = create_review_agent(tmp_path, "semantic-goal-review")
    execution = agent.goal("Explain the mechanism", ["Name the assumption"], turn_on_long_task=False)
    if input_value is not None:
        execution.input(input_value)
    execution.review()
    assert await execution.async_get_data() == "candidate-result"
    assert len(ReviewRequester.requests) == 2
    production, review = ReviewRequester.requests
    assert "Explain the mechanism" in production["prompt_text"]
    assert "Name the assumption" in production["prompt_text"]
    assert "turn_on_long_task" not in production["prompt_text"]
    contract = review["info"]["request_contract"]
    assert contract["goals"] == ["Explain the mechanism"]
    assert contract["success_criteria"] == ["Name the assumption"]
    assert review["prompt_text"].count("Explain the mechanism") == 1
    assert review["prompt_text"].count("Name the assumption") == 1
    with pytest.raises(RuntimeError, match="already started"):
        execution.goal("Must not mutate a settled run", turn_on_long_task=False)


@pytest.mark.asyncio
async def test_advisory_review_records_failure_without_changing_result(tmp_path):
    agent = create_review_agent(tmp_path, "advisory-review")
    contexts: list[AgentReviewContext] = []

    def reviewer(result: Any, context: AgentReviewContext):
        contexts.append(context)
        assert result == "candidate-result"
        return {
            "passed": False,
            "quality_level": "weak",
            "summary": "Useful but incomplete.",
            "issues": [{"criterion": "Requested detail", "finding": "Missing one requested detail.",
                        "evidence": "Candidate", "suggestions": ["Add that detail."]}],
            "overall_suggestions": [],
        }

    execution = agent.input("Produce a concise result.").review(reviewer)

    assert await execution.async_get_data() == "candidate-result"
    assert execution.status == "success"
    assert len(contexts) == 1
    assert contexts[0].on_fail == "warn"
    assert execution.review_results[0]["passed"] is False
    assert execution.review_results[0]["on_fail"] == "warn"
    assert execution.diagnostics["review"] == {
        "declared": 1,
        "completed": 1,
        "passed": 0,
        "failed": 1,
        "blocked": 0,
    }
    paths = [item.path for item in execution.stream.items]
    assert paths.index("review.started") < paths.index("review.completed") < paths.index("result")
    meta = await execution.async_get_meta()
    assert meta.get("reviews") == execution.review_results


@pytest.mark.asyncio
async def test_blocking_review_prevents_terminal_success(tmp_path):
    agent = create_review_agent(tmp_path, "required-verification")
    execution = agent.input("Produce a result.").review(lambda _result, _context: False, on_fail="block")

    with pytest.raises(AgentReviewError, match="Review did not pass") as raised:
        await execution.async_get_data()

    assert raised.value.review["on_fail"] == "block"
    assert raised.value.review["passed"] is False
    assert execution.status == "blocked"
    assert execution.result == "candidate-result"
    paths = [item.path for item in execution.stream.items]
    assert "review.blocked" in paths
    assert "result" not in paths
    meta = await execution.async_get_meta()
    assert meta["diagnostics"].get("review", {}).get("blocked") == 1


@pytest.mark.asyncio
async def test_review_handlers_run_in_fluent_order_and_accept_async_handlers(tmp_path):
    agent = create_review_agent(tmp_path, "ordered-review")
    calls: list[tuple[int, str]] = []

    def first(_result: Any, context: AgentReviewContext):
        calls.append((context.index, context.on_fail))
        return True

    async def second(_result: Any, context: AgentReviewContext):
        calls.append((context.index, context.on_fail))
        return {"passed": True, "summary": "Required check passed."}

    execution = agent.input("Produce a result.").review(first).review(second, on_fail="block")

    assert await execution.async_get_data() == "candidate-result"
    assert calls == [(1, "warn"), (2, "block")]
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
    from agently.builtins.plugins.AgentExecution.modules import route_execution

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


@pytest.mark.asyncio
async def test_default_review_reads_complete_transformed_artifact_and_isolates_prompt(tmp_path):
    agent = create_review_agent(tmp_path, "artifact-content-review")
    checked = []
    agent.validate(lambda value, _: checked.append(value) or True)
    body = "Complete artifact evidence.\n" * 1500
    execution = (
        agent.input("Produce a report.").instruct("Caller style constraint.")
        .artifact("report.md", lambda *_: body)
        .review(rules=["Check the supplied report."], on_fail="block")
    )
    assert await execution.async_get_data() == "candidate-result"
    assert checked == [{"value": "candidate-result"}]
    prompt = ReviewRequester.requests[-1]
    assert prompt["info"]["evidence"][0]["content"] == body
    assert prompt["info"]["evidence"][0]["coverage"] == "complete"
    assert prompt["info"]["review_rules"] == {"r1": "Check the supplied report."}
    assert "goals" not in prompt["info"]["request_contract"]
    assert "success_criteria" not in prompt["info"]["request_contract"]
    assert "Caller style constraint." not in str(prompt["prompt"]["instruct"])
    assert "Caller style constraint." in str(prompt["info"]["request_contract"])
    assert "[info.review_rules]" in prompt["prompt_text"]
    assert "verification_is_required" not in prompt["prompt_text"]
    assert "on_fail" not in prompt["prompt_text"]
    assert "score" not in prompt["output"].model_fields


@pytest.mark.asyncio
async def test_identical_candidate_refers_to_artifact_without_duplicate_body(tmp_path):
    agent = create_review_agent(tmp_path, "deduplicated-review")
    execution = agent.input("Produce text.").artifact("result.txt").review()
    await execution.async_get_data()
    prompt = ReviewRequester.requests[-1]
    assert prompt["input"]["candidate"] == {"same_content_as": "a1"}
    assert prompt["info"]["evidence"][0]["content"] == "candidate-result"


@pytest.mark.asyncio
async def test_unreadable_artifact_is_not_assessable_and_never_claims_model_review(tmp_path):
    agent = create_review_agent(tmp_path, "binary-review")
    execution = agent.input("Produce data.").artifact("image.png", lambda *_: b"\x00\xff").review()
    await execution.async_get_data()
    assert len(ReviewRequester.requests) == 1
    assert execution.review_results[0]["quality_level"] == "not_assessable"
    assert execution.review_results[0]["source"] == "host"
    assert execution.review_results[0]["passed"] is False
    assert execution.review_results[0]["issues"][0]["evidence"]


@pytest.mark.asyncio
async def test_handler_replaces_model_review_and_receives_frozen_rules(tmp_path):
    agent = create_review_agent(tmp_path, "handler-rules")
    rules = ["Check evidence."]
    contexts = []
    execution = agent.input("Produce text.").review(
        lambda _, context: contexts.append(context) or True, rules=rules,
    )
    rules.append("Later mutation.")
    await execution.async_get_data()
    assert contexts[0].rules == ("Check evidence.",)
    assert len(ReviewRequester.requests) == 1
    assert execution.review_results[0]["quality_level"] is None
    assert not hasattr(agent, "verify")
    assert not hasattr(execution, "verify")


@pytest.mark.parametrize("options", [{"on_fail": "retry"}, {"rules": " "}, {"rules": [""]}])
def test_review_rejects_unsupported_behavior_and_empty_rules(tmp_path, options):
    agent = create_review_agent(tmp_path, "invalid-rules")
    with pytest.raises(ValueError):
        agent.review(**options)


@pytest.mark.asyncio
@pytest.mark.parametrize("checks,passed", [([], True), (
    [{"rule_key": "r1", "status": "violated", "evidence": "Fixture"}], False,
)])
async def test_model_review_rejects_missing_rule_coverage_or_issue_support_without_retry(tmp_path, checks, passed):
    agent = create_review_agent(tmp_path, "incomplete-model-report")
    ReviewRequester.review_payload = {
        "checks": checks, "passed": passed, "issues": [], "overall_suggestions": [],
        "summary": "Protocol fixture.", "quality_level": "weak",
    }
    execution = agent.input("Produce a result.").review(rules="Check evidence.")
    with pytest.raises(ValueError):
        await execution.async_get_data()
    assert len(ReviewRequester.requests) == 2


@pytest.mark.asyncio
async def test_changed_artifact_is_not_accepted_from_earlier_readback_metadata(tmp_path):
    agent = create_review_agent(tmp_path, "changed-artifact")

    def change_after_delivery(_result, context):
        path = context.task_workspace.resolve_file_path(context.artifact_refs[0]["path"])
        path.write_text("Changed after trusted delivery.", encoding="utf-8")
        return True

    execution = (
        agent.input("Produce text.").artifact("result.txt")
        .review(change_after_delivery).review(on_fail="block")
    )
    with pytest.raises(AgentReviewError, match="incomplete"):
        await execution.async_get_data()
    assert len(ReviewRequester.requests) == 1
    assert execution.review_results[-1]["quality_level"] == "not_assessable"
    assert "trusted content version" in execution.review_results[-1]["issues"][0]["evidence"]
