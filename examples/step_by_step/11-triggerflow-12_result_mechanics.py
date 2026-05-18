import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_close_snapshot_demo():
    flow = TriggerFlow(name="step-12-close-snapshot")

    async def work(data: TriggerFlowRuntimeData):
        await data.async_set_state("output", f"work({data.input})")

    flow.to(work)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("task")
    snapshot = await execution.async_close()
    result = execution.result
    assert snapshot is not None
    assert snapshot["output"] == "work(task)"
    assert result.get_state("output") == "work(task)"
    print(snapshot)


async def triggerflow_manual_result_compat_demo():
    flow = TriggerFlow(name="step-12-manual-result-compat")

    async def work(data: TriggerFlowRuntimeData):
        await data.async_set_state("state_output", "kept in state")
        data.set_result({"manual_result": "compatibility override"})

    flow.to(work)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("task")
    snapshot = await execution.async_close()
    result = execution.result
    assert snapshot is not None
    final_result = await result.async_get_final_result()
    assert snapshot == {
        "state_output": "kept in state",
        "$final_result": {"manual_result": "compatibility override"},
    }
    assert final_result == {"manual_result": "compatibility override"}
    print({"snapshot": snapshot, "final_result": final_result})


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
    result = execution.result
    assert state is not None
    assert result.get_state("ping") == "pong"
    assert result.get_meta()["lifecycle_state"] == "closed"
    print({"state": state, "meta": result.get_meta()})


async def main():
    await triggerflow_close_snapshot_demo()
    await triggerflow_manual_result_compat_demo()
    await triggerflow_event_branch_close_demo()


if __name__ == "__main__":
    asyncio.run(main())
