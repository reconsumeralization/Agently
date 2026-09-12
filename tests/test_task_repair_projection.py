"""Deterministic repair-projection tests; all contracts and observations are synthetic."""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from agently import Agently
from agently.builtins.plugins.AgentExecution.long_task import Rework
from agently.builtins.plugins.AgentExecution.modules.revisions import content_digest
from agently.core.application.AgentTask import AgentTask
from agently.core.model.ModelRequest import ModelRequest


CRITERION = {
    "gate_kind": "criterion",
    "issue_code": "criterion_unsatisfied",
    "contract_subject": "verification:criterion_checks",
    "requirements": [{
        "criterion_id": "criterion:1",
        "gaps": ["Synthetic missing evidence binding."],
        "evidence_ids": ["synthetic-evidence:1"],
    }],
}
MATERIAL = {
    "gate_kind": "factual_integrity",
    "issue_code": "material_claim_audit_failed",
    "contract_subject": "artifact:factual_integrity",
    "requirements": [{
        "claim_key": "synthetic-claim:1",
        "carrier_id": "synthetic-carrier:1",
        "content_version_id": "synthetic-version:1",
        "state": "unsupported",
        "required_for_criterion_ids": ["criterion:1"],
        "evidence_ids": ["synthetic-evidence:1"],
    }],
}
EXACT_URL = "https://evidence.invalid/source/item?revision=R%2F1"
CONTRACTS = {
    "criterion_repair_contract": CRITERION,
    "material_claim_repair_contract": MATERIAL,
}


def _record(verification: dict[str, Any], *, anchors: bool = False) -> dict[str, Any]:
    record = {
        "iteration": 1,
        "plan": {"step_instruction": "Synthetic bounded work.", "execution_shape": "direct"},
        "verification": {"is_complete": False, **deepcopy(verification)},
        "verification_ref": {"id": "synthetic-verification:1"},
    }
    if anchors:
        record["execution_meta"] = {
            "status": "completed",
            "logs": {"route_logs": {}, "action_logs": [{
                "action_id": "synthetic_lookup",
                "action_call_id": "synthetic-call:1",
                "status": "success",
                "model_digest": {
                    "result_preview": {"href": EXACT_URL, "revision": "R/1"},
                    "result_preview_meta": {"truncated": False},
                },
            }]},
        }
    return record


def _task(verification: dict[str, Any], *, restored: bool = False, anchors: bool = False) -> Any:
    # Only initialize state used by the actual pure helpers; no live execution.
    task = object.__new__(AgentTask)
    task.options = {}
    task.iterations = [_record(verification, anchors=anchors)]
    task._resumed_iteration_summaries = []
    if restored:
        task._resumed_iteration_summaries = json.loads(json.dumps(task._iteration_prompt_summaries()))
        task.iterations = []
    return task


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("keys", [
    ("criterion_repair_contract",),
    ("material_claim_repair_contract",),
    ("criterion_repair_contract", "material_claim_repair_contract"),
])
def test_repair_projection_preserves_contracts_and_exact_anchors(restored, keys):
    contracts = {key: CONTRACTS[key] for key in keys}
    task = _task(contracts, restored=restored, anchors=True)
    cold = deepcopy((task.iterations, task._resumed_iteration_summaries))
    summaries = task._iteration_prompt_summaries()
    context = task._planner_repair_context(summaries)
    assert context == task._active_repair_context()
    for key in keys:
        assert context[key] == CONTRACTS[key]
    assert context["verification_ref"] == {"id": "synthetic-verification:1"}
    anchors = context["available_evidence_anchors"]
    assert any(ref["value"] == EXACT_URL for ref in anchors["source_refs"])
    assert anchors["action_result_previews"][0]["result_preview"] == {"href": EXACT_URL, "revision": "R/1"}
    assert (task.iterations, task._resumed_iteration_summaries) == cold


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("top_state", ["absent", "valid", "empty", "null", "list", "string", "false"])
def test_material_fallback_only_for_absent_top_level(restored, top_state):
    nested = deepcopy(MATERIAL)
    nested["requirements"][0]["claim_key"] = "synthetic-old-claim"
    verification = {"material_claim_audit": {"valid": False, "repair_contract": nested}}
    values = {"valid": MATERIAL, "empty": {}, "null": None, "list": [], "string": "invalid", "false": False}
    if top_state != "absent":
        verification["material_claim_repair_contract"] = deepcopy(values[top_state])
    expected = nested if top_state == "absent" else MATERIAL if top_state == "valid" else None
    direct = AgentTask._planner_repair_context([_record(verification)])
    task = _task(verification, restored=restored)
    cold = deepcopy((task.iterations, task._resumed_iteration_summaries))
    active = task._active_repair_context()
    assert direct.get("material_claim_repair_contract") == expected
    assert active.get("material_claim_repair_contract") == expected
    if expected is None:
        assert active == direct == {}
    assert (task.iterations, task._resumed_iteration_summaries) == cold


