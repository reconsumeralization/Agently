---
title: Batch Fan-Out
description: "TriggerFlow batch fan-out: parallel independent work, merge semantics, and named-branch best practices."
keywords: "Agently,TriggerFlow,batch,parallel,fan-out,collect"
---

# Batch Fan-Out

Use `batch` when multiple tasks are independent and can run in parallel before merging their results.

## Recommended example

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData

flow = TriggerFlow()

@flow.chunk("facts")
async def facts(data: TriggerFlowRuntimeData):
    await asyncio.sleep(0.01)
    return f"facts:{data.value}"

@flow.chunk("risks")
async def risks(data: TriggerFlowRuntimeData):
    await asyncio.sleep(0.01)
    return f"risks:{data.value}"

flow.batch(facts, risks).end()
print(flow.start("AI chips"))
```

## Good scenarios

- parallel tool calls
- parallel information extraction
- multiple independent analyses from one input

## Current best practices

- keep batch branches independent
- prefer named chunks so result keys, Mermaid, and config export stay stable
- combine with `concurrency` when downstream services are rate-limited

## No longer recommended

- forcing strongly ordered work into `batch`
- sharing mutable external state across branches without isolation
