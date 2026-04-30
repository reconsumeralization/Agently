import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_state_flow():
    flow = TriggerFlow(name="step-04-state-flow")

    async def prepare_user(data: TriggerFlowRuntimeData):
        await data.async_set_state("user", {"id": "u-001", "role": "admin"})
        return data.input

    async def prepare_env(data: TriggerFlowRuntimeData):
        await data.async_set_state("env", {"name": "prod"})
        return data.input

    async def summarize(data: TriggerFlowRuntimeData):
        await data.async_set_state(
            "summary",
            {
                "input": data.input,
                "user": data.get_state("user"),
                "env": data.get_state("env"),
            },
        )

    flow.to(prepare_user).to(prepare_env).to(summarize)

    execution = flow.create_execution()
    await execution.async_start("deploy")
    state = await execution.async_close()
    assert state["summary"]["user"]["id"] == "u-001"
    print(state["summary"])


if __name__ == "__main__":
    asyncio.run(triggerflow_state_flow())
