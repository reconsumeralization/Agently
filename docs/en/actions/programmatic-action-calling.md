---
title: Programmatic Action Calling
description: Use the programmatic ActionRuntime planning protocol for bounded, data-dependent read-only Action work.
keywords: Agently, Programmatic Action Calling, PTC, ActionRuntime, programmatic, TriggerFlow, TaskDAG
---

# Programmatic Action Calling

> Languages: **English** · [中文](../../cn/actions/programmatic-action-calling.md)

Programmatic Action Calling (PTC) lets a model write one bounded Python program
for an Action round. The program can call eligible Actions, branch on their
results, loop over data, and return a small projection. Intermediate Action
values stay inside the program instead of being copied into every model round.

PTC is the short product name. The public API value is `programmatic`:

```python
agent.set_action_loop(
    planning_protocol="programmatic",
    max_rounds=4,
)
```

This changes the ActionRuntime planning protocol. It is not an
`AgentExecution` strategy, a new workflow engine, or a synonym for TaskDAG.
`structured_plan` remains the default.

You can override the configured protocol for one explicit Action run:

```python
turn = agent.input("Compare the eligible records with their current limits.")
records = await agent.async_get_action_result(
    prompt=turn.request.prompt,
    planning_protocol="programmatic",
)
```

The per-call value wins over the Agent setting. Use the exact value
`"programmatic"`; there is no `"ptc"` or `"code_mode"` compatibility alias.

Low-level callers that use `generate_action_call(...)` only to inspect a
program decision must release an unexecuted call so its host-bound catalog
lease cannot accumulate:

```python
turn = agent.input("Inspect one program decision without executing it.")
calls = await agent.async_generate_action_call(
    prompt=turn.request.prompt,
    planning_protocol="programmatic",
)
try:
    print(calls)
finally:
    agent.release_programmatic_action_calls(calls)
```

The normal `get_action_result(...)` / AgentExecution path settles this lease
automatically.

## When to use it

Programmatic planning is a good fit when one bounded request needs:

- three or more dependent read calls;
- a branch whose next Action depends on an earlier result;
- a bounded loop or fan-out over records;
- local filtering, grouping, joining, sorting, or aggregation; or
- a small final projection from large intermediate Action values.

Prefer `structured_plan` or `native_tool_calls` for one or two small direct
calls. The code-runtime startup and program-generation work usually adds no
value in that case.

### Observed concurrent DSv4 Flash sample

The runnable
[`4_4_programmatic_vs_structured_deepseek.py`](../../../examples/action_runtime/4_4_programmatic_vs_structured_deepseek.py)
holds the task, output schema, source data, and read Action families constant,
declares independent Actions as parallel, and records real peak host Action
concurrency. It defaults to `deepseek-v4-flash` with thinking disabled.

A bounded 2026-09-02 acceptance sample ran one structured/PTC pair for fan-out,
large reduction, and scoped recovery:

| Observed fact | `structured_plan` | `programmatic` |
|---|---:|---:|
| Exact business results | 1 / 3 | 3 / 3 |
| Fan-out model requests | 4 | 3 |
| Recovery model requests | 5 | 3 |
| Fan-out peak Action concurrency | 4 | 3 |
| Recovery peak Action concurrency | 3 | 3 |

PTC returned the exact fan-out and large-reduction projections where the
sampled structured route did not. Its scoped-recovery program called fallback
only for the failed id after the parallel primary cohort settled. However, PTC
used more prompt/total tokens and elapsed time in every pair. Treat business
completion, model rounds, tokens, and latency as separate measurements.

This is one paired observation per workload, not a statistical superiority
claim. Use PTC for bounded runtime control and exact local computation when
that value justifies the SDK/Docker overhead; do not treat it as an automatic
optimization for every multi-call task.

## Current eligibility boundary

The current protocol generates the body of one Python 3.10+ async function. Its return value must
use the lossless JSON data model.

Programmatic mode does not expose every registered Action. It includes only an
Action that is:

- visible in the current Action run scope and `expose_to_model=True`;
- declared with `side_effect_level="read"`;
- declared `replay_safe=True`;
- not statically approval-required; and
- equipped with an explicit return contract that can be represented as JSON.

