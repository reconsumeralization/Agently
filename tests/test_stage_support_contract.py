from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import time
from pathlib import Path
from typing import cast

import pytest
from packaging.version import Version

import agently
from agently import TriggerFlow
from agently.core import EventCenter
from agently.core.runtime._task_support import StageManagedTaskScope
from agently.types.data import RuntimeEvent
from agently.types.trigger_flow import TriggerFlowRuntimeData


ROOT = Path(__file__).resolve().parents[1]


def test_private_stage_support_module_exists() -> None:
    module = importlib.import_module("agently.core.runtime._task_support")

    assert hasattr(module, "StageManagedTaskScope")
    assert hasattr(module, "ManagedTaskOutcome")


def test_private_stage_runtime_stream_transport_exists() -> None:
    module = importlib.import_module("agently.core.orchestration.TriggerFlow._runtime_stream_transport")

    assert hasattr(module, "StageRuntimeStreamTransport")


def test_project_declares_supported_stage_dependency() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "agently-stage (>=0.3.2,<0.4.0)" in pyproject
    assert Version(importlib.metadata.version("agently-stage")) >= Version("0.3.2")
    assert not hasattr(agently, "Stage")
    assert not hasattr(agently, "Tunnel")
    assert not hasattr(agently, "LocalTaskScope")


@pytest.mark.asyncio
async def test_stage_managed_scope_preserves_loop_origin_and_retained_error() -> None:
    scope = StageManagedTaskScope()
    caller_loop = asyncio.get_running_loop()

    async def fail() -> None:
        assert asyncio.get_running_loop() is caller_loop
        raise ValueError("retained")

    task = scope.spawn(
        fail(),
        origin="triggerflow:emit:test",
        retain_outcome=True,
    )
    with pytest.raises(ValueError, match="retained"):
        await task
    await asyncio.sleep(0)

    outcomes = scope.take_retained_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].origin == "triggerflow:emit:test"
    assert isinstance(outcomes[0].error, ValueError)
    assert scope.pending_count == 0
    await scope.close(timeout=0)


@pytest.mark.asyncio
async def test_stage_managed_scope_does_not_retain_parent_consumed_outcome() -> None:
    scope = StageManagedTaskScope()

    async def fail() -> None:
        raise ValueError("parent consumes")

    task = scope.spawn(
        fail(),
        origin="triggerflow:handler:test",
        retain_outcome=False,
    )
    with pytest.raises(ValueError, match="parent consumes"):
        await task
    await asyncio.sleep(0)

    assert scope.take_retained_outcomes() == ()
    await scope.close(timeout=0)


