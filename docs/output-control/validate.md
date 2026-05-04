---
title: Custom Output Validation
description: "Agently output control guide for custom output validation with .validate() and validate_handler."
keywords: "Agently,output control,validate,ensure_keys,structured output"
---

# Custom Output Validation

Use custom validation when field existence is not enough and the model must satisfy value-level business rules before the result is accepted.

Typical cases:

- status must be one of a limited production-safe set
- score must stay inside a range
- two fields must agree with each other
- a draft or placeholder value should trigger a retry

`validate` runs after structured parsing, strict output checks, and `ensure_keys`, and it shares the same `max_retries` budget.

## `.validate(...)`

Register request-side validators as part of the request pipeline:

```python
from agently import Agently

agent = Agently.create_agent()

result = (
  agent
  .input("Classify this ticket")
  .output({
    "status": (str, "ready / blocked / draft"),
    "severity": (str, "P0 / P1 / P2 / P3"),
  })
  .validate(
    lambda result, context: (
      result["status"] == "ready"
      and result["severity"] in {"P0", "P1", "P2", "P3"}
    )
  )
  .start(max_retries=2)
)
```

The handler receives:

- `result`: a canonical dict snapshot
- `context`: request/response metadata including `attempt_index`, `response_text`, `parsed_result`, and `result_object`

## `validate_handler=...`

Inject validators at execution time when the rule is request-specific:

```python
result = await agent.async_start(
  ensure_keys=["status", "severity"],
  validate_handler=lambda result, context: result["status"] == "ready",
  max_retries=2,
)
```

Request-side `.validate(...)` handlers run first. `validate_handler=` handlers run after them.

## Return values

### `True` / `False`

- `True`: pass
- `False`: fail and retry, if retry budget remains

### Dict result

Return a dict when you need more control:

```python
def validate_status(result, context):
  if result["status"] == "ready":
    return True
  return {
    "ok": False,
    "reason": "draft is not publishable",
    "payload": {"status": result["status"]},
  }
```

Supported control keys:

- `ok`
- `reason`
- `payload`
- `validator_name`
- `no_retry`
- `stop`
- `error` / `exception` / `raise`

`no_retry=True` or `stop=True` stops further retries. `raise` lets the validator choose the final exception.

## Reusing one response

Validation runs once per `ModelResponseResult` and the outcome is cached. If you need parsed data and typed objects from one request, reuse one response:

```python
response = (
  agent
  .input("Summarize this incident")
  .output({
    "summary": (str,),
    "severity": (str,),
  })
  .validate(lambda result, context: result["severity"] in {"P0", "P1", "P2", "P3"})
  .get_response()
)

data = await response.result.async_get_data()
typed = await response.result.async_get_data_object()
```

The validator does not rerun when you read `data` and `typed` from the same response.
