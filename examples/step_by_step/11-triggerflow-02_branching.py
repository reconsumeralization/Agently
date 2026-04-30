import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_when_demo():
    flow = TriggerFlow(name="step-02-when")

    async def prepare(data: TriggerFlowRuntimeData):
        await data.async_set_state("flag", "ready")
        await data.async_emit("Prepared", {"flag": "ready"})

    async def route(data: TriggerFlowRuntimeData):
        await data.async_set_state("when_payload", data.input)

    flow.to(prepare)
    flow.when("Prepared").to(route)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    state = await execution.async_close()
    assert state["when_payload"] == {"flag": "ready"}
    print("[when]", state)


async def triggerflow_if_condition_demo():
    flow = TriggerFlow(name="step-02-if-condition")

    async def score(data: TriggerFlowRuntimeData):
        return {"score": 82}

    async def store_grade(data: TriggerFlowRuntimeData):
        await data.async_set_state("grade", data.input)

    (
        flow.to(score)
        .if_condition(lambda data: data.input["score"] >= 90)
        .to(lambda _: "A")
        .elif_condition(lambda data: data.input["score"] >= 80)
        .to(lambda _: "B")
        .else_condition()
        .to(lambda _: "C")
        .end_condition()
        .to(store_grade)
    )

    execution = flow.create_execution()
    await execution.async_start(None)
    state = await execution.async_close()
    assert state["grade"] == "B"
    print("[if]", state)


async def triggerflow_match_demo():
    flow = TriggerFlow(name="step-02-match")

    async def store_result(data: TriggerFlowRuntimeData):
        await data.async_set_state("route", data.input)

    (
        flow.to(lambda _: "medium")
        .match()
        .case("low")
        .to(lambda _: "priority: low")
        .case("medium")
        .to(lambda _: "priority: medium")
        .case("high")
        .to(lambda _: "priority: high")
        .case_else()
        .to(lambda _: "priority: unknown")
        .end_match()
        .to(store_result)
    )

    execution = flow.create_execution()
    await execution.async_start(None)
    state = await execution.async_close()
    assert state["route"] == "priority: medium"
    print("[match]", state)


async def main():
    await triggerflow_when_demo()
    await triggerflow_if_condition_demo()
    await triggerflow_match_demo()


if __name__ == "__main__":
    asyncio.run(main())
