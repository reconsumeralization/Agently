import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData, ExecutionExchangeView, OutputValidateResultDict
from agently.utils import Settings


class ScriptedPatternRequester:
    name = "ScriptedPatternRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    responses: list[Any] = []
    requests: list[dict[str, Any]] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls, responses: list[Any]) -> None:
        cls.responses = list(responses)
        cls.requests = []

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        index = len(type(self).requests)
        if index >= len(type(self).responses):
            raise AssertionError(f"Unexpected Pattern ModelRequest at index {index}.")
        prompt_snapshot = self.prompt.get()
        assert isinstance(prompt_snapshot, dict)
        type(self).requests.append(prompt_snapshot)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"response": type(self).responses[index]},
            request_options={"stream": True},
            request_url="mock://builtin-agent-pattern",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        response = request_data.data["response"]
        content = response if isinstance(response, str) else json.dumps(response)
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
        yield "meta", {"provider": "mock-builtin-agent-pattern", "status": "completed"}


class ClarificationProvider:
    def __init__(self, response: Any):
        self.response = response
        self.published: list[dict[str, Any]] = []
        self.awaited: list[dict[str, Any]] = []

    def publish_request(self, execution_id, request, *, interrupt):
        self.published.append(
            {
                "execution_id": execution_id,
                "request": dict(request),
                "interrupt": dict(interrupt),
            }
        )
        return {"exchange_id": f"clarification-{len(self.published)}"}

    async def await_response(self, request):
        self.awaited.append(dict(request))
        return self.response


def create_pattern_agent(
    tmp_path: Path,
    name: str,
    responses: list[Any],
    *,
    settings_values: dict[str, Any] | None = None,
):
    ScriptedPatternRequester.reset(responses)
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    for key, value in (settings_values or {}).items():
        settings.set(key, value)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name=f"{name}-PluginManager",
    )
    plugin_manager.register("ModelRequester", ScriptedPatternRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    ).use_task_workspace(tmp_path / name)


def ready_payload() -> dict[str, Any]:
    return {
        "plan_ready": True,
        "planning_goal": "Ship a reliable release.",
        "final_deliverable": "An actionable release plan.",
        "readiness_summary": "Scope and acceptance are available.",
        "questions": [],
    }


def not_ready_payload() -> dict[str, Any]:
    return {
        "plan_ready": False,
        "planning_goal": "Ship a reliable release.",
        "final_deliverable": "An actionable release plan.",
        "readiness_summary": "The target environment is missing.",
        "questions": [
            {
                "question": "Which environment is the release targeting?",
                "why_needed": "The rollout and validation steps differ by environment.",
            }
        ],
    }


def test_default_plugins_register_bundled_agent_patterns():
    names = Agently.plugin_manager.get_plugin_list("AgentPattern")

    assert "plan" in names
    assert "long_content" in names
    assert Agently.settings.get("plugins.AgentPattern.plan.max_questions_per_round") == 3
    assert Agently.settings.get("plugins.AgentPattern.long_content.max_sections") == 12


@pytest.mark.asyncio
async def test_plan_pattern_runs_readiness_then_terminal_plan(tmp_path):
    agent = create_pattern_agent(
        tmp_path,
        "builtin-plan",
        [ready_payload(), "# Release plan\n\n1. Validate.\n2. Ship."],
    )
    execution = agent.input("Plan the release.").pattern("plan")

    result = await execution.async_get_data()

    assert result.startswith("# Release plan")
    assert len(ScriptedPatternRequester.requests) == 2
    assert execution.pattern_info == {
        "name": "plan",
        "source": "builtin",
        "selected_by": "pattern",
        "status": "completed",
        "used_default": False,
    }
    assert execution.route_info["selected_route"] == "agent_pattern"
    assert execution.diagnostics["pattern_run"] == {
        "name": "plan",
        "model_request_count": 2,
        "stages": ["readiness_1", "final_plan"],
        "clarification_rounds": 0,
    }
    assert len(execution.logs["model_response_ids"]) == 2
    paths = [item.path for item in execution.stream.items]
    assert paths.index("pattern.started") < paths.index("pattern.stage.started")
    assert paths.index("pattern.stage.completed") < paths.index("pattern.completed")


