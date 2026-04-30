import asyncio

from agently import TriggerFlow


async def normalize(data):
    return str(data.input).strip().lower()


async def store(data):
    await data.async_set_state("normalized", data.input)


def build_flow():
    flow = TriggerFlow(name="save-load-blueprint-demo")
    flow.register_chunk_handler(normalize)
    flow.register_chunk_handler(store)
    flow.to(normalize).to(store)
    return flow


async def run(flow, value):
    execution = flow.create_execution()
    await execution.async_start(value)
    return await execution.async_close()


async def main():
    source_flow = build_flow()
    blueprint = source_flow.save_blueprint()

    restored_flow = TriggerFlow(name="restored-blueprint-demo")
    restored_flow.register_chunk_handler(normalize)
    restored_flow.register_chunk_handler(store)
    restored_flow.load_blueprint(blueprint)

    state = await run(restored_flow, "  Agently  ")
    assert state["normalized"] == "agently"
    print(state)


asyncio.run(main())
