import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_bounded_cycle_demo():
    flow = TriggerFlow(name="step-15-bounded-cycle")

    async def loop_step(data: TriggerFlowRuntimeData):
        current = int(data.get_state("count", 0) or 0)
        seen = data.get_state("seen", []) or []
        seen.append(current)
        await data.async_set_state("seen", seen, emit=False)
        if current >= 3:
            await data.async_set_state("final", {"mode": "bounded", "seen": seen})
            return
        await data.async_set_state("count", current + 1, emit=False)
        data.emit_nowait("Loop", current + 1)

    flow.to(loop_step)
    flow.when("Loop").to(loop_step)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")
    state = await execution.async_close()
    assert state["final"]["seen"] == [0, 1, 2, 3]
    print(state["final"])


async def triggerflow_pause_between_turns_demo():
    flow = TriggerFlow(name="step-15-pause-between-turns")

    async def step(data: TriggerFlowRuntimeData):
        current = int(data.get_state("count", 0) or 0)
        turns = data.get_state("turns", []) or []
        turns.append(current)
        await data.async_set_state("turns", turns, emit=False)
        if current >= 2:
            await data.async_set_state("final", {"mode": "pause_resume", "turns": turns})
            return
        return await data.async_pause_for(
            type="human_input",
            payload={"question": f"continue from turn {current}?"},
            resume_event="ResumeLoop",
        )

    async def resume_loop(data: TriggerFlowRuntimeData):
        current = int(data.get_state("count", 0) or 0)
        if not isinstance(data.input, dict) or not data.input.get("continue"):
            await data.async_set_state("final", {"mode": "pause_resume", "stopped_by_user": True})
            return
        await data.async_set_state("count", current + 1, emit=False)
        await data.async_emit("Loop", current + 1)

    flow.to(step)
    flow.when("Loop").to(step)
    flow.when("ResumeLoop").to(resume_loop)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")
    for answer in [{"continue": True}, {"continue": True}]:
        interrupt_id = next(iter(execution.get_pending_interrupts()))
        await execution.async_continue_with(interrupt_id, answer)
    state = await execution.async_close()
    assert state["final"]["turns"] == [0, 1, 2]
    print(state["final"])


async def triggerflow_external_reentry_demo():
    flow = TriggerFlow(name="step-15-external-reentry")

    async def init(data: TriggerFlowRuntimeData):
        await data.async_set_state("total", 0, emit=False)

    async def on_tick(data: TriggerFlowRuntimeData):
        total = int(data.get_state("total", 0) or 0) + int(data.input)
        await data.async_set_state("total", total, emit=False)
        if total >= 3:
            await data.async_set_state("final", {"mode": "external_reentry", "total": total})

    flow.to(init)
    flow.when("Tick").to(on_tick)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")
    for delta in [1, 1, 1]:
        await execution.async_emit("Tick", delta)
    state = await execution.async_close()
    assert state["final"]["total"] == 3
    print(state["final"])


async def main():
    await triggerflow_bounded_cycle_demo()
    await triggerflow_pause_between_turns_demo()
    await triggerflow_external_reentry_demo()


if __name__ == "__main__":
    asyncio.run(main())
