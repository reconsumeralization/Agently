---
title: Tool Runtime (Tool Loop)
description: "Agently v4.0.8.2 Tool Loop: TriggerFlow-driven planning/execution loop, instant short-circuit, and result injection."
keywords: "Agently,Tool Loop,TriggerFlow,execution_commands,done_plans,round_index"
---

# Tool Runtime (Tool Loop)

> Applies to: `v4.0.8.2`

In `v4.0.8.2`, `Tool.async_plan_and_execute()` is not a simple while loop. It is a normalized runtime loop implemented on top of `TriggerFlow`.

## 1. Overall flow

```mermaid
flowchart TD
    A[request_prefix] --> B{tool.loop.enabled and tools available?}
    B -- no --> Z[skip Tool Loop]
    B -- yes --> C[START]
    C --> D[initialize_loop]
    D --> E[emit PLAN]
    E --> F[plan_step]
    F --> G{next_action}
    G -- response --> H[emit DONE]
    G -- execute --> I[emit EXECUTE]
    I --> J[execute_step]
    J --> K["update done_plans / last_round_records / round_index"]
    K --> E
    H --> L[return ToolExecutionRecord[]]
```

### How to read this diagram

- `PLAN` and `EXECUTE` are explicit runtime stages, not just documentation labels.
- That is why the Tool Loop can reuse the same stateful runtime model as TriggerFlow itself.

## 2. What round state looks like

```text
round_state = {
  "done_plans": [...],          # all execution records so far
  "last_round_records": [...],  # previous round only
  "round_index": 0,             # current round
}
```

### Design rationale

- `done_plans` provides long-term context so the planner does not forget completed work.
- `last_round_records` provides immediate feedback for retries and follow-up actions.
- `round_index` is a hard safety boundary.

## 3. Planning stage: `plan_step`

The default planner builds a dedicated `ModelRequest` and reads:

- `input.user_input`
- `input.user_extra_requirement`
- `input.available_tools`
- `info.done_plans`
- `info.last_round_result`
- `info.round_index`
- `info.max_rounds`

Required output shape:

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

Compatibility normalization:

- `tool_commands` is still normalized into `execution_commands`
- a single `tool_command` is promoted into a list
- if `next_action` is missing, the framework infers it from whether commands exist

## 4. `instant` short-circuit

The default planner watches `next_action` in structured streaming output.

If it can confirm early that:

- `next_action=response`

it closes the planning stream immediately and skips waiting for the full final body.

## 5. Execution stage: `execute_step`

The executor receives normalized `ToolCommand` items:

```python
{
    "purpose": str,
    "tool_name": str,
    "tool_kwargs": dict,
    "todo_suggestion": str,
    "next": str,
}
```

The default executor:

1. reads `tool.loop.concurrency`
2. runs `async_call_tool(tool_name, tool_kwargs)` concurrently
3. normalizes results into `ToolExecutionRecord`

Normalized execution record:

```python
{
    "purpose": str,
    "tool_name": str,
    "kwargs": dict,
    "todo_suggestion": str,
    "next": str,
    "success": bool,
    "result": Any,
    "error": str,
}
```

## 6. Stop conditions

The Tool Loop ends when any of the following is true:

- `next_action != "execute"`
- `use_tool` normalizes to `False`
- there are no executable commands
- `max_rounds` is reached

## 7. Convergence and injection

After the loop ends, if `done_plans` is non-empty, the framework:

- turns it into `prompt.action_results`
- injects `extra_instruction`
- writes raw records into `extra.tool_logs`

If no execution record exists, nothing is injected.
