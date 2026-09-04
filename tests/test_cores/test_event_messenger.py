import logging
import asyncio
import re
from typing import TYPE_CHECKING

import pytest

from agently import Agently
from agently.builtins.hookers.RuntimeConsoleSinkHooker import (
    RuntimeConsoleSinkHooker,
    _resolve_action_stage,
    _resolve_tool_stage,
    _resolve_tool_name,
    resolve_runtime_log_profile,
    should_render_console_event,
    should_render_storage_event,
)
from agently.core import EventCenter, ModelRequestResult, ObservationEventEmitter, RuntimeEventEmitter
from agently.core.runtime.RuntimeContext import bind_runtime_context
from agently.types.data import ErrorInfo, ObservationEvent, RunContext, RuntimeEvent
from agently.utils import Settings

if TYPE_CHECKING:
    from agently.types.data import ObservationEvent


_RUNTIME_LOG_KEYS = (
    "debug",
    "runtime.show_model_logs",
    "runtime.show_action_logs",
    "runtime.show_tool_logs",
    "runtime.show_trigger_flow_logs",
    "runtime.show_runtime_logs",
    "runtime.httpx_log_level",
)


def _snapshot_runtime_log_settings():
    return {key: Agently.settings.get(key, None) for key in _RUNTIME_LOG_KEYS}


def _restore_runtime_log_settings(snapshot):
    for key, value in snapshot.items():
        Agently.settings.set(key, value)
    level_name = Agently.settings.get("runtime.httpx_log_level", "WARNING")
    level = getattr(logging, str(level_name).upper(), logging.WARNING)
    logging.getLogger("httpx").setLevel(level)
    logging.getLogger("httpcore").setLevel(level)


def _build_runtime_log_settings(profile: str = "off") -> Settings:
    return Settings(
        {
            "runtime": {
                "show_model_logs": profile,
                "show_tool_logs": profile,
                "show_trigger_flow_logs": profile,
                "show_runtime_logs": profile,
            }
        }
    )


@pytest.mark.asyncio
async def test_async_runtime_emitter():
    emitter = Agently.event_center.create_emitter("Async Test")
    saved_event = None

    async def capture(event: "ObservationEvent"):
        nonlocal saved_event
        saved_event = event

    hook_name = "test_async_runtime_emitter.capture"
    Agently.event_center.register_hook(capture, event_types="runtime.info", hook_name=hook_name)
    try:
        await emitter.async_info("Hello")
        assert saved_event is not None
        assert saved_event.message == "Hello"
        assert saved_event.source == "Async Test"
        with pytest.raises(RuntimeError):
            await emitter.async_error("Something Wrong")
    finally:
        Agently.event_center.unregister_hook(hook_name)


def test_sync_runtime_emitter():
    emitter = Agently.event_center.create_emitter("Test", base_meta={"scope": "unit-test"})
    saved_event = None

    def capture(event: "ObservationEvent"):
        nonlocal saved_event
        saved_event = event

    hook_name = "test_sync_runtime_emitter.capture"
    Agently.event_center.register_hook(capture, event_types="runtime.info", hook_name=hook_name)
    try:
        emitter.info("Bye")
        assert saved_event is not None
        assert saved_event.message == "Bye"
        assert saved_event.meta["scope"] == "unit-test"
        with pytest.raises(RuntimeError):
            emitter.critical("Something Really Bad")
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_observation_event_names_are_preferred_aliases():
    assert issubclass(RuntimeEvent, ObservationEvent)
    assert issubclass(RuntimeEventEmitter, ObservationEventEmitter)
    assert hasattr(Agently, "emit_observation")
    assert hasattr(Agently, "async_emit_observation")

    ec = EventCenter()
    captured: list[ObservationEvent] = []

    async def capture(event: ObservationEvent):
        captured.append(event)

    ec.register_hook(capture, event_types="observation.alias", hook_name="capture_observation_alias")

    await ec.async_emit(ObservationEvent(event_type="observation.alias", message="alias object"))
    await ec.create_observation_emitter("ObservationTest").async_emit(
        "observation.alias",
        message="alias emitter",
    )
    await ec.async_emit(RuntimeEvent(event_type="observation.alias", message="legacy object"))

    assert all(isinstance(event, ObservationEvent) for event in captured)
    assert all(isinstance(event, RuntimeEvent) for event in captured)
    assert type(captured[0]) is RuntimeEvent
    assert type(captured[1]) is RuntimeEvent
    assert type(captured[2]) is RuntimeEvent
    assert [event.message for event in captured] == ["alias object", "alias emitter", "legacy object"]


@pytest.mark.asyncio
async def test_event_center_filtering():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def allowed_only(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(allowed_only, event_types="custom.allowed", hook_name="allowed_only")
    emitter = ec.create_emitter("TestModule", base_meta={"scope": "unit-test"})

    await emitter.async_emit("custom.allowed", message="first", payload={"row": 1})
    await emitter.async_emit("custom.blocked", message="second")

    assert len(captured) == 1
    assert captured[0].event_type == "custom.allowed"
    assert captured[0].payload == {"row": 1}
    assert captured[0].meta["scope"] == "unit-test"


@pytest.mark.asyncio
async def test_event_center_summary_delivery_policy_batches_high_frequency_events():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        capture,
        event_types="model.response.delta",
        hook_name="summary_capture",
        delivery_policy={"mode": "summary", "max_items": 3},
    )

    for value in ("A", "B", "C"):
        await ec.async_emit(
            {
                "event_type": "model.response.delta",
                "source": "model",
                "payload": {"delta": value},
                "meta": {"response_id": "response-1", "frequency": "high"},
            }
        )

    assert len(captured) == 1
    assert captured[0].event_type == "model.response.delta"
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 3
    assert captured[0].payload["count"] == 3
    assert [event["payload"]["delta"] for event in captured[0].payload["events"]] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_event_center_summary_policy_flushes_before_non_high_frequency_event():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        capture,
        hook_name="mixed_summary_capture",
        delivery_policy={"mode": "summary", "max_items": 10, "high_frequency_only": True},
    )

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    await ec.async_emit({"event_type": "model.response.completed", "payload": {"text": "A"}})

    assert len(captured) == 2
    assert captured[0].event_type == "model.response.delta"
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 1
    assert captured[1].event_type == "model.response.completed"
    assert "coalesced" not in captured[1].meta


@pytest.mark.asyncio
async def test_event_center_flush_releases_buffered_summary_events():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        capture,
        event_types="model.response.delta",
        hook_name="flush_summary_capture",
        delivery_policy={"mode": "summary", "max_items": 10},
    )

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    assert captured == []

    await ec.async_flush("flush_summary_capture")

    assert len(captured) == 1
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 1


@pytest.mark.asyncio
async def test_event_center_default_delivery_policy_remains_raw():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(capture, event_types="model.response.delta", hook_name="raw_capture")

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "B"}})

    assert [event.payload["delta"] for event in captured] == ["A", "B"]
    assert all("coalesced" not in event.meta for event in captured)


