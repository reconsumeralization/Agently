import asyncio

import pytest

from agently import TriggerFlow, TriggerFlowRuntimeData


def _two_interrupt_flow():
    flow = TriggerFlow()
    resumed: dict[str, object] = {}

    async def fan_out(data: TriggerFlowRuntimeData):
        await data.async_emit("ASK_A", data.value)
        await data.async_emit("ASK_B", data.value)

    async def ask_a(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange",
            exchange_kind="approval",
            interrupt_id="exchange-a",
            resume_to={"event": "GOT_A"},
        )

    async def ask_b(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange",
            exchange_kind="clarification",
            interrupt_id="exchange-b",
            resume_to={"event": "GOT_B"},
        )

    async def got_a(data: TriggerFlowRuntimeData):
        resumed["a"] = data.value

    async def got_b(data: TriggerFlowRuntimeData):
        resumed["b"] = data.value

    flow.to(fan_out)
    flow.when("ASK_A").to(ask_a)
    flow.when("ASK_B").to(ask_b)
    flow.when("GOT_A").to(got_a)
    flow.when("GOT_B").to(got_b)
    return flow, resumed


@pytest.mark.asyncio
async def test_concurrent_continue_with_on_different_interrupts_keeps_both_ledgers():
    flow, resumed = _two_interrupt_flow()
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("go")

    pending = execution.get_pending_interrupts()
    assert set(pending) == {"exchange-a", "exchange-b"}

    await asyncio.gather(
        execution.async_continue_with(
            "exchange-a",
            {"approved": True},
            resume_request_id="req-a",
            actor="tester-a",
        ),
        execution.async_continue_with(
            "exchange-b",
            {"answer": "42"},
            resume_request_id="req-b",
            actor="tester-b",
        ),
    )

    interrupts = execution._get_interrupts()
    for interrupt_id, request_id in (("exchange-a", "req-a"), ("exchange-b", "req-b")):
        interrupt = interrupts[interrupt_id]
        assert interrupt["status"] == "resumed", interrupt_id
        assert interrupt["resume_requests"][request_id]["status"] == "completed"
        assert interrupt["external_wait_request"]["dispatch_state"] == "completed"
    assert resumed == {"a": {"approved": True}, "b": {"answer": "42"}}

    await execution.async_close()


@pytest.mark.asyncio
async def test_concurrent_continue_with_on_same_interrupt_dispatches_once():
    flow, resumed = _two_interrupt_flow()
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("go")

    outcomes = await asyncio.gather(
        execution.async_continue_with("exchange-a", {"winner": 1}),
        execution.async_continue_with("exchange-a", {"winner": 2}),
        return_exceptions=True,
    )

    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    assert len(successes) == 1

    interrupt = execution.get_interrupt("exchange-a")
    assert isinstance(interrupt, dict)
    assert interrupt["status"] == "resumed"
    assert resumed["a"] in ({"winner": 1}, {"winner": 2})

    await execution.async_continue_with("exchange-b", {"answer": "ok"})
    await execution.async_close()


@pytest.mark.asyncio
async def test_duplicate_resume_request_id_on_same_interrupt_is_idempotent_under_race():
    flow, resumed = _two_interrupt_flow()
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("go")

    outcomes = await asyncio.gather(
        execution.async_continue_with(
            "exchange-a", {"approved": True}, resume_request_id="same-req"
        ),
        execution.async_continue_with(
            "exchange-a", {"approved": True}, resume_request_id="same-req"
        ),
        return_exceptions=True,
    )

    assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
    interrupt = execution.get_interrupt("exchange-a")
    assert isinstance(interrupt, dict)
    assert interrupt["status"] == "resumed"
    assert interrupt["resume_requests"]["same-req"]["status"] == "completed"
    assert resumed["a"] == {"approved": True}

    await execution.async_continue_with("exchange-b", {"answer": "ok"})
    await execution.async_close()
