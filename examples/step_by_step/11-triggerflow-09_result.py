import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_close_result_demo():
    flow = TriggerFlow(name="step-09-execution-result")

    async def worker(data: TriggerFlowRuntimeData):
        await data.async_set_state("output", f"work({data.input})")

    flow.to(worker)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("task-1")
    snapshot = await execution.async_close()
    result = execution.result
    assert snapshot is not None
    assert snapshot["output"] == "work(task-1)"
    assert result.get_state("output") == "work(task-1)"
    assert result.get_meta()["flow_name"] == "step-09-execution-result"
    print({"snapshot": snapshot, "meta": result.get_meta()})


async def triggerflow_set_result_compat_demo():
    flow = TriggerFlow(name="step-09-set-result-compat")

    async def worker(data: TriggerFlowRuntimeData):
        await data.async_set_state("output", f"work({data.input})")
        data.set_result({"compat_result": "explicit result still overrides close snapshot"})

    flow.to(worker)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("task-2")
    snapshot = await execution.async_close()
    result = execution.result
    assert snapshot is not None
    final_result = await result.async_get_final_result()
    assert snapshot["output"] == "work(task-2)"
    assert snapshot["$final_result"] == {
        "compat_result": "explicit result still overrides close snapshot",
    }
    assert final_result == {"compat_result": "explicit result still overrides close snapshot"}
    print({"snapshot": snapshot, "final_result": final_result})


async def main():
    await triggerflow_close_result_demo()
    await triggerflow_set_result_compat_demo()


if __name__ == "__main__":
    asyncio.run(main())
