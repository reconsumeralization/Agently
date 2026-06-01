---
title: Agently 4.1.3.2 Release Notes
description: Agently 4.1.3.2 release notes for AgentExecution task steps, runtime stall control, and RuntimeEvent delivery.
keywords: Agently, release notes, 4.1.3.2, AgentExecution, RuntimeEvent, Workspace, DevTools
---

# Agently 4.1.3.2 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.2.md)

Agently 4.1.3.2 completes the 4.1.3.2 AgentExecution task-step slice and adds
the runtime visibility and bounded-diagnostics work needed to make those steps
usable in real multi-step applications.

## What Changed

- `agent.create_execution(...)` now supports the existing turn behavior and the
  new bounded `mode="task_step"` shape with explicit `lineage`, `limits`, and
  Workspace injection.
- AgentExecution metadata and stream items now carry execution id, execution
  mode, lineage, diagnostics, route, logs, close snapshots, and workspace
  references for step-to-step correlation.
- Model-request budgets are enforced across direct model calls, DynamicTask
  model tasks, and Skills model stages.
- Runtime stall control now covers whole AgentExecution deadlines,
  no-progress windows, provider stream idle waits, response materialization,
  ActionRuntime stages, and ActionFlow loop close.
- `EventCenter` remains the runtime event hub. It receives `RuntimeEvent`,
  normalizes and delivers it, and applies outlet-level delivery policies such
  as raw delivery, summary delivery, and async background dispatch.
- DevTools keeps its `ObservationEvent` projection while adapting to the new
  RuntimeEvent hierarchy and grouped rendering semantics.
- Release acceptance now requires a coverage-first argument before implementation
  artifacts are cited as proof.

## Usage Shape

```python
workspace = Agently.create_workspace("issue-intake")

execution = agent.create_execution(
    mode="task_step",
    lineage={"task_id": "issue-intake", "step_id": "collect-open-issues"},
    limits={"max_model_requests": 3, "max_seconds": 60, "max_no_progress_seconds": 15},
    workspace=workspace,
)

async for item in execution.async_stream():
    print(item.meta["execution_id"], item.meta["execution_mode"], item.meta["lineage"])

await execution.async_record_workspace(
    kind="checkpoint",
    data={"status": "collected", "source": "official site search"},
)
context = await workspace.async_build_context(query="latest unprocessed issues")
```

`None` is the preferred unlimited budget marker. `-1` remains accepted for
compatibility where numeric settings already use it.

## Examples

- `examples/agent_auto_orchestration/20_agent_execution_task_step_workspace_loop.py`
  runs two task-step executions, passes lineage and limits explicitly, records
  observations and checkpoints into Workspace, rebuilds Workspace context
  between steps, and shows stream/meta correlation from a real model run.
- `examples/agent_auto_orchestration/21_agent_execution_github_issue_intake.py`
  demonstrates a DeepSeek-driven issue intake workflow where the agent receives
  shell capability, decides how to search, can use local `gh` when useful, and
  stores the intake result in Workspace without hard-coding a GitHub Issues API
  path.

## Compatibility

- Package version: `4.1.3.2`.
- Release manifest: `compatibility/releases/4.1.3.2.json`.
- Recommended `agently-devtools`: `>=0.1.6,<0.2.0`.
- Companion Skills guidance is aligned to the runtime, dynamic task, playbook,
  and debugging guidance used by this release.

## Issue Scope

This release closes the runtime visibility and bounded-stall work behind issues
#277 and #280. Broader provider adoption and long-run observation performance
monitoring remain release-line follow-up work rather than 4.1.3.2 blockers.
