from __future__ import annotations

from typing import Any

import pytest

from agently import Agently
from agently.core.application.AgentTask import AgentTask
from agently.types.data import TaskBoardRevision


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


async def _staged_finalization_inputs(task: AgentTask, body: str):
    staged_write = await task.task_workspace.write_file(
        "working/taskboard/final/final.md",
        body,
    )
    staged_read = await task.task_workspace.read_file(
        staged_write.path,
        max_bytes=staged_write.bytes + 1,
    )
    source_ref = {
        **await task.task_workspace._promote_file_identity(
            staged_write.path,
            role="task_workspace_artifact",
        ),
        "preview": staged_read.content,
        "read_bytes": staged_read.read_bytes,
        "truncated": False,
    }
    staged_refs, promotions = await task._taskboard_stage_required_final_deliverable_refs(
        [source_ref]
    )
    terminal_refs = task._taskboard_terminal_candidate_refs(None, staged_refs)
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-terminal-staging",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "final",
                        "objective": "Produce the terminal deliverable candidate.",
                        "required_outputs": ["final.md"],
                    }
                ],
            },
            "card_results": {
                "final": {
                    "card_id": "final",
                    "status": "completed",
                    "preview": {
                        "status": "completed",
                        "final_result": "final.md",
                        "remaining_work": [],
                    },
                }
            },
        }
    )
    prepared = {
        "result_status": "completed",
        "candidate_final_result": "final.md",
        "final_refs": terminal_refs,
        "staged_promotions": promotions,
        "trusted_terminal_refs": task._trusted_terminal_refs(terminal_refs),
        "final_candidate": {
            "accepted": True,
            "final_result": "final.md",
            "reason": "The staged candidate is ready for terminal verification.",
            "missing_criteria": [],
            "evidence_use": [],
        },
    }
    return revision, prepared, terminal_refs


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def test_task_workspace_atomic_promotion_copies_only_the_pinned_staged_bytes(tmp_path):
    task = _task(tmp_path, "terminal-atomic-promotion")
    staged = await task.task_workspace.write_file(
        "working/taskboard/final/final.md",
        "# Accepted candidate\n",
    )
    await task.task_workspace.write_file("final.md", "old accepted bytes\n")

    promoted = await task.task_workspace.atomic_promote_file(
        staged.path,
        "final.md",
        expected_sha256=staged.sha256,
    )

    readback = await task.task_workspace.read_file("final.md", max_bytes=4096)
    assert readback.content == "# Accepted candidate\n"
    assert readback.sha256 == staged.sha256 == promoted["sha256"]
    assert promoted["path"] == readback.path


async def test_task_workspace_atomic_promotion_digest_mismatch_preserves_target(tmp_path):
    task = _task(tmp_path, "terminal-atomic-promotion-mismatch")
    staged = await task.task_workspace.write_file(
        "working/taskboard/final/final.md",
        "candidate v1\n",
    )
    await task.task_workspace.write_file("final.md", "previous accepted bytes\n")
    await task.task_workspace.write_file(staged.path, "candidate v2\n")

    with pytest.raises(ValueError, match="staged TaskWorkspace file digest changed"):
        await task.task_workspace.atomic_promote_file(
            staged.path,
            "final.md",
            expected_sha256=staged.sha256,
        )

    readback = await task.task_workspace.read_file("final.md", max_bytes=4096)
    assert readback.content == "previous accepted bytes\n"


async def test_terminal_carrier_reads_staged_required_candidate_before_target_exists(tmp_path):
    task = _task(
        tmp_path,
        "terminal-staged-carrier",
        required_deliverables=["final.md"],
    )
    staged_write = await task.task_workspace.write_file(
        "working/taskboard/final/final.md",
        "verifier-visible staged body\n",
    )
    staged_ref = {
        **await task.task_workspace._promote_file_identity(
            staged_write.path,
            role="task_workspace_artifact",
        ),
        "staged_target_path": "final.md",
        "promotion_state": "staged",
        "preview": "verifier-visible staged body\n",
        "read_bytes": staged_write.bytes,
        "truncated": False,
    }

    candidate = await _materialize(
        task,
        execution_result={"file_refs": [staged_ref], "final_result": "final.md"},
    )

    assert not task.task_workspace.resolve_file_path("final.md").exists()
    assert len(candidate["carriers"]) == 1
    assert candidate["carriers"][0]["required"] is True
    assert candidate["carriers"][0]["text"] == "verifier-visible staged body\n"
    assert task._task_workspace_artifact_display_path(
        candidate["carriers"][0]["path"]
    ) == "working/taskboard/final/final.md"