@pytest.mark.asyncio
async def test_application_can_override_bundled_pattern_without_builtin_identity(tmp_path):
    class ApplicationPlanPattern:
        name = "plan"
        DEFAULT_SETTINGS: dict[str, Any] = {}

        def __init__(self, *, plugin_manager, settings):
            self.plugin_manager = plugin_manager
            self.settings = settings

        @staticmethod
        def _on_register() -> None:
            pass

        @staticmethod
        def _on_unregister() -> None:
            pass

        async def run(self, _execution, _run_default, /):
            return "application plan"

    agent = create_pattern_agent(tmp_path, "overridden-plan", [])
    agent.plugin_manager.register(
        "AgentPattern",
        ApplicationPlanPattern,
        activate=False,
    )
    execution = agent.input("Plan the release.").pattern("plan")

    assert await execution.async_get_data() == "application plan"
    assert execution.pattern_info["source"] == "plugin"
    assert ScriptedPatternRequester.requests == []


@pytest.mark.asyncio
async def test_plan_pattern_preserves_structured_output_and_request_validator(tmp_path):
    agent = create_pattern_agent(
        tmp_path,
        "structured-plan",
        [ready_payload(), {"steps": ["validate", "ship"]}],
    )
    validated: list[dict[str, Any]] = []

    def validator(value, _context):
        validated.append(dict(value))
        return bool(value["steps"])

    execution = (
        agent.input("Plan the release.")
        .output({"steps": ([str], "Ordered plan steps")}, format="json")
        .validate(validator)
        .pattern("plan")
    )

    assert await execution.async_get_data() == {"steps": ["validate", "ship"]}
    assert validated == [{"steps": ["validate", "ship"]}]
    first_output = ScriptedPatternRequester.requests[0]["output"]
    final_output = ScriptedPatternRequester.requests[1]["output"]
    assert "plan_ready" in first_output.model_fields
    assert "steps" in final_output


@pytest.mark.asyncio
async def test_plan_pattern_uses_execution_exchange_for_clarification(tmp_path):
    from agently.base import execution_exchange

    provider_id = "test-plan-clarification"
    provider = ClarificationProvider({"environment": "staging"})
    execution_exchange.register_provider(provider_id, provider, replace=True)
    try:
        agent = create_pattern_agent(
            tmp_path,
            "clarified-plan",
            [
                not_ready_payload(),
                ready_payload(),
                "# Staging release plan\n\n1. Validate staging.\n2. Ship.",
            ],
            settings_values={
                "interaction.mode": "hot",
                "interaction.exchange_provider": provider_id,
                "interaction.hot_wait_timeout": 1,
            },
        )
        execution = agent.input("Plan the release.").pattern("plan")

        result = await execution.async_get_data()

        assert result.startswith("# Staging release plan")
        assert len(provider.published) == 1
        assert len(provider.awaited) == 1
        second_input = ScriptedPatternRequester.requests[1]["input"]
        assert second_input["pattern_stage_input"]["clarifications"][0]["response"] == {
            "environment": "staging"
        }
        assert execution.diagnostics["pattern_run"]["clarification_rounds"] == 1
        assert cast(list[dict[str, Any]], execution._review_contract["clarifications"])[0]["response"] == {"environment": "staging"}
        assert "not execution" in str(execution._review_contract["deliverable_role"])
        paths = [item.path for item in execution.stream.items]
        assert "exchange.pending" in paths
        assert "exchange.resolved" in paths
    finally:
        execution_exchange.unregister_provider(provider_id)


@pytest.mark.asyncio
async def test_plan_pattern_uses_standard_agent_interaction_handler(tmp_path):
    from agently.base import execution_exchange

    seen: list[ExecutionExchangeView] = []
    registered_before = execution_exchange.list_providers()
    agent = create_pattern_agent(
        tmp_path,
        "agent-interaction-plan",
        [
            not_ready_payload(),
            ready_payload(),
            "# Staging release plan\n\n1. Validate staging.\n2. Ship.",
        ],
        settings_values={"interaction.mode": "durable"},
    )

    def handle_exchange(exchange: ExecutionExchangeView) -> dict[str, str]:
        seen.append(exchange)
        return {"environment": "staging"}

    execution = (
        agent.input("Plan the release.")
        .interact(handle_exchange)
        .pattern("plan")
    )

    result = await execution.async_get_data()

    assert result.startswith("# Staging release plan")
    assert agent.settings.get("interaction.mode") == "durable"
    assert execution.request.settings.get("interaction.mode") == "hot"
    assert execution_exchange.list_providers() == registered_before
    assert len(seen) == 1
    assert seen[0]["kind"] == "clarification"
    assert seen[0]["status"] == "pending"
    assert seen[0]["subject"] == "Plan clarification"
    assert seen[0]["payload"]["questions"][0]["question"].startswith("Which environment")
    assert seen[0]["request"].get("provider_metadata", {}).get("provider") == "agent_interaction"
    second_input = ScriptedPatternRequester.requests[1]["input"]
    assert second_input["pattern_stage_input"]["clarifications"][0]["response"] == {
        "environment": "staging"
    }


