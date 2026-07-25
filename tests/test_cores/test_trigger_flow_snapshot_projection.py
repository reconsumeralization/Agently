import asyncio
import copy
import json
from typing import Any

import pytest

from agently import TriggerFlow, TriggerFlowRuntimeData


class _MemorySnapshotStore:
    def __init__(self):
        self.state: dict[str, Any] | None = None

    async def put_snapshot(self, run_id: str, state: dict[str, Any], **kwargs: Any):
        _ = (run_id, kwargs)
        self.state = copy.deepcopy(state)
        return {"ref": "memory"}

    async def get_snapshot(self, run_id: str):
        _ = run_id
        return copy.deepcopy(self.state)


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _large_string_paths(
    value: Any,
    *,
    minimum_length: int = 100_000,
    path: str = "$",
) -> list[str]:
    paths: list[str] = []
    if isinstance(value, str) and len(value) >= minimum_length:
        return [path]
    if isinstance(value, dict):
        for key, item in value.items():
            paths.extend(
                _large_string_paths(
                    item,
                    minimum_length=minimum_length,
                    path=f"{path}.{key}",
                )
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(
                _large_string_paths(
                    item,
                    minimum_length=minimum_length,
                    path=f"{path}[{index}]",
                )
            )
    return paths


async def _create_resumed_execution(
    *,
    request_size: int = 100_000,
    response_size: int = 100_000,
):
    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="approval",
            payload={"request": "x" * request_size},
            resume_to="next",
        )

    async def done(data: TriggerFlowRuntimeData):
        return data.value

    flow = TriggerFlow(name="snapshot-projection-resumed")
    flow.to(pause).to(done)
    store = _MemorySnapshotStore()
    execution = flow.create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"snapshot_store": store},
    )
    await execution.async_start(None)
    response = {"response": "y" * response_size}
    await execution.async_continue_with(
        "approval",
        response,
        resume_request_id="approval-request-1",
    )
    return flow, execution, store, response


@pytest.mark.asyncio
async def test_trigger_flow_default_snapshot_preserves_full_terminal_history():
    _, execution, _, _ = await _create_resumed_execution()

    snapshot = execution.save(require_idle=True)

    assert _json_size(snapshot) > 1_200_000
    assert len(_large_string_paths(snapshot)) == 13
    assert snapshot["interrupts"]["approval"]["response"]["response"] == "y" * 100_000
    assert snapshot["resume_ledger"]["approval"]["approval-request-1"]["value"]["response"] == "y" * 100_000


@pytest.mark.asyncio
async def test_trigger_flow_digest_projection_bounds_terminal_history():
    _, execution, _, _ = await _create_resumed_execution()
    execution.set_snapshot_projection_policy(
        terminal_value_mode="digest",
        min_value_bytes=1,
    )

    snapshot = execution.save(require_idle=True)

    assert snapshot["schema_version"] == 2
    assert _json_size(snapshot) < 150_000
    assert len(_large_string_paths(snapshot)) == 1
    assert snapshot["snapshot_projection"]["applied"] is True
    assert snapshot["snapshot_projection"]["projected_value_count"] >= 6
    assert snapshot["snapshot_projection"]["original_value_bytes"] >= 600_000
    projected_response = snapshot["interrupts"]["approval"]["response"]
    assert projected_response["$triggerflow_projection"] == "value_digest"
    assert projected_response["algorithm"] == "sha256"
    assert projected_response["serialized_bytes"] > 100_000
    projected_attempt = next(
        attempt
        for attempt in snapshot["signal_net"]["signal_attempts"]
        if attempt["status"] == "completed" and "resume" in attempt.get("meta", {})
    )
    assert set(projected_attempt["meta"]["resume"]) == {
        "interrupt_id",
        "resume_request_id",
        "actor_id",
        "value",
        "interrupt",
    }


@pytest.mark.asyncio
async def test_trigger_flow_projection_keeps_pending_interrupt_payload_complete():
    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="approval",
            payload={"request": "x" * 100_000},
            resume_to="next",
        )

    flow = TriggerFlow(name="snapshot-projection-pending")
    flow.to(pause)
    store = _MemorySnapshotStore()
    execution = flow.create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"snapshot_store": store},
    )
    execution.set_snapshot_projection_policy(
        terminal_value_mode="digest",
        min_value_bytes=1,
    )
    await execution.async_start(None)

    snapshot = execution.save(require_idle=True)

    assert snapshot["snapshot_projection"]["applied"] is True
    assert snapshot["snapshot_projection"]["projected_terminal_interrupt_ids"] == []
    assert snapshot["interrupts"]["approval"]["payload"]["request"] == "x" * 100_000
    assert (
        snapshot["interrupts"]["approval"]["external_wait_request"]["audit_metadata"]["payload"]["request"]
        == "x" * 100_000
    )


