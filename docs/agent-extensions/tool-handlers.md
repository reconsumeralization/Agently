---
title: Tool Handlers (Default and Replace)
description: "Agently v4.0.8.2 tool handlers: plan_analysis_handler and tool_execution_handler signatures, default behavior, and replacement points."
keywords: "Agently,tool handlers,plan_analysis_handler,tool_execution_handler"
---

# Tool Handlers (Default and Replace)

> Applies to: `v4.0.8.2`

The Tool Loop intentionally exposes only two core extension points:

1. `plan_analysis_handler`
2. `tool_execution_handler`

## 1. Default handler chain and replacement points

```mermaid
flowchart LR
    A[prompt + settings + visible tools] --> B[plan_analysis_handler]
    B --> C[normalize into execution_commands]
    C --> D[tool_execution_handler]
    D --> E[ToolExecutionRecord[]]
    E --> F["action_results / tool_logs"]
    B -. replaceable .-> B2[custom planner]
    D -. replaceable .-> D2[custom executor]
```

### How to read this diagram

- The planner and executor are two separate cut points: decision vs side effects.
- Result injection is intentionally stable framework behavior, so replacing it is usually the wrong move.

### Design rationale

The key architectural boundary is not “can this be customized?” but “do not mix reasoning with side effects in the same handler.” If both happen in one layer, debugging becomes much harder.

## 2. `plan_analysis_handler`

Responsibilities:

- decide whether the next step is `execute` or `response`
- produce `execution_commands[]`

Signature:

```python
async def custom_plan_handler(
    prompt,
    settings,
    tool_list,
    done_plans,
    last_round_records,
    round_index,
    max_rounds,
    agent_name,
): ...
```

Recommended return shape:

```python
{
    "next_action": "execute" | "response",
    "execution_commands": [
        {
            "purpose": str,
            "tool_name": str,
            "tool_kwargs": dict,
            "todo_suggestion": str,
        }
    ],
}
```

## 3. `tool_execution_handler`

Responsibilities:

- execute `execution_commands`
- return auditable execution records

Signature:

```python
async def custom_execution_handler(
    tool_commands,
    settings,
    async_call_tool,
    done_plans,
    round_index,
    concurrency,
    agent_name,
): ...
```

Recommended return shape:

```python
[
    {
        "purpose": str,
        "tool_name": str,
        "kwargs": dict,
        "todo_suggestion": str,
        "success": bool,
        "result": Any,
        "error": str,
    }
]
```

## 4. Default behavior

Default planner:

- creates a dedicated `ModelRequest`
- listens to `instant.next_action`
- short-circuits when `next_action=response`

Default executor:

- uses `tool.loop.concurrency`
- calls `async_call_tool(...)` per command
- marks error strings as `success=False`

## 5. Replace at the agent layer

```python
from agently import Agently

agent = Agently.create_agent()

agent.register_tool_plan_analysis_handler(custom_plan_handler)
agent.register_tool_execution_handler(custom_execution_handler)
```

Restore defaults:

```python
agent.register_tool_plan_analysis_handler(None)
agent.register_tool_execution_handler(None)
```

## 6. Replace globally at the core layer

```python
from agently import Agently

Agently.tool.register_plan_analysis_handler(custom_plan_handler)
Agently.tool.register_tool_execution_handler(custom_execution_handler)
```

## 7. Design guidance

- keep planners focused on decisions
- keep executors focused on execution
- use `execution_commands` as the primary contract
- preserve fields like `purpose`, `tool_name`, `result`, and `error`
- keep `todo_suggestion` readable because it shapes future rounds