@pytest.mark.asyncio
async def test_event_center_background_hook_does_not_block_emit_path_and_flush_recovers():
    ec = EventCenter()
    slow_started = asyncio.Event()
    slow_can_finish = asyncio.Event()
    fast_captured: list[RuntimeEvent] = []

    async def slow_hook(event: RuntimeEvent):
        slow_started.set()
        await slow_can_finish.wait()

    async def fast_hook(event: RuntimeEvent):
        fast_captured.append(event)

    ec.register_hook(slow_hook, hook_name="slow", delivery_policy={"dispatch": "background"})
    ec.register_hook(fast_hook, hook_name="fast")

    started_at = asyncio.get_running_loop().time()
    await ec.async_emit({"event_type": "runtime.info", "message": "hello"})
    elapsed = asyncio.get_running_loop().time() - started_at

    assert elapsed < 0.05
    assert slow_started.is_set()
    assert [event.message for event in fast_captured] == ["hello"]

    slow_can_finish.set()
    await ec.async_flush("slow")


@pytest.mark.asyncio
async def test_event_center_emit_nowait_tracks_dispatch_until_flush():
    ec = EventCenter(idle_flush_seconds=None)
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def hook(event: RuntimeEvent):
        started.set()
        await release.wait()
        observed.append(event.event_type)

    ec.register_hook(hook, hook_name="tracked")
    dispatch = ec.emit_nowait(
        RuntimeEvent(
            event_type="runtime.info",
            source="emit-nowait-test",
        )
    )

    assert isinstance(dispatch, asyncio.Task)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert dispatch in ec._background_tasks

    release.set()
    await ec.async_flush()
    assert observed == ["runtime.info"]
    assert not ec._background_tasks


@pytest.mark.asyncio
async def test_event_center_idle_flush_recovers_background_delivery():
    ec = EventCenter(idle_flush_seconds=0.01, background_timeout=0.1)
    completed: list[str] = []

    async def hook(event: RuntimeEvent):
        await asyncio.sleep(0.01)
        completed.append(event.message or "")

    ec.register_hook(hook, hook_name="background", delivery_policy={"dispatch": "background"})

    await ec.async_emit({"event_type": "runtime.info", "message": "idle"})
    await asyncio.sleep(0.05)

    assert completed == ["idle"]


@pytest.mark.asyncio
async def test_event_center_idle_flush_cancels_stuck_background_delivery():
    ec = EventCenter(idle_flush_seconds=0.01, background_timeout=0.01)
    cancelled = asyncio.Event()

    async def hook(event: RuntimeEvent):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    ec.register_hook(hook, hook_name="stuck", delivery_policy={"dispatch": "background"})

    await ec.async_emit({"event_type": "runtime.info", "message": "stuck"})
    await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    await asyncio.sleep(0)

    assert not ec._background_tasks


@pytest.mark.asyncio
async def test_event_center_idle_flush_releases_summary_buffer():
    ec = EventCenter(idle_flush_seconds=0.01, background_timeout=0.1)
    captured: list[RuntimeEvent] = []

    async def hook(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        hook,
        hook_name="summary",
        event_types="model.response.delta",
        delivery_policy={"mode": "summary", "max_items": 10},
    )

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    assert captured == []

    await asyncio.sleep(0.05)

    assert len(captured) == 1
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 1


@pytest.mark.asyncio
async def test_event_center_emit_uses_hook_snapshot_when_hook_unregisters_during_flush():
    ec = EventCenter(idle_flush_seconds=None)
    victim_events: list[str] = []

    async def unregistering_hook(event: RuntimeEvent):
        ec.unregister_hook("victim")

    async def victim_hook(event: RuntimeEvent):
        victim_events.append(event.event_type)

    ec.register_hook(
        unregistering_hook,
        hook_name="unregistering",
        delivery_policy={"mode": "summary"},
    )
    ec.register_hook(victim_hook, hook_name="victim")

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    await ec.async_emit({"event_type": "request.completed", "message": "done"})

    assert victim_events == ["model.response.delta", "request.completed"]
    assert "victim" not in ec._hooks


@pytest.mark.asyncio
async def test_event_center_default_hook_dispatch_awaits_completion():
    ec = EventCenter()
    completed: list[str] = []

    async def slow_hook(event: RuntimeEvent):
        await asyncio.sleep(0.01)
        completed.append(event.message or "")

    ec.register_hook(slow_hook, hook_name="slow_default")

    await ec.async_emit({"event_type": "runtime.info", "message": "reliable"})

    assert completed == ["reliable"]


