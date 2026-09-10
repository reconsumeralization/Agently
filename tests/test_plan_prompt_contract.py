"""Synthetic transport checks: contract delivery, not model planning quality."""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from agently.core.model.Prompt import Prompt
from agently.builtins.plugins.AgentExecution.modules.execution import AgentExecution
from test_builtin_agent_executions import (
    ScriptedExecutionRequester,
    create_execution_agent,
    ready_payload,
)


class _Step(BaseModel):
    action: str = Field(description="仅针对离线环境制定操作步骤，不得假设可以联网。", min_length=1)


class _Plan(BaseModel):
    steps: list[_Step] = Field(description="步骤必须覆盖数据保留要求。", min_length=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
async def test_readiness_receives_original_output_contract_without_changing_result(
    tmp_path: Path, typed: bool,
) -> None:
    value = {"steps": [{"action": "Prepare offline."}]}
    schema = _Plan if typed else {
        "steps": ([{"action": (str, "仅针对离线环境制定操作步骤，不得假设可以联网。", True)}],
                  "步骤必须覆盖数据保留要求。", True),
    }
    agent = create_execution_agent(tmp_path, "plan-contract", [ready_payload(), value])
    validated: list[object] = []

    def validate(result: object, context: object) -> bool:
        validated.append(result)
        return True

    run = agent.create_execution("plan").input("制定执行计划。")
    run.output(schema, format="json").validate(validate)
    before = deepcopy(dict(run.prompt_snapshot))
    assert await run.async_get_data() == value
    first, final = ScriptedExecutionRequester.requests
    contract = first["info"][0]["stage_information"]["final_output_contract"]
    assert "仅针对离线环境制定操作步骤，不得假设可以联网。" in contract
    assert "步骤必须覆盖数据保留要求。" in contract
    assert "plan_ready" not in contract
    assert "plan_ready" in first["output"].model_fields
    assert final["output"] == schema
    assert "info" not in final
    assert validated == [value]
    assert run.prompt_snapshot == before
    rendered = Prompt(agent.plugin_manager, run.request.settings, prompt_dict=first).to_text()
    assert "仅针对离线环境制定操作步骤，不得假设可以联网。" in rendered
    assert "步骤必须覆盖数据保留要求。" in rendered
    assert "[info.stage_information.final_output_contract]" in rendered
    assert ScriptedExecutionRequester.model_dispatches == 2


@pytest.mark.asyncio
async def test_plain_plan_does_not_add_empty_output_contract(tmp_path: Path) -> None:
    agent = create_execution_agent(tmp_path, "plain-plan-contract", [ready_payload(), "Plan."])
    run = agent.create_execution("plan").input("Plan a task.")
    assert await run.async_get_data() == "Plan."
    first, _ = ScriptedExecutionRequester.requests
    assert "final_output_contract" not in first["info"][0]["stage_information"]
    assert "final_output_contract" not in str(first["instruct"])


@pytest.mark.asyncio
async def test_plan_projection_keeps_caller_context_and_host_readiness(tmp_path: Path) -> None:
    agent = create_execution_agent(tmp_path, "plan-projection", [ready_payload(), "Plan."])
    agent.set_settings("plugins.AgentExecution.plan.max_questions_per_round", 2)
    run = (agent.create_execution("plan").input("Plan a task.")
           .info({"policy": "Retain the original files."})
           .instruct("All recommendations must be labeled."))
    assert isinstance(run, AgentExecution)
    original = deepcopy(dict(run.prompt_snapshot))
    assert await run.async_get_data() == "Plan."
    first, final = ScriptedExecutionRequester.requests
    assert first["info"][0] == original["info"]
    assert final["info"] == [original["info"]]
    assert first["info"][1]["stage_information"]["max_questions_per_round"] == 2
    assert first["instruct"][0] == original["instruct"]
    assert final["instruct"][0] == original["instruct"]
    assert "[info.stage_information.max_questions_per_round]" in str(first["instruct"])
    assert "Return at most 2" not in str(first["instruct"])
    for request in (first, final):
        assert "execution_plugin" not in str(request["info"])
        assert "execution_stage" not in str(request["info"])
    transferred = final["input"]["execution_stage_input"]["validated_readiness"]
    assert transferred == {key: value for key, value in ready_payload().items()
                           if key not in {"plan_ready", "questions"}}
    assert run._producer_state["readiness"] == ready_payload()
    assert run.diagnostics["execution_run"]["stages"] == ["readiness_1", "final_plan"]
    assert run.prompt_snapshot == original


def test_readiness_only_declares_consumed_decision_and_exchange_fields() -> None:
    from agently.builtins.plugins.AgentExecution.modules.plan_flow import _PlanReadiness

    fields = _PlanReadiness.model_fields
    assert list(fields) == ["plan_ready", "readiness_summary", "questions"]
    assert fields["readiness_summary"].description == (
        "A concise user-facing explanation of what is sufficient or still missing to start planning; do not repeat individual questions."
    )


@pytest.mark.parametrize("ready", [False, True])
def test_readiness_accepts_only_three_fields_and_keeps_question_invariants(ready: bool) -> None:
    from agently.builtins.plugins.AgentExecution.modules.plan_flow import _PlanReadiness, _normalize_readiness

    questions = [] if ready else [{"question": "Which environment?", "why_needed": "Approval depends on it."}]
    value = {"plan_ready": ready, "readiness_summary": "Required information status.", "questions": questions}
    assert _PlanReadiness.model_validate(value).model_dump() == value
    assert _normalize_readiness(value, max_questions=3) == value
    with pytest.raises(ValueError, match="clarification question"):
        _normalize_readiness({**value, "plan_ready": not ready}, max_questions=3)
    with pytest.raises(ValueError, match="readiness_summary"):
        _normalize_readiness({**value, "readiness_summary": " "}, max_questions=3)
