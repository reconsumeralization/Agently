"""Host-only scope regressions; synthetic verdicts are not model-quality evidence."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agently import Agently
from agently.builtins.plugins.AgentExecution.modules.routes import _required_action_failure
from agently.core.application.AgentTask import AgentTask


@pytest.fixture
def scope_setup(tmp_path):
    agent = Agently.create_agent("task-required-scope").use_task_workspace(tmp_path / "files")
    calls: list[str] = []

    @agent.action_func
    def lookup_ticket(fail: bool = False) -> dict[str, str]:
        calls.append("lookup_ticket")
        if fail:
            raise RuntimeError("Deliberate read failure for Host gate coverage")
        return {"revision": "host-protocol-fixture"}

    @agent.action_func
    def acknowledge_revision(fail: bool = False) -> dict[str, bool]:
        calls.append("acknowledge_revision")
        if fail:
            raise RuntimeError("Deliberate Action failure for Host gate coverage")
        return {"acknowledged": True}

    def create(options: dict[str, Any] | None = None):
        return AgentTask(
            agent,
            goal="Read and acknowledge the ticket, then report the outcome.",
            success_criteria=["Both required Actions succeed before final delivery."],
            execution="flat",
            options=options,
        )

    return agent, create, calls


def required_options(form: str, *, nested: bool = False) -> dict[str, Any]:
    required = ["lookup_ticket", "acknowledge_revision"]
    constraints: dict[str, Any] = {
        "skills": {"required": ["review-guide"], "allowed": ["review-guide"]},
        "policy": {"allow": ["read"], "deny": ["delete"]},
    }
    if form == "mapping":
        constraints["actions"] = {"required": required, "allowed": required, "denied": ["erase_ticket"]}
    else:
        constraints["required_actions"] = required
        constraints["allowed_actions"] = required
    options = {"capability_constraints": constraints, "custom_option": {"retained": True}}
    return {"agent_task": options} if nested else options


def child_of(task):
    return task._create_bounded_child_execution(lineage={"task_id": task.id, "iteration": 1})


def verdict(task, meta: dict[str, Any]):
    """A synthetic acceptance proposal exercises only the deterministic final gate."""
    summary = task._cumulative_execution_evidence_summary(meta)
    return task._normalize_verification(
        {"is_complete": True, "requires_block": False, "final_result_required": True},
        execution_evidence_summary=summary,
        candidate_final_result="The host observed both Actions complete.",
    )


def final_meta() -> dict[str, Any]:
    # An answer-only child has no Action evidence or task-wide required metadata.
    return {"status": "success", "logs": {"action_logs": [], "route_logs": {}}}


async def dispatch(task, action_id: str, **kwargs: Any):
    """Run actual registered functions through the existing ActionRuntime carrier."""
    return await task._execute_bounded_action_commands(
        raw_commands=[{"action_id": action_id, "action_input": kwargs}],
        required_action_ids=[action_id],
        execution_id=f"{task.id}-{action_id}",
        code_prefix="test.scope",
        execution_kind="scope_protocol_test",
        command_source="explicit_test_commands",
        action_planning_model_requests=0,
        unit_label="scope test step",
        todo_suggestion="Execute the Host regression fixture.",
        concurrency=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["mapping", "legacy"])
@pytest.mark.parametrize("nested", [False, True])
async def test_child_removes_only_task_required(scope_setup, form, nested):
    _agent, create, _calls = scope_setup
    options = required_options(form, nested=nested)
    original = deepcopy(options)
    task = create(options)
    parent_original = deepcopy(task.options)
    child = child_of(task)

    assert child.required_action_ids() == []
    assert await _required_action_failure(child, route="model_request") is None
    assert options == original
    assert task.options == parent_original
    projected = child.options["agent_task"] if nested else child.options
    expected = deepcopy(original["agent_task"] if nested else original)
    constraints = expected["capability_constraints"]
    if form == "mapping":
        constraints["actions"].pop("required")
    else:
        constraints.pop("required_actions")
    assert projected["capability_constraints"] == constraints
    assert projected["custom_option"] == expected["custom_option"]
    assert task._task_contract_required_action_ids() == {"lookup_ticket", "acknowledge_revision"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "failed", "blocked"])
async def test_explicit_step_required_still_blocks(scope_setup, status):
    _agent, create, _calls = scope_setup
    task = create(required_options("mapping"))
    child = child_of(task).require_actions("acknowledge_revision")
    if status is not None:
        await child.record_action_log({"action_id": "acknowledge_revision", "status": status}, route="model_request")
    failure = await _required_action_failure(child, route="model_request")
    assert failure is not None
    assert failure["required_actions"] == ["acknowledge_revision"]
    assert failure["missing_actions"] == (["acknowledge_revision"] if status is None else [])
    assert failure["failed_actions"] == ([] if status is None else ["acknowledge_revision"])
    assert "lookup_ticket" not in child.required_action_ids()


@pytest.mark.asyncio
async def test_agent_defaults_belong_to_task_not_child(scope_setup):
    agent, create, _calls = scope_setup
    agent.require_actions("lookup_ticket", always=True)
    task = create()
    independent = agent.create_execution()
    assert independent.required_action_ids() == ["lookup_ticket"]
    assert await _required_action_failure(independent, route="model_request") is not None

    child = child_of(task)
    assert child.required_action_ids() == []
    assert await _required_action_failure(child, route="model_request") is None
    # Artifact carriers also create directly from projected child options.
    artifact_child = agent.create_execution(options=task._child_execution_options())
    assert artifact_child.required_action_ids() == []
    assert await _required_action_failure(artifact_child, route="model_request") is None
    assert child._fork_for_reconfiguration().required_action_ids() == []
    child.require_actions("acknowledge_revision")
    assert child._fork_for_reconfiguration().required_action_ids() == ["acknowledge_revision"]

    # A later Agent default cannot silently expand an already captured task contract.
    agent.require_actions("acknowledge_revision", always=True)
    assert task._task_contract_required_action_ids() == {"lookup_ticket"}
    restored = create(deepcopy(task.options))
    assert restored._task_contract_required_action_ids() == {"lookup_ticket"}
    assert set(agent.create_execution().required_action_ids()) == {"lookup_ticket", "acknowledge_revision"}
    assert verdict(task, final_meta())["is_complete"] is False


@pytest.mark.parametrize("form", ["mapping", "legacy"])
@pytest.mark.parametrize("nested", [False, True])
def test_final_gate_reads_parent_contract_without_child_required(scope_setup, form, nested):
    _agent, create, _calls = scope_setup
    task = create(required_options(form, nested=nested))
    result = verdict(task, final_meta())
    assert result["is_complete"] is False
    assert {"lookup_ticket", "acknowledge_revision"}.issubset(result["missing_required_capabilities"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_final_gate_uses_cumulative_real_action_logs(scope_setup, fail):
    _agent, create, calls = scope_setup
    # Keep unrelated Skill obligations out of this Action-only evidence fixture.
    task = create({"capability_constraints": {"actions": {"required": ["lookup_ticket", "acknowledge_revision"]}}})
    result, first_meta = await dispatch(task, "lookup_ticket")
    assert result["status"] == "completed"
    task.iterations.append({"execution_meta": first_meta})
    first = verdict(task, final_meta())
    assert first["is_complete"] is False
    assert "acknowledge_revision" in first["missing_required_capabilities"]
    assert "lookup_ticket" not in first["missing_required_capabilities"]

    result, second_meta = await dispatch(task, "acknowledge_revision", fail=fail)
    assert result["status"] == ("failed" if fail else "completed")
    task.iterations.append({"execution_meta": second_meta})
    child = child_of(task).require_actions("acknowledge_revision")
    for log in second_meta["logs"]["action_logs"]:
        await child.record_action_log(log, route="model_request")
    failure = await _required_action_failure(child, route="model_request")
    assert (failure is not None) is fail

    final = verdict(task, final_meta())
    assert final["is_complete"] is (not fail)
    if fail:
        assert "execution_risk_actions_present" in final["guard_reasons"]
    else:
        assert final["missing_required_capabilities"] == []
        assert final["final_result"] == "The host observed both Actions complete."
    assert calls == ["lookup_ticket", "acknowledge_revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_failed_read_keeps_task_requirement(scope_setup, required):
    _agent, create, calls = scope_setup
    task = create({
        "planner_capabilities": [
            {"id": "lookup_ticket", "kind": "action", "side_effect_level": "read", "replay_safe": True}
        ],
        "capability_constraints": {"actions": {"required": ["lookup_ticket"] if required else []}},
    })
    result, meta = await dispatch(task, "lookup_ticket", fail=True)
    assert result["status"] == "failed"
    task.iterations.append({"execution_meta": meta})
    summary = task._cumulative_execution_evidence_summary(final_meta())
    assert summary["failed_actions"] == ["lookup_ticket"]
    final = verdict(task, final_meta())
    assert final["is_complete"] is (not required)
    if required:
        assert "execution_risk_actions_present" in final["guard_reasons"]
        assert "lookup_ticket" not in final.get("non_blocking_failed_actions", [])
    else:
        assert final["non_blocking_failed_actions"] == ["lookup_ticket"]
    assert calls == ["lookup_ticket"]
