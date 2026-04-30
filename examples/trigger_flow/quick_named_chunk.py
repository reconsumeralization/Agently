import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="quick-named-chunk")


@flow.chunk("normalize-input")
async def normalize(data):
    return str(data.input).strip().lower()


async def store(data):
    await data.async_set_state("normalized", data.input)


flow.to(normalize).to(store)


async def main():
    execution = flow.create_execution()
    await execution.async_start("  Agently  ")
    state = await execution.async_close()
    assert state["normalized"] == "agently"
    print(state["normalized"])


asyncio.run(main())
