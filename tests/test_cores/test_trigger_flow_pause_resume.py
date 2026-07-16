import asyncio
import copy
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from agently import TriggerFlow, TriggerFlowInterruptEvent, TriggerFlowRuntimeData, Workspace


@pytest.mark.asyncio
async def test_trigger_flow_dynamic_pause_persists_waiting_snapshot_without_backfill(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    flow = TriggerFlow(name="dynamic-pause-default-workspace")

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    flow.to(ask_feedback)
    execution = flow.create_execution(
        auto_close=False,
        runtime_resources={"workspace": Workspace(tmp_path)},
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    try:
        assert workspace._backend is None
        assert execution._snapshot_store is None
        assert execution._runtime_event_store is None

        await execution.async_start("pricing")

        pending = execution.get_pending_interrupts()
        snapshot = await workspace.get_snapshot(execution.run_context.run_id)
        runtime_events = await workspace.query_runtime_events(execution.id)

        assert pending["approval"]["status"] == "waiting"
        assert snapshot is not None
        assert snapshot["interrupts"]["approval"]["status"] == "waiting"
        assert runtime_events == []
    finally:
        await execution.async_close(pending_interrupts="cancel")
    private_files = {
        path.relative_to(tmp_path).as_posix()
        for path in (tmp_path / ".agently").rglob("*")
        if path.is_file()
    }
    assert private_files == {
        ".agently/identity/state.json",
        ".agently/identity/state.lock",
    }


@pytest.mark.asyncio
async def test_trigger_flow_dynamic_pause_snapshot_failure_rolls_back_before_publish(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    published: list[dict[str, Any]] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            published.append(
                {
                    "execution_id": execution_id,
                    "request": request,
                    "interrupt": interrupt,
                }
            )
            return None

    execution = TriggerFlow(name="dynamic-pause-snapshot-failure").create_execution(
        auto_close=False,
        runtime_resources={
            "execution_exchange_provider": ExchangeProvider(),
            "workspace": Workspace(tmp_path),
        },
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))
    status_before_pause = execution.get_status()
    state_version_before_pause = execution._state_version

    async def fail_snapshot(*args, **kwargs):
        raise OSError("injected pause snapshot failure")

    monkeypatch.setattr(workspace, "put_snapshot", fail_snapshot)
    try:
        with pytest.raises(OSError, match="injected pause snapshot failure"):
            await execution.async_pause_for(
                type="approval",
                interrupt_id="approval",
                resume_to="next",
            )

        assert execution.get_interrupt("approval") is None
        assert execution.get_pending_interrupts() == {}
        assert execution.get_status() == status_before_pause
        assert execution._state_version >= state_version_before_pause
        assert published == []
    finally:
        await execution.async_close(pending_interrupts="cancel")


async def _run_overlapping_pause_snapshot_failure(
    *,
    first_interrupt_id: str,
    second_interrupt_id: str,
):
    first_snapshot_entered = asyncio.Event()
    release_first_snapshot = asyncio.Event()
    snapshot_calls = 0
    published: list[dict[str, Any]] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            published.append(
                {
                    "execution_id": execution_id,
                    "request": request,
                    "interrupt": interrupt,
                }
            )
            return None

    execution = TriggerFlow(name="overlapping-pause-snapshot").create_execution(
        auto_close=False,
        runtime_resources={"execution_exchange_provider": ExchangeProvider()},
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    async def controlled_snapshot(run_id, state, *, step_id=None, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            first_snapshot_entered.set()
            await release_first_snapshot.wait()
            raise OSError("injected older snapshot failure")
        return {"id": "newer-snapshot", "run_id": run_id, "step_id": step_id}

    workspace.put_snapshot = controlled_snapshot
    first_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 1},
            interrupt_id=first_interrupt_id,
            resume_to="next",
        )
    )
    await asyncio.wait_for(first_snapshot_entered.wait(), timeout=2)
    second_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 2},
            interrupt_id=second_interrupt_id,
            resume_to="next",
        )
    )
    if first_interrupt_id == second_interrupt_id:
        await asyncio.sleep(0.01)
        assert not second_task.done()
    else:
        await asyncio.wait_for(asyncio.shield(second_task), timeout=2)
    release_first_snapshot.set()
    with pytest.raises(OSError, match="injected older snapshot failure"):
        await first_task
    await second_task
    return execution, published


