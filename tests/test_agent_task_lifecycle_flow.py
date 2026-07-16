from __future__ import annotations

from typing import Any

import pytest

from agently import Agently
from agently.core.application.AgentTask import AgentTask
from agently.types.data import TaskBoardCardResult, TaskBoardRevision


EXPECTED_AGENT_TASK_LIFECYCLE_NODES = {
    "lifecycle.start",
    "context.prepare",
    "work.plan",
    "work.execute",
    "evidence.ingest",
    "outputs.materialize",
    "terminal.verify",
    "transition.decide",
}


def _task(tmp_path, name: str, *, execution: str) -> AgentTask:
    agent = Agently.create_agent(name).use_workspace(tmp_path / name)
    return AgentTask(
        agent,
        task_id=name,
        goal="Produce one verified result.",
        success_criteria=["The current result is verified."],
        execution=execution,
        max_iterations=1,
    )


@pytest.mark.parametrize("execution", ["flat", "taskboard"])
def test_agent_task_flow_exposes_complete_lifecycle_topology(tmp_path, execution):
    task = _task(tmp_path, f"lifecycle-topology-{execution}", execution=execution)

    config = task._flow.get_flow_config()
    chunk_names = {
        str(operator.get("name") or "")
        for operator in config["operators"]
        if operator.get("kind") == "chunk"
    }

    assert EXPECTED_AGENT_TASK_LIFECYCLE_NODES.issubset(chunk_names)
    assert "agent_task" not in chunk_names


def test_agent_task_lifecycle_signal_rejects_stale_state_version(tmp_path):
    task = _task(tmp_path, "lifecycle-stale-signal", execution="flat")
    stale_signal = {
        "task_id": task.id,
        "state_version": task._lifecycle_state.state_version,
        "frame_id": "frm_test",
        "iteration": 1,
    }
    task._lifecycle_state.advance(
        "context.prepare",
        expected_version=task._lifecycle_state.state_version,
        iteration=1,
    )

    with pytest.raises(ValueError, match="stale AgentTask lifecycle version"):
        task._require_lifecycle_signal(stale_signal)


def test_agent_task_lifecycle_signal_rejects_cross_task_frame(tmp_path):
    task = _task(tmp_path, "lifecycle-cross-task", execution="taskboard")

    with pytest.raises(ValueError, match="different task"):
        task._require_lifecycle_signal(
            {
                "task_id": "another-task",
                "state_version": task._lifecycle_state.state_version,
                "frame_id": "frm_test",
                "iteration": 1,
            }
        )


def test_agent_task_lifecycle_signal_projects_only_short_identity_fields(tmp_path):
    task = _task(tmp_path, "lifecycle-short-signal", execution="flat")

    signal = task._require_lifecycle_signal(
        {
            "task_id": task.id,
            "state_version": task._lifecycle_state.state_version,
            "frame_id": "frm_test",
            "iteration": 0,
            "plan_id": "pln_test",
            "work_result_id": "wrk_test",
            "evidence_ref": "evd_test",
            "prompt": "must not cross the lifecycle event boundary",
            "body": "must remain in the host-owned frame",
        }
    )

    assert signal == {
        "task_id": task.id,
        "state_version": task._lifecycle_state.state_version,
        "frame_id": "frm_test",
        "iteration": 0,
        "plan_id": "pln_test",
        "work_result_id": "wrk_test",
        "evidence_ref": "evd_test",
    }


