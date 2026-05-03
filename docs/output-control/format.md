---
title: Agently Output Format
description: "Agently output control guide Agently Output Format covering structured outputs, key constraints, and streaming parsing."
keywords: "Agently,structured output,output control,instant streaming,Agently Output Format"
---

# Agently Output Format

When you need outputs that are safe for machines to consume, you usually define the structure first and let the model fill it. Agently Output Format does exactly that, and clarity drives stability.

## Define the structure

Use `output()` with `(type, description)` at leaf nodes.

```python
from agently import Agently

agent = Agently.create_agent()

result = (
  agent
  .input("Explain recursion and provide exercises")
  .output({
    "Explanation": (str, "Concept explanation"),
    "ExampleCodes": ([(str, "Example code")], "At least 2"),
    "Exercises": [
      {
        "Question": (str, "Exercise question"),
        "Answer": (str, "Reference answer")
      }
    ]
  })
  .start()
)

print(result)
```

## Treat structure as a constraint

`output()` is not only formatting; it also steers the model to follow the schema.

Common patterns:

- Scalars: `(str, "desc")` / `(int, "desc")` / `(bool, "desc")`
- Lists: `[(str, "desc")]` or `[{...}]`
- Nested objects: `{ "key": (str, "desc") }`

## Anchor semantics with descriptions

Descriptions are part of the constraint. Be explicit about “what you want”.

```python
.output({
  "Positioning": (str, "One-line positioning"),
  "Highlights": [
    {
      "Title": (str, "Highlight title"),
      "Detail": (str, "One-line detail")
    }
  ]
})
```

Next: lock critical fields with `ensure_keys`.
