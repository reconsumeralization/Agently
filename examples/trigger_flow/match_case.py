import asyncio

from agently import TriggerFlow

flow = TriggerFlow(name="match-case-demo")


async def choose_priority(data):
    return data.input.get("priority", "unknown")


async def store_result(data):
    await data.async_set_state("result", data.input)


(
    flow.to(choose_priority)
    .match()
    .case("low")
    .to(lambda _: "queue")
    .case("high")
    .to(lambda _: "run-now")
    .case_else()
    .to(lambda _: "review")
    .end_match()
    .to(store_result)
)


async def main():
    execution = flow.create_execution()
    await execution.async_start({"priority": "high"})
    state = await execution.async_close()
    assert state["result"] == "run-now"
    print(state["result"])


asyncio.run(main())