@pytest.mark.asyncio
async def test_flat_lifecycle_executes_visible_stages_and_allocates_short_ids(
    tmp_path,
    monkeypatch,
):
    task = _task(tmp_path, "lifecycle-flat-stage-order", execution="flat")
    calls: list[str] = []

    def stage(name: str):
        async def run(frame: dict[str, Any]) -> dict[str, Any]:
            calls.append(name)
            if name == "transition.decide":
                task.status = "completed"
                task.result = {"status": "completed", "accepted": True}
                frame["iteration_result"] = {
                    "terminal": True,
                    "status": "completed",
                }
            return frame

        return run

    for name, method_name in (
        ("context.prepare", "_flat_context_prepare_stage"),
        ("work.plan", "_flat_work_plan_stage"),
        ("work.execute", "_flat_work_execute_stage"),
        ("outputs.materialize", "_flat_outputs_materialize_stage"),
        ("evidence.ingest", "_flat_evidence_ingest_stage"),
        ("terminal.verify", "_flat_terminal_verify_stage"),
        ("transition.decide", "_flat_transition_decide_stage"),
    ):
        monkeypatch.setattr(task, method_name, stage(name))

    execution = task._flow.create_execution(auto_close=False, workspace=False)
    await execution.async_start({"task_id": task.id})
    snapshot = await execution.async_close(reason="test.lifecycle_complete")

    assert calls == [
        "context.prepare",
        "work.plan",
        "work.execute",
        "outputs.materialize",
        "evidence.ingest",
        "terminal.verify",
        "transition.decide",
    ]
    assert task._lifecycle_state.current_frame_id.startswith("frm_")
    assert task._lifecycle_state.current_plan_id.startswith("pln_")
    assert task._lifecycle_state.work_result_id.startswith("wrk_")
    assert task._lifecycle_state.evidence_ref.startswith("evd_")
    assert snapshot["agent_task"]["status"] == "completed"
    topology = snapshot["agent_task"]["lifecycle_topology"]
    assert topology["signal_schema"]["required"] == [
        "task_id",
        "state_version",
        "frame_id",
        "iteration",
    ]


