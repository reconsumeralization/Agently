import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="nowait-loop-demo")


async def start_loop(data):
    await data.async_set_state("values", [], emit=False)
    data.emit_nowait("TICK", 1)


async def on_tick(data):
    values = data.get_state("values", []) or []
    values.append(data.input)
    await data.async_set_state("values", values, emit=False)
    if data.input < 3:
        data.emit_nowait("TICK", data.input + 1)
    else:
        await data.async_set_state("summary", {"count": len(values), "last": values[-1]})


flow.to(start_loop)
flow.when("TICK").to(on_tick)


async def main():
    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    state = await execution.async_close()
    assert state["values"] == [1, 2, 3]
    assert state["summary"] == {"count": 3, "last": 3}
    print(state["summary"])


asyncio.run(main())
