from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently_devtools import ObservationBridge


bridge = ObservationBridge(
    app_id="agently-main-examples",
    group_id="devtools-local-demo",
)
bridge.register(Agently)


flow = TriggerFlow(name="devtools-local-demo-flow")


@flow.chunk
def prepare(data: TriggerFlowRuntimeData):
    return {"topic": str(data.value), "status": "prepared"}


@flow.chunk
def finalize(data: TriggerFlowRuntimeData):
    payload = dict(data.value) if isinstance(data.value, dict) else {"value": data.value}
    payload["status"] = "completed"
    return payload


flow.to(prepare).to(finalize).end()


try:
    result = flow.start("release readiness")
    print(result)
finally:
    bridge.unregister(Agently)