@pytest.mark.asyncio
async def test_pause_snapshot_rollback_does_not_delete_newer_same_id_interrupt():
    execution, published = await _run_overlapping_pause_snapshot_failure(
        first_interrupt_id="same",
        second_interrupt_id="same",
    )
    try:
        stored = execution.get_interrupt("same")
        assert isinstance(stored, dict)
        assert stored["status"] == "waiting"
        assert stored["payload"] == {"attempt": 2}
        assert execution.get_status() == "waiting"
        assert [item["interrupt"]["payload"] for item in published] == [
            {"attempt": 2}
        ]
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_pause_snapshot_rollback_keeps_waiting_status_for_other_interrupt():
    execution, published = await _run_overlapping_pause_snapshot_failure(
        first_interrupt_id="older",
        second_interrupt_id="newer",
    )
    try:
        assert execution.get_interrupt("older") is None
        newer = execution.get_interrupt("newer")
        assert isinstance(newer, dict)
        assert newer["status"] == "waiting"
        assert execution.get_status() == "waiting"
        assert [item["interrupt"]["id"] for item in published] == ["newer"]
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_same_id_pause_does_not_publish_older_attempt_after_newer_attempt():
    first_snapshot_entered = asyncio.Event()
    release_first_snapshot = asyncio.Event()
    snapshot_calls = 0
    published: list[int] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            attempt = int(interrupt["payload"]["attempt"])
            published.append(attempt)
            return {"exchange_id": f"exchange-{attempt}"}

    execution = TriggerFlow(name="same-id-pause-publish-order").create_execution(
        auto_close=False,
        runtime_resources={"execution_exchange_provider": ExchangeProvider()},
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    async def controlled_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            first_snapshot_entered.set()
            await release_first_snapshot.wait()
        return {"id": f"snapshot-{snapshot_calls}"}

    workspace.put_snapshot = controlled_snapshot
    first_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 1},
            interrupt_id="same",
            resume_to="next",
        )
    )
    await asyncio.wait_for(first_snapshot_entered.wait(), timeout=2)
    second_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 2},
            interrupt_id="same",
            resume_to="next",
        )
    )
    await asyncio.sleep(0.01)
    assert not second_task.done()
    release_first_snapshot.set()

    try:
        await asyncio.gather(first_task, second_task)
        stored = execution.get_interrupt("same")
        assert isinstance(stored, dict)
        assert published == [1, 2]
        assert stored["payload"] == {"attempt": 2}
        assert stored["external_wait_request"]["exchange_id"] == "exchange-2"
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_same_id_cancelled_pause_attempts_leave_no_ghost_interrupt_or_fence():
    snapshot_entered = [asyncio.Event(), asyncio.Event()]
    snapshot_calls = 0
    published: list[dict[str, Any]] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            published.append(interrupt)
            return None

    execution = TriggerFlow(name="same-id-pause-cancellation").create_execution(
        auto_close=False,
        runtime_resources={"execution_exchange_provider": ExchangeProvider()},
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    async def blocked_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        call_index = snapshot_calls
        snapshot_calls += 1
        snapshot_entered[call_index].set()
        await asyncio.Event().wait()

    workspace.put_snapshot = blocked_snapshot
    first_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 1},
            interrupt_id="same",
            resume_to="next",
        )
    )
    await asyncio.wait_for(snapshot_entered[0].wait(), timeout=2)
    second_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 2},
            interrupt_id="same",
            resume_to="next",
        )
    )

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    await asyncio.wait_for(snapshot_entered[1].wait(), timeout=2)
    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task

    try:
        assert execution.get_interrupt("same") is None
        assert execution.get_pending_interrupts() == {}
        assert execution.get_status() == "created"
        assert execution._interrupts._pause_boundary_locks == {}
        assert published == []
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_same_id_failed_pause_attempts_leave_no_ghost_interrupt_or_fence():
    first_snapshot_entered = asyncio.Event()
    release_first_snapshot = asyncio.Event()
    snapshot_calls = 0

    execution = TriggerFlow(name="same-id-pause-double-failure").create_execution(
        auto_close=False,
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    async def failed_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            first_snapshot_entered.set()
            await release_first_snapshot.wait()
        raise OSError(f"snapshot failure {snapshot_calls}")

    workspace.put_snapshot = failed_snapshot
    first_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 1},
            interrupt_id="same",
            resume_to="next",
        )
    )
    await asyncio.wait_for(first_snapshot_entered.wait(), timeout=2)
    second_task = asyncio.create_task(
        execution.async_pause_for(
            type="approval",
            payload={"attempt": 2},
            interrupt_id="same",
            resume_to="next",
        )
    )
    await asyncio.sleep(0.01)
    assert not second_task.done()
    release_first_snapshot.set()

    results = await asyncio.gather(first_task, second_task, return_exceptions=True)
    try:
        assert all(isinstance(result, OSError) for result in results)
        assert execution.get_interrupt("same") is None
        assert execution.get_pending_interrupts() == {}
        assert execution.get_status() == "created"
        assert execution._interrupts._pause_boundary_locks == {}
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_same_id_failed_pause_restores_previous_exposed_interrupt():
    snapshot_calls = 0
    published: list[int] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            attempt = int(interrupt["payload"]["attempt"])
            published.append(attempt)
            return {"exchange_id": f"exchange-{attempt}"}

    execution = TriggerFlow(name="same-id-pause-restore-previous").create_execution(
        auto_close=False,
        runtime_resources={"execution_exchange_provider": ExchangeProvider()},
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    async def fail_second_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 2:
            raise OSError("snapshot-2-failed")
        return {"id": "snapshot-1"}

    workspace.put_snapshot = fail_second_snapshot
    try:
        await execution.async_pause_for(
            type="approval",
            payload={"attempt": 1},
            interrupt_id="same",
            resume_to="next",
        )
        previous = copy.deepcopy(execution.get_interrupt("same"))

        with pytest.raises(OSError, match="snapshot-2-failed"):
            await execution.async_pause_for(
                type="approval",
                payload={"attempt": 2},
                interrupt_id="same",
                resume_to="next",
            )

        assert published == [1]
        assert execution.get_interrupt("same") == previous
        assert execution.get_status() == "waiting"
        assert execution._interrupts._pause_boundary_locks == {}
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_continue_waits_until_pause_snapshot_and_exposure_complete():
    snapshot_entered = asyncio.Event()
    release_snapshot = asyncio.Event()
    published: list[str] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            published.append(str(interrupt["external_wait_request"]["dispatch_state"]))
            return {"exchange_id": "approval-1"}

    flow = TriggerFlow(name="pause-resume-boundary-order")

    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="same",
            resume_to="next",
        )

    async def finish(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(pause).to(finish)
    execution = flow.create_execution(
        auto_close=False,
        runtime_resources={"execution_exchange_provider": ExchangeProvider()},
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    async def blocked_snapshot(*args, **kwargs):
        snapshot_entered.set()
        await release_snapshot.wait()
        return {"id": "waiting-snapshot"}

    workspace.put_snapshot = blocked_snapshot
    start_task = asyncio.create_task(execution.async_start("draft"))
    await asyncio.wait_for(snapshot_entered.wait(), timeout=2)
    continue_task = asyncio.create_task(
        execution.async_continue_with(
            "same",
            "approved",
            resume_request_id="early-resume",
        )
    )
    await asyncio.sleep(0.01)

    try:
        assert not continue_task.done()
        assert published == []
        pending_before_exposure = execution.get_interrupt("same")
        assert isinstance(pending_before_exposure, dict)
        assert pending_before_exposure["status"] == "waiting"
        release_snapshot.set()
        await start_task
        await continue_task
        assert published == ["persisted"]
        stored = execution.get_interrupt("same")
        assert isinstance(stored, dict)
        assert stored["status"] == "resumed"
        assert stored["external_wait_request"]["dispatch_state"] == "completed"
    finally:
        if not release_snapshot.is_set():
            release_snapshot.set()
        await asyncio.gather(start_task, continue_task, return_exceptions=True)
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_close_cancellation_during_pause_snapshot_does_not_publish_stale_wait():
    snapshot_entered = asyncio.Event()
    release_snapshot = asyncio.Event()
    published: list[dict[str, Any]] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            published.append(interrupt)
            return None

    flow = TriggerFlow(name="pause-close-boundary-order")

    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="same",
            resume_to="next",
        )

    flow.to(pause)
    execution = flow.create_execution(
        auto_close=False,
        runtime_resources={"execution_exchange_provider": ExchangeProvider()},
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    async def blocked_snapshot(*args, **kwargs):
        snapshot_entered.set()
        await release_snapshot.wait()
        return {"id": "waiting-snapshot"}

    workspace.put_snapshot = blocked_snapshot
    start_task = asyncio.create_task(execution.async_start("draft"))
    await asyncio.wait_for(snapshot_entered.wait(), timeout=2)
    close_task = asyncio.create_task(
        execution.async_close(reason="cancel-during-snapshot", pending_interrupts="cancel")
    )
    await asyncio.sleep(0.01)

    try:
        stored = execution.get_interrupt("same")
        assert isinstance(stored, dict)
        assert stored["status"] == "cancelled"
        release_snapshot.set()
        await asyncio.gather(start_task, close_task)
        assert published == []
        cancelled = execution.get_interrupt("same")
        assert isinstance(cancelled, dict)
        assert cancelled["status"] == "cancelled"
    finally:
        if not release_snapshot.is_set():
            release_snapshot.set()
        await asyncio.gather(start_task, close_task, return_exceptions=True)
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_close_cancellation_during_persisted_event_does_not_publish_stale_wait(tmp_path):
    persisted_event_entered = asyncio.Event()
    release_persisted_event = asyncio.Event()
    published: list[dict[str, Any]] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            published.append(interrupt)
            return None

    flow = TriggerFlow(name="pause-close-persisted-event-order")

    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="same",
            resume_to="next",
        )

    flow.to(pause)
    workspace = Workspace(tmp_path)
    execution = flow.create_execution(
        auto_close=False,
        runtime_resources={
            "execution_exchange_provider": ExchangeProvider(),
            "workspace": workspace,
            "runtime_event_store": workspace,
        },
    )
    workspace = cast(Any, execution.require_runtime_resource("workspace"))
    runtime_event_store = cast(Any, execution._runtime_event_store)
    original_append_runtime_event = runtime_event_store.append_runtime_event

    async def blocked_persisted_event(execution_id, event, **kwargs):
        if event.event_type == "triggerflow.interrupt_persisted":
            persisted_event_entered.set()
            await release_persisted_event.wait()
        return await original_append_runtime_event(execution_id, event, **kwargs)

    runtime_event_store.append_runtime_event = blocked_persisted_event
    start_task = asyncio.create_task(execution.async_start("draft"))
    await asyncio.wait_for(persisted_event_entered.wait(), timeout=2)
    close_task = asyncio.create_task(
        execution.async_close(reason="cancel-during-persisted-event", pending_interrupts="cancel")
    )
    await asyncio.sleep(0.01)

    try:
        stored = execution.get_interrupt("same")
        assert isinstance(stored, dict)
        assert stored["status"] == "cancelled"
        release_persisted_event.set()
        await asyncio.gather(start_task, close_task)
        assert published == []
        cancelled = execution.get_interrupt("same")
        assert isinstance(cancelled, dict)
        assert cancelled["status"] == "cancelled"
    finally:
        if not release_persisted_event.is_set():
            release_persisted_event.set()
        await asyncio.gather(start_task, close_task, return_exceptions=True)
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_self_resume_can_repause_same_id_after_exposure_barrier():
    flow = TriggerFlow(name="same-id-self-repause")

    async def gate(data: TriggerFlowRuntimeData):
        if data.is_resume and data.resume.value == "complete":
            return "done"
        return await data.async_pause_for(
            type="approval",
            interrupt_id="gate",
            resume_to="self",
            max_resumes=2,
        )

    flow.to(gate)
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("draft")

    try:
        await asyncio.wait_for(
            execution.async_continue_with(
                "gate",
                "retry",
                resume_request_id="round-1",
            ),
            timeout=2,
        )
        repaused = execution.get_interrupt("gate")
        assert isinstance(repaused, dict)
        assert repaused["status"] == "waiting"
        assert repaused["resume_count"] == 1
        assert repaused["resume_requests"]["round-1"]["status"] == "completed"

        await asyncio.wait_for(
            execution.async_continue_with(
                "gate",
                "complete",
                resume_request_id="round-2",
            ),
            timeout=2,
        )
        resumed = execution.get_interrupt("gate")
        assert isinstance(resumed, dict)
        assert resumed["status"] == "resumed"
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_self_repause_dispatch_failure_preserves_newer_wait_generation():
    flow = TriggerFlow(name="same-id-self-repause-dispatch-failure")

    async def gate(data: TriggerFlowRuntimeData):
        if data.is_resume:
            await data.async_pause_for(
                type="approval",
                interrupt_id="gate",
                resume_to="self",
                max_resumes=2,
            )
            raise RuntimeError("after repause")
        return await data.async_pause_for(
            type="approval",
            interrupt_id="gate",
            resume_to="self",
            max_resumes=2,
        )

    flow.to(gate)
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("draft")
    first_generation = copy.deepcopy(execution.get_interrupt("gate"))

    try:
        with pytest.raises(RuntimeError, match="after repause"):
            await execution.async_continue_with(
                "gate",
                "retry",
                resume_request_id="round-1",
            )

        repaused = execution.get_interrupt("gate")
        assert isinstance(first_generation, dict)
        assert isinstance(repaused, dict)
        assert repaused["created_at"] != first_generation["created_at"]
        assert repaused["status"] == "waiting"
        assert repaused["external_wait_request"]["dispatch_state"] == "exposed"
        assert repaused["resume_requests"]["round-1"]["status"] == "dispatch_failed"
        assert execution.get_status() == "waiting"
        assert execution.is_waiting() is True
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_trigger_flow_pause_continue_with_saved_interrupt():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        data.set_runtime_data("draft", {"topic": data.value})
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_event="UserFeedback",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        return {
            "draft": data.get_runtime_data("draft"),
            "feedback": data.value,
        }

    flow.to(ask_feedback)
    flow.when("UserFeedback").to(finalize).end()

    execution = await flow.async_start_execution("pricing")
    pending_interrupts = execution.get_pending_interrupts()

    assert execution.get_status() == "waiting"
    assert len(pending_interrupts) == 1

    interrupt_id, interrupt = next(iter(pending_interrupts.items()))
    assert interrupt["resume_event"] == "UserFeedback"

    restored_execution = flow.create_execution()
    restored_execution.load(execution.save())

    assert restored_execution.get_status() == "waiting"
    assert interrupt_id in restored_execution.get_pending_interrupts()

    await restored_execution.async_continue_with(interrupt_id, {"approved": True})
    result = await restored_execution.async_get_result(timeout=1)
    close_snapshot = await restored_execution.async_close()

    assert close_snapshot["$final_result"] == result
    assert restored_execution.get_status() == "completed"
    assert result == {
        "draft": {"topic": "pricing"},
        "feedback": {"approved": True},
    }


@pytest.mark.asyncio
async def test_trigger_flow_pause_resume_to_next_continues_downstream_after_load():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        await data.async_set_state("draft", {"topic": data.value})
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            interrupt_id="approval",
            resume_to="next",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        return {
            "draft": data.get_state("draft"),
            "feedback": data.value,
        }

    flow.to(ask_feedback).to(finalize).end()

    execution = await flow.async_start_execution("pricing", wait_for_result=False)
    saved_state = execution.save()
    restored = flow.create_execution(auto_close=False)
    restored.load(saved_state)

    await restored.async_continue_with("approval", {"approved": True})
    result = await restored.async_get_result(timeout=1)

    assert result == {
        "draft": {"topic": "pricing"},
        "feedback": {"approved": True},
    }


@pytest.mark.asyncio
async def test_trigger_flow_pause_resume_to_self_exposes_resume_context():
    flow = TriggerFlow()

    async def gate(data: TriggerFlowRuntimeData):
        if data.is_resume:
            assert data.resume.origin_signal is not None
            await data.async_set_state(
                "resume",
                {
                    "interrupt_id": data.resume.interrupt_id,
                    "value": data.resume.value,
                    "origin_event": data.resume.origin_signal["trigger_event"],
                    "input": data.value,
                },
            )
            return "approved"
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            payload={"value": data.value},
            interrupt_id="gate",
            resume_to="self",
        )

    flow.to(gate).end()
    execution = await flow.async_start_execution("document", wait_for_result=False)

    await execution.async_continue_with("gate", {"approved": True})
    snapshot = await execution.async_close()

    assert snapshot["resume"] == {
        "interrupt_id": "gate",
        "value": {"approved": True},
        "origin_event": "START",
        "input": "document",
    }
    assert snapshot["$final_result"] == "approved"


