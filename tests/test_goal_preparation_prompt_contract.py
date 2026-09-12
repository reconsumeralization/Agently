"""Synthetic transport and structural checks, not model-quality evaluations."""

from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, Field, ValidationError

from agently.core.model.Prompt import Prompt
from agently.builtins.plugins.AgentExecution.modules.execution import AgentExecution
from agently.builtins.plugins.AgentExecution.modules.goal_preparation import prepare_missing_goal
from test_builtin_agent_executions import ScriptedExecutionRequester, create_execution_agent


class _Section(BaseModel):
    body: str = Field(description="仅使用离线资料，不得假设可以联网。", min_length=10)


class _Delivery(BaseModel):
    sections: list[_Section] = Field(description="包含交接安排。", min_length=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("output_format", ["json", "yaml_literal"])
async def test_preparation_receives_complete_original_contract(
    tmp_path: Path, typed: bool, output_format: str,
) -> None:
    response = {"status": "ready", "success_criteria": ["Use offline sources."], "missing_information": []}
    agent = create_execution_agent(tmp_path, "goal-contract", [response])
    run = cast(AgentExecution, agent.create_execution("long_task").input("准备交接报告。")
               .goal("解释交接安排。", turn_on_long_task=False))
    schema = _Delivery if typed else {"body": (str, "仅使用离线资料，不得假设可以联网。")}
    run.output(schema, format=output_format)
    run.set_execution_prompt("ensure_all_keys", True)
    before = deepcopy(dict(run.prompt_snapshot))
    seen: list[object] = []
    run.validate(lambda value, context: seen.append(value) or True)
    assert await prepare_missing_goal(run) is None
    request = ScriptedExecutionRequester.requests[0]
    contract = request["info"][0]["stage_information"]["final_output_contract"]
    expected = Prompt(agent.plugin_manager, run.request.settings, prompt_dict={
        key: before[key] for key in ("output", "output_format", "ensure_all_keys")
    }).to_text()
    assert contract == expected
    assert "仅使用离线资料，不得假设可以联网。" in contract
    if typed:
        assert "包含交接安排。" in contract
        assert "10 characters" in contract
    assert request["output_format"] == "json"
    assert "ensure_all_keys" not in request
    assert list(request["output"].model_fields) == ["status", "success_criteria", "missing_information"]
    assert request["goal"] == before["goal"]
    assert run.prompt_snapshot == before
    assert seen == []
    assert ScriptedExecutionRequester.model_dispatches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_context", [False, True])
async def test_preparation_omits_empty_contract_and_keeps_caller_and_host_context(
    tmp_path: Path, with_context: bool,
) -> None:
    agent = create_execution_agent(tmp_path, "goal-context", [{
        "status": "ready", "goals": ["Explain the process."],
        "success_criteria": ["Use the supplied facts."], "missing_information": [],
    }])
    run = cast(AgentExecution, agent.create_execution("long_task").input("Explain the process."))
    if with_context:
        run.info({"policy": "Preserve original records."}).instruct("Do not claim operations occurred.")
    before = deepcopy(dict(run.prompt_snapshot))
    assert await prepare_missing_goal(run) is None
    request = ScriptedExecutionRequester.requests[0]
    if with_context:
        assert request["info"] == [before["info"]]
        assert request["instruct"][0] == before["instruct"]
    else:
        assert "info" not in request
    rendered = Prompt(agent.plugin_manager, run.request.settings, prompt_dict=request).to_text()
    assert "final_output_contract" not in rendered
    assert "execution_plugin" not in rendered
    assert "execution_stage" not in str(request.get("info", []))
    assert "[input.execution_stage_input.missing_fields]" in rendered
    assert "[output.missing_information]" in rendered
    assert 'allowed values: ["ready","missing_information"]' in rendered
    assert "null or empty values are allowed" not in rendered
    assert "required; must not be null" in rendered
    assert "With status=ready: []" in rendered
    assert "goal-contract lists may be empty" in rendered
    assert run.diagnostics["execution_run"]["stages"] == ["goal_preparation"]
    origin = run.diagnostics["goal_preparation"]
    assert isinstance(origin, dict) and origin["request_id"] in run.logs["model_response_ids"]
    assert run.prompt_snapshot == before


@pytest.mark.asyncio
async def test_preparation_schema_matches_existing_structural_boundary(tmp_path: Path) -> None:
    agent = create_execution_agent(tmp_path, "goal-schema", [{
        "status": "missing_information", "goals": [], "success_criteria": [],
        "missing_information": ["Which subject?"],
    }])
    run = cast(AgentExecution, agent.create_execution("long_task").input("Handle it."))
    blocked = await prepare_missing_goal(run)
    assert blocked is not None and blocked["status"] == "blocked"
    schema = ScriptedExecutionRequester.requests[0]["output"]
    valid = {"status": "ready", "goals": ["  Goal  "],
             "success_criteria": ["Criterion"], "missing_information": []}
    assert schema.model_validate(valid).model_dump()["goals"] == ["Goal"]
    for field in schema.model_fields.values():
        assert field.is_required()
    invalid = [
        {**valid, "status": "unknown"}, {**valid, "status": None},
        {**valid, "goals": None}, {**valid, "goals": "Goal"},
        {**valid, "goals": [""]}, {**valid, "goals": [" \t\n"]},
        {**valid, "goals": [4]}, {**valid, "missing_information": None},
        {**valid, "unexpected": "extra"},
        *[{key: value for key, value in valid.items() if key != omitted} for omitted in valid],
    ]
    for value in invalid:
        with pytest.raises(ValidationError):
            schema.model_validate(value)
    assert ScriptedExecutionRequester.model_dispatches == 1
    assert run.task_record is None


@pytest.mark.asyncio
async def test_goal_projection_retains_late_task_context_lanes(tmp_path: Path) -> None:
    agent = create_execution_agent(tmp_path, "goal-late-context", [{
        "status": "ready", "goals": ["Explain the process."],
        "success_criteria": ["Use the supplied facts."], "missing_information": [],
    }])
    run = cast(AgentExecution, agent.create_execution("long_task").input("Explain the process."))
    run.task_context.put(role="instruction", content="Keep the supplied procedure.", required=True)
    run.task_context.put(role="information", content="Only offline records are available.", required=True)
    before = deepcopy(dict(run.prompt_snapshot))
    assert await prepare_missing_goal(run) is None
    request = ScriptedExecutionRequester.requests[0]
    instructions = request["instruct"][-1]["task_context_blocks"]
    facts = request["info"][-1]["task_context_blocks"]
    assert instructions[0]["content"] == "Keep the supplied procedure."
    assert facts[0]["content"] == "Only offline records are available."
    assert run.prompt_snapshot == before
    assert ScriptedExecutionRequester.model_dispatches == 1