@pytest.mark.asyncio
async def test_taskboard_work_subflow_traverses_outer_terminal_stages(
    tmp_path,
    monkeypatch,
):
    task = _task(tmp_path, "lifecycle-taskboard-stage-order", execution="taskboard")
    calls: list[str] = []

    async def context(frame: dict[str, Any]) -> dict[str, Any]:
        calls.append("context.prepare")
        return frame

    async def plan(frame: dict[str, Any]) -> dict[str, Any]:
        calls.append("work.plan")
        return frame

    async def stage(name: str, frame: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        if name == "transition.decide":
            task.status = "completed"
            task.result = {"status": "completed", "accepted": True}
            frame["iteration_result"] = {"terminal": True, "status": "completed"}
        return frame

    monkeypatch.setattr(task, "_taskboard_context_prepare_stage", context)
    monkeypatch.setattr(task, "_taskboard_work_plan_stage", plan)
    monkeypatch.setattr(
        task,
        "_taskboard_work_execute_stage",
        lambda frame: stage("work.execute", frame),
    )
    for name, method_name in (
        ("outputs.materialize", "_taskboard_outputs_materialize_stage"),
        ("evidence.ingest", "_taskboard_evidence_ingest_stage"),
        ("terminal.verify", "_taskboard_terminal_verify_stage"),
        ("transition.decide", "_taskboard_transition_decide_stage"),
    ):
        monkeypatch.setattr(
            task,
            method_name,
            lambda frame, stage_name=name: stage(stage_name, frame),
            raising=False,
        )

    execution = task._flow.create_execution(auto_close=False, workspace=False)
    await execution.async_start({"task_id": task.id})
    snapshot = await execution.async_close(reason="test.lifecycle_complete")

    assert calls == [
        "context.prepare",
        "work.plan",
        "work.execute",
        "outputs.materialize",
        "evidence.ingest",
        "terminal.verify",
        "transition.decide",
    ]
    assert snapshot["agent_task"]["status"] == "completed"
    owner = snapshot["agent_task"]["lifecycle_topology"]["taskboard_work_owner"]
    assert owner == {
        "node": "work.execute",
        "nested_flow": "task_board.lifecycle",
        "outer_terminal_nodes": [
            "outputs.materialize",
            "evidence.ingest",
            "terminal.verify",
            "transition.decide",
        ],
    }


@pytest.mark.asyncio
async def test_taskboard_verifier_protocol_retry_reenters_only_terminal_verify(
    tmp_path,
    monkeypatch,
):
    task = _task(tmp_path, "lifecycle-taskboard-verifier-retry", execution="taskboard")
    calls: list[str] = []
    verify_count = 0

    def stage(name: str):
        async def run(frame: dict[str, Any]) -> dict[str, Any]:
            nonlocal verify_count
            calls.append(name)
            if name == "terminal.verify":
                verify_count += 1
                if verify_count == 1:
                    frame["taskboard_transition_result"] = {
                        "terminal": False,
                        "status": "verification_retry",
                    }
                else:
                    task.status = "completed"
                    task.result = {"status": "completed", "accepted": True}
                    frame["taskboard_transition_result"] = {
                        "terminal": True,
                        "status": "completed",
                    }
            return frame

        return run

    for name, method_name in (
        ("context.prepare", "_taskboard_context_prepare_stage"),
        ("work.plan", "_taskboard_work_plan_stage"),
        ("work.execute", "_taskboard_work_execute_stage"),
        ("outputs.materialize", "_taskboard_outputs_materialize_stage"),
        ("evidence.ingest", "_taskboard_evidence_ingest_stage"),
        ("terminal.verify", "_taskboard_terminal_verify_stage"),
    ):
        monkeypatch.setattr(task, method_name, stage(name))

    execution = task._flow.create_execution(auto_close=False, workspace=False)
    await execution.async_start({"task_id": task.id})
    snapshot = await execution.async_close(reason="test.lifecycle_complete")

    assert calls == [
        "context.prepare",
        "work.plan",
        "work.execute",
        "outputs.materialize",
        "evidence.ingest",
        "terminal.verify",
        "terminal.verify",
    ]
    assert snapshot["agent_task"]["status"] == "completed"


@pytest.mark.asyncio
async def test_simulated_taskboard_protocol_retry_preserves_work_and_artifact(
    tmp_path,
    monkeypatch,
):
    """Warm simulated preflight: a malformed verifier join is verifier-owned."""

    task = AgentTask(
        Agently.create_agent("simulated-taskboard-protocol-retry").use_workspace(
            tmp_path / "workspace"
        ),
        task_id="simulated-taskboard-protocol-retry",
        goal="Write the formatted report to final.md.",
        success_criteria=["The formatted report is delivered at final.md."],
        execution="taskboard",
        max_iterations=2,
        options={
            "agent_task": {
                "required_deliverables": [{"path": "final.md"}],
            }
        },
    )
    report_text = "# Formatted Report\n\nThe requested formatting is complete.\n"
    write_result = await task.workspace.write_file("final.md", report_text)
    final_ref = {
        **write_result["file_refs"][0],
        "role": "workspace_artifact",
        "source": "agent_task.taskboard.card.deliver.workspace_artifact",
    }
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-simulated-ready",
            "status": "completed",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "deliver",
                        "objective": "Deliver final.md.",
                        "required_outputs": ["final.md"],
                    }
                ],
            },
            "card_results": {
                "deliver": TaskBoardCardResult(
                    card_id="deliver",
                    status="completed",
                    preview={
                        "status": "completed",
                        "sufficient": True,
                        "candidate_final_result": "final.md",
                        "artifact_manifest": {"path": "final.md"},
                        "remaining_work": [],
                    },
                    file_refs=(final_ref,),
                    artifact_refs=(final_ref,),
                ).to_dict()
            },
        }
    )
    initial_digest = write_result["sha256"]
    calls = {"work": 0, "verifier": 0, "finalizer": 0}

    async def context(frame: dict[str, Any]) -> dict[str, Any]:
        frame["context_pack"] = {
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        }
        return frame

    async def plan(frame: dict[str, Any]) -> dict[str, Any]:
        return frame

    async def work(frame: dict[str, Any]) -> dict[str, Any]:
        calls["work"] += 1
        frame["taskboard_revision"] = revision.to_dict()
        frame["taskboard_tick_index"] = 1
        return frame

    class FakeVerifierRequest:
        def __init__(self):
            self.instruction = ""
            self.input_value: dict[str, Any] = {}

        def input(self, value):
            self.input_value = value
            return self

        def instruct(self, value):
            self.instruction = str(value)
            return self

        def output(self, _value, *, format):
            assert format == "json"
            return self

        async def async_get_data(self):
            assert "Verify the task against every success criterion" in self.instruction
            calls["verifier"] += 1
            if calls["verifier"] == 2:
                protocol_repair = self.input_value["verification_protocol_repair"]
                assert protocol_repair["occurrence"] == 1
                assert protocol_repair["repair_contract"]["gate_kind"] == "output_contract"
                assert (
                    protocol_repair["repair_contract"]["issue_code"]
                    == "terminal_verifier_output_invalid"
                )
                assert protocol_repair["current_offered_reference_ids"] == []
            evidence_ids = ["ref_not_offered"] if calls["verifier"] == 1 else []
            return {
                "is_complete": True,
                "requires_block": False,
                "reason": "The current artifact is complete.",
                "failure_analysis": "",
                "acceptance_delta": [],
                "missing_criteria": [],
                "replan_instruction": "",
                "repair_constraints": [],
                "next_step_requirements": [],
                "final_result_required": True,
                "final_result": "final.md",
                "criterion_checks": [
                    {
                        "criterion_id": "criterion:1",
                        "satisfied": True,
                        "summary": "The declared artifact exists at final.md.",
                        "evidence_ids": evidence_ids,
                    }
                ],
                "material_claim_coverage_complete": True,
                "material_claim_checks": [],
            }

    async def fail_finalizer(*_args, **_kwargs):
        calls["finalizer"] += 1
        raise AssertionError("A promotable leaf artifact must skip final synthesis.")

    monkeypatch.setattr(task, "_taskboard_context_prepare_stage", context)
    monkeypatch.setattr(task, "_taskboard_work_plan_stage", plan)
    monkeypatch.setattr(task, "_taskboard_work_execute_stage", work)
    monkeypatch.setattr(task.agent, "create_temp_request", FakeVerifierRequest)
    monkeypatch.setattr(task, "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(
        task,
        "_apply_language_policy_to_request",
        lambda *_args, **_kwargs: None,
    )

    execution = task._flow.create_execution(auto_close=False, workspace=False)
    await execution.async_start({"task_id": task.id})
    snapshot = await execution.async_close(reason="test.simulated_complete")
    current_ref = await task.workspace._promote_file_identity(
        "final.md",
        role="test_readback",
    )

    assert calls == {"work": 1, "verifier": 2, "finalizer": 0}
    assert snapshot["agent_task"]["status"] == "completed"
    assert task.result["accepted"] is True
    assert current_ref["sha256"] == initial_digest
    records = task._terminal_convergence_state.snapshot()["records"]
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["issue"] == {
        "gate_kind": "output_contract",
        "issue_code": "terminal_verifier_output_invalid",
        "contract_subject": "verification:response",
    }
    assert record["occurrence"] == 1
    assert record["resolved"] is True


