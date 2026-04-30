import asyncio

from agently import TriggerFlow, TriggerFlowBlueprint

blueprint = TriggerFlowBlueprint(name="blueprint-event-demo")


async def on_ping(data):
    await data.async_set_state("last_ping", data.input)


blueprint.add_event_handler("PING", on_ping)
flow = TriggerFlow(blueprint=blueprint, name="blueprint-execution-demo")


async def run_one(value):
    execution = flow.create_execution(auto_close=False)
    execution.emit_nowait("PING", value)
    return await execution.async_close()


async def main():
    first, second = await asyncio.gather(run_one("one"), run_one("two"))
    assert first["last_ping"] == "one"
    assert second["last_ping"] == "two"
    print({"first": first["last_ping"], "second": second["last_ping"]})


asyncio.run(main())