@pytest.mark.asyncio
async def test_event_center_infers_source_for_emitter_and_direct_emit():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def capture(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(capture, hook_name="capture")

    class SourceOwner:
        name = "InferredOwner"

        def build_emitter(self):
            return ec.create_emitter()

        async def emit_directly(self):
            await ec.async_emit({"event_type": "custom.direct", "message": "direct"})

    owner = SourceOwner()
    emitter = owner.build_emitter()
    await emitter.async_emit("custom.emitter", message="via emitter")
    await owner.emit_directly()

    assert len(captured) == 2
    assert captured[0].source == "InferredOwner"
    assert captured[1].source == "InferredOwner"


@pytest.mark.asyncio
async def test_event_center_matches_triggerflow_aliases_for_legacy_subscriptions():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def capture(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(capture, event_types="workflow.execution_started", hook_name="capture")
    emitter = ec.create_emitter("TriggerFlowTest")

    await emitter.async_emit("triggerflow.execution_started", message="started")

    assert len(captured) == 1
    assert captured[0].event_type == "triggerflow.execution_started"
    assert captured[0].message == "started"


@pytest.mark.asyncio
async def test_event_center_keeps_action_and_tool_loop_filters_exact():
    ec = EventCenter()
    action_captured: list["ObservationEvent"] = []
    tool_captured: list["ObservationEvent"] = []

    async def capture_action(event: "ObservationEvent"):
        action_captured.append(event)

    async def capture_tool(event: "ObservationEvent"):
        tool_captured.append(event)

    ec.register_hook(capture_action, event_types="action.loop_started", hook_name="capture_action")
    ec.register_hook(capture_tool, event_types="tool.loop_started", hook_name="capture_tool")
    emitter = ec.create_emitter("ActionFlowTest")

    await emitter.async_emit("action.loop_started", message="started")
    await emitter.async_emit("tool.loop_started", message="legacy started")

    assert [event.event_type for event in action_captured] == ["action.loop_started"]
    assert [event.event_type for event in tool_captured] == ["tool.loop_started"]


@pytest.mark.asyncio
async def test_event_center_normalizes_cancelled_error():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def capture(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(capture, event_types="runtime.error", hook_name="capture_cancelled_error")

    await ec.async_emit(
        {
            "event_type": "runtime.error",
            "error": asyncio.CancelledError(),
        }
    )

    assert len(captured) == 1
    assert captured[0].error is not None
    assert captured[0].error.type == "CancelledError"
    assert captured[0].error.module == "asyncio.exceptions"


def test_runtime_log_profiles_keep_default_off_quiet():
    settings = _build_runtime_log_settings("off")

    assert not should_render_console_event(RuntimeEvent(event_type="model.requesting"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="tool.loop_started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="triggerflow.execution_started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="request.completed"), settings)
    assert not should_render_console_event(
        RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings
    )

    assert not should_render_storage_event(RuntimeEvent(event_type="model.requesting", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert should_render_storage_event(
        RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings
    )
    assert should_render_storage_event(RuntimeEvent(event_type="model.requester.error", level="ERROR"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="request.failed", level="WARNING"), settings)


def test_runtime_log_profiles_simple_mode_uses_summary_whitelists():
    settings = _build_runtime_log_settings("simple")

    assert should_render_console_event(RuntimeEvent(event_type="model.requesting", message="requesting"), settings)
    assert should_render_console_event(
        RuntimeEvent(event_type="model.completed", payload={"raw_text": "done"}), settings
    )
    assert should_render_console_event(RuntimeEvent(event_type="model.streaming", message="delta"), settings)
    assert should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream.delta",
            payload={"source": "model_request", "path": "model.delta", "delta": "A"},
        ),
        settings,
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream.delta",
            payload={"source": "model_request", "path": "step_result", "delta": "A"},
            run=RunContext(
                run_id="child-execution",
                run_kind="agent_execution",
                root_run_id="root-execution",
                parent_run_id="parent-execution",
            ),
        ),
        settings,
    )
    assert should_render_console_event(RuntimeEvent(event_type="prompt.built", message="prompt"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="model.request_failed", level="ERROR"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="model.validation_failed", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.loop_started", message="started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="action.plan_ready", message="ready"), settings)
    assert should_render_console_event(
        RuntimeEvent(event_type="action.started", payload={"action_type": "tool", "action_name": "get_weather"}),
        settings,
    )
    assert should_render_console_event(RuntimeEvent(event_type="action.completed", level="INFO"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.approval_required", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.blocked", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.failed", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="tool.loop_started", message="started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="tool.plan_ready", message="ready"), settings)
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="tool.loop_started",
            message="compat started",
            meta={"compat_event_alias": True, "compat_alias_for": "action.loop_started"},
        ),
        settings,
    )
    assert should_render_console_event(
        RuntimeEvent(event_type="triggerflow.execution_started", message="execution started"),
        settings,
    )
    assert should_render_console_event(RuntimeEvent(event_type="triggerflow.execution_failed", level="ERROR"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="triggerflow.signal", message="signal"), settings)
    assert should_render_console_event(
        RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings
    )
    assert should_render_console_event(RuntimeEvent(event_type="agent_execution.started", message="started"), settings)
    assert should_render_console_event(
        RuntimeEvent(event_type="execution_resource.ensuring", message="checking"), settings
    )
    assert should_render_console_event(
        RuntimeEvent(event_type="execution_resource.progress", message="pulling image"), settings
    )
    assert should_render_console_event(
        RuntimeEvent(event_type="execution_resource.ready", message="ready"), settings
    )
    assert should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={"stream_kind": "phase", "path": "agent_task.phase.planned", "value": {"phase": "planned"}},
        ),
        settings,
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={"stream_kind": "model_delta", "path": "agent_task.model.delta", "value": "A"},
        ),
        settings,
    )
    assert should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream.delta",
            payload={"stream_kind": "progress_delta", "path": "agent_task.progress", "delta": "A"},
        ),
        settings,
    )
    assert not should_render_console_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="request.failed", level="ERROR"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.requesting", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.failed", level="ERROR"), settings)
    assert not should_render_storage_event(
        RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings
    )
    assert not should_render_storage_event(
        RuntimeEvent(
            event_type="tool.loop_failed",
            level="ERROR",
            meta={"compat_event_alias": True, "compat_alias_for": "action.loop_failed"},
        ),
        settings,
    )


