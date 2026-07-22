import asyncio

import pytest

from agently import TriggerFlow


async def _wait_for_frames(execution, count: int):
    async def wait():
        while len(execution.get_sub_flow_frames()) != count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1)
    return execution.get_sub_flow_frames()


@pytest.mark.asyncio
async def test_running_sub_flow_is_visible_before_child_completion():
    child_entered = asyncio.Event()
    release_child = asyncio.Event()

    child = TriggerFlow(name="active-child")

    async def child_work(data):
        child_entered.set()
        await release_child.wait()
        return data.value

    child.to(child_work)

    parent = TriggerFlow(name="active-parent")
    parent.to_sub_flow(child, name="run-child")

    execution = parent.create_execution(auto_close=False)
    start_task = asyncio.create_task(execution.async_start("job"))
    try:
        await asyncio.wait_for(child_entered.wait(), timeout=1)

        frames = execution.get_sub_flow_frames()
        assert len(frames) == 1
        frame = next(iter(frames.values()))
        assert frame["status"] == "running"
        assert frame["parent_execution_id"] == execution.id
        assert frame["child_flow_name"] == "active-child"
    finally:
        release_child.set()
        await asyncio.wait_for(start_task, timeout=1)
        await execution.async_close()


@pytest.mark.asyncio
async def test_cancel_active_sub_flow_fences_side_effect_write_back_and_continuation():
    child_entered = asyncio.Event()
    release_child = asyncio.Event()
    side_effects: list[str] = []
    continuations: list[str] = []

    child = TriggerFlow(name="cancel-child")

    async def child_work(data):
        child_entered.set()
        await release_child.wait()
        side_effects.append("committed")
        await data.async_set_state("result", "child-result", emit=False)

    child.to(child_work)

    parent = TriggerFlow(name="cancel-parent")

    async def continue_parent(data):
        continuations.append(data.value)

    parent.to_sub_flow(
        child,
        name="run-cancel-child",
        write_back={"value": "result.result"},
    ).to(continue_parent)

    execution = parent.create_execution(auto_close=False)
    runtime_events: list[tuple[str, dict]] = []
    original_emit_runtime_event = execution._emit_runtime_event

    async def capture_runtime_event(event_type, **kwargs):
        runtime_events.append((event_type, kwargs.get("payload", {})))
        return await original_emit_runtime_event(event_type, **kwargs)

    execution._emit_runtime_event = capture_runtime_event
    start_task = asyncio.create_task(execution.async_start("job"))
    try:
        await asyncio.wait_for(child_entered.wait(), timeout=1)
        frames = await _wait_for_frames(execution, 1)
        frame_id = next(iter(frames))
        child_runtime_events: list[str] = []
        live_child = execution._live_sub_flow_executions[frame_id]
        original_child_emit_runtime_event = live_child._emit_runtime_event

        async def capture_child_runtime_event(event_type, **kwargs):
            child_runtime_events.append(event_type)
            return await original_child_emit_runtime_event(event_type, **kwargs)

        live_child._emit_runtime_event = capture_child_runtime_event

        assert (
            await execution.async_cancel_sub_flow(frame_id, reason="superseded") is True
        )
        await asyncio.wait_for(start_task, timeout=1)
        release_child.set()
        await asyncio.sleep(0)

        frame = execution.get_sub_flow_frames()[frame_id]
        assert frame["status"] == "cancelled"
        assert frame["cancel_reason"] == "superseded"
        assert side_effects == []
        assert continuations == []
        assert execution.is_open()
        assert "chunk.failed" not in child_runtime_events
        assert (
            await execution.async_cancel_sub_flow(frame_id, reason="duplicate") is False
        )
        sub_flow_events = [
            event
            for event in runtime_events
            if event[0].startswith("triggerflow.sub_flow_")
        ]
        assert [event_type for event_type, _payload in sub_flow_events] == [
            "triggerflow.sub_flow_started",
            "triggerflow.sub_flow_cancel_requested",
            "triggerflow.sub_flow_cancelled",
        ]
        assert all(
            payload["frame_id"] == frame_id for _event_type, payload in sub_flow_events
        )
        assert all(
            payload["child_execution_id"] == frame["child_execution_id"]
            for _event_type, payload in sub_flow_events
        )
    finally:
        release_child.set()
        if not start_task.done():
            start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)
        await execution.async_close()