@pytest.mark.parametrize("nested", [None, [], "invalid", False, {}])
def test_invalid_nested_material_never_becomes_a_contract(nested):
    task = _task({"material_claim_audit": {"repair_contract": nested},
                  "criterion_repair_contract": CRITERION})
    context = task._active_repair_context()
    assert context["criterion_repair_contract"] == CRITERION
    assert "material_claim_repair_contract" not in context


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("latest", [{"is_complete": True, **CONTRACTS}, {"is_complete": False}])
def test_latest_verification_does_not_revive_previous_repairs(restored, latest):
    task = _task(CONTRACTS, restored=restored)
    task.iterations.append({**_record(latest), "iteration": 2})
    assert task._active_repair_context() == {}


def test_completed_and_prose_only_records_do_not_invent_contracts():
    assert _task({"is_complete": True, **CONTRACTS}, restored=True)._active_repair_context() == {}
    task = _task({
        "failure_analysis": "Synthetic prose mentions claim_key and a source path; it is not a contract.",
        "replan_instruction": "Use https://not-evidence.invalid/guessed as synthetic advisory text.",
    })
    context = task._active_repair_context()
    assert context["replan_instruction"]
    assert not (set(CONTRACTS) & set(context))
    assert "available_evidence_anchors" not in context


def test_nested_only_resumed_summary_preserves_existing_contract_but_cannot_recover_lost_criterion():
    task = _task({})
    task.iterations = []
    task._resumed_iteration_summaries = [{
        "iteration": 1,
        "verification": {"is_complete": False, "material_claim_audit": {"repair_contract": deepcopy(MATERIAL)}},
    }]
    context = task._active_repair_context()
    assert context["material_claim_repair_contract"] == MATERIAL
    assert "criterion_repair_contract" not in context


def _native_task(tmp_path, strategy: Literal["flat", "taskboard"] = "flat"):
    agent = Agently.create_agent("repair-projection-contract").use_task_workspace(tmp_path / "work")
    return AgentTask(agent, goal="Repair the supplied synthetic evidence contract.",
                     success_criteria=["Preserve supplied evidence identities."], execution=strategy)


class _RequestCaptured(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["flat", "taskboard"])
async def test_actual_planners_receive_the_projected_contract(tmp_path, monkeypatch, strategy):
    task = _native_task(tmp_path, strategy)
    task.iterations = [_record(CONTRACTS)]
    captured = {}

    def stop_before_dispatch(request, **_kwargs):
        captured.update(deepcopy(request.prompt.get("input")))
        raise _RequestCaptured()

    # Keep the real planner, ModelRequest, and Prompt. Stop at the dispatch
    # boundary instead of returning a fake model answer or testing source text.
    monkeypatch.setattr(ModelRequest, "get_result", stop_before_dispatch)
    with pytest.raises(_RequestCaptured):
        if strategy == "flat":
            await task._request_plan(2, {})
        else:
            await task._request_taskboard_plan({})
    for key, contract in CONTRACTS.items():
        assert captured["repair_context"][key] == contract
    if strategy == "flat":
        assert captured["previous_iterations"][0]["verification"]["criterion_repair_contract"] == CRITERION
    else:
        assert "previous_iterations" not in captured


@pytest.mark.asyncio
async def test_resume_snapshot_and_rebind_preserve_repair_contracts(tmp_path, monkeypatch):
    task = _native_task(tmp_path)
    task.options["record_store_recovery"] = True
    task.iterations = [_record(CONTRACTS)]
    snapshots = []

    async def capture_snapshot(_run_id, state, **_kwargs):
        snapshots.append(json.loads(json.dumps(state)))

    assert task.record_store is not None
    monkeypatch.setattr(task.record_store, "put_snapshot", capture_snapshot)
    await task._write_resume_snapshot(1, task.iterations[0]["verification"])
    assert len(snapshots) == 1
    restored = AgentTask._from_resume_state(
        task.agent, task.id, snapshots[0], task_workspace=task.task_workspace, record_store=task.record_store,
    )
    assert restored._active_repair_context() == task._active_repair_context()
    for key, contract in CONTRACTS.items():
        assert restored._active_repair_context()[key] == contract


@pytest.mark.asyncio
async def test_flat_rework_receives_preserved_summary_without_running_model(monkeypatch):
    task = _task(CONTRACTS)
    task.result = None
    content = {"strategy": "flat", "iterations": deepcopy(task.iterations)}
    owner = SimpleNamespace(task_record=task, _producer_state={"content": content, "digest": content_digest(content)},
                            _rework_feedback={"reason": "Synthetic revision request."})
    captured = {}

    async def stop_before_model(_owner, **kwargs):
        captured.update(deepcopy(kwargs["stage_input"]))
        raise _RequestCaptured()

    monkeypatch.setattr(Rework, "run_model_stage", stop_before_model)
    with pytest.raises(_RequestCaptured):
        await Rework.prepare_task_rework(cast(Any, owner))
    summary = captured["work"][0]["summary"]
    assert summary["verification"]["criterion_repair_contract"] == CRITERION
    assert summary["verification"]["material_claim_audit"]["repair_contract"] == MATERIAL
    assert task.iterations == content["iterations"]