@pytest.mark.asyncio
async def test_agent_task_rethrows_event_stage_failure_to_host(tmp_path, monkeypatch):
    task = _task(tmp_path, "lifecycle-stage-failure", execution="flat")

    async def fail_context(frame: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("synthetic lifecycle stage failure")

    monkeypatch.setattr(task, "_flat_context_prepare_stage", fail_context)

    with pytest.raises(RuntimeError, match="synthetic lifecycle stage failure"):
        await task.async_run()


@pytest.mark.asyncio
async def test_terminal_stage_result_skips_unexecuted_lifecycle_stages(
    tmp_path,
    monkeypatch,
):
    task = _task(tmp_path, "lifecycle-terminal-shortcut", execution="flat")
    calls: list[str] = []

    async def terminal_context(frame: dict[str, Any]) -> dict[str, Any]:
        calls.append("context.prepare")
        task.status = "timed_out"
        task.result = {"status": "timed_out", "accepted": False}
        frame["iteration_result"] = {"terminal": True, "status": "timed_out"}
        return frame

    async def transition(frame: dict[str, Any]) -> dict[str, Any]:
        calls.append("transition.decide")
        return frame

    async def fail_unexecuted(frame: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("A terminal stage result must bypass all later work stages.")

    monkeypatch.setattr(task, "_flat_context_prepare_stage", terminal_context)
    monkeypatch.setattr(task, "_flat_transition_decide_stage", transition)
    for method_name in (
        "_flat_work_plan_stage",
        "_flat_work_execute_stage",
        "_flat_outputs_materialize_stage",
        "_flat_evidence_ingest_stage",
        "_flat_terminal_verify_stage",
    ):
        monkeypatch.setattr(task, method_name, fail_unexecuted)

    execution = task._flow.create_execution(auto_close=False, workspace=False)
    await execution.async_start({"task_id": task.id})
    await execution.async_close(reason="test.lifecycle_complete")

    assert calls == ["context.prepare", "transition.decide"]
    assert task._lifecycle_state.current_plan_id == ""
    assert task._lifecycle_state.work_result_id == ""
    assert task._lifecycle_state.evidence_ref == ""
