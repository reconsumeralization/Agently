---
title: Workspace
description: Durable Workspace records for multi-turn task information management.
keywords: Agently, Workspace, records, artifacts, checkpoints, multi-turn tasks
---

# Workspace

Workspace is the durable information boundary for multi-turn tasks. Use it when
task information should survive across turns without being copied into prompts,
Session history, or compact execution state.

Workspace V1 is a foundation API. It stores and indexes records; it does not
decide what the model should remember or what step to run next.

```python
agent = (
    Agently.create_agent("repo-worker")
    .use_workspace("./.agently/runs/issue-123")
)

ref = await agent.workspace.ingest(
    content=pytest_output,
    collection="observations",
    kind="test_output",
    summary="pytest failed in route fallback test",
    scope={"task_id": "issue-123", "turn": 1},
    source={"type": "command", "name": "pytest"},
)

records = await agent.workspace.search(
    "route fallback",
    filters={"collection": "observations", "kind": "test_output"},
)

context_pack = await agent.workspace.build_context(
    goal="Fix the route fallback failure.",
    scope={"task_id": "issue-123"},
    budget={"tokens": 12000},
    profile="software_dev",
)
```

## What It Stores

Use records for observations, decisions, artifacts, and compact checkpoints.
Large command output, generated reports, transcripts, and patches should be
stored as Workspace content and represented in runtime state by record refs.

```python
checkpoint_ref = await agent.workspace.checkpoint(
    "issue-123",
    {"phase": "debugging", "refs": [ref]},
    step_id="run-tests",
)

state = await agent.workspace.get_data(checkpoint_ref)
latest = await agent.workspace.latest_checkpoint("issue-123")
history = await agent.workspace.checkpoint_history("issue-123")
```

`get(...)` reads stored content as text. Use `get_data(...)` when records contain
JSON-compatible structured data such as dicts, lists, or checkpoint state.

## Links And Diagnostics

Links record typed relationships between records and can be queried without
accessing backend storage directly.

```python
decision_ref = await agent.workspace.put(
    {"decision": "Patch route fallback"},
    collection="decisions",
    kind="loop_decision",
    scope={"task_id": "issue-123"},
)

await agent.workspace.link(decision_ref, ref, relation="responds_to")
links = await agent.workspace.links(source=decision_ref, relation="responds_to")

capabilities = agent.workspace.capabilities()
```

`capabilities()` reports the active backend components for content, metadata,
checkpoint, text index, policy, and vector index. This is useful when replacing
the local backend with plugins.

## Action Boundary

`agent.use_workspace(...)` also defines `agent.workspace.content_root`.
Filesystem-like action helpers inherit that boundary when no explicit root or
cwd is passed.

```python
agent.enable_workspace(write=True)
agent.enable_shell(commands=["pwd", "pytest"])
agent.enable_nodejs()
```

Pass an explicit `root=` or `cwd=` when an action must use an independent
directory.

## Not A Memory Strategy

Workspace V1 intentionally does not expose model-callable memory verbs such as
`remember(...)`, `observe(...)`, or `decide(...)`. Those are higher-level
affordances for future Action, Recall, or WorkLoop layers. In V1, application
code decides what to write, and the Recall skeleton packages stored records into
a `ContextPack` through pluggable planner, retriever, and context-builder
profiles.

## Plugin Seams

Workspace exposes low-level backend seams for content, metadata, checkpoints,
text index, policy, and vector index. The default local backend is filesystem
content plus SQLite metadata/FTS and `NoopVectorIndex`. Recall exposes
`RecallPlanner`, `Retriever`, and `ContextBuilder`; advanced model-assisted
planning, vector retrieval, reranking, and compression are expected to arrive as
plugins over this foundation.

See `examples/workspace/workspace_loop_foundation.py` for an explicit
TriggerFlow loop that stores structured observations, links decisions to
evidence, checkpoints compact state, and recalls a ContextPack.
