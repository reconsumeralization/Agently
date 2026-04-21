from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently_devtools import ObservationBridge


watched_flow = TriggerFlow(name="devtools-watched-flow")
ignored_flow = TriggerFlow(name="devtools-ignored-flow")


@watched_flow.chunk
def watched_step(data: TriggerFlowRuntimeData):
    return {"flow": "watched", "input": data.value}


@ignored_flow.chunk
def ignored_step(data: TriggerFlowRuntimeData):
    return {"flow": "ignored", "input": data.value}


watched_flow.to(watched_step).end()
ignored_flow.to(ignored_step).end()

bridge = ObservationBridge(
    app_id="agently-main-examples",
    group_id="devtools-selective-watch-demo",
    auto_watch=False,
)
bridge.watch(watched_flow)
bridge.register(Agently)


try:
    print("Running watched flow")
    print(watched_flow.start("keep this run"))
    print("Running ignored flow")
    print(ignored_flow.start("do not upload this run"))
finally:
    bridge.unregister(Agently)