@pytest.mark.asyncio
async def test_trigger_flow_hidden_start_rejects_pause_without_resume_handle():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_to="next",
        )

    flow.to(ask_feedback)

    with pytest.raises(RuntimeError, match="requires an exposed execution handle"):
        await flow.async_start("pricing")


def test_trigger_flow_hidden_runtime_stream_rejects_pause_without_resume_handle():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_to="next",
        )

    flow.to(ask_feedback)

    with pytest.raises(RuntimeError, match="requires an exposed execution handle"):
        list(flow.get_runtime_stream("pricing", timeout=1))


@pytest.mark.asyncio
async def test_trigger_flow_close_rejects_pending_interrupt_by_default_and_can_cancel():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            payload={"question": "approve?"},
            interrupt_id="approval",
            resume_to="next",
        )

    flow.to(ask_feedback)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("pricing", wait_for_result=False)

    with pytest.raises(RuntimeError, match="pending interrupts are waiting"):
        await execution.async_close()

    assert execution.get_status() == "waiting"
    assert "approval" in execution.get_pending_interrupts()

    snapshot = await execution.async_close(pending_interrupts="cancel")

    assert execution.get_status() == "cancelled"
    assert execution.get_pending_interrupts() == {}
    cancelled_interrupt = execution.get_interrupt("approval")
    assert isinstance(cancelled_interrupt, dict)
    assert cancelled_interrupt["status"] == "cancelled"
    assert isinstance(snapshot, dict)


