import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="for-each-demo")


async def double(data):
    await asyncio.sleep(0.01)
    return data.input * 2


async def store_items(data):
    await data.async_set_state("items", data.input)


flow.for_each(concurrency=2).to(double).end_for_each().to(store_items)


async def main():
    execution = flow.create_execution()
    await execution.async_start([1, 2, 3])
    state = await execution.async_close()
    assert state["items"] == [2, 4, 6]
    print(state["items"])


asyncio.run(main())
