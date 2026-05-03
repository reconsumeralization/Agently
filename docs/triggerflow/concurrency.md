---
title: Concurrency Limits
description: "TriggerFlow concurrency control: batch, for_each, execution-level limits, and runtime adjustment."
keywords: "Agently,TriggerFlow,concurrency,batch,for_each,execution"
---

# Concurrency Limits

TriggerFlow has three commonly used concurrency control layers:

- `batch(..., concurrency=n)`
- `for_each(concurrency=n)`
- `create_execution(concurrency=n)` / `execution.set_concurrency(n)`

## Recommended mental model

- `batch` / `for_each`
  limit local fan-out
- execution-level concurrency
  protects the whole execution globally

## Recommended example

```python
execution = flow.create_execution(concurrency=2)
execution.set_concurrency(1)
```

## Current best practices

- when you have API or DB rate limits, prefer an execution-level global cap
- when you only need local throttling, use batch / for_each concurrency
- in most service deployments, using both layers is the most robust approach

## No longer recommended

- letting one execution saturate all external dependencies without limits
- using concurrency settings to compensate for a bad orchestration topology