For `@agent.action_func`, use a precise Python return annotation. For an
executor-backed registration, pass the equivalent `returns=` contract. An
Action without a return contract is excluded with diagnostics; Agently does
not treat it as an implicit `Any` result.

Write and exec Actions, installation, payment, publication, deletion, and
approval-pending work remain outside PTC V1. Keep those operations as ordinary,
graph-visible Actions with their own approval and evidence boundaries.

## Execution and safety

One programmatic round follows this boundary:

```text
ModelRequest creates a bounded Python program
  -> reserved run_action_program Action
  -> isolated, binding-capable code ExecutionResource
  -> each nested call re-enters ActionRuntime and ActionDispatcher
  -> bounded program return/logs enter the next model round
```

The model does not call `run_action_program` directly and applications should
not register that reserved Action id. Every nested call still passes the
registered schema, host policy, resource, timeout, result-normalization, and
Action-evidence checks. Program code never receives credentials, policy
overrides, canonical Action call ids, or live host objects.

PTC requires a binding-capable `code_execution` provider that satisfies
required isolation. It fails closed when no eligible provider is ready and
never silently falls back to `trusted_local`. The program process has no direct
network or host-environment access; a registered Action may still use an
explicitly authorized network or managed resource through its normal executor.
On POSIX hosts, the built-in Docker provider and its gVisor variant support the
host-binding bridge; provider probes still have to verify the required
capabilities before each resource becomes eligible.

Nested Actions are exclusive by default. A host may opt an independently safe
Action into overlap when registering it:

```python
agent.register_action(
    name="lookup_record",
    desc="Read one independent record.",
    kwargs={"record_id": (str, "Record id")},
    func=lookup_record,
    returns={"record_id": (str, "Record id")},
    concurrency_mode="parallel",
)
```

The generated SDK carries the exact `concurrency_mode`. A program may use
`asyncio.gather(...)` for independent calls. The host overlaps only Actions
declared `parallel`, up to `action.programmatic.max_parallel_subcalls`; an
`exclusive` Action waits for earlier work, runs alone, and blocks later starts
until it settles. `read` and `replay_safe` alone never imply parallel safety.

Only the program's bounded `print(...)` output and JSON-compatible return value
form the outer Action result. Nested Action records remain canonical evidence
and observation data, while their complete values stay outside later model-hot
context unless the program deliberately returns a bounded projection.

## PTC versus TaskDAG and TriggerFlow

PTC can replace a fine-grained Action DAG whose only purpose is short-lived
tool control and local data processing. It does not replace a graph that owns
business lifecycle.

| Need | Choose |
|---|---|
| Data-dependent read calls inside one bounded Action round | `programmatic` Action planning |
| A submitted/model-generated DAG that must be validated before execution | TaskDAG / DynamicTask |
| Stable application-owned stages, branches, fan-out, joins, or intervention | TriggerFlow |
| Approval, external wait, persistence, partial rerun, compensation, or restart-safe recovery | TriggerFlow / TaskDAG macro stages |

The common composition is a coarse TriggerFlow or TaskDAG with one bounded PTC
segment inside a node, followed by host validation and any irreversible Action
as separate graph-visible stages.

`DAGActionFlow` remains supported. Programmatic mode does not deprecate it,
TaskDAG, DynamicTask, or `TriggerFlowActionFlow`.

## Recovery and partial results

A running Python interpreter, awaited binding, and provider IPC channel are
live resources, not serializable workflow state. TriggerFlow may save before a
program starts and after it settles, but it cannot resume the live interpreter
from the middle of the program.

If approval becomes required or policy changes during a nested call, Agently
records the ordinary subcall result and settles the current program. Any
durable approval belongs outside that settled boundary; after resume, start a
new program decision against the current Action catalog and policy.

Completed read calls are not rolled back when later program code fails. Treat
the returned records and bounded diagnostics as partial execution evidence, not
as automatic business acceptance.

## See also

- [Action Runtime](action-runtime.md) — Action registration, planning, dispatch, and evidence
- [ExecutionResource](execution-environment.md) — managed code-runtime selection and isolation
- [TaskDAG / Dynamic Task](../dynamic-task/README.md) — validated DAG data
- [TriggerFlow Overview](../triggerflow/overview.md) — durable application-owned orchestration