@pytest.mark.asyncio
async def test_emit_to_active_sub_flow_uses_child_signal_net():
    child_entered = asyncio.Event()
    stop_child = asyncio.Event()
    forwarded_values: list[str] = []

    child = TriggerFlow(name="signal-child")

    async def child_work(data):
        child_entered.set()
        await stop_child.wait()
        return "done"

    async def receive_stop(data):
        forwarded_values.append(data.value)
        stop_child.set()

    child.to(child_work)
    child.when("StopRequested").to(receive_stop)

    parent = TriggerFlow(name="signal-parent")
    parent.to_sub_flow(child, name="run-signal-child", concurrency=2)

    execution = parent.create_execution(auto_close=False)
    start_task = asyncio.create_task(execution.async_start("job"))
    try:
        await asyncio.wait_for(child_entered.wait(), timeout=1)
        frame_id = next(iter((await _wait_for_frames(execution, 1))))

        await execution.async_emit_to_sub_flow(frame_id, "StopRequested", "newer-run")
        await asyncio.wait_for(start_task, timeout=1)

        assert forwarded_values == ["newer-run"]
        assert execution.get_sub_flow_frames()[frame_id]["status"] == "completed"
        with pytest.raises(RuntimeError, match="is not active"):
            await execution.async_emit_to_sub_flow(frame_id, "StopRequested")
    finally:
        stop_child.set()
        await asyncio.gather(start_task, return_exceptions=True)
        await execution.async_close()


@pytest.mark.asyncio
async def test_cancel_claim_cancels_inflight_sub_flow_signal_without_late_dispatch():
    child_entered = asyncio.Event()
    release_child = asyncio.Event()
    signal_dispatch_entered = asyncio.Event()
    release_signal_dispatch = asyncio.Event()

    child = TriggerFlow(name="signal-cancel-race-child")

    async def child_work(_data):
        child_entered.set()
        await release_child.wait()

    child.to(child_work)

    parent = TriggerFlow(name="signal-cancel-race-parent")
    parent.to_sub_flow(child)

    execution = parent.create_execution(auto_close=False)
    start_task = asyncio.create_task(execution.async_start("job"))
    signal_task = None
    try:
        await asyncio.wait_for(child_entered.wait(), timeout=1)
        frame_id = next(iter((await _wait_for_frames(execution, 1))))
        live_child = execution._live_sub_flow_executions[frame_id]
        original_child_emit = live_child.async_emit

        async def blocked_child_emit(*args, **kwargs):
            signal_dispatch_entered.set()
            await release_signal_dispatch.wait()
            return await original_child_emit(*args, **kwargs)

        live_child.async_emit = blocked_child_emit
        signal_task = asyncio.create_task(
            execution.async_emit_to_sub_flow(frame_id, "TooLate", "value")
        )
        await asyncio.wait_for(signal_dispatch_entered.wait(), timeout=1)

        assert (
            await execution.async_cancel_sub_flow(frame_id, reason="race-winner")
            is True
        )
        await asyncio.wait_for(start_task, timeout=1)

        with pytest.raises(RuntimeError, match="was cancelled while forwarding"):
            await signal_task
        assert execution.get_sub_flow_frames()[frame_id]["status"] == "cancelled"
    finally:
        release_signal_dispatch.set()
        release_child.set()
        if signal_task is not None:
            await asyncio.gather(signal_task, return_exceptions=True)
        await asyncio.gather(start_task, return_exceptions=True)
        await execution.async_close()


