---
title: Tools
description: The compat tool surface — use_tool, use_tools, use_mcp, use_sandbox, tool_func.
keywords: Agently, tools, use_tool, use_tools, use_mcp, use_sandbox, tool_func
---

# Tools

> Languages: **English** · [中文](../../cn/actions/tools.md)

The tool family is Agently's **compatibility surface** for letting a model call functions, MCP servers, and sandboxes. New code should prefer the action surface — see [Action Runtime](action-runtime.md). The tool family still works, maps cleanly into the new runtime, and is documented here for users who already have it in their code.

## Surface map

| Old (compat) | New (preferred) | What it does |
|---|---|---|
| `@agent.tool_func` | `@agent.action_func` | mark a function and derive its schema |
| `agent.use_tool(my_func)` | `agent.use_actions(my_func)` | register one |
| `agent.use_tools([a, b])` | `agent.use_actions([a, b])` | register many |
| `agent.use_mcp(url)` | `agent.use_mcp(url)` | unchanged — MCP mounting |
| `agent.use_sandbox(...)` | `agent.use_sandbox(...)` | unchanged — sandbox mounting |
| `extra.tool_logs` | `extra.action_logs` | call records produced by the loop |
| `Agently.tool` | `Agently.action` | global registry helper |

Both columns route into the same internal action runtime. The old names are not implementations of a separate `ToolManager` plugin; they're aliases for convenience.

## Minimal example

```python
from agently import Agently

agent = Agently.create_agent()


@agent.tool_func
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


agent.use_tool(add)

result = agent.input("What is 3333 + 6666?").start()
print(result)
```

The model sees `add` as a callable tool and decides whether to invoke it.

## Auto-func — model-backed implementation

The `@agent.auto_func` decorator turns a function signature + docstring into a model-backed implementation that uses the agent's tools / actions:

```python
@agent.auto_func
def calculate(formula: str) -> int:
    """Compute {formula}. MUST USE ACTIONS to ensure the answer is correct."""
    ...


print(calculate("3333+6666=?"))
```

The decorated function has no body (`...`). At call time, the agent runs the model with the tools registered, and returns the result.

## When to use which surface

For greenfield code: use the **action** surface (see [Action Runtime](action-runtime.md)). It's where extensions, plugin types, and architectural improvements happen.

Stay on the **tool** surface when:

- You're maintaining existing code that uses these names.
- A library or sample you're integrating uses them.

The tool family is not going away — but new features land on the action side first.

## Built-in tools

A few common capabilities are shipped as built-in tools:

- **Search** — web search wrappers
- **Browse** — page fetch and summarization
- **Cmd** — restricted shell execution

Find them under `examples/builtin_tools/` and `agently/builtins/...`. They illustrate how the tool surface composes into real agents.

## See also

- [Action Runtime](action-runtime.md) — the preferred surface with full architecture
- [MCP](mcp.md) — `agent.use_mcp(...)` details
- [Coding Agents](../development/coding-agents.md) — coding-agent guidance for projects using built-in search/browse and custom actions
