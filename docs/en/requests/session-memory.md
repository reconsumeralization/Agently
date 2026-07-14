---
title: Session Memory
description: How Session attaches to agents, records multi-turn history, bounds the context window, and imports / exports state.
keywords: Agently, session, activate_session, chat history, memo, context window
---

# Session Memory

> Languages: **English** · [中文](../../cn/requests/session-memory.md)

Session is Agently's multi-turn conversation container. It keeps the full conversation history (`full_context`) and the window that is actually injected into the next request (`context_window`). The default strategy only bounds length; if you need summarization, long-term preferences, or more specialized pruning, register your own analysis / resize handlers.

## Enable and disable

```python
from agently import Agently

agent = Agently.create_agent()
agent.activate_session(session_id="support-demo")

agent.input("Remember this: my order id is A-100.").start()
reply = agent.input("What is my order id?").start()

agent.deactivate_session()
```

`activate_session(session_id=...)` creates or reuses the `Session` for that id and writes `runtime.session_id` into the agent settings. Use `deactivate_session()` to stop injecting session chat history into future requests.

## Workspace-backed long-term memory

For durable memory, attach a `SessionMemory` plugin. The built-in sample plugin
is `AgentlyMemory`; it stores records in Workspace and injects retrieved memory
into the next request.

```python
from agently.core import Session

workspace = Agently.create_workspace("./support-memory")

session = Session()
session.use_memory(mode="AgentlyMemory", workspace=workspace)
```

Agent-created sessions can bind the agent Workspace automatically:

```python
agent = Agently.create_agent()
agent.use_workspace("./support-memory")
agent.activate_session(session_id="support-demo")

agent.activated_session.use_memory(mode="AgentlyMemory")
```

The configured path is the ordinary Workspace root; applications should not
bind `.agently` itself as a Workspace. Merely creating a Session, activating it,
or binding a Workspace creates no private state. The first record-backed memory
write or query lazily creates `.agently/workspace.db`. Vector providers remain
unmaterialized in the default record-only mode. Enable them only when memory
really needs vector writes or queries:

```python
agent.set_settings("session.memory.AgentlyMemory.vector_index.enabled", True)
```

`vector_index.enabled=True` makes extracted memory records request vector
indexing; a real vector operation is what materializes the configured embedding
and vector providers.

`AgentlyMemory` writes memory records with:

- `collection="memory"`
- `kind="global_memory"` for `GLOBAL_MEMORY`
- `kind="session_memory"` for `SESSION_MEMORY`
- fixed `provenance`, `tags`, `memory_scope`, and optional `vector_index`
  metadata

`GLOBAL_MEMORY` is shared inside one Workspace. `SESSION_MEMORY` is also scoped
by `runtime.session_id`. A standalone `Session` must pass `workspace=...`; if a
Workspace-backed memory plugin needs storage and no Workspace is available,
Agently raises a clear error.

The memory body shape and model prompts are configurable under
`session.memory.AgentlyMemory.*`. Prompt overrides use Configure-Prompt-shaped
`.execution` blocks:

```python
agent.set_settings(
    "session.memory.AgentlyMemory.body_schema",
    {
        "preference": "string",
        "project": "string",
        "evidence": "short string",
    },
)

agent.set_settings(
    "session.memory.AgentlyMemory.extract.execution.instruct",
    "Extract durable user preferences and project facts only.",
)
```

The model owns extraction, compression, retrieval-query planning, and rerank
judgment. Deterministic code only validates shape, applies Workspace filters,
stores records, and enforces budgets. For small memory scopes,
`AgentlyMemory` skips rerank when candidate count is below
`session.memory.AgentlyMemory.retrieve.rerank_min_candidates` (default `2`) and
records `memory_rerank_skipped` diagnostics. If rerank fails after retry,
retrieval falls back to deterministic candidates and records diagnostics.
`AgentlyMemory` also keeps memory usable when rerank drops every candidate in a
memory scope: it reruns that scope without rerank, injects the deterministic
memory package, and records `memory_rerank_empty_fallback` diagnostics. Set
`session.memory.AgentlyMemory.retrieve.keep_candidates_on_empty_rerank=False` to
disable that safeguard.

## Chat history methods

With a session active, these agent methods proxy to the current session:

```python
agent.set_chat_history([
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hi, I'm an Agently assistant."},
])

agent.add_chat_history({"role": "user", "content": "Continue the same topic."})
agent.reset_chat_history()
```

The current session is available as `agent.activated_session`. Without an active session, these methods fall back to the ordinary agent prompt chat-history behavior.

## Default window policy

The built-in setting is:

```python
agent.set_settings("session.max_length", 12000)
```

When the approximate text length of `context_window` exceeds `session.max_length`, the default handler runs `simple_cut`: it keeps the newest messages that fit. If even the newest message is too long, it keeps the tail of that message.

This is an approximate character count over serialized messages, not exact token accounting.

## Choose what gets recorded

By default, after a request finishes, Session appends the rendered prompt text as user content and the result data as assistant content. To record only specific paths:

```python
agent.set_settings("session.input_keys", ["info.task", "input.question"])
agent.set_settings("session.reply_keys", ["answer", "score"])
```

`session.input_keys` is resolved from prompt data; `session.reply_keys` is resolved from parsed result data. Set them back to `None` for the default recording behavior.

## Custom resize / memo

The built-in Session does not automatically call a model to summarize history. `memo` is a serializable field that your resize handler can update:

```python
def analysis_handler(full_context, context_window, memo, session_settings):
    if len(context_window) > 6:
        return "keep_last_four"
    return None


def keep_last_four(full_context, context_window, memo, session_settings):
    new_memo = {
        "previous_turns": len(full_context) - 4,
        "note": "Older turns were summarized by application code.",
    }
    return None, list(context_window[-4:]), new_memo


agent.register_session_analysis_handler(analysis_handler)
agent.register_session_resize_handler("keep_last_four", keep_last_four)
```

Use resize handlers for chat-window policy and the `memo` field. Use
`session.use_memory(...)` when the memory should be durable Workspace records.

## Import / export

```python
from agently.core import Session

session = agent.activated_session
json_text = session.get_json_session()
yaml_text = session.get_yaml_session()

restored = Session(settings=agent.settings)
restored.load_json_session(json_text)

agent.sessions[restored.id] = restored
agent.activate_session(session_id=restored.id)
```

Aliases also exist: `session.to_json()` / `session.to_yaml()`, and `session.load_json(...)` / `session.load_yaml(...)`.

## Boundaries

Session owns multi-turn chat history, the current context window, an optional memo field, memory-plugin attachment, and import / export. Workspace owns durable storage and retrieval. `SessionMemory` plugins own memory strategy. Agently does not add a cross-Workspace user profile in V1.

## See also

- [Context Engineering](context-engineering.md) — when to use session, prompt info, or KB
- [Knowledge Base](../knowledge/knowledge-base.md) — retrieval-backed context
- [Prompt Management](prompt-management.md) — how chat history enters a request