def test_runtime_log_profiles_detail_mode_selects_diagnostics_without_dumping_transport_mirrors():
    settings = _build_runtime_log_settings("detail")

    assert should_render_console_event(RuntimeEvent(event_type="model.streaming", message="delta"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="tool.plan_ready", message="ready"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="triggerflow.signal", message="signal"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="model.reasoning.delta", level="DEBUG"), settings)
    assert not should_render_console_event(
        RuntimeEvent(event_type="model.status", payload={"status": "completed"}), settings
    )
    assert should_render_console_event(
        RuntimeEvent(event_type="model.status", level="ERROR", payload={"status": "failed"}), settings
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={"stream_kind": "runtime_progress", "path": "runtime.progress.model.delta.progress"},
        ),
        settings,
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={
                "stream_kind": "child_execution",
                "path": "agent_task.iteration.1.execution.runtime.progress.route_selection.started",
            },
        ),
        settings,
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={
                "stream_kind": "child_execution",
                "path": "agent_task.iteration.1.execution.acceptance_points[0].criterion",
            },
        ),
        settings,
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={
                "path": "result",
                "source": "agent_execution",
                "route": "model_request",
                "value": "already rendered by model.completed",
            },
        ),
        settings,
    )
    assert should_render_console_event(RuntimeEvent(event_type="session.applied_to_request", level="INFO"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.completed", level="INFO"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.completed", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)


def test_execution_resource_console_simple_uses_readable_compact_image_progress(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )

    events = [
        RuntimeEvent(
            event_type="execution_resource.ensuring",
            payload={"kind": "code_execution", "phase": "provider_probe"},
        ),
        RuntimeEvent(
            event_type="execution_resource.progress",
            payload={
                "provider_id": "docker",
                "phase": "image_pull_started",
                "image": "node:22-slim",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.progress",
            message="75782e20ea1f: Pulling fs layer",
            payload={
                "provider_id": "docker",
                "phase": "image_pull_progress",
                "image": "node:22-slim",
                "line": "75782e20ea1f: Pulling fs layer",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.progress",
            message="Status: Downloaded newer image for node:22-slim",
            payload={
                "provider_id": "docker",
                "phase": "image_pull_progress",
                "image": "node:22-slim",
                "line": "Status: Downloaded newer image for node:22-slim",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.progress",
            message="docker.io/library/node:22-slim",
            payload={
                "provider_id": "docker",
                "phase": "image_pull_progress",
                "image": "node:22-slim",
                "line": "docker.io/library/node:22-slim",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.progress",
            payload={
                "provider_id": "docker",
                "phase": "image_pull_completed",
                "image": "node:22-slim",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.probed",
            payload={
                "kind": "code_execution",
                "provider_id": "docker",
                "phase": "provider_selected",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.progress",
            payload={
                "provider_id": "docker",
                "phase": "image_inspection",
                "image": "node:22-slim",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.progress",
            payload={
                "provider_id": "docker",
                "phase": "image_ready",
                "image": "node:22-slim",
            },
        ),
        RuntimeEvent(
            event_type="execution_resource.ready",
            payload={
                "kind": "code_execution",
                "provider_id": "docker",
                "phase": "ready",
                "image_preparation": {"image": "node:22-slim"},
            },
        ),
    ]
    for event in events:
        RuntimeConsoleSinkHooker._handle_execution_resource_event(event, "simple")  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert "[Environment] [Code execution]" in rendered
    assert "Looking for an available environment for code execution." in rendered
    assert "Stage: Downloading" in rendered
    assert "This may take a few minutes on the first run." in rendered
    assert "[Environment] [Docker image node:22-slim] Downloading layer 75782e20ea1f." in rendered
    assert "Stage: Downloaded" in rendered
    assert "Docker passed the environment checks." in rendered
    assert "Code execution environment is ready with Docker using node:22-slim." in rendered
    assert "Checking whether Docker image" not in rendered
    assert "Stage: Image Ready" not in rendered
    assert "provider=" not in rendered
    assert "phase=" not in rendered
    assert "Status: Downloaded newer image" not in rendered
    assert "docker.io/library/node:22-slim" not in rendered


def test_execution_resource_console_detail_leads_with_explanation_then_diagnostics(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._handle_execution_resource_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="execution_resource.failed",
            level="ERROR",
            payload={
                "kind": "code_execution",
                "provider_id": "docker",
                "phase": "provider_probe",
                "reason": "docker_daemon_unavailable",
                "suggestion": "Start Docker and retry.",
                "error_code": "execution_resource.provider_unavailable",
            },
        ),
        "detail",
    )

    rendered = "".join(printed)
    assert rendered.index("Could not prepare the code execution environment.") < rendered.index("Diagnostics:")
    assert "Reason: docker daemon unavailable" in rendered
    assert "Next step: Start Docker and retry." in rendered
    assert '"phase": "provider_probe"' in rendered
    assert '"error_code": "execution_resource.provider_unavailable"' in rendered


def test_action_logs_prefer_action_setting_and_fall_back_to_tool_setting():
    legacy_settings = Settings({"runtime": {"show_tool_logs": "simple"}})
    assert resolve_runtime_log_profile(legacy_settings, "action.started") == "simple"
    assert should_render_console_event(RuntimeEvent(event_type="action.started"), legacy_settings)

    preferred_settings = Settings({"runtime": {"show_action_logs": "off", "show_tool_logs": "detail"}})
    assert resolve_runtime_log_profile(preferred_settings, "action.started") == "off"
    assert not should_render_console_event(RuntimeEvent(event_type="action.started"), preferred_settings)

    parent_settings = Settings({"runtime": {"show_action_logs": "detail"}})
    child_settings = Settings({"runtime": {"show_tool_logs": "off"}}, parent=parent_settings)
    assert resolve_runtime_log_profile(child_settings, "action.started") == "off"


def test_tool_console_stage_uses_event_type_before_success_payload():
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.loop_started")) == "Started"
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.loop_completed")) == "Completed"
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.loop_failed", level="ERROR")) == "Failed"
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.plan_ready")) == "Plan Ready"
    assert _resolve_tool_stage(RuntimeEvent(event_type="custom.completed", payload={"success": True})) == "Completed"
    assert _resolve_tool_stage(RuntimeEvent(event_type="custom.failed", payload={"success": False})) == "Failed"


def test_action_console_stage_uses_action_loop_event_types():
    assert _resolve_action_stage(RuntimeEvent(event_type="action.loop_started")) == "Started"
    assert _resolve_action_stage(RuntimeEvent(event_type="action.plan_ready")) == "Plan Ready"
    assert _resolve_action_stage(RuntimeEvent(event_type="action.loop_completed")) == "Completed"
    assert _resolve_action_stage(RuntimeEvent(event_type="action.loop_failed", level="ERROR")) == "Failed"


def test_tool_console_name_uses_action_payload_and_record():
    assert _resolve_tool_name(RuntimeEvent(event_type="tool.loop_started")) is None
    assert (
        _resolve_tool_name(RuntimeEvent(event_type="action.started", payload={"action_name": "get_weather"}))
        == "get_weather"
    )
    assert (
        _resolve_tool_name(
            RuntimeEvent(event_type="action.completed", payload={"record": {"tool_name": "search_docs"}})
        )
        == "search_docs"
    )


def test_tool_console_rendering_does_not_mark_loop_start_as_failed(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_tool_event(  # type: ignore[attr-defined]
        RuntimeEvent(event_type="tool.loop_started", message="Tool loop started."),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_tool_event(  # type: ignore[attr-defined]
        RuntimeEvent(event_type="tool.loop_completed", message="Tool loop completed."),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "Started" in rendered
    assert "Completed" in rendered
    assert "ToolLoop" in rendered
    assert "Tool-unknown" not in rendered
    assert "Failed" not in rendered


def test_action_console_rendering_shows_action_name_and_type(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_action_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="action.completed",
            message="Action 'get_weather' completed.",
            payload={"action_type": "tool", "action_name": "get_weather"},
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "Action-get_weather" in rendered
    assert "type=tool" in rendered
    assert "Completed" in rendered
    assert "Action-unknown" not in rendered


def test_action_console_simple_shows_purpose_arguments_and_result_preview(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_action_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="action.started",
            payload={
                "action_type": "tool",
                "action_name": "lookup_ticket",
                "command": {
                    "purpose": "Load the incident record.",
                    "arguments": {"ticket_id": "INC-42"},
                },
            },
        ),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_action_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="action.completed",
            payload={
                "action_type": "tool",
                "action_name": "lookup_ticket",
                "record": {
                    "data": {
                        "status": "resolved",
                        "meta": {"provider_capabilities": {"internal": True}},
                    }
                },
            },
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "Purpose: Load the incident record." in rendered
    assert 'Arguments: {"ticket_id": "INC-42"}' in rendered
    assert 'Result: {"status": "resolved"}' in rendered
    assert "provider_capabilities" not in rendered


def test_action_console_rendering_shows_loop_without_unknown_action(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_action_event(  # type: ignore[attr-defined]
        RuntimeEvent(event_type="action.loop_started", message="Action loop started."),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "ActionLoop" in rendered
    assert "Started" in rendered
    assert "Action-unknown" not in rendered


def test_model_console_simple_renders_request_and_result(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.requesting",
            source="probe",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-1",
                "provider_family": "OpenAICompatible",
                "request": {
                    "request_options": {"model": "qwen", "stream": False},
                    "request_url": "https://example.test/v1/chat/completions",
                    "stream": False,
                },
            },
        ),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.completed",
            source="probe",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-1",
                "raw_text": "Revenue risk is moderate.",
                "result": {"summary": "fallback should not win when raw_text exists"},
            },
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "Requesting" in rendered
    assert "provider=OpenAICompatible" in rendered
    assert "model=qwen" in rendered
    assert "request_options" not in rendered
    assert "Done" in rendered
    assert "Revenue risk is moderate." in rendered
    assert "fallback should not win" not in rendered


def test_prompt_console_uses_readable_prompt_text_in_simple_and_detail(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )

    event = RuntimeEvent(
        event_type="prompt.built",
        source="ModelRequest",
        payload={
            "agent_name": "debug-agent",
            "response_id": "resp-prompt",
            "prompt_text": "[INPUT]\nSummarize the incident.\n[INFO]\nAudience: support team",
            "prompt": {"input": "fallback should not render"},
        },
    )
    RuntimeConsoleSinkHooker._handle_generic_event(event, "simple")  # type: ignore[attr-defined]
    RuntimeConsoleSinkHooker._handle_generic_event(event, "detail")  # type: ignore[attr-defined]
    RuntimeConsoleSinkHooker._flush_deferred_console_blocks(force=True)  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert rendered.count("Stage: Prompt") == 2
    assert rendered.count("Summarize the incident.") == 2
    assert "fallback should not render" not in rendered


def test_model_console_simple_keeps_final_delta_before_done_and_suppresses_delayed_projection(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.requesting",
            source="ModelRequest",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-simple",
                "request": {"request_options": {"model": "qwen"}, "stream": True},
            },
        ),
        "simple",
    )
    for delta in ("A", "B"):
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.streaming",
                source="ModelRequestResult",
                payload={
                    "agent_name": "debug-agent",
                    "response_id": "resp-simple",
                    "delta": delta,
                },
            ),
            "simple",
        )
        RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="agent_execution.stream.delta",
                source="BaseAgent",
                payload={
                    "execution_id": "exec-simple",
                    "source": "model_request",
                    "path": "model.delta",
                    "delta": delta,
                },
            ),
            "simple",
            model_profile="simple",
        )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.streaming",
            source="ModelRequestResult",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-simple",
                "delta": "😊",
            },
        ),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.completed",
            source="ModelRequestResult",
            payload={"agent_name": "debug-agent", "response_id": "resp-simple", "raw_text": "AB"},
        ),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="agent_execution.stream.delta",
            source="BaseAgent",
            payload={
                "execution_id": "exec-simple",
                "source": "model_request",
                "path": "model.delta",
                "delta": "😊",
            },
        ),
        "simple",
        model_profile="simple",
    )

    rendered = "".join(printed)
    assert rendered.count("Stage: Streaming") == 1
    assert rendered.count("AB😊") == 1
    assert "Model response completed." in rendered
    assert rendered.index("AB😊") < rendered.index("Stage: Done")
    assert "Stage: Streaming" not in rendered[rendered.index("Stage: Done") :]


