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

## Built-in actions and legacy tools

A few common capabilities are shipped as built-in action packages:

- **Search** — web search wrappers
- **Browse** — page fetch and readable-content extraction
- **Cmd** — low-level restricted shell execution

Use the action-native import path for new code:

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15, backend="auto"))
agent.use_actions(Browse())
```

`Search(...)` registers `search`, `search_news`, `search_wikipedia`, and
`search_arxiv`. `Browse(...)` registers `browse`. Implementations live under
`agently.builtins.actions`. `agently.builtins.tools` remains a thin legacy import
facade for existing examples and applications; it may add old `tool_info_list`
metadata, but it should not own built-in capability implementation. `agent.use_tools(...)`,
`agent.tool_func`, and `Agently.tool` remain supported compatibility surfaces.
Do not use `tool_info_list` / `BuiltInTool` as the new authoring API for built-in
capabilities.

By default, `Browse()` tries Jina Reader first, then Playwright, BS4, and
restricted curl. Jina Reader sends the target public URL to `https://r.jina.ai/`
and consumes its URL-to-Markdown output; when the primary Reader endpoint has a
transport or service failure, Browse tries the official alternate endpoint
`https://r.jinaai.cn/` before falling back to local backends. If the external
Reader service boundary is not acceptable for an application, disable it with
`Browse(enable_jina_reader=False, fallback_order=("playwright", "bs4", "curl"))`.

Search is backed by the `ddgs` package. Keep `backend="auto"` for the default
strategy, or pass a specific ddgs backend such as `yahoo`, `brave`,
`duckduckgo`, `google`, `startpage`, `mojeek`, `wikipedia`, or `yandex`.
HTTP 200 from a backend does not guarantee parsed search results; when a backend
returns no usable result, Search falls back through configured/default ddgs
backends. A true no-result search returns `[]` as a successful action result
instead of failing the action loop.

When an earlier backend fails but a later fallback returns usable results, the
Action result uses `status="partial_success"` with `success=True` and backend
diagnostics. Treat that as usable evidence plus observability, not as an
`action.failed` terminal condition.

Search and Browse accept explicit `proxy=` and `timeout=` configuration on the
package object. They also retry transient transport failures once by default
(`max_attempts=2`, `retry_backoff_seconds=0.25`). This covers short network
disconnects such as incomplete chunked reads, timeouts, connection resets, and
proxy handshakes; it is not a substitute for a long-term unavailable network or
an unreachable proxy.
Browse's default backend order is Jina Reader -> Playwright -> BS4 -> restricted
curl. The curl backend only receives normalized URL candidates from Browse and
is not exposed as a shell action. The Jina Reader backend receives only the
target URL and Reader-specific headers; it is a third-party page-reading
backend, not a Search replacement. Browse automatically continues past obvious
Reader failures such as transport errors, HTTP 5xx, empty content, or clear
block/captcha/error pages, but it does not make semantic judgments about whether
ambiguous page text is task-sufficient.

When an Agent-level language policy is set with `agent.language("zh-CN")`,
registered Search/Browse packages receive compatible locale defaults unless the
package was configured explicitly. Search derives its provider-specific default
`region` inside the Search package; Browse uses the policy as an
`Accept-Language` header. The policy is guidance for query/source recall and
process text, not a replacement for task-specific source requirements.

Browse preserves direct `Browse.browse(url)` compatibility by returning text,
but the registered `browse` Action uses structured results. If all Browse
backends fail, the Action result is `status="error"` with backend diagnostics
rather than a successful `"Can not browse ..."` text artifact.

The registered Browse Action also owns basic URL recovery and remote-file
handoff. Bare domains and same-host `http` / `https` candidates are tried before
the action gives up, and structured results include the selected URL, retry
candidates, canonical links, same-site links, attempts, and any security
downgrade diagnostic. When Browse receives a PDF, Office file, image, or other
download-like binary response and the current execution has a bound TaskWorkspace,
it materializes the bytes into `downloads/` and returns file refs plus bounded
`read_file` preview. Browse does not parse the document itself; TaskWorkspace file
IO handlers own that later read. Without a bound TaskWorkspace, remote-file Browse
fails closed instead of sending raw bytes into the model hot path.

For shell access, prefer `agent.enable_shell(...)`, which mounts a managed
`run_bash` action. `Cmd` remains available as a low-level compatibility package
and as an implementation helper for Bash execution. Use shell for tests,
builds, git inspection, and read-only diagnostics; use TaskWorkspace file actions
such as `read_file`, `grep_files`, `edit_file`, and `apply_patch` for file
reading, searching, editing, and writing.

See `examples/builtin_actions/` for the current action-native examples.
Historical built-in tool examples live under `examples/archived/builtin_tools/`
and point back to the current replacements.

## See also

- [Action Runtime](action-runtime.md) — the preferred surface with full architecture
- [MCP](mcp.md) — `agent.use_mcp(...)` details
- [Coding Agents](../development/coding-agents.md) — coding-agent guidance for projects using built-in search/browse and custom actions