@pytest.mark.asyncio
async def test_cancel_waiting_sub_flow_cancels_projected_interrupt_without_resuming_child():
    continued: list[object] = []

    child = TriggerFlow(name="waiting-child")

    async def wait_for_approval(data):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="child-approval",
            resume_to="next",
        )

    child.to(wait_for_approval).to(lambda data: data.value)

    parent = TriggerFlow(name="waiting-parent")
    parent.to_sub_flow(child).to(lambda data: continued.append(data.value))

    execution = parent.create_execution(auto_close=False)
    await execution.async_start("job")
    try:
        frames = execution.get_sub_flow_frames()
        frame_id, frame = next(iter(frames.items()))
        projected_interrupt_ids = list(frame["projected_interrupts"])

        assert frame["status"] == "waiting"
        assert len(projected_interrupt_ids) == 1
        assert (
            await execution.async_cancel_sub_flow(frame_id, reason="approval-withdrawn")
            is True
        )

        cancelled_frame = execution.get_sub_flow_frames()[frame_id]
        projected_interrupt = execution.get_interrupt(projected_interrupt_ids[0])
        assert cancelled_frame["status"] == "cancelled"
        assert execution.get_pending_interrupts() == {}
        assert projected_interrupt is not None
        assert projected_interrupt["status"] == "cancelled"
        assert continued == []
        assert execution.is_open()
    finally:
        await execution.async_close()


@pytest.mark.asyncio
async def test_concurrent_active_sub_flows_are_controlled_by_frame_id():
    entered = {"a": asyncio.Event(), "b": asyncio.Event()}
    releases = {"a": asyncio.Event(), "b": asyncio.Event()}
    completed: list[str] = []

    child = TriggerFlow(name="parallel-child")

    async def child_work(data):
        key = data.value
        entered[key].set()
        await releases[key].wait()
        completed.append(key)
        return key

    child.to(child_work)

    parent = TriggerFlow(name="parallel-parent")
    parent.when("run:a").to_sub_flow(child, name="child-a")
    parent.when("run:b").to_sub_flow(child, name="child-b")

    execution = parent.create_execution(auto_close=False)
    task_a = await execution.async_emit_nowait("run:a", "a")
    task_b = await execution.async_emit_nowait("run:b", "b")
    assert task_a is not None
    assert task_b is not None
    try:
        await asyncio.wait_for(
            asyncio.gather(entered["a"].wait(), entered["b"].wait()), timeout=1
        )
        frames = await _wait_for_frames(execution, 2)
        frame_by_value = {
            frame["parent_value"]: frame_id for frame_id, frame in frames.items()
        }

        assert (
            await execution.async_cancel_sub_flow(
                frame_by_value["a"], reason="replace-a"
            )
            is True
        )
        releases["b"].set()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=1)

        terminal = execution.get_sub_flow_frames()
        assert terminal[frame_by_value["a"]]["status"] == "cancelled"
        assert terminal[frame_by_value["b"]]["status"] == "completed"
        assert completed == ["b"]
    finally:
        releases["a"].set()
        releases["b"].set()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        await execution.async_close()


@pytest.mark.asyncio
async def test_completion_first_makes_late_cancel_a_noop():
    release_child = asyncio.Event()
    continued: list[str] = []

    child = TriggerFlow(name="complete-first-child")

    async def child_work(data):
        await release_child.wait()
        await data.async_set_state("result", "completed-first", emit=False)

    child.to(child_work)

    parent = TriggerFlow(name="complete-first-parent")
    parent.to_sub_flow(child, write_back={"value": "result.result"}).to(
        lambda data: continued.append(data.value)
    )

    execution = parent.create_execution(auto_close=False)
    start_task = asyncio.create_task(execution.async_start("job"))
    try:
        frames = await _wait_for_frames(execution, 1)
        frame_id = next(iter(frames))
        release_child.set()
        await asyncio.wait_for(start_task, timeout=1)

        assert (
            await execution.async_cancel_sub_flow(frame_id, reason="too-late") is False
        )
        assert execution.get_sub_flow_frames()[frame_id]["status"] == "completed"
        assert continued == ["completed-first"]
    finally:
        release_child.set()
        await asyncio.gather(start_task, return_exceptions=True)
        await execution.async_close()


