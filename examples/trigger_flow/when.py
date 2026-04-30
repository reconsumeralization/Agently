import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="when-demo")


async def prepare(data):
    await data.async_set_state("request", {"topic": data.input, "status": "prepared"})
    await data.async_emit("REQUEST_PREPARED", {"topic": data.input})


async def route_prepared(data):
    await data.async_set_state("route", {"event": data.event, "payload": data.input})


flow.to(prepare)
flow.when("REQUEST_PREPARED").to(route_prepared)


async def main():
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("refund")
    state = await execution.async_close()
    assert state["request"]["status"] == "prepared"
    assert state["route"]["payload"] == {"topic": "refund"}
    print(state)


asyncio.run(main())