def test_model_console_detail_keeps_structured_result_priority(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.completed",
            source="probe",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-1",
                "raw_text": "raw final text",
                "result": {"summary": "structured final result"},
            },
        ),
        "detail",
    )

    rendered = "\n".join(printed)
    assert "structured final result" in rendered
    assert "raw final text" not in rendered


def test_model_console_simple_renders_validation_reason_and_retry_transition(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.validation_failed",
            source="ModelRequestResult",
            message="Output validation failed in require_ready.",
            level="WARNING",
            payload={
                "agent_name": "publisher",
                "response_id": "resp-1",
                "validator_name": "require_ready",
                "reason": "status must be ready before publication.",
                "attempt_index": 1,
                "max_retries": 1,
                "stop": False,
                "no_retry": False,
                "validation_payload": {"expected_status": "ready", "actual_status": "draft"},
                "response_text": '{"status":"draft"}',
            },
        ),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.retrying",
            source="ModelRequestResult",
            message="Output validation failed. Preparing retry.",
            level="WARNING",
            payload={
                "agent_name": "publisher",
                "response_id": "resp-1",
                "retry_reason": "validate",
                "attempt_index": 1,
                "next_attempt_index": 2,
                "validation_reason": "status must be ready before publication.",
                "response_text": '{"status":"draft"}',
            },
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "require_ready: status must be ready before publication. (attempt 1/2)" in rendered
    assert "Validation retry -> attempt 2" in rendered
    assert "expected_status" not in rendered
    assert rendered.count("status must be ready before publication.") == 1
    assert '{"status":"draft"}' not in rendered


def test_model_console_simple_renders_validation_error_without_traceback(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.validation_error",
            source="ModelRequestResult",
            message="Output validation failed in flaky_validator.",
            level="ERROR",
            payload={
                "agent_name": "publisher",
                "response_id": "resp-1",
                "validator_name": "flaky_validator",
                "reason": "validator boom",
                "attempt_index": 1,
                "max_retries": 1,
                "error_kind": "RuntimeError",
            },
            error=ErrorInfo(
                type="RuntimeError",
                message="validator boom",
                traceback="traceback must stay hidden in simple mode",
            ),
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "flaky_validator raised RuntimeError: validator boom (attempt 1/2)" in rendered
    assert "traceback must stay hidden in simple mode" not in rendered


def test_model_console_detail_validation_adds_only_new_facts(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.completed",
            source="ModelRequestResult",
            payload={
                "agent_name": "publisher",
                "response_id": "resp-1",
                "result": {"status": "draft", "marker": "MODEL_RESPONSE_SENTINEL"},
            },
        ),
        "detail",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.validation_failed",
            source="ModelRequestResult",
            message="Output validation failed in require_ready.",
            level="WARNING",
            payload={
                "agent_name": "publisher",
                "response_id": "resp-1",
                "validator_name": "require_ready",
                "reason": "status must be ready before publication.",
                "attempt_index": 1,
                "max_retries": 1,
                "stop": False,
                "no_retry": False,
                "validation_payload": {"expected_status": "ready", "actual_status": "draft"},
                "response_text": '{"status":"draft","marker":"MODEL_RESPONSE_SENTINEL"}',
            },
        ),
        "detail",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.retrying",
            source="ModelRequestResult",
            message="Output validation failed. Preparing retry.",
            level="WARNING",
            payload={
                "agent_name": "publisher",
                "response_id": "resp-1",
                "retry_reason": "validate",
                "attempt_index": 1,
                "next_attempt_index": 2,
                "validation_reason": "status must be ready before publication.",
                "validation_payload": {"expected_status": "ready", "actual_status": "draft"},
                "response_text": '{"status":"draft","marker":"MODEL_RESPONSE_SENTINEL"}',
            },
        ),
        "detail",
    )

    rendered = "\n".join(printed)
    assert rendered.count("MODEL_RESPONSE_SENTINEL") == 1
    assert rendered.count("status must be ready before publication.") == 1
    assert rendered.count("require_ready") == 1
    assert '[Context]: {"expected_status": "ready", "actual_status": "draft"}' in rendered
    assert "Validation retry: attempt 1 -> 2" in rendered
    assert "retryable=true" not in rendered
    assert "stop=false" not in rendered
    assert "no_retry=false" not in rendered


@pytest.mark.parametrize(
    ("stop", "no_retry", "expected"),
    [
        (True, False, "[Decision]: no retry (stop=true)"),
        (False, True, "[Decision]: no retry (no_retry=true)"),
        (True, True, "[Decision]: no retry (stop=true, no_retry=true)"),
    ],
)
def test_model_console_detail_validation_renders_only_non_default_decision(
    monkeypatch,
    stop: bool,
    no_retry: bool,
    expected: str,
):
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(arg) for arg in args)),
    )

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.validation_failed",
            source="ModelRequestResult",
            message="Output validation failed in require_ready.",
            level="WARNING",
            payload={
                "validator_name": "require_ready",
                "reason": "publication is blocked by policy.",
                "attempt_index": 1,
                "max_retries": 3,
                "stop": stop,
                "no_retry": no_retry,
            },
        ),
        "detail",
    )

    rendered = "\n".join(printed)
    assert expected in rendered