@pytest.mark.asyncio
async def test_child_failure_records_failed_frame_and_skips_parent_continuation():
    child_entered = asyncio.Event()
    release_child = asyncio.Event()
    continued: list[object] = []

    child = TriggerFlow(name="failed-child")

    async def child_work(_data):
        child_entered.set()
        await release_child.wait()
        raise RuntimeError("child-failed")

    child.to(child_work)

    parent = TriggerFlow(name="failed-parent")
    parent.to_sub_flow(child).to(lambda data: continued.append(data.value))

    execution = parent.create_execution(auto_close=False)
    start_task = asyncio.create_task(execution.async_start("job"))
    try:
        await asyncio.wait_for(child_entered.wait(), timeout=1)
        frame_id = next(iter((await _wait_for_frames(execution, 1))))
        release_child.set()

        with pytest.raises(RuntimeError, match="child-failed"):
            await asyncio.wait_for(start_task, timeout=1)

        frame = execution.get_sub_flow_frames()[frame_id]
        assert frame["status"] == "failed"
        assert frame["error"]["type"] == "RuntimeError"
        assert continued == []
    finally:
        release_child.set()
        await asyncio.gather(start_task, return_exceptions=True)
        await execution.async_close()


@pytest.mark.asyncio
async def test_late_managed_child_failure_during_close_propagates_to_parent():
    branch_entered = asyncio.Event()
    release_branch = asyncio.Event()
    continued: list[object] = []

    child = TriggerFlow(name="late-failed-child")

    async def start_branch(data):
        await data.async_emit_nowait("late-branch")

    async def late_branch(_data):
        branch_entered.set()
        await release_branch.wait()
        raise RuntimeError("late-child-failed")

    child.to(start_branch)
    child.when("late-branch").to(late_branch)

    parent = TriggerFlow(name="late-failed-parent")
    parent.to_sub_flow(child).to(lambda data: continued.append(data.value))

    execution = parent.create_execution(auto_close=False)
    start_task = asyncio.create_task(execution.async_start("job"))
    try:
        await asyncio.wait_for(branch_entered.wait(), timeout=1)
        frame_id = next(iter((await _wait_for_frames(execution, 1))))

        async def wait_for_child_close():
            while True:
                live_child = execution._live_sub_flow_executions.get(frame_id)
                if live_child is not None and live_child._close_started:
                    return
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_child_close(), timeout=1)
        release_branch.set()

        with pytest.raises(RuntimeError, match="late-child-failed"):
            await asyncio.wait_for(start_task, timeout=1)

        assert execution.get_sub_flow_frames()[frame_id]["status"] == "failed"
        assert continued == []
    finally:
        release_branch.set()
        await asyncio.gather(start_task, return_exceptions=True)
        await execution.async_close()


@pytest.mark.asyncio
async def test_skip_exceptions_preserves_opt_in_suppression_for_late_managed_failure():
    branch_entered = asyncio.Event()
    release_branch = asyncio.Event()

    flow = TriggerFlow(name="skip-late-failure", skip_exceptions=True)

    async def start_branch(data):
        await data.async_emit_nowait("late-branch")

    async def late_branch(_data):
        branch_entered.set()
        await release_branch.wait()
        raise RuntimeError("suppressed-late-failure")

    flow.to(start_branch)
    flow.when("late-branch").to(late_branch)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("job")
    close_task = asyncio.create_task(execution.async_close())
    try:
        await asyncio.wait_for(branch_entered.wait(), timeout=1)
        release_branch.set()

        result = await asyncio.wait_for(close_task, timeout=1)

        assert isinstance(result, dict)
        assert execution.is_closed()
    finally:
        release_branch.set()
        await asyncio.gather(close_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_active_sub_flow_snapshot_is_not_restart_resumable():
    child_entered = asyncio.Event()
    release_child = asyncio.Event()

    child = TriggerFlow(name="snapshot-child")

    async def child_work(_data):
        child_entered.set()
        await release_child.wait()

    child.to(child_work)
    parent = TriggerFlow(name="snapshot-parent")
    parent.to_sub_flow(child)

    execution = parent.create_execution(auto_close=False)
    start_task = asyncio.create_task(execution.async_start("job"))
    try:
        await asyncio.wait_for(child_entered.wait(), timeout=1)
        await _wait_for_frames(execution, 1)
        snapshot = execution.save()

        restored = parent.create_execution(auto_close=False)
        with pytest.raises(RuntimeError, match="active sub-flow frame"):
            restored.load(snapshot)
    finally:
        release_child.set()
        await asyncio.gather(start_task, return_exceptions=True)
        await execution.async_close()
