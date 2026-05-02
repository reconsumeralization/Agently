# pyright: reportMissingImports=false

import asyncio

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently_devtools import ObservationBridge


watched_flow = TriggerFlow(name="devtools-watched-flow")
ignored_flow = TriggerFlow(name="devtools-ignored-flow")


@watched_flow.chunk
async def watched_step(data: TriggerFlowRuntimeData):
    await data.async_set_state("result", {"flow": "watched", "input": data.input})


@ignored_flow.chunk
async def ignored_step(data: TriggerFlowRuntimeData):
    await data.async_set_state("result", {"flow": "ignored", "input": data.input})


watched_flow.to(watched_step)
ignored_flow.to(ignored_step)

bridge = ObservationBridge(
    app_id="agently-main-examples",
    group_id="devtools-selective-watch-demo",
    auto_watch=False,
)
bridge.watch(watched_flow)
bridge.register(Agently)


async def run_flow(flow: TriggerFlow, value: str):
    execution = flow.create_execution()
    await execution.async_start(value)
    return await execution.async_close()


async def main():
    print("Running watched flow")
    print(await run_flow(watched_flow, "keep this run"))
    print("Running ignored flow")
    print(await run_flow(ignored_flow, "do not upload this run"))


try:
    asyncio.run(main())
finally:
    bridge.unregister(Agently)
