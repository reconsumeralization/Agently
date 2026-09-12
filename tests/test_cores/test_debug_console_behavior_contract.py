from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from agently.builtins.hookers.RuntimeConsoleSinkHooker import (
    RuntimeConsoleSinkHooker,
    should_render_console_event,
)
from agently.core import EventCenter
from agently.core.runtime.RuntimeContext import bind_runtime_context
from agently.types.data import RunContext, RuntimeEvent
from agently.utils import Settings


@dataclass(frozen=True)
class DebugProfileContract:
    profile: str
    model_stream_blocks: int
    agent_execution_stream_blocks: int
    progress_completion_visible: bool
    null_control_visible: bool


PROFILE_CONTRACTS = (
    DebugProfileContract(
        profile="simple",
        model_stream_blocks=1,
        agent_execution_stream_blocks=1,
        progress_completion_visible=False,
        null_control_visible=False,
    ),
    DebugProfileContract(
        profile="detail",
        model_stream_blocks=1,
        agent_execution_stream_blocks=0,
        progress_completion_visible=True,
        null_control_visible=True,
    ),
)


def _settings(profile: str) -> Settings:
    return Settings(
        {
            "runtime": {
                "show_model_logs": profile,
                "show_action_logs": profile,
                "show_tool_logs": profile,
                "show_trigger_flow_logs": profile,
                "show_runtime_logs": profile,
            }
        }
    )


def _stream_block_count(rendered: str, header: str) -> int:
    return len(re.findall(rf"{re.escape(header)}[^\n]*\nStage:\s*Streaming", rendered))


@pytest.mark.parametrize(("profile", "visible"), [("simple", False), ("detail", True)])
def test_action_planning_model_stream_is_diagnostic_not_duplicate_business_output(
    profile: str,
    visible: bool,
) -> None:
    action_planning_run = RunContext.create(
        run_kind="model_request",
        meta={"model_request_role": "action_planning"},
    )
    event = RuntimeEvent(
        event_type="model.streaming",
        source="AgentlyResponseParser",
        payload={"response_id": "action-round", "delta": '"response":"answer"'},
        run=action_planning_run,
    )

    assert should_render_console_event(event, _settings(profile)) is visible

    failure = RuntimeEvent(
        event_type="model.validation_failed",
        source="AgentlyResponseParser",
        level="WARNING",
        message="Action-or-Response validation failed.",
        run=action_planning_run,
    )
    assert should_render_console_event(failure, _settings(profile)) is True


