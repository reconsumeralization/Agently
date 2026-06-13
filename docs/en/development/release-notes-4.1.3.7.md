---
title: Agently 4.1.3.7 Release Notes
description: Agently 4.1.3.7 release notes for AgentExecution-backed AgentTaskLoop hardening, goal and effort configuration, Skills context-pack DAG support, and release-blocker runtime fixes.
keywords: Agently, release notes, 4.1.3.7, AgentExecution, AgentTaskLoop, SkillsExecutor, ActionRuntime, TriggerFlow, Workspace
---

# Agently 4.1.3.7 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.7.md)

Agently 4.1.3.7 hardens the AgentExecution-backed AgentTaskLoop path. The
release keeps `AgentExecution` as the recommended Agent run owner while making
goal pursuit clearer, easier to validate, and better aligned with Skills,
Actions, DAG-shaped bounded steps, Workspace evidence, and release diagnostics.

This is not a new public `AgentTask` lifecycle and not a TriggerFlow distributed
recovery milestone. Future AdaptiveLoop / BootstrapLoop packaging, multi-task
scheduling, background autonomy, and resumable AgentExecution strategies remain
explicitly deferred.

## Recommended Shape

Use one execution draft and configure Prompt, Skills, Actions, goals, and
effort around that draft:

```python
result = (
    agent
    .use_skills("website-builder", "seo-reviewer")
    .use_actions(write_file, read_file)
    .goal(
        [
            "Build a small product website.",
            "Include brand introduction, product features, target users, and contact information.",
        ],
        success_criteria=[
            "The final artifact is a runnable page file.",
            "The page content covers every supplied business fact.",
            "Execution evidence includes file write, readback, and content inspection.",
        ],
    )
    .effort(
        "medium",
        budget={"iteration_limit": 3, "model_call_limit": 10},
        execution={"step_plan": "auto"},
        verification={"strictness": "normal"},
    )
    .start()
)

data = result.get_data()
task_refs = result.task_refs
meta = result.get_meta()
```

`agent.goal(goal_or_goals, success_criteria=None)` and the plural readability
alias `agent.goals(...)` return task-strategy `AgentExecution` drafts. Detailed
`effort(...)` controls planning depth, budget, verification, replan, progress,
and optional DAG-shaped bounded steps; it does not grant permissions or bypass
host policy.

## Core Changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Goal Pursuit API | Goal and success criteria are one execution-first configuration surface. | `agent.goal([...], success_criteria=[...]).effort(...).start()` | Additive recommended shape; `AgentTask` is still not the primary public lifecycle. | AgentTaskLoop tests, release manifest, docs, examples. |
| Effort and bounded steps | `effort(...)` now carries budget, planning, verification, replan, progress, and `execution.step_plan` guidance. | Use presets for normal tasks; add detailed sections only when the host needs explicit bounds. | `execution.step_plan` guides strategy only; DAG completion is step evidence, not task acceptance. | `tests/test_agent_task_loop.py`, auto-orchestration docs, compatibility manifest. |
| Verification and replan | Goal Pursuit reports configured, planned, executing, evidence, verified, guarded, replan, and terminal phase concepts. | Consume result, stream, task refs, and metadata through `AgentExecutionResult`. | Completion still requires model verifier acceptance plus host guards. | AgentTaskLoop tests and `examples/agent_task/goal_pursuit_acceptance_matrix.py`. |
| Skills context packs | SkillsExecutor exposes task-aware context-pack and TaskDAG resolver support for planner and DAG consumption. | Use `agent.use_skills(...)`; custom planners can call context-pack APIs when needed. | SkillsExecutor remains a capability adapter and does not execute bundled scripts directly. | SkillsExecutor tests, main compatibility metadata, Agently-Skills validation. |
| ActionRuntime blockers | Action metadata redacts execution-environment secrets, action-loop timeout covers the full loop, explicit action-loop replies do not re-enter ActionRuntime, and empty native tool-call plans return diagnostics. | Use current Action APIs and inspect `agent.action.summarize_records(...)` for host-facing records. | Fixes release blockers without changing ActionRuntime ownership. | ActionRuntime tests and compatibility manifest. |
| DevTools companion | DevTools UI, playground, and runtime observation semantics align with `agent_execution` naming. | Release Agently 4.1.3.7 with `agently-devtools` 0.1.9 and keep the main manifest recommendation at `>=0.1.9,<0.2.0`. | Synchronous companion release required; do not publish main with a stale DevTools recommendation. | Main compatibility manifest, DevTools package metadata, DevTools tests. |
| TriggerFlow and Workspace foundation | Current manifests record the landed TriggerFlow runtime integrity and Workspace durable-provider contracts that support this release line. | Treat TriggerFlow recovery and Workspace provider seams as foundation capabilities; do not present future distributed recovery as complete AgentTask work. | Deferred production distributed recovery remains explicit. | TriggerFlow / Workspace tests, examples, and specs. |

## Compatibility

- Package version: `4.1.3.7`.
- Release manifest: `compatibility/releases/4.1.3.7.json`.
- Recommended `agently-devtools`: `>=0.1.9,<0.2.0`.
- `AgentTurn`, `create_turn(...)`, `set_turn_prompt(...)`,
  `set_request_prompt(...)`, `one_turn`, `task_step`, and `task_scope` are not
  current 4.1.3.7 recommended surfaces.
- `agent.resume(task_id)` / `await agent.async_resume(task_id)` resume a
  checkpointed AgentTaskLoop as a task-strategy `AgentExecution`.
  `AgentExecutionResult.resume()` delegates to the same Agent facade when the
  result carries resumable `task_refs`; `resume_task(...)` remains a
  compatibility alias, not the recommended lifecycle.

## Validation Summary

- Static typing and deterministic tests cover AgentExecution chaining,
  AgentTaskLoop verification/replan, progress and snapshot streams, task refs,
  Workspace evidence links, Skills context-pack integration, ActionRuntime
  release blockers, TriggerFlow runtime/resource behavior, Workspace provider
  contracts, and compatibility registry alignment.
- The Goal Pursuit release example
  `examples/agent_task/goal_pursuit_acceptance_matrix.py` records a real local
  Ollama run on 2026-06-12 with an accepted task and a max-iteration partial
  task. Key output includes `accepted.status="completed"`,
  `accepted.artifact_status="accepted"`, `partial.status="max_iterations"`,
  `partial.accepted=false`, and `partial.guard_reasons` containing
  `missing_criteria_present`.
- Agently-Skills guidance was updated for the same 4.1.3.7 usage shape and
  should be validated with the companion validation suite before promotion.

## Deferred Scope

4.1.3.7 deliberately does not complete multi-task scheduling, background
autonomy, distributed lease ownership, full durable pause/resume, production
external storage providers, or TriggerFlow-backed AdaptiveLoop / BootstrapLoop
packaging for AgentTaskLoop. Those remain future architecture work.
