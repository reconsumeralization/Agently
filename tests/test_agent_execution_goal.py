"""Deterministic contracts for semantic goals and host-owned activation."""

import pytest

from agently import Agently


@pytest.fixture
def agent(tmp_path):
    return Agently.create_agent().use_task_workspace(tmp_path)


@pytest.mark.asyncio
async def test_goal_default_preserves_long_task_activation(agent):
    implicit = agent.goal("Resolve the incident", ["Explain the cause"])
    explicit = agent.goal("Resolve the incident", ["Explain the cause"], turn_on_long_task=True)
    assert (await implicit.select_route())[0] == "agent_task"
    assert (await explicit.select_route())[0] == "agent_task"
    assert implicit.review_declarations == explicit.review_declarations == []


@pytest.mark.asyncio
async def test_goal_false_is_visible_prompt_without_task_activation(agent):
    execution = agent.input("Source excerpt").goal(
        "Explain the excerpt", ["Name the assumption"], turn_on_long_task=False,
    )
    assert (await execution.select_route())[0] == "model_request"
    assert execution.goal_items == ["Explain the excerpt"]
    assert execution.success_criteria_items == ["Name the assumption"]
    assert execution.prompt.get("goal") == execution.goal_items
    assert execution.prompt.get("success_criteria") == execution.success_criteria_items
    text = execution.get_prompt_text()
    assert "Explain the excerpt" in text
    assert "Name the assumption" in text
    assert "turn_on_long_task" not in text
    assert agent.agent_prompt.get("goal") is None
    assert agent.create_execution().goal_items == []


@pytest.mark.asyncio
async def test_repeated_goal_disables_only_goal_owned_activation(agent):
    execution = agent.goal("First goal")
    assert (await execution.select_route())[0] == "agent_task"
    assert execution.goal("Second goal", turn_on_long_task=False) is execution
    assert (await execution.select_route())[0] == "model_request"
    assert execution.goal_items == ["Second goal"]
    assert "First goal" not in execution.get_prompt_text()
    execution.goal("Third goal")
    assert (await execution.select_route())[0] == "agent_task"


@pytest.mark.parametrize("strategy,expected", [("direct", "model_request"), ("flat", "agent_task"), ("taskboard", "agent_task")])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_goal_preserves_explicit_strategy(agent, strategy, expected, enabled):
    execution = agent.strategy(strategy).goal("Describe the result", turn_on_long_task=enabled)
    assert (await execution.select_route())[0] == expected


@pytest.mark.asyncio
async def test_goal_prompt_is_the_authoritative_source(agent):
    execution = agent.goal("First goal", turn_on_long_task=False)
    execution.set_execution_prompt("goal", ["Updated goal"])
    execution.set_execution_prompt("success_criteria", ["Updated criterion"])
    assert execution.goal_items == ["Updated goal"]
    assert execution.success_criteria_items == ["Updated criterion"]
    assert (await execution.select_route())[0] == "model_request"
    execution.remove_execution_prompt("goal")
    assert execution.goal_items == []


@pytest.mark.asyncio
async def test_semantic_options_do_not_reenable_goal_after_opt_out(agent):
    execution = agent.create_execution(options={"task": {"goal": "Initial", "success_criteria": ["Criterion"]}})
    assert (await execution.select_route())[0] == "agent_task"
    execution.goal("Updated", turn_on_long_task=False)
    execution.configure_options({"meta": {"request_id": "test"}})
    assert execution.goal_items == ["Updated"]
    assert (await execution.select_route())[0] == "model_request"


@pytest.mark.asyncio
async def test_goals_alias_and_execution_options_roundtrip(agent):
    execution = agent.goals(["First", "Second"], turn_on_long_task=False)
    assert (await execution.select_route())[0] == "model_request"
    assert execution.goals("Third", turn_on_long_task=False) is execution
    restored = agent.create_execution(options=execution.effective_options)
    assert restored.goal_items == ["Third"]
    assert (await restored.select_route())[0] == "model_request"


@pytest.mark.parametrize("invalid", [None, "false", 0, 1])
def test_goal_switch_requires_bool_and_invalid_input_is_atomic(agent, invalid):
    execution = agent.goal("Original")
    with pytest.raises(TypeError, match="turn_on_long_task"):
        execution.goal("Must not replace", turn_on_long_task=invalid)
    assert execution.goal_items == ["Original"]
