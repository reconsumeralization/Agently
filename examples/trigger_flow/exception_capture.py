import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="exception-capture-demo")


async def fail(data):
    await data.async_set_state("started", True)
    raise RuntimeError("demo failure")


flow.to(fail)


async def main():
    execution = flow.create_execution()
    try:
        await execution.async_start("demo")
    except RuntimeError as error:
        await execution.async_set_state("error", str(error), emit=False)
    state = await execution.async_close()
    assert state["started"] is True
    assert state["error"] == "demo failure"
    print(state)


asyncio.run(main())
