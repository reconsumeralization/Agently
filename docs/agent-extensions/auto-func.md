---
title: auto_func
description: "Agently agent extensions guide auto_func covering tools, MCP, KeyWaiter, and auto functions."
keywords: "Agently,agent extensions,tool calling,MCP,KeyWaiter,auto_func"
---

# auto_func

`auto_func` turns a function into a model‑backed interface:

- **Signature** → input structure  
- **Docstring** → instruction  
- **Return annotation** → output schema  

## Example

```python
from agently import Agently

agent = Agently.create_agent()

@agent.auto_func
def calculate(formula: str) -> int:
  """
  Return result of {formula}.
  MUST USE TOOLS TO ENSURE THE ANSWER IS ACTUAL NO MATTER WHAT.
  """
  ...

result = calculate("3333+6666=?")
print(result)
```

## Async functions

```python
@agent.auto_func
async def plan(task: str) -> dict:
  """Return a plan for {task}."""
  ...

result = await plan("Launch a feature")
```

## Notes

- Generator functions are not supported  
- Return annotation is used as the output schema  
