---
title: Agently 4.1.4.2 development notes
description: Breaking TaskContext, TaskWorkspace, RecordStore, and SkillLibrary ownership convergence.
keywords: Agently, 4.1.4.2, TaskContext, TaskWorkspace, RecordStore, SkillLibrary
---

# Agently 4.1.4.2 development notes

4.1.4.2 is a breaking development-line architecture update. The former
combined Workspace and Skills execution ownership is replaced rather than
deprecated through aliases.

## New owner boundaries

- `TaskContext` owns the task information aggregate and source bindings.
- `ContextReader` owns consumer/phase-bound retrieval and progressive
  disclosure into `ContextPackage`.
- `TaskWorkspace` owns task files, containment, mutation policy, readback,
  digests, and file refs.
- `RecordStore` owns records, retrieval indexes, links, checkpoints,
  TriggerFlow snapshots/events, and SessionMemory persistence. The local store
  materializes under `<root>/.agently/records/records.db`.
- `SkillLibrary` owns immutable installed Skill and Skill-pack revisions.
- `AgentExecution` owns task-scoped Skill selection/binding and shares its
  TaskContext with AgentTask.

Removed public/development concepts include `Workspace`, `ContextBuilder`,
`SkillsManager`, the SkillsExecutor plugin/strategy engine,
`skill_activation`, `workspace_operation`, `create_workspace`, and
`use_workspace`.

## Skills application interface

`agent.use_skills(...)`, `agent.require_skills(...)`, and
`agent.use_skills_packs(...)` bind installed Skill revisions directly to an
ordinary AgentExecution. There is no `skills` route.

`Agently.skills_executor` remains a thin compatibility/management facade for
local install, configure, list, inspect, read, context-pack projection, and the
TaskDAG helper. It does not fetch remote sources, infer or grant capabilities,
actionize scripts, select routes, or execute Skill-local strategies.

`agent.run_skills_task(...)` is a result-shaped adapter over ordinary
AgentExecution.

Skill revision availability, concrete ModelRequest-bound context consumption,
and Action execution evidence are separate facts. AgentTask does not expose
Skills as planner capabilities or accept a `skills` execution shape.
`skills.revisions.bound` reports revision binding without claiming activation;
`skills.context.bound` reports actual response-bound context consumption.

## AgentTask and durability

AgentTask planning, observations, verification, and replan state stay in memory
and runtime logs by default. Set
`options={"agent_task": {"record_store_recovery": True}}` only when restart
recovery is required. Final files and their trusted physical readbacks remain
TaskWorkspace artifacts; recovery refs remain RecordStore refs.

When recovery is enabled, AgentTask also snapshots TaskContext entries,
reconstructible built-in sources, ContextReader disclosure state, exact
ContextPackages, and ContextConsumptions. Skill Context resumes by immutable
revision reference; unsupported custom ContextSources fail resume explicitly.

Required TaskWorkspace delivery paths fail closed when the current physical
file cannot be read back. TaskWorkspace readback cannot satisfy a required
Action or Skill binding.

## TriggerFlow and Blocks

TriggerFlow accepts `record_store=...`; `record_store=False` opts out. A
RecordStore may also provide explicit snapshot, runtime-event, lease, and
artifact-ref ports. TriggerFlow does not create a TaskWorkspace.

Blocks retains read-only `context_read` over a caller-bound ContextReader.
Writes and other side effects stay with TaskWorkspace Actions, RecordStore,
ActionRuntime, policy, or host code.

## Migration

```python
agent = (
    Agently.create_agent("review")
    .use_task_workspace("./project", mode="read_only")
    .use_record_store("./project-state", mode="read_write")
)
```

This development-line change intentionally provides no shim for the removed
combined owner. The rollback baseline for this refactor is recorded in the
local development history and spec evidence.
