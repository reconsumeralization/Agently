"""Host-only A2 regressions: real Actions/Carrier, synthetic plans and verdicts.

No model calls or model-quality claims. Random Action values test transport,
not a hand-written substitute for semantic planning or business acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import json
import secrets

import pytest

from agently import Agently
from agently.builtins.plugins.AgentExecution.long_task.TerminalConvergence import TerminalIssue
from agently.core.application.AgentTask import AgentTask
from agently.core.model.ModelRequest import ModelRequest


class RequestCaptured(Exception):
    pass


@pytest.fixture
def setup(tmp_path):
    agent = Agently.create_agent("flat-observation-contract").use_task_workspace(tmp_path / "files")
    agent.use_record_store(tmp_path / "records")
    observed = []

    @agent.action_func
    def lookup_ticket(label: str = "first", fail: bool = False) -> dict[str, str]:
        if fail:
            raise RuntimeError("Synthetic Action error")
        value = {"revision": secrets.token_hex(16), "label": label}
        observed.append(value)
        return value

    @agent.action_func
    def acknowledge_revision(revision: str) -> dict[str, str]:
        assert revision == observed[-1]["revision"]
        return {"revision": revision, "outcome": "acknowledged"}

    task = AgentTask(
        agent, goal="Read the current ticket, acknowledge its revision, then report the observed outcome.",
        success_criteria=["Read and acknowledge before final delivery."], execution="flat", max_iterations=3,
        options={"capability_constraints": {"actions": {"required": ["lookup_ticket", "acknowledge_revision"]}}},
    )
    return task, observed


def plan_for(action_id="lookup_ticket", mode="inline_final", **kwargs):
    return {
        "execution_shape": "actions", "required_action_ids": [action_id],
        "step_instruction": "Perform only this bounded Action and return its observation.",
        "expected_evidence": "The actual Action return value.", "rationale": "Synthetic protocol fixture.",
        "deliverable_mode": mode,
        "action_commands": [{"action_id": action_id, "action_input": kwargs}],
    }


def context_for(task):
    return {"goal": task.goal, "items": [], "omitted": [], "profile": "none", "diagnostics": {}}


async def frame_after_action(task, *, iteration=1, plan=None):
    plan = task._normalize_step_plan(plan or plan_for())
    context = context_for(task)
    result, meta = await task._execute_step(iteration, plan, context)
    frame = {
        "iteration": iteration, "plan": plan, "context_pack": context, "decision_ref": None,
        "execution_result": result, "execution_meta": meta,
        "execution_failed": meta.get("status") in {"failed", "blocked"}, "grounding_patch_mode": False,
    }
    frame = await task._flat_outputs_materialize_stage(frame)
    return await task._flat_evidence_ingest_stage(frame)


def assert_progress(verification, missing):
    assert verification["verification_source"] == "consumer_driven_continuation"
    assert verification["is_complete"] is False
    assert verification["failure_analysis"] == ""
    assert verification["missing_criteria"] == []
    assert verification["acceptance_delta"] == []
    assert verification["repair_constraints"] == []
    assert verification["next_step_requirements"] == []
    assert verification["replan_signal"]["status"] == "continue"
    assert verification["missing_required_capabilities"] == missing
    assert not verification.get("guard_reasons")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["", "inline_final"])
@pytest.mark.parametrize("consumer", ["planner", "narrow", "worker"])
async def test_actual_action_carrier_stages_reach_next_request_without_failure(setup, monkeypatch, mode, consumer):
    task, observed = setup

    async def forbid_verifier(*_a, **_kw):
        raise AssertionError("A successful observation has no final candidate yet")

    monkeypatch.setattr(task, "_run_terminal_verification", forbid_verifier)
    frame = await frame_after_action(task, plan=plan_for(mode=mode))
    assert frame["execution_result"]["ready_for_final_verification"] is False
    assert frame["execution_result"]["remaining_work"] == []
    await task._flat_terminal_verify_stage(frame)
    assert_progress(frame["verification"], ["acknowledge_revision"])
    captured = []

    def capture(request, **_kwargs):
        captured.append({"input": deepcopy(request.prompt.get("input")), "prompt": request.prompt.to_text()})
        raise RequestCaptured()

    monkeypatch.setattr(ModelRequest, "get_result", capture)
    next_plan = plan_for("acknowledge_revision")
    next_plan.pop("action_commands")
    if consumer == "planner":
        with pytest.raises(RequestCaptured):
            await task._request_plan(2, context_for(task))
    elif consumer == "narrow":
        with pytest.raises(RequestCaptured):
            await task._try_flat_narrow_action_command_request(2, next_plan, context_for(task))
    else:
        # Use the actual child Execution and its ModelRequest; all dispatches
        # are intercepted, including any attempted error-path request.
        try:
            await task._run_bounded_agent_execution_step(
                2, {"execution_shape": "direct", "step_instruction": "Report the observed revision."}, context_for(task),
            )
        except RequestCaptured:
            pass
    assert captured
    request = next(item for item in captured if isinstance(item["input"], dict) and item["input"].get("repair_context"))
    incoming = request["input"]["repair_context"]
    assert "distinguishes intermediate observations from verification feedback" in request["prompt"]
    assert "pending capabilities" in request["prompt"]
    assert "Actual guard and repair findings still apply." in request["prompt"]
    assert incoming["verification_source"] == "consumer_driven_continuation"
    assert incoming["failure_analysis"] == ""
    assert incoming["missing_required_capabilities"] == ["acknowledge_revision"]
    previews = incoming["available_evidence_anchors"]["action_result_previews"]
    assert any(item.get("result_preview") == observed[0] for item in previews)
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_observations_accumulate_without_becoming_final_acceptance(setup):
    task, observed = setup
    first = await frame_after_action(task)
    await task._flat_terminal_verify_stage(first)
    second = await frame_after_action(task, iteration=2, plan=plan_for("acknowledge_revision", revision=observed[0]["revision"]))
    await task._flat_terminal_verify_stage(second)
    assert_progress(second["verification"], [])
    assert task.result is None
    # Synthetic rejection is still rejection after complete required coverage.
    rejected = task._normalize_verification(
        {"is_complete": False, "missing_criteria": ["Synthetic candidate disagrees with the supplied outcome."]},
        execution_evidence_summary=task._cumulative_execution_evidence_summary({"status": "success", "logs": {}}),
    )
    assert rejected["is_complete"] is False
    assert "missing_criteria_present" in rejected["guard_reasons"]


@pytest.mark.asyncio
async def test_budget_exhaustion_after_observation_is_not_accepted(setup):
    task, _observed = setup
    task.max_iterations = 1
    frame = await frame_after_action(task)
    await task._flat_terminal_verify_stage(frame)
    await task._flat_transition_decide_stage(frame)
    assert task.result["accepted"] is False
    assert task.result["status"] == "capability_unavailable"
    assert frame["verification"]["missing_required_capabilities"] == ["acknowledge_revision"]


@pytest.mark.asyncio
async def test_repeated_action_values_and_source_survive_summary_roundtrip(setup):
    task, observed = setup
    for iteration, label in enumerate(["first", "second"], 1):
        frame = await frame_after_action(task, iteration=iteration, plan=plan_for(label=label))
        await task._flat_terminal_verify_stage(frame)
    summaries = json.loads(json.dumps(task._iteration_prompt_summaries()))
    task._resumed_iteration_summaries = summaries
    task.iterations = []
    projected = task._active_repair_context()
    assert projected["verification_source"] == "consumer_driven_continuation"
    previews = projected["available_evidence_anchors"]["action_result_previews"]
    for value in observed:
        assert any(item.get("result_preview") == value for item in previews)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "candidate_final_result", "final_result", "artifact_manifest", "artifact_manifest_empty", "artifact_refs", "file_refs",
    "task_workspace_artifact", "sectioned_task_workspace_artifact", "inventory", "active_issue", "repair_contract", "convergence",
])
async def test_existing_candidate_artifact_and_repair_are_excluded(setup, monkeypatch, boundary):
    task, _observed = setup
    plan = plan_for(mode=boundary if boundary.endswith("workspace_artifact") else "")
    if boundary == "inventory":
        task._lifecycle_state.replace_carriers([], expected_version=task._lifecycle_state.state_version)
    elif boundary in {"active_issue", "repair_contract"}:
        setattr(task._lifecycle_state, boundary, {"synthetic": True})
    elif boundary == "convergence":
        task._terminal_convergence_state.record_detection(
            TerminalIssue("criterion", "unsatisfied", "synthetic"), "a" * 64, repair_contract={},
        )
    original = task._execute_bounded_action_commands

    async def carrier(**kwargs):
        result, meta = await original(**kwargs)
        if boundary in {"candidate_final_result", "final_result"}:
            result[boundary] = "Synthetic explicit candidate"
        elif boundary == "artifact_manifest":
            result[boundary] = {"path": "report.md", "sections": []}
        elif boundary == "artifact_manifest_empty":
            result["artifact_manifest"] = {}
        elif boundary in {"artifact_refs", "file_refs"}:
            result[boundary] = [{"path": "report.md"}]
        return result, meta

    monkeypatch.setattr(task, "_execute_bounded_action_commands", carrier)
    result, meta = await task._try_flat_preplanned_action_calls(1, plan)
    assert "ready_for_final_verification" not in result
    assert task._should_request_flat_final_verification(result, meta)[0] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_explicit_readiness_is_preserved_and_true_reaches_verifier(setup, monkeypatch, ready):
    task, _observed = setup
    original = task._execute_bounded_action_commands

    async def carrier(**kwargs):
        result, meta = await original(**kwargs)
        result.update(ready_for_final_verification=ready, remaining_work=["Synthetic later work"])
        return result, meta

    monkeypatch.setattr(task, "_execute_bounded_action_commands", carrier)
    result, meta = await task._try_flat_preplanned_action_calls(1, plan_for())
    assert result["ready_for_final_verification"] is ready
    assert task._should_request_flat_final_verification(result, meta)[0] is ready
    if ready:
        async def capture_verifier(*_a, **_kw):
            raise RequestCaptured()
        monkeypatch.setattr(task, "_run_terminal_verification", capture_verifier)
        frame = {"iteration": 1, "plan": plan_for(), "context_pack": context_for(task), "decision_ref": None,
                 "observation_ref": None, "execution_result": result, "execution_meta": meta}
        with pytest.raises(RequestCaptured):
            await task._flat_terminal_verify_stage(frame)


@pytest.mark.asyncio
async def test_failed_action_never_gets_new_skip(setup):
    task, _observed = setup
    result, meta = await task._try_flat_preplanned_action_calls(1, plan_for(fail=True))
    assert meta["status"] == "failed"
    assert "ready_for_final_verification" not in result
    assert task._should_request_flat_final_verification(result, meta)[0] is True


@pytest.mark.asyncio
async def test_grounding_guard_survives_list_diagnostics_continuation(setup, monkeypatch):
    task, _observed = setup
    frame = await frame_after_action(task)
    frame["execution_result"]["evidence_use"] = [{"claim": "Synthetic unsupported claim", "evidence_ids": ["unknown"]}]
    frame["execution_meta"]["diagnostics"] = []
    frame = await task._flat_evidence_ingest_stage(frame)
    assert frame["grounding_guard"]["valid"] is False
    await task._flat_terminal_verify_stage(frame)
    verification = frame["verification"]
    assert "evidence_ledger_grounding_guard_failed" in verification["guard_reasons"]
    assert verification["missing_criteria"]
    assert verification["failure_analysis"]
    assert verification["replan_signal"]["status"] == "repair"
    captured = []

    def capture(request, **_kwargs):
        captured.append({"input": deepcopy(request.prompt.get("input")), "prompt": request.prompt.to_text()})
        raise RequestCaptured()

    monkeypatch.setattr(ModelRequest, "get_result", capture)
    try:
        await task._run_bounded_agent_execution_step(
            2, {"execution_shape": "direct", "step_instruction": "Use the supplied observation and guard feedback."},
            context_for(task),
        )
    except RequestCaptured:
        pass
    request = next(item for item in captured if isinstance(item["input"], dict) and item["input"].get("repair_context"))
    incoming = request["input"]["repair_context"]
    assert incoming["verification_source"] == "consumer_driven_continuation"
    assert "evidence_ledger_grounding_guard_failed" in incoming["guard_reasons"]
    assert incoming["failure_analysis"] == verification["failure_analysis"]
    assert incoming["missing_criteria"] == verification["missing_criteria"]
    assert incoming["replan_signal"]["status"] == "repair"
    assert "distinguishes intermediate observations from verification feedback" in request["prompt"]
    assert "Actual guard and repair findings still apply." in request["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["repair", "clarify"])
@pytest.mark.parametrize("consumer", ["planner", "narrow", "worker"])
async def test_structured_signal_survives_continuation_and_summary_to_each_request(setup, monkeypatch, status, consumer):
    task, observed = setup
    frame = await frame_after_action(task)
    token = secrets.token_hex(8)
    signal = {
        "status": status,
        "reason": f"Synthetic exact {status} instruction for source {token}.",
        "evidence_refs": [f"evidence:{token}:source"],
        "invalidated_output_refs": [f"output:{token}:stale"],
        "reusable_output_refs": [f"output:{token}:retained"],
        "affected_execution_block_ids": [f"execution:{token}"],
        "affected_plan_block_ids": [f"plan:{token}"],
        "missing_capabilities": ["acknowledge_revision"],
    }
    frame["execution_meta"]["replan_signals"] = [signal]
    frame["execution_result"]["remaining_work"] = [f"Synthetic original work-unit requirement {token}."]
    await task._flat_terminal_verify_stage(frame)
    verification = frame["verification"]
    assert verification["is_complete"] is False
    assert "structured_replan_signal" in verification["guard_reasons"]
    assert signal["reason"] in verification["replan_instruction"]
    assert frame["execution_result"]["remaining_work"][0] in verification["next_step_requirements"]
    expected = verification["replan_signals"]
    assert all(expected[0][key] == value for key, value in signal.items())
    # Exercise the actual summary-only snapshot boundary, not a handcrafted
    # repair_context. Exact identities must remain structured after JSON replay.
    summaries = json.loads(json.dumps(task._iteration_prompt_summaries()))
    task._resumed_iteration_summaries = summaries
    task.iterations = []
    assert task._active_repair_context()["replan_signals"] == expected
    captured = []

    def capture(request, **_kwargs):
        captured.append(deepcopy(request.prompt.get("input")))
        raise RequestCaptured()

    monkeypatch.setattr(ModelRequest, "get_result", capture)
    if consumer == "planner":
        with pytest.raises(RequestCaptured):
            await task._request_plan(2, context_for(task))
    elif consumer == "narrow":
        plan = plan_for("acknowledge_revision")
        plan.pop("action_commands")
        with pytest.raises(RequestCaptured):
            await task._try_flat_narrow_action_command_request(2, plan, context_for(task))
    else:
        try:
            await task._run_bounded_agent_execution_step(
                2, {"execution_shape": "direct", "step_instruction": "Consume the structured source feedback."},
                context_for(task),
            )
        except RequestCaptured:
            pass
    incoming = next(item["repair_context"] for item in captured if isinstance(item, dict) and item.get("repair_context"))
    assert incoming["verification_source"] == "consumer_driven_continuation"
    assert incoming["replan_signals"] == expected
    assert signal["reason"] in incoming["replan_instruction"]
    assert frame["execution_result"]["remaining_work"][0] in incoming["advisory_next_step_requirements"]
    assert "structured_replan_signal" in incoming["guard_reasons"]
    assert incoming["failure_analysis"]
    assert incoming["replan_signal"]["status"] != "continue"
    previews = incoming["available_evidence_anchors"]["action_result_previews"]
    assert any(item.get("result_preview") == observed[0] for item in previews)


@pytest.mark.parametrize("summary, guard", [
    ({"status": "failed"}, "execution_status_failed"),
    ({"status": "timed_out"}, "execution_status_failed"),
    ({"failed_actions": ["lookup_ticket"]}, "execution_risk_actions_present"),
    ({"failed_actions": ["unknown_effect"]}, "execution_risk_actions_present"),
    ({"blocked_actions": ["lookup_ticket"]}, "execution_risk_actions_present"),
    ({"approval_required_actions": ["lookup_ticket"]}, "execution_risk_actions_present"),
    ({"current_replan_signals": [{"status": "blocked", "reason": "Synthetic approval required."}]}, "structured_replan_signal_blocked"),
    ({"current_replan_signals": [{"status": "repair", "reason": "Synthetic invalid local result."}]}, "structured_replan_signal"),
])
def test_nonterminal_keeps_real_risk_and_replan_guards(setup, summary, guard):
    task, _observed = setup
    verification = task._normalize_verification(
        {"is_complete": False, "reason": "Intermediate observation."},
        execution_evidence_summary=summary, terminal=False,
    )
    assert verification["is_complete"] is False
    assert guard in verification["guard_reasons"]
    assert verification["replan_signal"]["status"] != "continue"


def test_model_source_or_terminal_field_cannot_waive_terminal_gate(setup):
    task, _observed = setup
    verification = task._normalize_verification(
        {"is_complete": True, "verification_source": "consumer_driven_continuation", "terminal": False},
        execution_evidence_summary={"status": "success"},
    )
    assert verification["is_complete"] is False
    assert "required_capability_evidence_missing" in verification["guard_reasons"]
    assert task._should_request_flat_final_verification(
        {"answer": "Unknown plugin answer", "verification_source": "consumer_driven_continuation"}, {"status": "success"},
    )[0] is True


def test_old_summary_without_source_is_not_reclassified(setup):
    task, _observed = setup
    assert task._planner_repair_context([{"verification": {"is_complete": False}}]) == {}
    old = {"verification": {"is_complete": False, "failure_analysis": "Legacy failure"}}
    projected = task._planner_repair_context([old])
    assert projected["failure_analysis"] == "Legacy failure"
    assert "verification_source" not in projected


def test_taskboard_and_old_records_without_source_keep_their_projection(setup):
    task, _observed = setup
    task.iterations = [{"iteration": 1, "verification": {
        "is_complete": False, "failure_analysis": "Synthetic existing feedback",
        "guard_reasons": ["missing_criteria_present"], "replan_signal": {"status": "repair"},
        "missing_required_capabilities": ["lookup_ticket"], "missing_capability_evidence": ["lookup_ticket"],
    }}]
    summary = task._iteration_prompt_summaries()[0]["verification"]
    assert "verification_source" not in summary
    assert "guard_reasons" not in summary
    assert "replan_signal" not in summary
    assert "missing_required_capabilities" not in summary
    assert "missing_capability_evidence" not in summary
    projected = task._active_repair_context()
    assert projected["failure_analysis"] == "Synthetic existing feedback"
    assert "guard_reasons" not in projected