@pytest.mark.asyncio
async def test_plan_pattern_fails_closed_for_durable_clarification(tmp_path):
    agent = create_pattern_agent(
        tmp_path,
        "durable-plan",
        [not_ready_payload()],
        settings_values={"interaction.mode": "durable"},
    )
    execution = agent.input("Plan the release.").pattern("plan")

    with pytest.raises(RuntimeError, match="cannot yet return a resumable Pattern handle"):
        await execution.async_get_data()

    assert len(ScriptedPatternRequester.requests) == 1
    assert execution.pattern_info["status"] == "failed"
    assert "exchange.pending" in [item.path for item in execution.stream.items]


@pytest.mark.asyncio
async def test_plan_pattern_rejects_transport_continuation_mix(tmp_path):
    agent = create_pattern_agent(tmp_path, "continued-plan", [])
    execution = (
        agent.input("Plan the release.")
        .ensure_long_output()
        .pattern("plan")
    )

    with pytest.raises(ValueError, match="cannot be combined with ensure_long_output"):
        await execution.async_get_data()

    assert ScriptedPatternRequester.requests == []


@pytest.mark.asyncio
async def test_builtin_pattern_model_stages_share_agent_execution_budget(tmp_path):
    agent = create_pattern_agent(
        tmp_path,
        "bounded-plan-requests",
        [ready_payload(), "This second provider response must not be consumed."],
    )
    execution = (
        agent.create_execution(limits={"max_model_requests": 1})
        .input("Plan the release.")
        .pattern("plan")
    )

    with pytest.raises(RuntimeError, match="max_model_requests"):
        await execution.async_get_data()

    # The second request object is rendered before the shared runtime budget
    # rejects provider dispatch, matching the ordinary ModelRequest contract.
    assert len(ScriptedPatternRequester.requests) == 2
    assert execution.execution_context.model_request_count == 1
    assert execution.pattern_info["status"] == "failed"


@pytest.mark.asyncio
async def test_long_content_pattern_plans_writes_and_host_assembles(tmp_path):
    agent = create_pattern_agent(
        tmp_path,
        "long-content",
        [
            {
                "document_title": "Architecture Guide",
                "sections": [
                    {"section_id": "context", "title": "Context", "brief": "Set the context."},
                    {"section_id": "design", "title": "Design", "brief": "Explain the design."},
                    {"section_id": "checks", "title": "Checks", "brief": "List acceptance checks."},
                ],
            },
            {"body": "Context body unique text.", "continuity_note": "Use term Pattern Carrier."},
            {"body": "Design body.", "continuity_note": "Validation precedes release."},
            {"body": "Checks body.", "continuity_note": ""},
        ],
    )
    execution = agent.input("Write an architecture guide.").pattern("long_content")

    result = await execution.async_get_data()

    assert result == (
        "# Architecture Guide\n\n"
        "## Context\n\nContext body unique text.\n\n"
        "## Design\n\nDesign body.\n\n"
        "## Checks\n\nChecks body."
    )
    assert len(ScriptedPatternRequester.requests) == 4
    second_writer_prompt = json.dumps(
        ScriptedPatternRequester.requests[2],
        ensure_ascii=False,
        default=str,
    )
    assert "Use term Pattern Carrier." in second_writer_prompt
    assert "Context body unique text." not in second_writer_prompt
    assert execution.diagnostics["pattern_run"] == {
        "name": "long_content",
        "model_request_count": 4,
        "stages": ["section_plan", "section_1", "section_2", "section_3"],
        "section_count": 3,
        "assembled_chars": len(result),
        "assembly": "host_ordered",
    }
    assert len(execution.logs["model_response_ids"]) == 4


@pytest.mark.asyncio
async def test_long_content_pattern_composes_with_artifact_and_review(tmp_path):
    agent = create_pattern_agent(
        tmp_path,
        "long-content-artifact",
        [
            {
                "document_title": "Report",
                "sections": [
                    {"section_id": "summary", "title": "Summary", "brief": "Summarize."}
                ],
            },
            {"body": "Accepted report body.", "continuity_note": ""},
        ],
    )
    reviewed: list[str] = []

    def reviewer(value, _context):
        reviewed.append(value)
        return True

    execution = (
        agent.input("Write a report.")
        .pattern("long_content")
        .artifact("report.md")
        .review(reviewer)
    )

    result = await execution.async_get_data()

    assert reviewed == [result]
    artifact_path = execution.task_workspace.resolve_file_path(
        execution.artifact_results[0]["path"]
    )
    assert artifact_path.read_text() == result
    paths = [item.path for item in execution.stream.items]
    assert paths.index("pattern.completed") < paths.index("artifact.completed")
    assert paths.index("artifact.completed") < paths.index("review.started")


