---
title: match / case
description: "TriggerFlow match/case routing: value mapping, predicate cases, and hit_first vs hit_all."
keywords: "Agently,TriggerFlow,match,case,hit_first,hit_all"
---

# match / case

`match / case` is a good fit for “input value -> handler mapping”.

## 1. Fixed-value routing

```python
from agently import TriggerFlow, TriggerFlowRuntimeData

flow = TriggerFlow()

@flow.chunk("low")
async def low(_: TriggerFlowRuntimeData):
    return "priority: low"

@flow.chunk("medium")
async def medium(_: TriggerFlowRuntimeData):
    return "priority: medium"

@flow.chunk("unknown")
async def unknown(_: TriggerFlowRuntimeData):
    return "priority: unknown"

(
    flow.to(lambda _: "medium")
    .match()
    .case("low")
    .to(low)
    .case("medium")
    .to(medium)
    .case_else()
    .to(unknown)
    .end_match()
    .end()
)
```

## 2. Match modes

- `hit_first`
  default; stop after the first match
- `hit_all`
  useful for multi-tag or multi-label routing

## Current best practices

- use `match` for value mapping
- use `if / elif / else` for priority-ordered rules
- use named predicate functions if the flow needs config export

## No longer recommended

- putting complicated side effects inside predicate cases
- expecting anonymous lambda case predicates to export to JSON/YAML
