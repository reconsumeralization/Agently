import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData


async def runtime_event_data_demo():
    flow = TriggerFlow(name="step-06-runtime-event-data")

    async def inspect_event(data: TriggerFlowRuntimeData):
        await data.async_set_state("seen_event", data.event)
        await data.async_emit("CustomEvent", {"from": data.event, "value": data.input})
        return data.input

    async def store_custom_event(data: TriggerFlowRuntimeData):
        await data.async_set_state(
            "custom_event",
            {
                "event": data.event,
                "type": data.trigger_type,
                "payload": data.input,
                "layer": data.layer_mark,
            },
        )

    flow.to(inspect_event)
    flow.when("CustomEvent").to(store_custom_event)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("hello")
    state = await execution.async_close()
    assert state["seen_event"] == "START"
    assert state["custom_event"]["payload"]["value"] == "hello"
    print(state)


if __name__ == "__main__":
    asyncio.run(runtime_event_data_demo())
