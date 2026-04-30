import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_close_result_demo():
    flow = TriggerFlow(name="step-09-close-result")

    async def worker(data: TriggerFlowRuntimeData):
        await data.async_set_state("output", f"work({data.input})")

    flow.to(worker)

    execution = flow.create_execution()
    await execution.async_start("task-1")
    state = await execution.async_close()
    assert state["output"] == "work(task-1)"
    print(state)


async def triggerflow_set_result_compat_demo():
    flow = TriggerFlow(name="step-09-set-result-compat")

    async def worker(data: TriggerFlowRuntimeData):
        await data.async_set_state("output", f"work({data.input})")
        data.set_result({"compat_result": "explicit result still overrides close snapshot"})

    flow.to(worker)

    execution = flow.create_execution()
    await execution.async_start("task-2")
    result = await execution.async_close()
    assert result["compat_result"] == "explicit result still overrides close snapshot"
    print(result)


async def main():
    await triggerflow_close_result_demo()
    await triggerflow_set_result_compat_demo()


if __name__ == "__main__":
    asyncio.run(main())
