from agently import TriggerFlow, TriggerFlowRuntimeData
from agently_devtools import EvaluationBridge, EvaluationCase, EvaluationRunner


def build_flow():
    flow = TriggerFlow(name="support-triage-flow")

    @flow.chunk
    def classify(data: TriggerFlowRuntimeData):
        text = str(data.value).lower()
        if "refund" in text:
            return "billing"
        if "shipment" in text:
            return "logistics"
        return "general"

    flow.to(classify).end()
    return flow


bridge = EvaluationBridge(
    base_url="http://127.0.0.1:15596",
    app_id="agently-main-examples",
    group_id="devtools-evaluation-demo",
)
runner = EvaluationRunner(bridge=bridge)

binding = bridge.bind_triggerflow_factory(
    build_flow,
    suite_id="support-routing",
    target_name="support-triage-flow",
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
