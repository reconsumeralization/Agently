import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_basics():
    flow = TriggerFlow(name="step-01-basics")

    async def greet(data: TriggerFlowRuntimeData):
        await data.async_set_state("greeting", f"Hello, {data.input}")
        return data.input

    async def finish(data: TriggerFlowRuntimeData):
        await data.async_set_state("finished", True)

    flow.to(greet).to(finish)

    execution = flow.create_execution()
    await execution.async_start("Agently")
    state = await execution.async_close()
    assert state["greeting"] == "Hello, Agently"
    print(state)


if __name__ == "__main__":
    asyncio.run(triggerflow_basics())