async def test_taskboard_stages_each_explicit_required_deliverable(tmp_path):
    task = _task(
        tmp_path,
        "terminal-staged-multiple",
        required_deliverables=["report.md", "data.json"],
    )
    source_refs = []
    for path, body in (
        ("working/taskboard/final/report.md", "# Report\n"),
        ("working/taskboard/final/data.json", '{"ok": true}\n'),
    ):
        written = await task.task_workspace.write_file(path, body)
        readback = await task.task_workspace.read_file(
            written.path,
            max_bytes=written.bytes + 1,
        )
        source_refs.append(
            {
                **await task.task_workspace._promote_file_identity(
                    written.path,
                    role="task_workspace_artifact",
                ),
                "preview": readback.content,
                "read_bytes": readback.read_bytes,
                "truncated": False,
            }
        )

    staged_refs, promotions = await task._taskboard_stage_required_final_deliverable_refs(
        source_refs
    )

    assert {item["target_path"] for item in promotions} == {
        "report.md",
        "data.json",
    }
    assert {item.get("staged_target_path") for item in staged_refs} >= {
        "report.md",
        "data.json",
    }
    assert not task.task_workspace.resolve_file_path("report.md").exists()
    assert not task.task_workspace.resolve_file_path("data.json").exists()


async def test_staged_complete_readback_keeps_only_bounded_hot_preview(tmp_path):
    task = _task(
        tmp_path,
        "terminal-staged-bounded-preview",
        required_deliverables=["final.md"],
    )
    body = "# Long staged artifact\n\n" + ("evidence line\n" * 600)
    written = await task.task_workspace.write_file(
        "working/taskboard/final/final.md",
        body,
    )
    source_ref = await task.task_workspace._promote_file_identity(
        written.path,
        role="task_workspace_artifact",
    )

    staged_refs, promotions = await task._taskboard_stage_required_final_deliverable_refs(
        [source_ref]
    )

    staged_ref = next(
        ref for ref in staged_refs if ref.get("staged_target_path") == "final.md"
    )
    assert promotions[0]["source_sha256"] == written.sha256
    assert staged_ref["complete_readback_verified"] is True
    assert staged_ref["preview_truncated"] is True
    assert staged_ref["truncated"] is True
    assert len(staged_ref["preview"]) <= 4000
    assert staged_ref["preview"] != body


async def test_rejected_staged_candidate_does_not_create_required_deliverable(
    tmp_path,
    monkeypatch,
):
    task = _task(
        tmp_path,
        "terminal-staged-rejected",
        required_deliverables=["final.md"],
    )
    revision, prepared, terminal_refs = await _staged_finalization_inputs(
        task,
        "rejected candidate bytes\n",
    )

    async def reject_verification(*_args: Any, **kwargs: Any):
        assert kwargs["missing_deliverables"] == []
        assert kwargs["terminal_refs"] == terminal_refs
        assert not task.task_workspace.resolve_file_path("final.md").exists()
        verification = {
            "is_complete": False,
            "requires_block": True,
            "reason": "The staged candidate was rejected.",
            "missing_criteria": ["Repair the staged candidate."],
            "final_result_required": True,
            "final_result": "",
        }
        return {
            "transition": "blocked",
            "verification": verification,
            "issue": {},
            "repair_contract": {},
            "accepted_carrier_ids": [],
            "rejected_carrier_ids": [],
            "terminal_result": {
                "terminal_refs": terminal_refs,
                "final_file_refs": terminal_refs,
                "final_result": "",
            },
        }

    monkeypatch.setattr(task, "_run_terminal_verification", reject_verification)
    monkeypatch.setattr(task, "_record_phase", _noop)
    monkeypatch.setattr(task, "_emit", _noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={"goal": task.goal, "items": [], "omitted": [], "diagnostics": {}},
        prepared_outputs=prepared,
    )

    assert result == {"terminal": True, "status": "blocked"}
    assert task.result["accepted"] is False
    assert not task.task_workspace.resolve_file_path("final.md").exists()