@pytest.mark.asyncio
async def test_trigger_flow_projected_resume_digest_preserves_idempotency_after_load():
    flow, execution, store, response = await _create_resumed_execution()
    execution.set_snapshot_projection_policy(
        terminal_value_mode="digest",
        min_value_bytes=1,
    )
    snapshot = execution.save(require_idle=True)
    restored = flow.create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"snapshot_store": store},
    )
    restored.load(snapshot)

    repeated = await restored.async_continue_with(
        "approval",
        response,
        resume_request_id="approval-request-1",
    )

    assert repeated is not None
    assert repeated["status"] == "resumed"
    with pytest.raises(ValueError, match="conflicting resume_request_id"):
        await restored.async_continue_with(
            "approval",
            {"response": "different"},
            resume_request_id="approval-request-1",
        )


@pytest.mark.asyncio
async def test_trigger_flow_projected_terminal_history_loads_with_later_pending_interrupt():
    async def first(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="first",
            payload={"request": "x" * 100_000},
            resume_to="next",
        )

    async def second(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="second",
            payload={"request": "second"},
            resume_to="next",
        )

    async def done(data: TriggerFlowRuntimeData):
        await data.async_set_state("final", data.value, emit=False)
        return data.value

    flow = TriggerFlow(name="snapshot-projection-later-pending")
    flow.to(first).to(second).to(done)
    store = _MemorySnapshotStore()
    execution = flow.create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"snapshot_store": store},
    )
    execution.set_snapshot_projection_policy(
        terminal_value_mode="digest",
        min_value_bytes=1,
    )
    await execution.async_start(None)
    await execution.async_continue_with(
        "first",
        {"response": "y" * 100_000},
        resume_request_id="request-first",
    )
    snapshot = execution.save(require_idle=True)

    assert snapshot["interrupts"]["first"]["payload"]["$triggerflow_projection"] == "value_digest"
    assert snapshot["interrupts"]["second"]["payload"] == {"request": "second"}

    restored = flow.create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"snapshot_store": store},
    )
    restored.load(snapshot)
    await restored.async_continue_with(
        "second",
        {"response": "final"},
        resume_request_id="request-second",
    )

    assert restored.get_state("final") == {"response": "final"}


@pytest.mark.asyncio
async def test_trigger_flow_snapshot_projection_defers_while_execution_is_active():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(data: TriggerFlowRuntimeData):
        entered.set()
        await release.wait()
        return data.value

    flow = TriggerFlow(name="snapshot-projection-active")
    flow.to(blocked)
    execution = flow.create_execution(auto_close=False, record_store=False)
    execution.set_snapshot_projection_policy(
        terminal_value_mode="digest",
        min_value_bytes=1,
    )
    start_task = asyncio.create_task(execution.async_start("value"))
    await entered.wait()
    snapshot = execution.save()
    release.set()
    await start_task

    assert snapshot["snapshot_projection"]["applied"] is False
    assert snapshot["snapshot_projection"]["deferred_reason"] == "execution_not_idle"


@pytest.mark.asyncio
async def test_trigger_flow_snapshot_rejects_corrupt_value_digest_projection():
    flow, execution, _, _ = await _create_resumed_execution()
    execution.set_snapshot_projection_policy(
        terminal_value_mode="digest",
        min_value_bytes=1,
    )
    snapshot = execution.save(require_idle=True)
    snapshot["resume_ledger"]["approval"]["approval-request-1"]["value"]["sha256"] = "not-a-digest"

    report = flow.create_execution(auto_close=False).inspect_load(snapshot)

    assert report["ready"] is False
    assert any(
        diagnostic["code"] == "triggerflow.snapshot.invalid_value_digest" for diagnostic in report["diagnostics"]
    )
    with pytest.raises(ValueError, match="invalid sha256"):
        flow.create_execution(auto_close=False).load(snapshot)


def test_trigger_flow_snapshot_projection_policy_validates_configuration():
    execution = TriggerFlow(name="snapshot-projection-policy-validation").create_execution(auto_close=False)
    set_policy = getattr(execution, "set_snapshot_projection_policy")

    with pytest.raises(ValueError, match="terminal_value_mode"):
        set_policy(terminal_value_mode="omit")
    with pytest.raises(ValueError, match="min_value_bytes"):
        set_policy(min_value_bytes=-1)
    with pytest.raises(ValueError, match="min_value_bytes"):
        set_policy(min_value_bytes=True)


def test_trigger_flow_v2_loader_accepts_legacy_v1_full_snapshot():
    flow = TriggerFlow(name="snapshot-projection-v1-compatibility")

    async def stage(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(stage)
    snapshot = flow.create_execution(auto_close=False).save()
    legacy = copy.deepcopy(snapshot)
    legacy["schema_version"] = 1
    legacy.pop("snapshot_projection", None)

    restored = flow.create_execution(auto_close=False)
    restored.load(legacy)

    assert restored.save()["schema_version"] == 2


def test_trigger_flow_v2_loader_requires_valid_projection_metadata():
    flow = TriggerFlow(name="snapshot-projection-contract")
    snapshot = flow.create_execution(auto_close=False).save()
    snapshot["snapshot_projection"]["version"] = 999

    report = flow.create_execution(auto_close=False).inspect_load(snapshot)

    assert report["ready"] is False
    assert any(
        diagnostic["code"] == "triggerflow.snapshot.invalid_projection_version"
        for diagnostic in report["diagnostics"]
    )
