import asyncio
import time

from agently import TriggerFlow

flow = TriggerFlow(name="global-concurrency-demo")


async def handle(data):
    await asyncio.sleep(0.05)
    return data.input


async def store_batch(data):
    await data.async_set_state("batch", data.input)


flow.batch(
    ("a", handle),
    ("b", handle),
    ("c", handle),
    ("d", handle),
).to(store_batch)


async def main():
    execution = flow.create_execution(concurrency=2)
    started_at = time.perf_counter()
    await execution.async_start("demo")
    state = await execution.async_close()
    elapsed = time.perf_counter() - started_at
    assert set(state["batch"]) == {"a", "b", "c", "d"}
    assert elapsed >= 0.1
    print({"batch": state["batch"], "elapsed": round(elapsed, 3)})


asyncio.run(main())