async def test_accepted_staged_candidate_is_promoted_then_completely_read_back(
    tmp_path,
    monkeypatch,
):
    task = _task(
        tmp_path,
        "terminal-staged-accepted",
        required_deliverables=["final.md"],
    )
    body = "# Accepted terminal body\n\nComplete evidence-backed result.\n"
    revision, prepared, terminal_refs = await _staged_finalization_inputs(
        task,
        body,
    )

    async def accept_verification(*_args: Any, **kwargs: Any):
        assert kwargs["missing_deliverables"] == []
        assert kwargs["terminal_refs"] == terminal_refs
        assert not task.task_workspace.resolve_file_path("final.md").exists()
        verification = {
            "is_complete": True,
            "requires_block": False,
            "reason": "The staged bytes are accepted.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": "final.md",
        }
        return {
            "transition": "accepted",
            "verification": verification,
            "issue": {},
            "repair_contract": {},
            "accepted_carrier_ids": [],
            "rejected_carrier_ids": [],
            "terminal_result": {
                "terminal_refs": terminal_refs,
                "final_file_refs": terminal_refs,
                "final_result": "staged candidate",
            },
        }

    monkeypatch.setattr(task, "_run_terminal_verification", accept_verification)
    monkeypatch.setattr(task, "_record_phase", _noop)
    monkeypatch.setattr(task, "_emit", _noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={"goal": task.goal, "items": [], "omitted": [], "diagnostics": {}},
        prepared_outputs=prepared,
    )

    readback = await task.task_workspace.read_file("final.md", max_bytes=4096)
    assert result == {"terminal": True, "status": "completed"}
    assert readback.content == body
    assert task.result["accepted"] is True
    assert len(task.result["artifact_refs"]) == 1
    assert task.result["artifact_refs"][0]["path"] == readback.path
    assert task.result["artifact_refs"][0]["sha256"] == readback.sha256


async def test_staged_promotion_failure_prevents_completed(
    tmp_path,
    monkeypatch,
):
    task = _task(
        tmp_path,
        "terminal-staged-promotion-failure",
        required_deliverables=["final.md"],
    )
    revision, prepared, terminal_refs = await _staged_finalization_inputs(
        task,
        "accepted but not promotable\n",
    )

    async def accept_verification(*_args: Any, **_kwargs: Any):
        inventory = task._lifecycle_state.carrier_inventory
        assert inventory is not None
        accepted_ids = [carrier.carrier_id for carrier in inventory.carriers]
        task._lifecycle_state.record_terminal_transition(
            "accepted",
            expected_version=task._lifecycle_state.state_version,
            accepted_carrier_ids=accepted_ids,
        )
        verification = {
            "is_complete": True,
            "requires_block": False,
            "reason": "The staged bytes are semantically accepted.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": "final.md",
        }
        return {
            "transition": "accepted",
            "verification": verification,
            "issue": {},
            "repair_contract": {},
            "accepted_carrier_ids": accepted_ids,
            "rejected_carrier_ids": [],
            "terminal_result": {
                "terminal_refs": terminal_refs,
                "final_file_refs": terminal_refs,
                "final_result": "staged candidate",
            },
        }

    async def fail_promotion(*_args: Any, **_kwargs: Any):
        raise OSError("disk unavailable")

    monkeypatch.setattr(task, "_run_terminal_verification", accept_verification)
    monkeypatch.setattr(task.task_workspace, "atomic_promote_file", fail_promotion)
    monkeypatch.setattr(task, "_record_phase", _noop)
    monkeypatch.setattr(task, "_emit", _noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={"goal": task.goal, "items": [], "omitted": [], "diagnostics": {}},
        prepared_outputs=prepared,
    )

    assert result == {"terminal": True, "status": "blocked"}
    assert task.result["accepted"] is False
    assert "promotion" in task.result["reason"].lower()
    assert task._lifecycle_state.terminal_decision["transition"] == "blocked"
    assert not task.task_workspace.resolve_file_path("final.md").exists()


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


async def test_declared_file_deliverable_keeps_inline_summary_outside_terminal_inventory(tmp_path):
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
    ]
    assert len({carrier["carrier_id"] for carrier in candidate["carriers"]}) == 1
    assert all(carrier["required"] is True for carrier in candidate["carriers"])
    assert candidate["diagnostics"][0]["code"] == (
        "agent_task.terminal_carrier.inline_projection_not_a_deliverable"
    )


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
            "syntax_role": "prose",
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
