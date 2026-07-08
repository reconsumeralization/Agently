---
title: Agently 4.1.4.1 Release Notes
description: Agently 4.1.4.1 release notes for AgentExecutionResult business-data and full-data reader compatibility.
keywords: Agently, release notes, 4.1.4.1, AgentExecutionResult, get_data, get_full_data
---

# Agently 4.1.4.1 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.4.1.md)

Agently 4.1.4.1 is a compatibility release after the 4.1.4 AgentExecution and
TaskBoard release. It restores stable AgentExecution result-reader and facade
usage while keeping the 4.1.4 task-execution baseline.

## AgentExecution Result Views

`AgentExecutionResult.get_data()` now has the same business-result meaning across
direct, flat, and TaskBoard routes. Direct model-request routes keep returning
the ordinary parsed result. Task-strategy routes that return a terminal envelope
with `final_result` expose that `final_result` through `get_data()`, parsed
against the declared `output(...)` contract when possible.

Use `get_full_data()` / `async_get_full_data()` when caller code needs the full
route/task payload, including `status`, `accepted`, `artifact_status`,
`taskboard`, `completion_notes`, diagnostics, or other execution internals.
`get_text()` / `async_get_text()` still read the full payload so task-strategy
`final_response` remains the preferred user-facing final text.

This fixes the previous mismatch where AgentTask-backed executions could make
`get_data()` return the internal terminal envelope while direct executions
returned the business object.

## AgentExecution Facade and Lifecycle

Agent quick-prompt chains such as `agent.input(...).output(...).start()` create
a fresh `AgentExecution` for that expression and no longer reuse stale completed
results in loops.

An explicitly captured `AgentExecution` remains one independent run. Once it has
started, prompt/config mutators such as `input(...)` or `output(...)` now raise
a lifecycle error instead of silently creating a second run from a completed
record. For the next request boundary, create a new execution from
`agent.input(...)`, `agent.create_execution(...)`, or
`execution.create_execution(...)`.

The execution facade now also exposes the basic readers used by early examples:
`get_data_object()`, `get_key_result()`, `wait_keys(...)`,
`when_key(...).start_waiter()`, and `streaming_print()`.
`get_generator(type="specific")` yields the same `(event, data)` tuples as
`ModelRequestResult`, `get_generator(type="instant")` preserves structured
`full_data` snapshots, public delta streams no longer print raw provider
`original_delta` chunks, and `execution.get_prompt_text()` remains available
before and after execution for prompt inspection.

## Public Typing Gate

`compatibility/public-typing-allowlist.json` records the current intentional
`Any` compatibility boundaries for public surfaces. The release gate scans the
listed public surfaces automatically, so new public methods must be fully typed
unless the release adds a reviewed allowlist entry with owner, reason, narrowing
plan, and expiry.

## Release-Pinned Usage Examples

`examples/release_pinned_usage/` now records release-pinned developer usage
checks for public facade shapes that should remain stable across releases. The
selected scripts are listed in
`examples/release_pinned_usage/pinned_usage_manifest.json` and must not be
edited, replaced, or removed without explicit maintainer confirmation for the
release.

## SkillsManager Internal Owner

Skills now have an internal canonical owner named `SkillsManager` for
installation/discovery, progressive context disclosure, capability need
discovery, and policy-gated local Action candidate binding. `SkillsExecutor`
remains a legacy compatibility facade for existing code such as
`Agently.skills_executor` and `agent.run_skills_task(...)`; new user guidance
prefers `agent.use_skills(...)` and AgentExecution Skills selection. Context
packs are built internally for ordinary execution and remain available as an
advanced custom-planner/TaskDAG integration surface.

Local Action resolution can reuse already-mounted non-standard Actions such as
`local_python_runner` when the capability need is unambiguous, then binds final
execution to the exact `action_id`. Ambiguous, low-confidence, policy-denied,
or resource-unavailable matches fail closed with diagnostics.

## Compatibility

- Package version: `4.1.4.1`.
- Release manifest: `compatibility/releases/4.1.4.1.json`.
- Existing task terminal envelope fields are unchanged; callers that depended
  on them should switch from `get_data()` to `get_full_data()`.
- Completed-execution prompt/config chaining now fails fast; new service code
  and examples should treat each request as a new execution.
- Public typing allowlist entries are exception records, not a list of allowed
  public methods.
- Release-pinned usage scripts under `examples/release_pinned_usage/` are
  release gates; usage-shape edits require maintainer confirmation.
- `SkillsManager` is internal. Do not recommend `Agently.skills_manager` as a
  public API; use Agent Skills APIs or the legacy `Agently.skills_executor`
  facade only for compatibility hooks.
