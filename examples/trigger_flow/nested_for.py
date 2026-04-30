import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="nested-for-demo")


async def make_groups(data):
    return [[f"{data.input}-a", f"{data.input}-b"], [f"{data.input}-c"]]


async def expand_group(data):
    return data.input


async def mark_item(data):
    return {"item": data.input, "ready": True}


async def store_nested(data):
    await data.async_set_state("nested", data.input)


(
    flow.to(make_groups)
    .for_each(concurrency=2)
    .to(expand_group)
    .for_each(concurrency=2)
    .to(mark_item)
    .end_for_each()
    .end_for_each()
    .to(store_nested)
)


async def main():
    execution = flow.create_execution()
    await execution.async_start("demo")
    state = await execution.async_close()
    assert state["nested"] == [
        [{"item": "demo-a", "ready": True}, {"item": "demo-b", "ready": True}],
        [{"item": "demo-c", "ready": True}],
    ]
    print(state["nested"])


asyncio.run(main())