def test_model_console_detail_validation_bounds_context_and_traceback(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.validation_failed",
            source="ModelRequestResult",
            message="Output validation failed in require_ready.",
            level="WARNING",
            payload={
                "validator_name": "require_ready",
                "reason": "invalid output",
                "validation_payload": {"body": ("x" * 1000) + "CONTEXT_TAIL_SENTINEL"},
            },
        ),
        "detail",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.validation_error",
            source="ModelRequestResult",
            message="Output validation failed in flaky_validator.",
            level="ERROR",
            payload={
                "validator_name": "flaky_validator",
                "reason": "validator boom",
                "error_kind": "RuntimeError",
            },
            error=ErrorInfo(
                type="RuntimeError",
                message="validator boom",
                traceback="\n".join(
                    [
                        *(f"trace-line-{index:02d}" for index in range(1, 15)),
                        "trace-line-15" + ("z" * 2500),
                        "trace-line-16",
                    ]
                ),
            ),
        ),
        "detail",
    )

    rendered = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(printed))
    context_line = next(line for line in rendered.splitlines() if line.startswith("[Context]: "))
    context_value = context_line.removeprefix("[Context]: ")
    assert len(context_value) <= 500
    assert context_value.endswith("... [truncated]")
    assert "CONTEXT_TAIL_SENTINEL" not in context_value

    traceback_value = rendered.split("[Traceback]:\n", 1)[1]
    assert len(traceback_value) <= 2000
    assert traceback_value.startswith("[truncated] ...\n")
    assert "trace-line-01" not in traceback_value
    assert "trace-line-16" in traceback_value


def test_model_console_validation_fallback_tolerates_malformed_optional_payload(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(" ".join(str(arg) for arg in args)),
    )

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.validation_failed",
            source="ModelRequestResult",
            message="Fallback validation message.",
            level="WARNING",
            payload={
                "attempt_index": True,
                "max_retries": False,
                "validation_payload": "not-a-mapping",
            },
        ),
        "detail",
    )

    rendered = "\n".join(printed)
    assert "unknown validator: Fallback validation message." in rendered
    assert "attempt" not in rendered
    assert "[Context]" not in rendered


def test_agent_execution_console_simple_renders_process_summary(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="agent_execution.stream",
            source="BaseAgent",
            payload={
                "execution_id": "exec-1",
                "stream_kind": "phase",
                "path": "agent_task.phase.planned",
                "value": {"phase": "planned", "detail": "Plan accepted."},
            },
            meta={"execution_id": "exec-1"},
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "AgentExecution" in rendered
    assert "Execution-exec-1" in rendered
    assert "Process" in rendered
    assert "kind=phase" in rendered
    assert "agent_task.phase.planned" in rendered
    assert "Plan accepted." in rendered


def test_model_console_detail_keeps_stream_open_across_agent_execution_projection(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]

    for delta in ("A", "B"):
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.streaming",
                source="ModelRequestResult",
                payload={
                    "agent_name": "debug-agent",
                    "response_id": "resp-1",
                    "delta": delta,
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="agent_execution.stream.delta",
                source="BaseAgent",
                payload={
                    "execution_id": "exec-1",
                    "source": "model_request",
                    "path": "model.delta",
                    "delta": delta,
                    "stream_event_type": "delta",
                },
            ),
            "detail",
        )

    RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="agent_execution.stream",
            source="BaseAgent",
            payload={
                "execution_id": "exec-1",
                "stream_kind": "phase",
                "path": "agent_task.phase.planned",
                "value": {"phase": "planned"},
            },
        ),
        "detail",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.completed",
            source="ModelRequestResult",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-1",
                "result": "AB",
            },
        ),
        "detail",
    )

    rendered = "".join(printed)
    assert rendered.count("Stage: Streaming") == 1
    assert "Detail:\nAB" in rendered
    assert rendered.index("Stage: Done") < rendered.index("[Deferred diagnostics]")
    assert rendered.index("[Deferred diagnostics]") < rendered.index("[AgentExecution]")
    assert "Streaming continues" not in rendered
    assert "agent_execution.stream.delta" not in rendered
    assert "agent_task.phase.planned" in rendered


def test_model_console_detail_defers_request_and_process_diagnostics_until_stream_finishes(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="agent_execution.stream",
                source="BaseAgent",
                payload={
                    "execution_id": "exec-focus",
                    "path": "route.selected",
                    "source": "agent_execution",
                    "route": "model_request",
                    "value": {"sentinel": "PROCESS_SENTINEL"},
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.request_started",
                source="ModelRequest",
                message="REQUEST_STARTED_SENTINEL",
                payload={"agent_name": "focus-agent", "response_id": "resp-focus"},
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_generic_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="prompt.built",
                source="ModelRequest",
                payload={
                    "agent_name": "focus-agent",
                    "response_id": "resp-focus",
                    "prompt_text": "PROMPT_SENTINEL",
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.requesting",
                source="ModelRequest",
                payload={
                    "agent_name": "focus-agent",
                    "response_id": "resp-focus",
                    "request": {
                        "model": "REQUEST_SENTINEL",
                        "stream": True,
                        "messages": [],
                    },
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.streaming",
                source="ModelRequestResult",
                payload={
                    "agent_name": "focus-agent",
                    "response_id": "resp-focus",
                    "delta": "A1",
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="agent_execution.completed",
                source="BaseAgent",
                message="EXECUTION_COMPLETED_SENTINEL",
                payload={"execution_id": "exec-other"},
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.streaming",
                source="ModelRequestResult",
                payload={
                    "agent_name": "focus-agent",
                    "response_id": "resp-focus",
                    "delta": "A2",
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="ModelRequestResult",
                payload={
                    "agent_name": "focus-agent",
                    "response_id": "resp-focus",
                    "result": "A-final",
                },
            ),
            "detail",
        )
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    done_index = rendered.index("Stage: Done")
    assert "Detail:\nA1A2" in rendered
    assert "Streaming continues" not in rendered
    assert rendered.count("[Deferred diagnostics]") == 1
    for sentinel in (
        "PROCESS_SENTINEL",
        "REQUEST_STARTED_SENTINEL",
        "PROMPT_SENTINEL",
        "REQUEST_SENTINEL",
        "EXECUTION_COMPLETED_SENTINEL",
    ):
        assert sentinel not in rendered[:done_index]
        assert sentinel in rendered[done_index:]


def test_model_console_deferred_diagnostics_are_bounded(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(
            "".join(str(arg) for arg in args) + kwargs.get("end", "\n")
        ),
    )
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker._CONSOLE_DEFERRED_MAX_ENTRIES",
        1,
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        RuntimeConsoleSinkHooker._defer_console_block("[First]", "Process", "kept")  # type: ignore[attr-defined]
        RuntimeConsoleSinkHooker._defer_console_block("[Second]", "Process", "omitted")  # type: ignore[attr-defined]
        RuntimeConsoleSinkHooker._flush_deferred_console_blocks(force=True)  # type: ignore[attr-defined]
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert "[First] [Deferred]" in rendered
    assert "kept" in rendered
    assert "[Second]" not in rendered
    assert "1 additional diagnostic event(s)" in rendered
    assert "were omitted by ConsoleSink limits" in rendered


def test_model_console_failure_before_first_delta_releases_deferred_diagnostics(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(
            "".join(str(arg) for arg in args) + kwargs.get("end", "\n")
        ),
    )
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        RuntimeConsoleSinkHooker._handle_generic_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="prompt.built",
                source="ModelRequest",
                payload={
                    "agent_name": "failed-agent",
                    "response_id": "resp-failed",
                    "prompt_text": "FAILED_PROMPT_SENTINEL",
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.requesting",
                source="ModelRequest",
                payload={
                    "agent_name": "failed-agent",
                    "response_id": "resp-failed",
                    "request": {"model": "failed-model", "stream": True},
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.request_failed",
                source="ModelRequest",
                level="ERROR",
                message="FAILED_BEFORE_DELTA",
                payload={"agent_name": "failed-agent", "response_id": "resp-failed"},
            ),
            "detail",
        )
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert rendered.index("FAILED_BEFORE_DELTA") < rendered.index("[Deferred diagnostics]")
    assert rendered.index("[Deferred diagnostics]") < rendered.index("FAILED_PROMPT_SENTINEL")


def test_model_console_concurrent_completed_background_uses_fifo_final_result(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        for response_id, delta in (("resp-a", "A1"), ("resp-b", "B-buffered"), ("resp-a", "A2")):
            RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
                RuntimeEvent(
                    event_type="model.streaming",
                    source="AgentlyResponseParser",
                    payload={
                        "agent_name": "concurrent-agent",
                        "response_id": response_id,
                        "delta": delta,
                    },
                ),
                "detail",
            )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-b",
                    "result": {"message": "B-final"},
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-a",
                    "result": {"message": "A-final"},
                },
            ),
            "detail",
        )
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert rendered.count("Stage: Streaming") == 1
    assert rendered.count("Another model response is running in the background") == 1
    assert "Streaming continues" not in rendered
    assert "B-buffered" not in rendered
    assert rendered.index("A1") < rendered.index("A2") < rendered.index("A-final")
    assert rendered.index("A-final") < rendered.index("B-final")


def test_model_console_concurrent_running_background_loads_buffer_then_streams(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        for response_id, delta in (("resp-a", "A"), ("resp-b", "B1")):
            RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
                RuntimeEvent(
                    event_type="model.streaming",
                    source="AgentlyResponseParser",
                    payload={
                        "agent_name": "concurrent-agent",
                        "response_id": response_id,
                        "delta": delta,
                    },
                ),
                "detail",
            )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-a",
                    "result": {"message": "A-final"},
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.streaming",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-b",
                    "delta": "B2",
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-b",
                    "result": {"message": "B-final"},
                },
            ),
            "detail",
        )
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert rendered.count("Stage: Streaming") == 2
    assert rendered.count("Another model response is running in the background") == 1
    assert "Streaming continues" not in rendered
    assert "Detail:\nB1B2" in rendered
    assert rendered.index("A-final") < rendered.index("B1") < rendered.index("B2") < rendered.index("B-final")


