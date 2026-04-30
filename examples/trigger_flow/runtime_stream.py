import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="runtime-stream-demo")


async def stream_steps(data):
    await data.async_put_into_stream({"stage": "start", "input": data.input})
    await data.async_put_into_stream({"stage": "finish", "input": data.input})
    await data.async_set_state("done", True)


flow.to(stream_steps)


async def main():
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("Agently")
    close_task = asyncio.create_task(execution.async_close())
    items = [item async for item in execution.get_async_runtime_stream(timeout=None)]
    state = await close_task
    assert [item["stage"] for item in items] == ["start", "finish"]
    assert state["done"] is True
    print(items)


asyncio.run(main())