@pytest.mark.asyncio
async def test_stage_managed_scope_does_not_report_consumed_cancellation_twice() -> None:
    scope = StageManagedTaskScope()
    task = scope.spawn(
        asyncio.sleep(10),
        origin="triggerflow:emit:cancelled",
        retain_outcome=True,
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert scope.take_retained_outcomes() == ()
    await scope.close(timeout=0)


@pytest.mark.asyncio
async def test_stage_managed_scope_skips_unused_outcome_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    task_support = importlib.import_module("agently.core.runtime._task_support")
    original_outcome = task_support.ManagedTaskOutcome
    projections = 0

    def record_projection(*args: object, **kwargs: object):
        nonlocal projections
        projections += 1
        return original_outcome(*args, **kwargs)

    monkeypatch.setattr(task_support, "ManagedTaskOutcome", record_projection)
    scope = StageManagedTaskScope()

    await scope.spawn(
        asyncio.sleep(0),
        origin="eventcenter:background:no-consumer",
    )
    await asyncio.sleep(0)

    assert projections == 0
    await scope.close(timeout=0)


@pytest.mark.asyncio
async def test_stage_managed_scope_success_notification_has_no_unused_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_support = importlib.import_module("agently.core.runtime._task_support")
    original_outcome = task_support.ManagedTaskOutcome
    projections = 0
    callback_arguments: list[tuple[object, ...]] = []

    def record_projection(*args: object, **kwargs: object):
        nonlocal projections
        projections += 1
        return original_outcome(*args, **kwargs)

    def on_done(*args: object) -> None:
        callback_arguments.append(args)

    monkeypatch.setattr(task_support, "ManagedTaskOutcome", record_projection)
    scope = StageManagedTaskScope(on_done=on_done)

    await scope.spawn(
        asyncio.sleep(0),
        origin="triggerflow:emit:successful",
        retain_outcome=True,
    )
    await asyncio.sleep(0)

    assert projections == 0
    assert callback_arguments == [()]
    assert scope.take_retained_outcomes() == ()
    await scope.close(timeout=0)


@pytest.mark.asyncio
async def test_event_center_retains_native_task_support_after_stage_rejection() -> None:
    center = EventCenter()
    completed = asyncio.Event()

    async def hook(_event: object) -> None:
        completed.set()

    center.register_hook(
        hook,
        hook_name="stage-backed",
        delivery_policy={"dispatch": "background"},
    )
    await center.async_emit({"event_type": "runtime.info", "message": "test"})
    await asyncio.wait_for(completed.wait(), timeout=1)
    await center.async_flush()

    assert not hasattr(center, "_background_task_scope")
    assert center._background_tasks == set()
    assert "agently_stage" not in type(center).__module__


def test_triggerflow_execution_uses_private_stage_stream_transport() -> None:
    module = importlib.import_module("agently.core.orchestration.TriggerFlow._runtime_stream_transport")
    execution = TriggerFlow(name="stage-stream-transport").create_execution(auto_close=False)

    assert isinstance(
        execution._runtime_stream_transport,
        module.StageRuntimeStreamTransport,
    )


def test_event_center_remains_reusable_across_sequential_loops() -> None:
    center = EventCenter(idle_flush_seconds=None)
    delivered: list[int] = []

    async def hook(event: RuntimeEvent) -> None:
        delivered.append(event.payload["index"])

    center.register_hook(
        hook,
        hook_name="cross-loop",
        delivery_policy={"dispatch": "background"},
    )

    async def emit(index: int) -> None:
        await center.async_emit(
            {
                "event_type": "runtime.info",
                "payload": {"index": index},
            }
        )
        await center.async_flush()

    asyncio.run(emit(1))
    asyncio.run(emit(2))

    assert delivered == [1, 2]


@pytest.mark.asyncio
async def test_triggerflow_stage_stream_replays_exact_items_to_concurrent_readers() -> None:
    flow = TriggerFlow(name="stage-stream-concurrent-readers")

    async def publish(data: TriggerFlowRuntimeData) -> None:
        await data.async_put("first")
        await asyncio.sleep(0)
        await data.async_put("second")

    flow.to(publish)
    execution = flow.create_execution(auto_close=True, auto_close_timeout=0)

    async def collect() -> list[str]:
        return [
            cast(str, item)
            async for item in execution.get_async_runtime_stream(
                "start",
                timeout=1,
            )
        ]

    first, second = await asyncio.gather(collect(), collect())

    assert first == ["first", "second"]
    assert second == ["first", "second"]


@pytest.mark.asyncio
async def test_triggerflow_stage_stream_keeps_public_state_stage_free() -> None:
    flow = TriggerFlow(name="stage-stream-no-public-leak")
    execution = flow.create_execution(auto_close=False)

    await execution.async_put_into_stream({"visible": "item"})
    await execution.async_stop_stream()
    items = [item async for item in execution.get_async_runtime_stream(timeout=1)]
    snapshot = execution.save()

    assert items == [{"visible": "item"}]
    serialized = json.dumps(snapshot, default=str)
    assert "agently_stage" not in serialized
    assert "LocalTaskScope" not in serialized
    assert "Tunnel" not in serialized


@pytest.mark.asyncio
async def test_triggerflow_retains_top_level_failure_until_close() -> None:
    flow = TriggerFlow(name="stage-support-completed-failure")
    execution = flow.create_execution(auto_close=False)

    async def fail_before_close() -> None:
        raise RuntimeError("managed task failed before close")

    task = execution._track_task(
        asyncio.create_task(fail_before_close()),
        origin="emit_nowait:event:fail",
    )
    with pytest.raises(RuntimeError, match="managed task failed before close"):
        await task
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="managed task failed before close"):
        await execution.async_close()


@pytest.mark.asyncio
async def test_triggerflow_close_timeout_is_one_settlement_deadline() -> None:
    flow = TriggerFlow(name="stage-support-close-deadline")
    execution = flow.create_execution(auto_close=False)
    cancellation_seen = asyncio.Event()

    async def suppress_cancellation_temporarily() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancellation_seen.set()
            await asyncio.sleep(0.12)

    execution._track_task(
        asyncio.create_task(suppress_cancellation_temporarily()),
        origin="emit_nowait:event:slow-cancel",
    )
    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match="slow-cancel"):
        await execution.async_close(timeout=0.02)
    elapsed = time.monotonic() - started_at

    assert cancellation_seen.is_set()
    assert elapsed < 0.08
