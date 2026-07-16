from __future__ import annotations

import pytest

from agently import Agently
from agently.core.application.AgentTask import AgentTask
from agently.core.application.AgentTask.LifecycleState import (
    AgentTaskLifecycleState,
    TerminalCarrier,
)


def _workspace_carrier(
    carrier_id: str,
    content_version_id: str,
    *,
    path: str = "final.md",
    digest: str = "a" * 64,
) -> dict[str, object]:
    return {
        "carrier_id": carrier_id,
        "kind": "workspace_artifact",
        "required": True,
        "content_version_id": content_version_id,
        "path": path,
        "content_digest": digest,
        "source_work_result_id": "work_A",
        "status": "materialized",
    }


def test_lifecycle_state_versions_advance_and_reject_stale_consumers():
    state = AgentTaskLifecycleState(
        task_id="task_A",
        requested_strategy="auto",
        effective_strategy="flat",
    )

    assert state.state_version == 1
    assert state.require_version(1) is state
    state.advance("context.prepare", expected_version=1, iteration=1)

    assert state.state_version == 2
    assert state.phase == "context.prepare"
    assert state.iteration == 1
    with pytest.raises(ValueError, match="stale AgentTask lifecycle version"):
        state.require_version(1)


def test_lifecycle_state_replaces_carriers_atomically_and_rejects_duplicate_ids():
    state = AgentTaskLifecycleState(
        task_id="task_carriers",
        requested_strategy="flat",
        effective_strategy="flat",
    )
    state.advance("evidence.ingest", expected_version=1, iteration=1)

    inventory = state.replace_carriers(
        [_workspace_carrier("car_A", "cv_A")],
        expected_version=2,
    )

    assert state.state_version == 3
    assert state.phase == "outputs.materialized"
    assert inventory.inventory_version == 1
    assert inventory.state_version == 3
    assert inventory.carriers[0].state_version == 3
    assert inventory.carriers[0].carrier_id == "car_A"
    with pytest.raises(ValueError, match="duplicate terminal carrier_id"):
        state.replace_carriers(
            [
                _workspace_carrier("car_B", "cv_B"),
                _workspace_carrier("car_B", "cv_C"),
            ],
            expected_version=3,
        )
    assert state.state_version == 3
    assert state.carrier_inventory == inventory


def test_changed_workspace_content_replaces_current_carrier_without_reusing_identity():
    state = AgentTaskLifecycleState(
        task_id="task_changed_path",
        requested_strategy="flat",
        effective_strategy="flat",
    )
    first = state.replace_carriers(
        [_workspace_carrier("car_A", "cv_A", digest="a" * 64)],
        expected_version=1,
    )
    old_carrier = first.carriers[0]
    second = state.replace_carriers(
        [_workspace_carrier("car_B", "cv_B", digest="b" * 64)],
        expected_version=2,
    )

    assert second.inventory_version == 2
    assert [carrier.carrier_id for carrier in second.carriers] == ["car_B"]
    assert old_carrier.carrier_id == "car_A"
    assert old_carrier.content_version_id == "cv_A"
    assert all(carrier.carrier_id != old_carrier.carrier_id for carrier in second.carriers)


def test_lifecycle_state_serialization_round_trip_preserves_current_versions_only():
    state = AgentTaskLifecycleState(
        task_id="task_round_trip",
        requested_strategy="taskboard",
        effective_strategy="taskboard",
    )
    state.advance("work.execute", expected_version=1, iteration=4)
    state.replace_carriers(
        [
            _workspace_carrier("car_file", "cv_file"),
            {
                "carrier_id": "car_inline",
                "kind": "inline_final_result",
                "required": True,
                "content_version_id": "inline:" + "c" * 64,
                "path": "",
                "content_digest": "c" * 64,
                "source_work_result_id": "work_B",
                "status": "proposed",
            },
        ],
        expected_version=2,
    )

    restored = AgentTaskLifecycleState.from_dict(state.to_dict())

    assert restored.to_dict() == state.to_dict()
    restored_inventory = restored.carrier_inventory
    state_inventory = state.carrier_inventory
    assert restored_inventory is not None
    assert state_inventory is not None
    assert restored_inventory.carriers == state_inventory.carriers
    assert isinstance(restored_inventory.carriers[0], TerminalCarrier)


@pytest.mark.asyncio
async def test_agent_task_resume_restores_private_lifecycle_state(tmp_path):
    agent = Agently.create_agent("lifecycle-state-resume").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="lifecycle-state-resume",
        goal="Produce final.md.",
        success_criteria=["final.md exists."],
        execution="flat",
        options={"workspace_recovery": True},
    )
    task._lifecycle_state.advance("evidence.ingest", expected_version=1, iteration=1)
    task._lifecycle_state.replace_carriers(
        [_workspace_carrier("car_resume", "cv_resume")],
        expected_version=2,
    )
    await task._write_resume_snapshot(
        1,
        {
            "is_complete": False,
            "requires_block": False,
            "reason": "Continue after restart.",
            "missing_criteria": ["One more step is required."],
        },
    )

    resumed = await AgentTask.async_resume(
        agent,
        task.id,
        workspace=tmp_path / "workspace",
    )

    assert resumed._lifecycle_state.to_dict() == task._lifecycle_state.to_dict()
    resumed_inventory = resumed._lifecycle_state.carrier_inventory
    assert resumed_inventory is not None
    assert resumed_inventory.carriers[0].carrier_id == "car_resume"
