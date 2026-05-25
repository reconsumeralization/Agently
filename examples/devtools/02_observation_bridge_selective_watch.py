# pyright: reportMissingImports=false

import asyncio

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData


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

bridge = Agently.create_observation_bridge(
    watched_flow,
    app_id="agently-main-examples",
    group_id="devtools-selective-watch-demo",
)


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
    bridge.unregister()

# Stable expected key output from the declared run:
# watched flow result.input == "keep this run" and ignored flow result.input == "do not upload this run".
#
# How it works:
# Agently.create_observation_bridge(watched_flow, ...) binds the DevTools bridge and
# selectively enables event forwarding for only that flow.
# ignored_flow runs without any bridge events being emitted.  Both flows produce local
# output normally; the difference is only in which events reach the devtools server.
#
# Flow:
# bridge = Agently.create_observation_bridge(watched_flow, ...)
#   only this flow emits events
#   |
#   v
# watched_flow("keep this run")  -> state["result"]["flow"] = "watched"
# ignored_flow("do not upload this run") -> state["result"]["flow"] = "ignored"
#   (no bridge events for ignored_flow)
#   |
#   v
# bridge.unregister()
