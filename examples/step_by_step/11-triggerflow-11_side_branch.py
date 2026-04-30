import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_side_branch_demo():
    flow = TriggerFlow(name="step-11-side-branch")

    async def main_task(data: TriggerFlowRuntimeData):
        return f"main: {data.input}"

    async def side_task(data: TriggerFlowRuntimeData):
        await data.async_set_state("side", f"side saw {data.input}")

    async def store_main(data: TriggerFlowRuntimeData):
        await data.async_set_state("main", data.input)

    flow.to(main_task).side_branch(side_task).to(store_main)

    execution = flow.create_execution()
    await execution.async_start("hello")
    state = await execution.async_close()
    assert state["main"] == "main: hello"
    assert state["side"] == "side saw main: hello"
    print(state)


if __name__ == "__main__":
    asyncio.run(triggerflow_side_branch_demo())
