from typing_extensions import assert_type

from agently import Agently
from agently_devtools import (
    EvaluationCase,
    ObservationBridge,
    RuntimeObservationService,
)


bridge = ObservationBridge("http://127.0.0.1:9999")
case = EvaluationCase(case_id="typing-smoke", input={"value": 1})
service = RuntimeObservationService(Agently.settings)

assert_type(bridge, ObservationBridge)
assert_type(case, EvaluationCase)
assert_type(service, RuntimeObservationService)
