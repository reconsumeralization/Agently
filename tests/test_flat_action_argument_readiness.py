"""Deterministic protocol tests; synthetic model responses are not quality evidence."""
from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agently import Agently
from agently.core.application.AgentTask import AgentTask


class ReadinessRequest:
    id = "readiness-protocol-request"

    def __init__(self, response: Any):
        self.response = response
        self.prompts: dict[str, Any] = {}

    def input(self, value: Any) -> ReadinessRequest:
        self.prompts["input"] = value
        return self

    def info(self, value: Any) -> ReadinessRequest:
        self.prompts["info"] = value
        return self

    def instruct(self, value: Any) -> ReadinessRequest:
        self.prompts["instruct"] = value
        return self

    def output(self, value: Any, **kwargs: Any) -> ReadinessRequest:
        self.prompts["output"] = value
        return self

    def get_result(self) -> ReadinessRequest:
        return self

    async def async_get_data(self) -> Any:
        return self.response


@pytest.fixture
def readiness_setup(tmp_path, monkeypatch):
    agent = Agently.create_agent("flat-readiness-protocol").use_task_workspace(tmp_path / "files")
    calls: list[str] = []

    @agent.action_func
    def record_value(value: str) -> dict[str, str]:
        calls.append(value)
        return {"value": value}

    task = AgentTask(agent, goal="Record the selected value.", success_criteria=["The value is recorded."], execution="flat")
    plan = {
        "execution_shape": "actions",
        "required_action_ids": ["record_value"],
        "step_instruction": "Record the selected value.",
    }
    context = {"goal": task.goal, "items": [], "omitted": [], "profile": "none", "diagnostics": {}}
    monkeypatch.setattr(cast(Any, task), "_apply_language_policy_to_request", lambda *_a, **_k: None)
    requests: list[ReadinessRequest] = []

    def install(response: Any) -> ReadinessRequest:
        request = ReadinessRequest(response)

        def create() -> ReadinessRequest:
            requests.append(request)
            return request

        monkeypatch.setattr(agent, "create_temp_request", create)
        return request

    return task, plan, context, calls, requests, install


def command(value: str) -> dict[str, Any]:
    return {"purpose": "Record the value.", "action_id": "record_value", "action_input": {"value": value}}


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [["one"], ["one", "two"]])
async def test_ready_commands_keep_serial_dispatch(readiness_setup, monkeypatch, values):
    task, plan, context, calls, requests, install = readiness_setup
    install({"requires_observation": False, "action_commands": [command(v) for v in values]})

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Ready commands must not create a child execution")

    monkeypatch.setattr(task, "_run_bounded_agent_execution_step", forbidden)
    result, meta = await task._execute_step(1, plan, context)
    assert calls == values
    assert result["status"] == "completed"
    assert len(requests) == 1
    assert meta["diagnostics"][0]["action_planning_model_requests"] == 1
    assert meta["diagnostics"][0]["command_concurrency"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("child_status", ["completed", "failed"])
async def test_observation_handoff_preserves_child_result_and_context(readiness_setup, monkeypatch, child_status):
    task, plan, context, calls, requests, install = readiness_setup
    install({"requires_observation": True, "action_commands": []})
    child_inputs: list[Any] = []

    async def child(iteration, child_plan, child_context, **kwargs):
        assert calls == []
        child_inputs.append((iteration, child_plan, child_context, kwargs))
        return {"status": child_status}, {"status": child_status, "execution_id": "real-child-owner"}

    monkeypatch.setattr(task, "_run_bounded_agent_execution_step", child)
    result, meta = await task._execute_step(2, plan, context)
    assert result["status"] == child_status
    assert meta["execution_id"] == "real-child-owner"
    assert meta["action_command_planning"] == {
        "command_source": "flat_action_command_request",
        "action_planning_model_requests": 1,
        "requires_observation": True,
        "command_count": 0,
    }
    assert len(child_inputs) == len(requests) == 1
    assert child_inputs[0][0] == 2
    assert child_inputs[0][1]["required_action_ids"] == plan["required_action_ids"]
    assert child_inputs[0][2] == context
    assert set(child_inputs[0][3]) == {
        "require_step_actions", "carrier_output_policy", "scoped_retrieval_results", "evidence_ledger"
    }
    assert child_inputs[0][3]["require_step_actions"] is True
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response,code", [
    ({"action_commands": [command("unsafe")]}, "invalid_readiness"),
    ({"requires_observation": "false", "action_commands": [command("unsafe")]}, "invalid_readiness"),
    ({"requires_observation": 0, "action_commands": [command("unsafe")]}, "invalid_readiness"),
    ({"requires_observation": True, "action_commands": [command("unsafe")]}, "invalid_readiness"),
    ({"requires_observation": True}, "invalid_readiness"),
    ({"requires_observation": True, "action_commands": {}}, "invalid_readiness"),
    ({"requires_observation": False, "action_commands": []}, "empty_model_result"),
    (None, "invalid_readiness"),
])
async def test_malformed_readiness_has_no_side_effect_or_fallback(readiness_setup, monkeypatch, response, code):
    task, plan, context, calls, requests, install = readiness_setup
    install(response)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Invalid readiness must not silently start another producer")

    monkeypatch.setattr(task, "_run_bounded_agent_execution_step", forbidden)
    result, meta = await task._execute_step(1, plan, context)
    assert result["status"] == "failed"
    assert meta["diagnostics"][0]["code"] == f"agent_task.flat.action_commands.{code}"
    assert calls == []
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("options,expected", [({}, None), ({"action_loop_max_rounds": 4}, 4),
    ({"agent_task": {"action_loop_max_rounds": 0}}, 0), ({"action_loop_max_rounds": None}, None)])
async def test_child_requires_step_actions_only_for_observation_handoff(
    readiness_setup, monkeypatch, required, options, expected
):
    task, plan, context, _calls, _requests, _install = readiness_setup
    task.options.update(options)
    captured: list[Any] = []

    async def run_child(*, execution, **_kwargs):
        captured.append(execution)
        return {}, {"status": "success"}

    monkeypatch.setattr(task, "_run_bounded_child_execution", run_child)
    await task._run_bounded_agent_execution_step(1, plan, context, require_step_actions=required)
    assert len(captured) == 1
    assert captured[0].required_action_ids() == (["record_value"] if required else [])
    assert task.agent._collect_required_action_ids() == []
    expected_bound = expected if required or options else task._task_action_loop_max_rounds()
    assert captured[0].request.settings.get("action.loop.max_rounds") == expected_bound
    assert captured[0].request.settings.get("tool.loop.max_rounds") == expected_bound


@pytest.mark.asyncio
async def test_observation_handoff_preserves_cancellation(readiness_setup, monkeypatch):
    task, plan, context, calls, requests, install = readiness_setup
    install({"requires_observation": True, "action_commands": []})

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(task, "_run_bounded_agent_execution_step", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await task._execute_step(1, plan, context)
    assert calls == []
    assert len(requests) == 1
