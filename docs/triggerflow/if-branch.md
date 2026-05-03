---
title: if / elif / else
description: "TriggerFlow conditional branches: priority-ordered routing, named condition best practices, and config export boundaries."
keywords: "Agently,TriggerFlow,if,elif,else,condition,config export"
---

# if / elif / else

Use `if / elif / else` when your routing logic is priority-ordered.

## Recommended example

```python
from agently import TriggerFlow, TriggerFlowRuntimeData

flow = TriggerFlow()

def is_high_score(data: TriggerFlowRuntimeData):
    return data.value["score"] >= 90

def is_medium_score(data: TriggerFlowRuntimeData):
    return data.value["score"] >= 80

@flow.chunk("grade_a")
async def grade_a(_: TriggerFlowRuntimeData):
    return "A"

@flow.chunk("grade_b")
async def grade_b(_: TriggerFlowRuntimeData):
    return "B"

@flow.chunk("grade_c")
async def grade_c(_: TriggerFlowRuntimeData):
    return "C"

(
    flow.to(lambda _: {"score": 82})
    .if_condition(is_high_score)
    .to(grade_a)
    .elif_condition(is_medium_score)
    .to(grade_b)
    .else_condition()
    .to(grade_c)
    .end_condition()
    .end()
)
```

## Current best practices

- use **named condition functions** when the flow needs config export
- keep condition handlers side-effect free
- register condition handlers before `load_*_flow()` if the flow is imported from config

## No longer recommended

- putting heavy side effects inside condition handlers
- expecting anonymous lambda conditions to export to JSON/YAML