@pytest.mark.asyncio
async def test_trigger_flow_close_cancel_persists_external_wait_cancel_state():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            payload={"question": "approve?"},
            interrupt_id="approval",
            resume_to="next",
            channel_id="ops",
            provider_id="manual-approval",
        )

    flow.to(ask_feedback)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("pricing", wait_for_result=False)
    await execution.async_close(pending_interrupts="cancel")

    saved_state = execution.save()
    saved_interrupt = saved_state["interrupts"]["approval"]
    assert saved_interrupt["status"] == "cancelled"
    assert saved_interrupt["external_wait_request"]["dispatch_state"] == "cancelled"

    restored = flow.create_execution(auto_close=False)
    restored.load(saved_state)
    restored_interrupt = restored.get_interrupt("approval")
    assert isinstance(restored_interrupt, dict)
    assert restored_interrupt["status"] == "cancelled"
    assert restored_interrupt["external_wait_request"]["dispatch_state"] == "cancelled"


def test_trigger_flow_public_interrupt_event_type_matches_runtime_stream_shape():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_event="UserFeedback",
        )

    flow.to(ask_feedback)

    execution = flow.create_execution(auto_close=False)
    interrupt_event = next(execution.get_runtime_stream("pricing", timeout=1))
    validated_event = TypeAdapter(TriggerFlowInterruptEvent).validate_python(interrupt_event)

    assert validated_event["type"] == "interrupt"
    assert validated_event["action"] == "pause"
    assert validated_event["execution_id"]
    assert validated_event["interrupt"]["type"] == "human_input"
    assert validated_event["interrupt"].get("resume_event") == "UserFeedback"
    assert validated_event["interrupt"].get("source_execution_id") == validated_event["execution_id"]
    assert validated_event["interrupt"].get("source_operator_id")
    assert validated_event["interrupt"].get("source_signal") == validated_event.get("signal")
    assert "continuation_event" in validated_event["interrupt"]