def test_model_console_background_buffer_overflow_uses_complete_terminal_result(monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(
            "".join(str(arg) for arg in args) + kwargs.get("end", "\n")
        ),
    )
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker._CONSOLE_STREAM_BUFFER_MAX_CHARS",
        3,
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        for response_id, delta in (("resp-a", "A"), ("resp-b", "12345")):
            RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
                RuntimeEvent(
                    event_type="model.streaming",
                    source="AgentlyResponseParser",
                    payload={
                        "agent_name": "concurrent-agent",
                        "response_id": response_id,
                        "delta": delta,
                    },
                ),
                "simple",
            )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-a",
                    "result": "A-final",
                },
            ),
            "simple",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.streaming",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-b",
                    "delta": "678",
                },
            ),
            "simple",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-b",
                    "result": "12345678\nFULL-TERMINAL-TAIL",
                },
            ),
            "simple",
        )
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert "Full output pending" in rendered
    assert "buffered characters omitted by ConsoleSink" not in rendered
    assert "12345678\nFULL-TERMINAL-TAIL" in rendered
    assert rendered.count("FULL-TERMINAL-TAIL") == 1


def test_model_console_simple_result_without_rendered_stream_is_not_preview_truncated(monkeypatch):
    printed: list[str] = []
    complete_result = "RESULT-HEAD\n" + ("x" * 5000) + "\nRESULT-TAIL"
    monkeypatch.setattr(
        "builtins.print",
        lambda *args, **kwargs: printed.append(
            "".join(str(arg) for arg in args) + kwargs.get("end", "\n")
        ),
    )
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.requesting",
                source="ModelRequest",
                payload={
                    "agent_name": "nonstream-agent",
                    "response_id": "resp-nonstream",
                    "request": {"model": "test-model", "stream": True},
                },
            ),
            "simple",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "nonstream-agent",
                    "response_id": "resp-nonstream",
                    "result": complete_result,
                },
            ),
            "simple",
        )
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert complete_result in rendered
    assert "RESULT-TAIL" in rendered
    assert "..." not in rendered


def test_model_console_background_failure_is_immediate_without_releasing_foreground(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    try:
        for response_id, delta in (("resp-a", "A1"), ("resp-b", "B-buffered")):
            RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
                RuntimeEvent(
                    event_type="model.streaming",
                    source="AgentlyResponseParser",
                    payload={
                        "agent_name": "concurrent-agent",
                        "response_id": response_id,
                        "delta": delta,
                    },
                ),
                "detail",
            )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.failed",
                source="ModelRequest",
                level="ERROR",
                message="B failed immediately",
                payload={"agent_name": "concurrent-agent", "response_id": "resp-b"},
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.streaming",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-a",
                    "delta": "A2",
                },
            ),
            "detail",
        )
        RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="model.completed",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "concurrent-agent",
                    "response_id": "resp-a",
                    "result": "A-final",
                },
            ),
            "detail",
        )
    finally:
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert rendered.count("Stage: Streaming") == 1
    assert "B-buffered" not in rendered
    assert rendered.index("A1") < rendered.index("B failed immediately") < rendered.index("A2")
    assert "Streaming continues" in rendered
    assert "A-final" in rendered


def test_agent_execution_console_simple_streams_task_delta_in_one_block(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]

    for delta in ("A", "B"):
        RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
            RuntimeEvent(
                event_type="agent_execution.stream.delta",
                source="BaseAgent",
                payload={
                    "execution_id": "exec-1",
                    "stream_kind": "progress_delta",
                    "path": "agent_task.iteration.1.progress.plan.message",
                    "delta": delta,
                    "execution_strategy": "flat",
                },
            ),
            "simple",
        )

    RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="agent_execution.stream",
            source="BaseAgent",
            payload={
                "execution_id": "exec-1",
                "stream_kind": "progress",
                "path": "agent_task.iteration.1.progress.plan",
                "value": {"message": "AB"},
            },
        ),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="agent_execution.stream",
            source="BaseAgent",
            payload={
                "execution_id": "exec-1",
                "stream_kind": "phase",
                "path": "agent_task.phase.planned",
                "value": {"phase": "planned"},
            },
        ),
        "simple",
    )

    rendered = "".join(printed)
    assert rendered.count("Stage: Streaming") == 1
    assert "Detail:\nAB\n[AgentExecution]" in rendered
    assert rendered.count("AB") == 1
    assert "kind=progress" not in rendered


