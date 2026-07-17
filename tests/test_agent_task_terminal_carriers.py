from __future__ import annotations

from typing import Any

import pytest

from agently import Agently
from agently.core.application.AgentTask import AgentTask


pytestmark = pytest.mark.asyncio


def _task(tmp_path, name: str, *, required_deliverables=None) -> AgentTask:
    agent = Agently.create_agent(name).use_task_workspace(tmp_path / name)
    options = (
        {"required_deliverables": list(required_deliverables)}
        if required_deliverables is not None
        else None
    )
    return AgentTask(
        agent,
        task_id=name,
        goal="Produce the current terminal result.",
        success_criteria=["The current terminal result is delivered."],
        execution="flat",
        options=options,
    )


async def _materialize(
    task: AgentTask,
    *,
    execution_result,
    evidence_summary=None,
    work_result_id="work_A",
):
    await task._replace_terminal_carriers(
        execution_result=execution_result,
        execution_evidence_summary=evidence_summary or {},
        source_work_result_id=work_result_id,
    )
    return await task._current_terminal_candidate()


async def test_terminal_carriers_replace_changed_task_workspace_content_with_new_identity(tmp_path):
    task = _task(tmp_path, "terminal-carrier-changed-path", required_deliverables=["final.md"])
    first_write = await task.task_workspace.write_file("final.md", "first version\n")
    first = await _materialize(
        task,
        execution_result={"file_refs": first_write["file_refs"], "final_result": "final.md"},
    )
    first_carrier = first["carriers"][0]

    await task.task_workspace.write_file("final.md", "second version\n")
    second = await _materialize(
        task,
        execution_result={"file_refs": first_write["file_refs"], "final_result": "final.md"},
        work_result_id="work_B",
    )
    second_carrier = second["carriers"][0]

    assert first_carrier["text"] == "first version\n"
    assert second_carrier["text"] == "second version\n"
    assert first_carrier["carrier_id"].startswith("car_")
    assert second_carrier["carrier_id"].startswith("car_")
    assert second_carrier["carrier_id"] != first_carrier["carrier_id"]
    assert second_carrier["content_version_id"] != first_carrier["content_version_id"]
    inventory = task._lifecycle_state.carrier_inventory
    assert inventory is not None
    assert [carrier.carrier_id for carrier in inventory.carriers] == [
        second_carrier["carrier_id"]
    ]


async def test_terminal_carriers_do_not_reintroduce_obsolete_cumulative_readback_body(tmp_path):
    task = _task(tmp_path, "terminal-carrier-current-readback")
    first_write = await task.task_workspace.write_file("final.md", "obsolete body\n")
    stale_ref = {
        **first_write["file_refs"][0],
        "role": "task_workspace_artifact",
        "readback": {"content": "obsolete body\n", "truncated": False},
    }
    await task.task_workspace.write_file("final.md", "current body\n")

    candidate = await _materialize(
        task,
        execution_result={"candidate_final_result": "final.md", "file_refs": []},
        evidence_summary={"artifact_refs": [stale_ref]},
    )

    assert candidate["text"] == "current body\n"
    assert "obsolete body" not in candidate["text"]


async def test_terminal_carriers_keep_task_workspace_and_inline_results_independent(tmp_path):
    task = _task(tmp_path, "terminal-carrier-independent", required_deliverables=["final.md"])
    write_result = await task.task_workspace.write_file("final.md", "file result\n")

    candidate = await _materialize(
        task,
        execution_result={
            "candidate_final_result": "Separate inline summary.",
            "file_refs": write_result["file_refs"],
        },
    )

    assert [carrier["kind"] for carrier in candidate["carriers"]] == [
        "task_workspace_artifact",
        "inline_final_result",
    ]
    assert len({carrier["carrier_id"] for carrier in candidate["carriers"]}) == 2
    assert all(carrier["required"] is True for carrier in candidate["carriers"])


async def test_terminal_carriers_exclude_task_workspace_pointer_from_inline_inventory(tmp_path):
    task = _task(tmp_path, "terminal-carrier-pointer", required_deliverables=["final.md"])
    write_result = await task.task_workspace.write_file("final.md", "file result\n")

    candidate = await _materialize(
        task,
        execution_result={
            "candidate_final_result": "final.md",
            "file_refs": write_result["file_refs"],
        },
    )

    assert [carrier["kind"] for carrier in candidate["carriers"]] == ["task_workspace_artifact"]