@pytest.mark.asyncio
async def test_trigger_flow_when_and_state_is_isolated_per_execution():
    flow = TriggerFlow()
    flow.when({"event": ["A", "B"]}, mode="and").to(lambda data: data.value).end()

    execution_1 = flow.create_execution()
    execution_2 = flow.create_execution()

    await execution_1.async_emit("A", "execution-1-a")
    await execution_2.async_emit("B", "execution-2-b")

    with pytest.warns(UserWarning):
        assert await execution_1.async_get_result(timeout=0.01) is None
    with pytest.warns(UserWarning):
        assert await execution_2.async_get_result(timeout=0.01) is None

    await execution_1.async_emit("B", "execution-1-b")
    await execution_2.async_emit("A", "execution-2-a")

    result_1 = await execution_1.async_get_result(timeout=1)
    result_2 = await execution_2.async_get_result(timeout=1)

    assert result_1 == {
        "event": {
            "A": "execution-1-a",
            "B": "execution-1-b",
        }
    }
    assert result_2 == {
        "event": {
            "A": "execution-2-a",
            "B": "execution-2-b",
        }
    }


@pytest.mark.asyncio
async def test_trigger_flow_batch_state_is_isolated_per_execution():
    flow = TriggerFlow()

    async def left(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.01)
        return {"left": data.value}

    async def right(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.01)
        return {"right": data.value}

    flow.batch(left, right).to(lambda data: data.value).end()

    execution_1 = flow.create_execution()
    execution_2 = flow.create_execution()

    await asyncio.gather(
        execution_1.async_start("first"),
        execution_2.async_start("second"),
    )

    result_1 = await execution_1.async_get_result(timeout=1)
    result_2 = await execution_2.async_get_result(timeout=1)

    assert result_1 == {
        "left": {"left": "first"},
        "right": {"right": "first"},
    }
    assert result_2 == {
        "left": {"left": "second"},
        "right": {"right": "second"},
    }
