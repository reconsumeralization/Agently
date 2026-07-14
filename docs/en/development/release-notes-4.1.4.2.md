---
title: Agently 4.1.4.2 Development Notes
description: Development-line notes for the breaking Workspace storage and lifecycle simplification.
keywords: Agently, 4.1.4.2, Workspace, AgentTask, TriggerFlow, storage, retention
---

# Agently 4.1.4.2 Development Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.4.2.md)

Agently 4.1.4.2 is the current development target. It contains a breaking
Workspace redesign intended to keep ordinary runs close to zero persistent
overhead while preserving explicit recovery and durable-information use cases.

## Workspace Boundary

- `Workspace(root)` now exposes `root` itself as the ordinary file boundary.
- The default root is the entry script directory, with current working directory
  fallback.
- External files are readable and read-only by default. Use
  `mode="read_write"` or an approved file Action for mutation.
- `.agently` is the reserved private area. Its files, database, records,
  vectors, recovery, memory, and Skills state are created independently and
  only when used.
- New products that cannot be written externally use
  `.agently/files/<execution-id>/...`. There is no public `files_root`, generated
  Workspace guide, or framework-owned artifact-directory taxonomy.

## Terminal Storage

AgentExecution and AgentTask keep the full live process in memory and
observation output rather than duplicating it into Workspace. Terminal cleanup
keeps only selected fallback products whose trusted refs pass physical
readback; drafts, intermediate files, unselected products, and invalid refs are
removed. Ordinary external files are never cleanup targets.

AgentTask restart state is opt-in through:

```python
agent.create_task(
    goal="Prepare the report.",
    options={"agent_task": {"workspace_recovery": True}},
)
```

## TriggerFlow Durability

A TriggerFlow Workspace binding may provide direct file or record access, but
it no longer becomes a RuntimeEvent store automatically. Save, pause, and load
may activate Workspace snapshot recovery when required. Durable RuntimeEvent
replay or Workspace-backed audit must bind `runtime_event_store` explicitly.
Ordinary audit remains the responsibility of logs, EventCenter sinks, or
DevTools.

Large TriggerFlow close results remain available to the in-process caller but
are omitted from the terminal RuntimeEvent projection once they exceed the
bounded inline limit. A large result with selected file products projects only
their compact refs. TriggerFlow does not create a Workspace record solely to
carry that event result.

## State Assignment

`StateData.set(...)` and item assignment now replace the existing target value,
including lists, mappings, sets, and empty collections. Recursive composition
remains explicit through `StateData.update(...)`; list accumulation remains
explicit through `append(...)` / `extend(...)`. TriggerFlow `set_state(...)`,
`async_set_state(...)`, and flow-data setters therefore record the exact new
value instead of retaining stale collection members. This prevents cleared
queues, restored snapshots, and TaskBoard progress mappings from silently
growing across ticks.

## Compatibility

This is a development-line breaking change. Removed interim Workspace layout
APIs are not retained as aliases. The released 4.1.4.1 compatibility manifest
and package version remain unchanged until release preparation starts;
`compatibility/in-development.json` owns the 4.1.4.2 target.

Acceptance experiments and full repository gates remain pending until the
feature branch is accepted.
