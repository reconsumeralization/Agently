---
title: Agently 4.1.3.6 Release Notes
description: Agently 4.1.3.6 release notes for AgentExecution ownership, Result-first consumption, OpenAI-compatible stream-end hardening, and the bounded task-loop slice.
keywords: Agently, release notes, 4.1.3.6, AgentExecution, AgentExecutionResult, AgentTaskLoop, ModelResponseResult
---

# Agently 4.1.3.6 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.6.md)

Agently 4.1.3.6 is the AgentExecution ownership release. It consolidates the
recommended public surface around `AgentExecution` and `AgentExecutionResult`,
hardens OpenAI-compatible stream completion, and publishes a narrow
single-task task-loop slice without presenting it as the full future AgentTask
system.

## What Changed

### AgentExecution is the one-run owner

Quick prompt chains now align with the same public ownership model as explicit
executions:

```python
execution = (
    agent
    .input("Classify this customer request.")
    .output({"category": (str, "billing, support, or sales", True)})
)

result = execution.get_result()
data = result.get_data()
meta = result.get_meta()
```

`AgentTurn`, `create_turn(...)`, and `set_turn_prompt(...)` remain
compatibility surfaces for older 4.1.3 examples and migration paths, but they
are no longer the recommended public lifecycle. The request-local prompt draft,
route choice, stream, metadata, and result facade now belong to
`AgentExecution`.

### Result-first consumption is the recommended shape

Use `get_result()` when the same run may be read as text, data, metadata,
stream events, or task refs. Agent quick prompt chains return
`AgentExecutionResult`; direct `ModelRequest` builders return
`ModelResponseResult`.

Result-named stream aliases are now the recommended root-level names. Older
response-named aliases remain compatibility-only under `agently.types.data` and
are deprecated for Agently 4.2 removal.

### Bounded task loops are carried by AgentExecution

`agent.create_task(...)` and `agent.create_task_loop(...)` return task-strategy
`AgentExecution` drafts. The current slice is intentionally narrow:

- one business task
- one Agent owner
- bounded iteration guidance, roughly 2-5 rounds
- explicitly enabled Actions, Skills, or Dynamic Task candidates
- model-owned planning, verification, and replan
- conservative host guards for missing criteria, risky action evidence,
  approval-required actions, and final deliverables

```python
execution = agent.create_task(
    "Prepare a customer-safe incident update from the provided evidence.",
    success_criteria=[
        "Names the customer impact",
        "Separates confirmed facts from unknowns",
        "Lists the next customer-facing action",
    ],
    max_iterations=3,
)

result = execution.get_result()
data = result.get_data()
task_refs = result.task_refs
meta = result.get_meta()
```

`completed` means the model verification accepted the result and host guards
accepted the artifact. Reaching `max_iterations` can still return
`accepted=false` and `artifact_status=partial`. `AgentExecutionResult.resume()`
is reserved and reports `supported=false` until a resumable strategy lands.

### OpenAI-compatible stream completion is hardened

Issue #287 is fixed on the 4.1.3.6 line. Some OpenAI-compatible gateways send
a usage-only final SSE chunk with missing or empty `choices` before `[DONE]`.
`OpenAICompatible.broadcast_response(...)` now preserves the accumulated
content and synthesizes the terminal message instead of indexing into an empty
list.

The same stream-end path now treats `GeneratorExit` as control flow instead of
a model requester error, avoiding a spurious empty `model.requester.error` after
an otherwise successful stream.

### Release guardrails now include foundation examples

The release workflow now requires a Foundation example effect gate for substrate
capabilities touched or claimed by a release, such as ModelRequest/ModelResponse,
TriggerFlow, Dynamic Task/TaskDAG, ActionRuntime, ExecutionEnvironment, and
provider protocols. Tests are not enough on their own: the corresponding core
`examples/` scenario must be run against the release candidate, using real
DeepSeek or local Ollama when model-owned behavior is involved.

## Compatibility

- Package version: `4.1.3.6`.
- Release manifest: `compatibility/releases/4.1.3.6.json`.
- Recommended `agently-devtools`: `>=0.1.8,<0.2.0`.
- `AgentTurn`, `create_turn(...)`, `set_turn_prompt(...)`, and
  `set_request_prompt(...)` remain compatible migration surfaces.
- DevTools display issues #288 and #289 are DevTools-side work and are not
  closed by this Agently package release.

## Validation Summary

- Static typing and tests cover AgentExecution result facades, task-loop result
  refs, stream/meta access, OpenAI-compatible usage-only final chunks, and
  compatibility registry alignment.
- Foundation example effect gate for the touched ModelRequest/ModelResponse
  substrate passed with DeepSeek:
  `python examples/step_by_step/05-response_result.py` produced
  `result_type=ModelResponseResult`, `data_has_definition=True`,
  `meta_has_id=True`, `result_cached=True`, and
  `delta_event_count_positive=True`.
- The 4.1.3.6 AgentExecution use-case example
  `examples/agent_auto_orchestration/22_unified_agent_execution_result.py`
  passed with DeepSeek and produced `quick_result_type=AgentExecutionResult`,
  `quick_category=renewal_risk`, `task_strategy=task_loop`, and
  `task_result_status=completed`.
- Agently-Skills guidance was updated with the same foundation example gate and
  validated with the companion validation suite.

## Deferred Scope

This is not the complete future AgentTask system. Multi-task scheduling,
distributed leases, background autonomy, full durable pause/resume, and
Workspace-as-automatic-memory planning remain deferred. The 4.1.3.7 line should
harden the AgentExecution-carried task-loop strategy without replacing the
public ownership boundary introduced here.
