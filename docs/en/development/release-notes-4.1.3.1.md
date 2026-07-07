---
title: Agently 4.1.3.1 Release Notes
description: Agently 4.1.3.1 release notes for the Workspace foundation and explicit multi-turn task information management.
keywords: Agently, release notes, 4.1.3.1, Workspace, Recall, Action Runtime
---

# Agently 4.1.3.1 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.1.md)

Agently 4.1.3.1 is a foundation release for explicit multi-turn task
information management. It adds a durable Workspace substrate, a Recall context
skeleton, and Action Runtime defaults that let application code store and recover
task information across repeated execution steps.

This release does not introduce autonomous WorkLoop planning. Application code,
TriggerFlow definitions, or ordinary Python loops still decide when to observe,
write records, search, checkpoint, and build context.

## Highlights

- `agent.use_workspace(...)` configures one Workspace with structured records,
  SQLite metadata/FTS, managed content storage, and an editable `files_root`.
- `agent.workspace` exposes `put(...)`, `get(...)`,
  `get_data(...)`, `search(...)`, `link(...)`, `links(...)`,
  `checkpoint(...)`, `latest_checkpoint(...)`, `history(...)`,
  `capabilities()`, and `build_context(...)`; `ingest(...)` is retained as a
  compatibility alias for older code.
- Recall is plugin-shaped through `ContextPlanner`, `Retriever`, and
  `ContextBuilder`; default `auto` and `software_dev` profiles are available.
- `agent.enable_workspace_file_actions(...)` exposes the Workspace file working
  tree as list/search/read/write actions without creating a second Workspace.
- Shell and Node.js helpers inherit `agent.workspace.files_root` when a
  Foundation Workspace is configured.
- `agent.enable_workspace(...)` remains as a compatibility alias and warns in
  favor of `enable_workspace_file_actions(...)`.
- OpenAI-compatible requesters now preserve an explicit `Authorization` header
  even when an `api_key` is also configured.

## Recommended Usage

```python
agent = Agently.create_agent("issue-run").use_workspace("./.agently/runs/issue-123")

record = await agent.workspace.put(
    content={"route": None, "status": "failed"},
    collection="observations",
    kind="route_attempt",
    summary="Provider returned no route candidate",
    scope={"task_id": "issue-123", "area": "routing"},
    source={"type": "workflow", "step": "attempt-1"},
)

await agent.workspace.checkpoint(
    "issue-123",
    {"status": "failed", "evidence": record["id"]},
    step_id="attempt-1",
)

context = await agent.workspace.build_context(
    goal="Prepare the second routing attempt",
    scope={"task_id": "issue-123", "area": "routing"},
    budget={"max_items": 4},
)
```

Expose file actions separately when the model or action layer needs to read or
write files in the Workspace file area:

```python
agent.use_workspace("./.agently/runs/issue-123")
agent.enable_workspace_file_actions(write=True)
agent.enable_shell(commands=["cat", "python"])
```

Action outputs are not automatically memory. Store them explicitly:

```python
result = agent.action.execute_action("inspect_workspace_files", {"cmd": "cat notes/runtime.txt"})
await agent.workspace.put(
    content={"stdout": result["data"]["stdout"]},
    collection="observations",
    kind="action_output",
    summary="Shell inspection output",
    scope={"task_id": "issue-123"},
    source={"type": "action", "name": "inspect_workspace_files"},
)
```

## Examples

- `examples/workspace/workspace_loop_foundation.py` demonstrates an explicit
  TriggerFlow loop that stores observations and decisions, links evidence, writes
  checkpoints, and builds Recall context.
- `examples/workspace/workspace_with_action_output.py` demonstrates writing a
  file through Workspace file actions, reading it through a shell action, then
  explicitly writing the action output into Workspace before building context.

## Compatibility

- Package version: `4.1.3.1`.
- Release manifest: `compatibility/releases/4.1.3.1.json`.
- Recommended `agently-devtools`: `>=0.1.5,<0.2.0`.
- Workspace advanced Recall hardening, vector retrieval, model-assisted recall
  planning, and WorkLoop self-planning remain planned follow-up work.