@pytest.mark.asyncio
async def test_final_validator_checks_assembled_document_once_without_replay(tmp_path):
    agent = create_pattern_agent(tmp_path, "document-validator", [
        {"document_title": "Report", "sections": [
            {"section_id": "s1", "title": "Summary", "brief": "Summarize."},
        ]},
        {"body": "Document body.", "continuity_note": ""},
    ])
    checked = []
    agent.validate(lambda value, context: checked.append((value, context)) or False)
    execution = agent.input("Write a report.").pattern("long_content").artifact("rejected.md")
    with pytest.raises(ValueError, match="Output validation failed"):
        await execution.async_get_data(max_retries=3)
    assert len(checked) == 1
    value, context = checked[0]
    assert value["value"] == "# Report\n\n## Summary\n\nDocument body."
    assert context.parsed_result == value["value"]
    assert context.max_retries == 0
    assert context.model_run_context is None
    assert context.response_id == ""
    assert len(ScriptedPatternRequester.requests) == 2
    assert execution.artifact_results == []


@pytest.mark.asyncio
async def test_wrapper_validator_only_sees_transformed_default_result(tmp_path):
    agent = create_pattern_agent(tmp_path, "wrapper-validator", ["raw"])
    checked = []

    async def wrapper(_execution, run_default):
        return {"wrapped": await run_default()}

    def validator(value, _context):
        checked.append(value)
        return value == {"wrapped": "raw"}

    agent.validate(validator)
    execution = agent.input("Produce text.").pattern(wrapper)
    assert await execution.async_get_data() == {"wrapped": "raw"}
    assert checked == [{"wrapped": "raw"}]
    assert len(ScriptedPatternRequester.requests) == 1


@pytest.mark.asyncio
async def test_agent_task_final_validation_does_not_replay_task(tmp_path, monkeypatch):
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules import route_execution
    agent = create_pattern_agent(tmp_path, "task-validator", [])
    calls, checked = [], []

    async def task(_execution, _route_meta):
        calls.append("task")
        return {"final_response": "done"}

    async def validator(value, _context) -> OutputValidateResultDict:
        checked.append(value)
        return {"ok": False, "no_retry": True, "reason": "Final business gate rejected."}

    monkeypatch.setattr(route_execution, "run_agent_task_route", task)
    execution = agent.goal("Complete a task.").strategy("flat")
    with pytest.raises(ValueError, match="Final business gate"):
        await execution.async_get_data(validate_handler=validator, max_retries=5)
    assert calls == ["task"]
    assert checked == [{"final_response": "done"}]


@pytest.mark.asyncio
async def test_long_content_pattern_rejects_over_limit_plan_before_writing(tmp_path):
    agent = create_pattern_agent(
        tmp_path,
        "bounded-long-content",
        [
            {
                "document_title": "Too many sections",
                "sections": [
                    {"section_id": "one", "title": "One", "brief": "First."},
                    {"section_id": "two", "title": "Two", "brief": "Second."},
                ],
            }
        ],
        settings_values={"plugins.AgentPattern.long_content.max_sections": 1},
    )
    execution = agent.input("Write a report.").pattern("long_content")

    with pytest.raises(ValueError, match="exceeds max_sections=1"):
        await execution.async_get_data()

    assert len(ScriptedPatternRequester.requests) == 1
    assert execution.pattern_info["status"] == "failed"


@pytest.mark.asyncio
async def test_long_content_pattern_rejects_structured_output_before_model_call(tmp_path):
    agent = create_pattern_agent(tmp_path, "structured-long-content", [])
    execution = (
        agent.input("Write a report.")
        .output({"report": (str, "report")})
        .pattern("long_content")
    )

    with pytest.raises(ValueError, match="cannot be combined with a structured .output"):
        await execution.async_get_data()

    assert ScriptedPatternRequester.requests == []


@pytest.mark.asyncio
async def test_long_content_pattern_rejects_transport_continuation_mix(tmp_path):
    agent = create_pattern_agent(tmp_path, "continued-long-content", [])
    execution = (
        agent.input("Write a report.")
        .ensure_long_output()
        .pattern("long_content")
    )

    with pytest.raises(ValueError, match="cannot be combined with ensure_long_output"):
        await execution.async_get_data()

    assert ScriptedPatternRequester.requests == []
