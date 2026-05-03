---
title: ensure_keys for Critical Fields
description: "Agently output control guide ensure_keys for Critical Fields covering structured outputs, key constraints, and streaming parsing."
keywords: "Agently,structured output,output control,instant streaming,ensure_keys for Critical Fields"
---

# ensure_keys for Critical Fields

When critical fields occasionally go missing, you usually want a complete result anyway. `ensure_keys` retries to make sure those fields appear and improves stability.

## Mark critical fields

```python
from agently import Agently

agent = Agently.create_agent()

result = (
  agent
  .input("Explain recursion and provide exercises")
  .output({
    "Explanation": (str, "Concept explanation"),
    "Exercises": [
      {
        "Question": (str, "Exercise question"),
        "Answer": (str, "Reference answer")
      }
    ]
  })
  .start(
    ensure_keys=["Exercises[*].Question", "Exercises[*].Answer"],
  )
)
```

## Path style

`ensure_keys` uses `dot` paths by default and also supports `slash`.

```python
.start(
  ensure_keys=["Exercises[*].Question", "Exercises[*].Answer"],
  key_style="dot",
)
```

## Retry and failure strategy

```python
.start(
  ensure_keys=["Exercises[*].Question", "Exercises[*].Answer"],
  max_retries=2,
  raise_ensure_failure=False,
)
```

Recommended:

- Use `ensure_keys` for business-critical fields
- Keep `max_retries` between 1 and 3
- Set `raise_ensure_failure=False` when you want best-effort output

Next: how output order affects stability.
