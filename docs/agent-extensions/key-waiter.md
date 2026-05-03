---
title: KeyWaiter
description: "Agently agent extensions guide KeyWaiter covering tools, MCP, KeyWaiter, and auto functions."
keywords: "Agently,agent extensions,tool calling,MCP,KeyWaiter,KeyWaiter"
---

# KeyWaiter

When output is structured (Agently Output Format), KeyWaiter lets you **retrieve a key before the full response completes**. This is useful for streaming UI or early triggers.

KeyWaiter relies on `instant` structured streaming, so you must define `output()` first.

## Get one key early

```python
from agently import Agently

agent = Agently.create_agent()

agent.input("34643523+52131231=?").output(
  {
    "thinking": (str,),
    "result": (float,),
    "reply": (str,),
  }
)

reply = agent.get_key_result("thinking")
print(reply)
```

## Stream multiple keys

```python
gen = agent.wait_keys(["thinking", "reply"])
for key, value in gen:
  print(key, value)
```

## Callback per key

```python
(
  agent.input("34643523+52131231=?")
  .output(
    {
      "thinking": (str,),
      "result": (float,),
      "reply": (str,),
    }
  )
  .when_key("thinking", lambda v: print("🤔:", v))
  .when_key("result", lambda v: print("✅:", v))
  .when_key("reply", lambda v: print("⏩:", v))
  .start_waiter()
)
```

## Rules

- `output()` is required  
- `must_in_prompt=True` enforces key validation  
