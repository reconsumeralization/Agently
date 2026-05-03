---
title: for_each Fan-Out
description: "TriggerFlow for_each: list fan-out, ordered merge, and when to use it."
keywords: "Agently,TriggerFlow,for_each,list,parallel"
---

# for_each Fan-Out

Use `for_each` when the same logic should run over many list items in parallel.

## Recommended example

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData

flow = TriggerFlow()

@flow.chunk("input_list")
async def input_list(_: TriggerFlowRuntimeData):
    return [1, 2, 3]

@flow.chunk("square")
async def square(data: TriggerFlowRuntimeData):
    await asyncio.sleep(0.01)
    return data.value * data.value

(
    flow.to(input_list)
    .for_each(concurrency=2)
    .to(square)
    .end_for_each()
    .end()
)
```

## Good scenarios

- batch validation
- batch feature extraction
- batch retrieval or transformation

## Current best practices

- keep item handlers free of shared mutable side effects
- inject tools through `runtime_resources`, not closures
- set `concurrency` explicitly when item work is expensive

## No longer recommended

- using `for_each` for long-lived item workflows that wait on external approvals
- writing order-sensitive shared mutation inside item handlers