async def _emit_contract_sequence(event_center: EventCenter) -> None:
    await event_center.async_emit(
        RuntimeEvent(
            event_type="model.requesting",
            source="ModelRequest",
            payload={
                "agent_name": "behavior-lock",
                "response_id": "resp-lock",
                "request_text": "USER: behavior contract",
            },
        )
    )
    for model_delta in ("A", "B"):
        await event_center.async_emit(
            RuntimeEvent(
                event_type="model.streaming",
                source="AgentlyResponseParser",
                payload={
                    "agent_name": "behavior-lock",
                    "response_id": "resp-lock",
                    "delta": model_delta,
                },
                meta={"high_frequency": True},
            )
        )
        await event_center.async_emit(
            RuntimeEvent(
                event_type="agent_execution.stream",
                source="BaseAgent",
                payload={
                    "execution_id": "exec-lock",
                    "source": "model_request",
                    "path": "reply",
                    "value": model_delta,
                },
            )
        )
        await event_center.async_emit(
            RuntimeEvent(
                event_type="agent_execution.stream.delta",
                source="BaseAgent",
                payload={
                    "execution_id": "exec-lock",
                    "source": "model_request",
                    "path": "model.original_delta",
                    "delta": model_delta,
                },
                meta={"high_frequency": True},
            )
        )
    await event_center.async_emit(
        RuntimeEvent(
            event_type="model.completed",
            source="AgentlyResponseParser",
            payload={
                "agent_name": "behavior-lock",
                "response_id": "resp-lock",
                "raw_text": "AB",
                "result": {"reply": "AB"},
            },
        )
    )
    for progress_delta in ("X", "Y"):
        await event_center.async_emit(
            RuntimeEvent(
                event_type="agent_execution.stream.delta",
                source="BaseAgent",
                payload={
                    "execution_id": "exec-lock",
                    "source": "agent_task",
                    "stream_kind": "progress_delta",
                    "path": "agent_task.iteration.1.progress.plan.message",
                    "delta": progress_delta,
                },
                meta={"high_frequency": True},
            )
        )
    await event_center.async_emit(
        RuntimeEvent(
            event_type="agent_execution.stream",
            source="BaseAgent",
            payload={
                "execution_id": "exec-lock",
                "source": "agent_task",
                "stream_kind": "progress",
                "path": "agent_task.iteration.1.progress.plan",
                "value": {"message": "XY"},
            },
        )
    )
    await event_center.async_emit(
        RuntimeEvent(
            event_type="agent_execution.stream.delta",
            source="BaseAgent",
            payload={
                "execution_id": "exec-lock",
                "source": "agent_task",
                "stream_kind": "child_execution",
                "path": "agent_task.iteration.1.execution.$status",
                "delta": None,
                "value": {"status": "streaming_parse_deferred"},
            },
            meta={"high_frequency": True},
        )
    )
    await event_center.async_emit(
        RuntimeEvent(
            event_type="agent_execution.stream",
            source="BaseAgent",
            payload={
                "execution_id": "exec-lock",
                "source": "agent_task",
                "stream_kind": "phase",
                "path": "agent_task.phase.planned",
                "value": {"phase": "planned"},
            },
        )
    )
    await event_center.async_emit(
        RuntimeEvent(
            event_type="request.failed",
            source="behavior-lock",
            level="WARNING",
            message="visible warning",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", PROFILE_CONTRACTS, ids=lambda item: item.profile)
async def test_debug_console_base_profile_behavior_lock(monkeypatch, contract: DebugProfileContract) -> None:
    printed: list[str] = []
    event_center = EventCenter()
    hook_name = f"debug_console.behavior_lock.{contract.profile}"

    def capture_print(*args, **kwargs):
        printed.append("".join(str(arg) for arg in args) + kwargs.get("end", "\n"))

    monkeypatch.setattr("builtins.print", capture_print)
    monkeypatch.setattr(
        "agently.builtins.hookers.RuntimeConsoleSinkHooker.color_text",
        lambda value, **_kwargs: str(value),
    )
    RuntimeConsoleSinkHooker._on_register()  # type: ignore[attr-defined]
    event_center.register_hook(
        RuntimeConsoleSinkHooker.handler,
        hook_name=hook_name,
        delivery_policy=RuntimeConsoleSinkHooker.delivery_policy,
    )
    try:
        with bind_runtime_context(settings=_settings(contract.profile)):
            await _emit_contract_sequence(event_center)
    finally:
        event_center.unregister_hook(hook_name)
        RuntimeConsoleSinkHooker._on_unregister()  # type: ignore[attr-defined]

    rendered = "".join(printed)
    assert _stream_block_count(rendered, "[ModelRequest]") == contract.model_stream_blocks
    assert _stream_block_count(rendered, "[AgentExecution]") == contract.agent_execution_stream_blocks
    assert ('"path": "agent_task.iteration.1.progress.plan"' in rendered) is (
        contract.progress_completion_visible
    )
    assert ("streaming_parse_deferred" in rendered) is contract.null_control_visible

    assert "Stage: Requesting" in rendered
    assert "Stage: Done" in rendered
    assert "agent_task.phase.planned" in rendered
    assert "visible warning" in rendered
    assert "coalesced runtime events" not in rendered
    assert "Detail:\nNone" not in rendered
    assert "model.original_delta" not in rendered

    if contract.profile == "simple":
        assert "Detail:\nXY\n[AgentExecution]" in rendered
        assert rendered.count("XY") == 1
    else:
        assert '"reply": "AB"' in rendered
