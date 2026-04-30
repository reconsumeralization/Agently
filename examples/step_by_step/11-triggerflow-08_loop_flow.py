import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def loop_flow_demo():
    flow = TriggerFlow(name="step-08-loop-flow")

    async def start_loop(data: TriggerFlowRuntimeData):
        await data.async_set_state("values", [], emit=False)
        data.emit_nowait("Loop", 0)

    async def loop_step(data: TriggerFlowRuntimeData):
        values = data.get_state("values", []) or []
        values.append(data.input)
        await data.async_set_state("values", values, emit=False)
        if data.input < 3:
            data.emit_nowait("Loop", data.input + 1)
        else:
            await data.async_set_state("done", {"last": data.input, "count": len(values)})

    flow.to(start_loop)
    flow.when("Loop").to(loop_step)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")
    state = await execution.async_close()
    assert state["values"] == [0, 1, 2, 3]
    print(state["done"])


if __name__ == "__main__":
    asyncio.run(loop_flow_demo())