async def test_verifier_claim_projection_contains_only_current_inventory_content(tmp_path):
    task = _task(tmp_path, "terminal-carrier-verifier-projection", required_deliverables=["final.md"])
    first_write = await task.task_workspace.write_file("final.md", "old\n")
    await _materialize(
        task,
        execution_result={"file_refs": first_write["file_refs"], "final_result": "final.md"},
    )
    await task.task_workspace.write_file("final.md", "new\n")
    current = await _materialize(
        task,
        execution_result={"file_refs": first_write["file_refs"], "final_result": "final.md"},
        work_result_id="work_B",
    )

    projection = task._material_claim_candidates_for_verifier(current)

    assert projection == [
        {
            "claim_key": "claim_1",
            "text": "new",
            "delivery_kind": "task_workspace_artifact",
            "path": "final.md",
        }
    ]
    assert not {
        "carrier_id",
        "content_version_id",
        "artifact_quote",
    }.intersection(projection[0])
    assert not hasattr(task, "_latest_grounding_candidate")


async def test_shared_terminal_verification_rejects_complete_verdict_for_rejected_required_carrier(
    tmp_path,
    monkeypatch,
):
    task = _task(tmp_path, "terminal-shared-rejected", required_deliverables=["final.md"])
    write_result = await task.task_workspace.write_file("final.md", "Unsupported factual claim.\n")
    candidate = await _materialize(
        task,
        execution_result={"file_refs": write_result["file_refs"], "final_result": "final.md"},
    )
    carrier_id = candidate["carrier_id"]

    async def request_verification(*_args: Any, **_kwargs: Any):
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "The deliverable is complete.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": "final.md",
            "material_claim_audit": {
                "valid": False,
                "failed_carrier_ids": [carrier_id],
            },
            "material_claim_repair_contract": {
                "gate_kind": "factual_integrity",
                "issue_code": "unsupported_material_claim",
                "contract_subject": f"carrier:{carrier_id}",
                "requirements": [
                    {
                        "claim_key": "claim:1",
                        "carrier_id": carrier_id,
                        "content_version_id": candidate["content_version_id"],
                        "artifact_quote": "Unsupported factual claim.",
                        "state": "unsupported",
                    }
                ],
            },
            "strict_terminal_gates_applied": True,
        }

    monkeypatch.setattr(task, "_request_verification", request_verification)
    transition = await task._run_terminal_verification(
        1,
        plan={"deliverable_mode": "task_workspace_artifact"},
        execution_result={"file_refs": write_result["file_refs"], "final_result": "final.md"},
        execution_meta={"execution_id": "work_A", "logs": {}},
        context_pack={
            "goal": task.goal,
            "profile": "balanced",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert transition["transition"] == "repair"
    assert transition["verification"]["is_complete"] is False
    assert transition["rejected_carrier_ids"] == [carrier_id]
    assert transition["accepted_carrier_ids"] == []
    inventory = task._lifecycle_state.carrier_inventory
    assert inventory is not None
    assert inventory.carriers[0].status == "rejected"


async def test_shared_terminal_verification_projects_exact_current_accepted_carriers(
    tmp_path,
    monkeypatch,
):
    task = _task(tmp_path, "terminal-shared-accepted", required_deliverables=["final.md"])
    write_result = await task.task_workspace.write_file("final.md", "Full report body.\n")
    candidate = await _materialize(
        task,
        execution_result={
            "file_refs": write_result["file_refs"],
            "candidate_final_result": "Separate concise summary.",
        },
    )
    current_ids = [carrier["carrier_id"] for carrier in candidate["carriers"]]

    async def request_verification(*_args: Any, **_kwargs: Any):
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "All required outputs are accepted.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": "Separate concise summary.",
            "material_claim_audit": {"valid": True, "failed_carrier_ids": []},
            "strict_terminal_gates_applied": True,
        }

    monkeypatch.setattr(task, "_request_verification", request_verification)
    transition = await task._run_terminal_verification(
        1,
        plan={"deliverable_mode": "task_workspace_artifact"},
        execution_result={
            "file_refs": write_result["file_refs"],
            "candidate_final_result": "Separate concise summary.",
        },
        execution_meta={"execution_id": "work_A", "logs": {}},
        context_pack={
            "goal": task.goal,
            "profile": "balanced",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert transition["transition"] == "accepted"
    assert transition["accepted_carrier_ids"] == current_ids
    assert transition["rejected_carrier_ids"] == []
    assert transition["terminal_result"]["carrier_ids"] == current_ids
    assert transition["terminal_result"]["final_result"].startswith(
        "TaskWorkspace artifact delivered at final.md"
    )
    assert "Full report body" not in transition["terminal_result"]["final_result"]
    inventory = task._lifecycle_state.carrier_inventory
    assert inventory is not None
    assert {carrier.status for carrier in inventory.carriers} == {"accepted"}
