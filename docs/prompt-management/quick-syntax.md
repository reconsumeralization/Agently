---
title: Quick Syntax and always
description: "Agently prompt engineering guide Quick Syntax and always covering layered prompts, config-driven prompts, and mappings."
keywords: "Agently,prompt engineering,prompt management,AI agent development,Quick Syntax and always"
---

# Quick Syntax and always

When writing prompts in code, you usually want brevity and clarity. Quick syntax is a convenience layer that makes the recommended slots easier to use and improves readability, while keeping the same underlying prompt model.

## What quick syntax does

These methods are shorthand for `set_request_prompt` or `set_agent_prompt`:

| Method | Slot | Purpose |
| --- | --- | --- |
| `input()` | `input` | Request input |
| `info()` | `info` | Background info |
| `instruct()` | `instruct` | Directives and constraints |
| `examples()` | `examples` | Examples |
| `output()` | `output` | Output structure |
| `attachment()` | `attachment` | Attachments |
| `options()` | `options` | Model request options |

Rule-style helpers:

| Method | Purpose | Writes |
| --- | --- | --- |
| `rule()` | Rules with instruction template | `system.rule` + `instruct` |
| `role()` | Role with instruction template | `system.your_role` + `instruct` |
| `user_info()` | User info with instruction template | `system.user_info` + `instruct` |

`system()` writes into the `system` slot. In Agent quick syntax, use `always=True` to write into Agent Prompt; otherwise it writes into Request Prompt.

## What the always flag means

For Agent quick methods, `always=True` writes to Agent Prompt (persistent). Otherwise it writes to Request Prompt (per-call).

```python
agent = Agently.create_agent()

# Agent Prompt: long-lived rules
agent.instruct("Keep outputs concise.", always=True)

# Request Prompt: current request
agent.input("Explain recursion and give an example.")
```

Quick syntax is about readability and speed; it still writes to standard slots. For production, keep long‑lived rules in Agent Prompt and move templates to config files.

## Chained syntax for clean prompt expression

Chained calls keep input, constraints, and output in a single semantic block, making prompts readable and maintainable in code.

```python
from agently import Agently

agent = Agently.create_agent()

result = (
  agent
  .info({"Context": "A production-ready AI application framework"})
  .input("Write a developer-facing product introduction")
  .instruct("Give a one-line positioning, then 2 highlights")
  .output({
    "Positioning": (str, "One-line positioning"),
    "Highlights": [
      {
        "Title": (str, "Highlight title"),
        "Detail": (str, "One-line detail")
      }
    ]
  })
  .start()
)

print(result)
```
