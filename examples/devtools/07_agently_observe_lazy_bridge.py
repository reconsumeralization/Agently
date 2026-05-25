# pyright: reportMissingImports=false

import asyncio

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData


flow = TriggerFlow(name="devtools-lazy-observe-flow")


@flow.chunk
async def enrich(data: TriggerFlowRuntimeData):
    topic = str(data.input)
    await data.async_put_into_stream({"stage": "enrich", "topic": topic})
    return {
        "topic": topic,
        "priority": "high" if "release" in topic.lower() else "normal",
    }


@flow.chunk
async def summarize(data: TriggerFlowRuntimeData):
    payload = dict(data.input) if isinstance(data.input, dict) else {"topic": str(data.input)}
    payload["status"] = "observed"
    payload["summary"] = f"{payload['topic']} -> {payload['priority']}"
    await data.async_set_state("result", payload)


flow.to(enrich).to(summarize)


bridge = Agently.observe(
    flow,
    app_id="agently-main-examples",
    group_id="devtools-agently-observe-demo",
    timeout=0.5,
)


async def main():
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("release readiness")
    snapshot = await execution.async_close(timeout=1)
    stream_items = [item async for item in execution.get_async_runtime_stream(timeout=None)]
    result = snapshot["result"]
    print(
        {
            "status": result["status"],
            "priority": result["priority"],
            "stream_items": len(stream_items),
            "flow_name": execution.result.get_meta()["flow_name"],
        }
    )


try:
    asyncio.run(main())
finally:
    bridge.unregister()

# Stable expected key output from the declared run:
# {'status': 'observed', 'priority': 'high', 'stream_items': 1, 'flow_name': 'devtools-lazy-observe-flow'}
# Without a running listener, a buffered ObservationBridge warning is expected but the flow still completes.
#
# How it works:
# Agently.observe(...) is a convenience alias for Agently.create_observation_bridge(...).
# The Agently side lazily imports agently_devtools only when this bridge is created,
# binds ObservationBridge to the global Agently EventCenter, and then bridge.watch(flow)
# narrows uploads to this TriggerFlow.
#
# Flow:
# Agently.observe(flow, ...) -> LazyImport loads agently_devtools and binds ObservationBridge
#   |
#   v
# enrich: emits one runtime stream item and returns priority="high"
# summarize: writes result.status="observed"
#   |
#   v
# async_close() -> snapshot.result.status == "observed"
