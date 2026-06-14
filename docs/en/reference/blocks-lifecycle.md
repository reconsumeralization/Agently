---
title: Blocks Lifecycle
description: How ExecutionPlan, PlanBlocks, ExecutionBlocks, TriggerFlow, Skills, TaskDAG, and evidence fit together.
keywords: Agently, Blocks, ExecutionPlan, PlanBlock, ExecutionBlock, TriggerFlow, Skills, TaskDAG, evidence
---

# Blocks Lifecycle

Blocks is an internal lifecycle bridge for complex task execution. It is not a
second public task runtime. The outer task lifecycle still belongs to
`AgentExecution` and the AgentTaskLoop strategy, while TriggerFlow remains the
execution substrate.

The lifecycle is:

```text
TaskFrame
-> ExecutionPlan with PlanBlock instances
-> Blocks compiler
-> ExecutionBlockGraph
-> TriggerFlow execution
-> EvidenceEnvelope and ResultAdapter output
-> AgentTaskLoop verification and host guards
```

## Ownership

| Concept | Owner | Meaning |
|---|---|---|
| `ExecutionPlan` | AgentTaskLoop / AgentExecution strategy | One bounded plan segment for the current task frame. |
| `PlanBlock` | Blocks planning catalog | Planner-visible capability specification with inputs, outputs, capability needs, evidence contract, and runtime binding choices. |
| `ExecutionBlock` | Blocks runtime catalog | Trusted runtime block that lowers to one TriggerFlow chunk or a fixed chunk/signal group. |
| `ExecutionBlockGraph` | Blocks compiler output | TriggerFlow-ready lowering artifact, similar to a compiled TaskDAG. |
| `TaskDAG` | TaskDAG modules | DAG data, validation, dependency semantics, and semantic output mapping. |
| `TriggerFlow` | TriggerFlow | Runtime dispatch, signals, joins, concurrency, pause/resume, stream, close snapshot, and recovery. |
| `EvidenceEnvelope` | Blocks mapper / AgentTaskLoop | Runtime facts used by the verifier and deterministic host guards. |

## Skill Activation

Skills are progressive context and capability packages. A `skill_activation`
PlanBlock may load selected `SKILL.md` guidance and resource refs under a
budget, infer capability needs, and recommend downstream PlanBlocks. It does
not execute scripts, grant Actions/MCP/shell/browser access, or prove side
effects.

Use the current facade when application code needs this lower-level view:

```python
activation = Agently.skills_executor.activate_skill(
    "incident-review",
    task="review ticket evidence",
)
```

Side-effect evidence must come from downstream `action_call`,
`workspace_operation`, `approval_wait`, or other concrete execution blocks.

## Direct Skills Compatibility

`agent.run_skills_task(...)` remains the explicit Skills facade, but it is backed
by the same Blocks lowering path. A run builds an internal ExecutionPlan with one
`skill_activation` PlanBlock per selected Skill and one concrete strategy
PlanBlock:

- `single_shot` lowers to a handler-backed `model_request` ExecutionBlock.
- `runtime_chain`, `staged`, `react`, and custom route labels lower to
  handler-backed `flow_segment` ExecutionBlocks.

The resulting `SkillExecution.close_snapshot["blocks"]` contains the
ExecutionPlan, ExecutionBlockGraph, TriggerFlow close snapshot, ResultAdapter
output, and EvidenceEnvelope. Treat old strategy names as compatibility route
labels and diagnostics, not as a separate Skills-owned lifecycle.

## Runtime Blocks

Blocks only run trusted runtime code. Handler-backed blocks such as
`action_call`, `model_request`, `flow_segment`, and `agent_step` require a
runtime handler. `workspace_operation` requires a bound Workspace resource.
`approval_wait` uses the framework PolicyApproval / TriggerFlow pause surface.
`external_wait` uses TriggerFlow pause/resume.

PlanBlock and ExecutionBlock registries validate known block kinds, trusted
runtime binding references, signal contracts, and resource/capability
requirements. Compile also fails closed when plan edges point at missing blocks,
a capability is denied, or a pending capability has no matching `approval_wait`.

When `approval_wait` or `external_wait` opens a TriggerFlow pause, Blocks records
`waiting` evidence for the block. The resume decision remains in the TriggerFlow
interrupt/resume ledger; do not treat a waiting block as terminal task
acceptance.

If a required handler or resource is missing, the block fails closed. If a block
emits a structured `ReplanSignal`, Blocks cancels only the named affected
ExecutionBlocks and their downstream blocks; AgentTaskLoop still owns the next
repair/replan decision.

## TaskDAG Through Blocks

`TaskDAGExecutor.compile_blocks(...)` validates the TaskDAG with the TaskDAG
validator, then lowers validated DAG nodes into an `ExecutionBlockGraph`.
TaskDAG still owns graph validation, dependency result wiring, and semantic
output projection. Blocks do not re-validate the graph or accept task
completion.

```python
result = await TaskDAGExecutor({"local_handler": local_handler}).async_run_blocks(
    graph,
    graph_input={"doc": "policy"},
)
```

Treat the returned result and evidence as input for outer verification, not as
automatic business completion.

## Example

See `examples/blocks/01_blocks_lifecycle_infrastructure_smoke.py` for a
runnable infrastructure-level example. It demonstrates Skill activation,
handler-backed action execution, Workspace evidence, validation, ResultAdapter,
and EvidenceEnvelope without presenting a mock business system as model-owned
success.
