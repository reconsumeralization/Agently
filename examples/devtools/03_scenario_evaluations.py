import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently_devtools import EvaluationBinding, EvaluationBridge, EvaluationCase, EvaluationRunner


def build_flow():
    flow = TriggerFlow(name="support-triage-flow")

    @flow.chunk
    async def classify(data: TriggerFlowRuntimeData):
        text = str(data.input).lower()
        if "refund" in text:
            route = "billing"
        elif "shipment" in text:
            route = "logistics"
        else:
            route = "general"
        await data.async_set_state("route", route)

    flow.to(classify)
    return flow


async def run_flow(flow: TriggerFlow, value: str):
    execution = flow.create_execution()
    await execution.async_start(value)
    state = await execution.async_close()
    return state["route"]


def build_executor(active_flow: TriggerFlow):
    def execute(case: EvaluationCase):
        return asyncio.run(run_flow(active_flow, case.input))

    return execute


bridge = EvaluationBridge(
    base_url="http://127.0.0.1:15596",
    app_id="agently-main-examples",
    group_id="devtools-evaluation-demo",
)
runner = EvaluationRunner(bridge=bridge)

binding = EvaluationBinding(
    bridge=bridge,
    suite_id="support-routing",
    target_type="triggerflow",
    target_name="support-triage-flow",
    executor=build_executor(build_flow()),
    target_factory=build_flow,
    target_executor_factory=build_executor,
)

report = runner.run(
    binding,
    cases=[
        EvaluationCase(case_id="refund", input="Need a refund for a duplicate payment."),
        EvaluationCase(case_id="shipping", input="Where is my shipment now?"),
        EvaluationCase(case_id="other", input="Just want general help."),
    ],
    rules=[
        lambda record: record.output in {"billing", "logistics", "general"},
        lambda record: record.error is None,
    ],
    rounds=2,
)

print(
    {
        "suite_id": report.suite_id,
        "passed_rounds": report.passed_rounds,
        "total_rounds": report.total_rounds,
    }
)
