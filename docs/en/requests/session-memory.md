---
title: Session memory
description: Session history and RecordStore-backed long-term memory.
keywords: Agently, Session, memory, RecordStore, AgentlyMemory
---

# Session memory

`Session` owns multi-turn chat history, the active context window, an optional
memo field, memory-plugin attachment, and import/export. It does not own durable
storage or general task context.

The built-in `AgentlyMemory` plugin stores long-term memories in RecordStore and
retrieves relevant candidates before a later request.

```python
agent = Agently.create_agent("support").use_record_store(
    "./support-memory",
    mode="read_write",
)
agent.activate_session(session_id="customer-42")
session = agent.activated_session
assert session is not None
session.use_memory(mode="AgentlyMemory")
```

The local database is materialized lazily at
`./support-memory/.agently/records/records.db`. A TaskWorkspace is unrelated and
is only needed when the task reads or writes files.

`GLOBAL_MEMORY` shares the configured RecordStore search scope.
`SESSION_MEMORY` additionally includes the current session id. Applications
that need user, tenant, or project isolation must set and enforce those scopes
at the RecordStore boundary.

Configure extraction and retrieval under `session.memory.AgentlyMemory.*`:

```python
agent.set_settings(
    "session.memory.AgentlyMemory.body_schema",
    {"project": "string", "preference": "string", "evidence": "short string"},
)
agent.set_settings("session.memory.AgentlyMemory.extract.max_memories", 2)
agent.set_settings(
    "session.memory.AgentlyMemory.retrieve.budget",
    {"chars": 2000, "item_chars": 800, "rerank_candidates": 3},
)
agent.set_settings("record_store.vector_index.enabled", True)
```

Memory extraction, prose relevance, rerank, and summarization are model-owned
semantic work. Host code validates schemas, applies RecordStore filters,
persists accepted records, and enforces budgets. The plugin does not use
keyword tables as the semantic owner.

Use session chat history for immediate conversational continuity. Use
`session.use_memory(...)` for durable RecordStore-backed recall. Use
TaskContext/ContextReader when an execution needs a broader package assembled
from Skills, files, records, and direct task entries.
