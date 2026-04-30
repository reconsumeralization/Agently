import asyncio

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently_devtools import ObservationBridge


bridge = ObservationBridge(
    app_id="agently-main-examples",
    group_id="devtools-local-demo",
)
bridge.register(Agently)

flow = TriggerFlow(name="devtools-local-demo-flow")


@flow.chunk
async def prepare(data: TriggerFlowRuntimeData):
    return {"topic": str(data.input), "status": "prepared"}


@flow.chunk
async def finalize(data: TriggerFlowRuntimeData):
    payload = dict(data.input) if isinstance(data.input, dict) else {"value": data.input}
    payload["status"] = "completed"
    await data.async_set_state("result", payload)


flow.to(prepare).to(finalize)


async def main():
    execution = flow.create_execution()
    await execution.async_start("release readiness")
    print(await execution.async_close())


try:
    asyncio.run(main())
finally:
    bridge.unregister(Agently)
