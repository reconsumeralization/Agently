import asyncio
import time

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_concurrency():
    flow = TriggerFlow(name="step-03-concurrency")

    async def echo(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.05)
        return f"echo: {data.input}"

    async def store_batch(data: TriggerFlowRuntimeData):
        await data.async_set_state("batch", data.input)

    flow.batch(
        ("a", echo),
        ("b", echo),
        ("c", echo),
    ).to(store_batch)

    execution = flow.create_execution(concurrency=2)
    started_at = time.perf_counter()
    await execution.async_start("hello")
    state = await execution.async_close()
    elapsed = time.perf_counter() - started_at
    assert set(state["batch"]) == {"a", "b", "c"}
    assert elapsed >= 0.1
    print({"batch": state["batch"], "elapsed": round(elapsed, 3)})

    flow_2 = TriggerFlow(name="step-03-for-each")

    async def double(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.01)
        return data.input * 2

    async def store_items(data: TriggerFlowRuntimeData):
        await data.async_set_state("items", data.input)

    flow_2.for_each(concurrency=2).to(double).end_for_each().to(store_items)
    execution_2 = flow_2.create_execution()
    await execution_2.async_start([1, 2, 3, 4])
    state_2 = await execution_2.async_close()
    assert state_2["items"] == [2, 4, 6, 8]
    print(state_2)


if __name__ == "__main__":
    asyncio.run(triggerflow_concurrency())
