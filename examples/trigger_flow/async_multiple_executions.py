import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="async-multiple-executions")


async def task(data):
    await asyncio.sleep(0.01 * int(data.input))
    await data.async_set_state("output", {"input": data.input, "status": "done"})


flow.to(task)


async def run_one(value):
    execution = flow.create_execution()
    await execution.async_start(value)
    return await execution.async_close()


async def main():
    states = await asyncio.gather(*(run_one(i) for i in range(5)))
    assert [state["output"]["input"] for state in states] == [0, 1, 2, 3, 4]
    print(states)


asyncio.run(main())
