import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_close_snapshot_demo():
    flow = TriggerFlow(name="step-12-close-snapshot")

    async def work(data: TriggerFlowRuntimeData):
        await data.async_set_state("output", f"work({data.input})")

    flow.to(work)

    execution = flow.create_execution()
    await execution.async_start("task")
    state = await execution.async_close()
    assert state["output"] == "work(task)"
    print(state)


async def triggerflow_manual_result_compat_demo():
    flow = TriggerFlow(name="step-12-manual-result-compat")

    async def work(data: TriggerFlowRuntimeData):
        await data.async_set_state("state_output", "kept in state")
        data.set_result({"manual_result": "compatibility override"})

    flow.to(work)

    execution = flow.create_execution()
    await execution.async_start("task")
    result = await execution.async_close()
    assert result == {"manual_result": "compatibility override"}
    print(result)


async def triggerflow_event_branch_close_demo():
    flow = TriggerFlow(name="step-12-event-branch-close")

    async def emit_event(data: TriggerFlowRuntimeData):
        await data.async_emit("Ping", "pong")

    async def on_ping(data: TriggerFlowRuntimeData):
        await data.async_set_state("ping", data.input)

    flow.to(emit_event)
    flow.when("Ping").to(on_ping)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    state = await execution.async_close()
    assert state["ping"] == "pong"
    print(state)


async def main():
    await triggerflow_close_snapshot_demo()
    await triggerflow_manual_result_compat_demo()
    await triggerflow_event_branch_close_demo()


if __name__ == "__main__":
    asyncio.run(main())
