import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="batch-demo")


def make_handler(label):
    async def handle(data):
        await asyncio.sleep(0.01)
        return f"{label}:{data.input}"

    return handle


async def store_batch(data):
    await data.async_set_state("batch", data.input)


flow.batch(
    ("draft", make_handler("draft")),
    ("review", make_handler("review")),
    ("ship", make_handler("ship")),
    concurrency=2,
).to(store_batch)


async def main():
    execution = flow.create_execution()
    await execution.async_start("demo")
    state = await execution.async_close()
    assert state["batch"] == {
        "draft": "draft:demo",
        "review": "review:demo",
        "ship": "ship:demo",
    }
    print(state["batch"])


asyncio.run(main())
