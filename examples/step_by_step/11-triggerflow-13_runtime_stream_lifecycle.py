import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_runtime_stream_close_demo():
    flow = TriggerFlow(name="step-13-runtime-stream-close")

    async def stream_steps(data: TriggerFlowRuntimeData):
        await data.async_put_into_stream("step-1")
        await data.async_put_into_stream("step-2")
        await data.async_set_state("done", True)

    flow.to(stream_steps)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")
    close_task = asyncio.create_task(execution.async_close())
    events = [event async for event in execution.get_async_runtime_stream(timeout=None)]
    state = await close_task
    assert events == ["step-1", "step-2"]
    assert state["done"] is True
    print({"events": events, "state": state})


if __name__ == "__main__":
    asyncio.run(triggerflow_runtime_stream_close_demo())
