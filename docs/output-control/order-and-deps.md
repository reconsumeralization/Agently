---
title: Order and Dependency Control
description: "Agently output control guide Order and Dependency Control covering structured outputs, key constraints, and streaming parsing."
keywords: "Agently,structured output,output control,instant streaming,Order and Dependency Control"
---

# Order and Dependency Control

In multi-step outputs, later fields often depend on earlier ones. You usually want the model to list facts first, then summarize. Agently generates outputs in order, so good structure improves stability.

## List facts first, then summarize

```python
from agently import Agently

agent = Agently.create_agent()

result = (
  agent
  .input("Where to find release dates for Dark Souls 3 and GTA6, and how to buy them?")
  .output({
    "InfoList": [
      {
        "Topic": (str, "Which game or subject"),
        "KeyFact": (str, "Key fact needed to answer"),
        "IsKnown": (bool, "Whether the fact is confirmed")
      }
    ],
    "Confirmed": (str, "Only explain confirmed facts"),
    "Uncertain": (str, "List items still uncertain")
  })
  .start(
    ensure_keys=["InfoList[*].Topic", "InfoList[*].KeyFact", "InfoList[*].IsKnown"]
  )
)
```

## Protect dependency fields

If later fields depend on earlier ones, include those earlier fields in `ensure_keys`.

## Ordering tips

- Lists before summaries
- Decision before explanation
- Structured fields before free text

Next: Instant structured streaming.