def test_agent_execution_console_detail_keeps_projection_when_model_logs_are_off(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]

    RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="agent_execution.stream.delta",
            source="BaseAgent",
            payload={
                "execution_id": "exec-1",
                "source": "model_request",
                "path": "model.delta",
                "delta": "A",
            },
        ),
        "detail",
        model_profile="off",
    )

    assert "Stage: Streaming" in "".join(printed)


@pytest.mark.asyncio
async def test_runtime_console_sink_renders_generic_runtime_events(monkeypatch):
    printed: list[str] = []
    settings = _build_runtime_log_settings("detail")

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    with bind_runtime_context(settings=settings):
        await RuntimeConsoleSinkHooker.handler(
            RuntimeEvent(
                event_type="session.applied_to_request",
                source="SessionExtension",
                message="Session context applied.",
            )
        )

    rendered = "\n".join(printed)
    assert "[SessionExtension] [session.applied_to_request]" in rendered
    assert "Session context applied." in rendered


@pytest.mark.asyncio
async def test_runtime_console_sink_uses_run_context_log_settings(monkeypatch):
    snapshot = _snapshot_runtime_log_settings()
    printed: list[str] = []
    ec = EventCenter()
    hook_name = "runtime_console_sink.test"

    def capture_print(*args, **kwargs):
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)
    ec.register_hook(RuntimeConsoleSinkHooker.handler, hook_name=hook_name)

    try:
        Agently.set_settings("debug", False)
        request = Agently.create_request("debug-check")
        request.set_settings("debug", True)

        with bind_runtime_context(settings=request.settings):
            await ec.async_emit(
                {
                    "event_type": "model.requesting",
                    "source": "probe",
                    "level": "INFO",
                    "message": "requesting",
                    "run": request._create_request_run_context(),
                }
            )

        assert printed
        assert any("requesting" in line for line in printed)
    finally:
        ec.unregister_hook(hook_name)
        _restore_runtime_log_settings(snapshot)


@pytest.mark.asyncio
async def test_runtime_console_sink_raw_delivery_preserves_detail_stream(monkeypatch):
    printed: list[str] = []
    settings = _build_runtime_log_settings("detail")
    ec = EventCenter()
    hook_name = "runtime_console_sink.raw_stream"

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    ec.register_hook(
        RuntimeConsoleSinkHooker.handler,
        hook_name=hook_name,
        delivery_policy=RuntimeConsoleSinkHooker.delivery_policy,
    )

    try:
        with bind_runtime_context(settings=settings):
            for delta in ("A", "B"):
                await ec.async_emit(
                    RuntimeEvent(
                        event_type="model.streaming",
                        source="ModelRequestResult",
                        payload={
                            "agent_name": "debug-agent",
                            "response_id": "resp-raw",
                            "delta": delta,
                        },
                        meta={"high_frequency": True},
                    )
                )
                await ec.async_emit(
                    RuntimeEvent(
                        event_type="agent_execution.stream",
                        source="BaseAgent",
                        payload={
                            "execution_id": "exec-raw",
                            "source": "agent_execution",
                            "stream_kind": "runtime_progress",
                            "path": "runtime.progress.model.delta.progress",
                            "value": {"stage": "model.delta", "status": "progress"},
                        },
                    )
                )
                await ec.async_emit(
                    RuntimeEvent(
                        event_type="agent_execution.stream",
                        source="BaseAgent",
                        payload={
                            "execution_id": "exec-raw",
                            "source": "model_request",
                            "path": "reply",
                            "value": delta,
                        },
                    )
                )
                await ec.async_emit(
                    RuntimeEvent(
                        event_type="agent_execution.stream.delta",
                        source="BaseAgent",
                        payload={
                            "execution_id": "exec-raw",
                            "source": "model_request",
                            "path": "model.delta",
                            "delta": delta,
                        },
                        meta={"high_frequency": True},
                    )
                )
                await ec.async_emit(
                    RuntimeEvent(
                        event_type="agent_execution.stream.delta",
                        source="BaseAgent",
                        payload={
                            "execution_id": "exec-raw",
                            "source": "model_request",
                            "path": "model.original_delta",
                            "delta": delta,
                            "meta": {"specific_event": "original_delta"},
                        },
                        meta={"high_frequency": True},
                    )
                )
            await ec.async_emit(
                RuntimeEvent(
                    event_type="agent_execution.stream.delta",
                    source="BaseAgent",
                    payload={
                        "execution_id": "exec-raw",
                        "source": "agent_task",
                        "stream_kind": "child_execution",
                        "path": "agent_task.iteration.1.execution.$status",
                        "delta": None,
                        "value": {"status": "streaming_parse_deferred"},
                    },
                    meta={"high_frequency": True},
                )
            )
            await ec.async_emit(
                RuntimeEvent(
                    event_type="agent_execution.stream",
                    source="BaseAgent",
                    payload={
                        "execution_id": "exec-raw",
                        "stream_kind": "phase",
                        "path": "agent_task.phase.planned",
                        "value": {"phase": "planned"},
                    },
                )
            )
            await ec.async_emit(
                RuntimeEvent(
                    event_type="model.completed",
                    source="ModelRequestResult",
                    payload={
                        "agent_name": "debug-agent",
                        "response_id": "resp-raw",
                        "result": "AB",
                    },
                )
            )
    finally:
        ec.unregister_hook(hook_name)
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert rendered.count("Stage: Streaming") == 1
    assert "Detail:\nAB" in rendered
    assert rendered.index("Stage: Done") < rendered.index("[Deferred diagnostics]")
    assert rendered.index("[Deferred diagnostics]") < rendered.index("[AgentExecution]")
    assert "Streaming continues" not in rendered
    assert "coalesced runtime events" not in rendered
    assert "runtime.progress.model.delta.progress" not in rendered
    assert "Stage: Process" in rendered
    assert "streaming_parse_deferred" in rendered
    assert "Detail:\nNone" not in rendered


@pytest.mark.asyncio
async def test_response_parser_observations_keep_result_log_settings(monkeypatch):
    printed: list[str] = []
    settings = _build_runtime_log_settings("detail")
    request_run = RunContext.create(run_kind="request", agent_name="debug-agent", response_id="resp-parser")

    class FakeParser:
        def drain_runtime_observations(self):
            return [
                {
                    "kind": "streaming",
                    "source": "AgentlyResponseParser",
                    "payload": {"delta": "A"},
                }
            ]

    result = object.__new__(ModelRequestResult)
    result._response_parser = FakeParser()  # type: ignore[attr-defined]
    result.settings = settings  # type: ignore[attr-defined]
    result.agent_name = "debug-agent"  # type: ignore[attr-defined]
    result._response_id = "resp-parser"  # type: ignore[attr-defined]
    result.request_run_context = request_run  # type: ignore[attr-defined]
    result.model_run_context = request_run  # type: ignore[attr-defined]

    async def emit_to_console(event):
        await RuntimeConsoleSinkHooker.handler(RuntimeEvent.model_validate(event))

    monkeypatch.setattr("agently.base.async_emit_runtime", emit_to_console)
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append("".join(map(str, args))))
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]

    await result._drain_response_parser_observations()  # type: ignore[attr-defined]

    assert "Stage: Streaming" in "".join(printed)
