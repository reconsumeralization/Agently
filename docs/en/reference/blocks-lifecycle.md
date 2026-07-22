---
title: Blocks lifecycle
description: Current Blocks plan, execution, context-read, result, and evidence contracts.
keywords: Agently, Blocks, ExecutionPlan, ExecutionBlockGraph, context_read
---

# Blocks lifecycle

Blocks lowers a validated `ExecutionPlan` into an `ExecutionBlockGraph`, binds
that graph to TriggerFlow, and maps terminal state into result/evidence views.
It is an execution substrate, not a Skill engine or persistence manager.

## Current block ownership

- `model_request`: one model-owned semantic request;
- `action_call`, `mcp_tool_call`, `script_action`: explicit capability calls;
- `context_read`: one bounded read from a caller-bound `ContextReader`;
- `validation`: host or model validation at an explicit boundary;
- `flow_segment`: a trusted developer-owned subflow;
- join/control blocks defined by the current plan contracts.

There is no `skill_activation` block. AgentExecution binds SkillLibrary
revisions into TaskContext before Blocks is involved. There is no
`workspace_operation` block. File mutation belongs to TaskWorkspace Actions;
record mutation belongs to RecordStore; approval belongs to the owning policy
and runtime.

## Context read

```python
task_context = TaskContext("refund-review")
task_context.put(
    role="instruction",
    content="Refunds above USD 1000 require finance approval.",
    source_ref="policy/refunds",
    required=True,
)
reader = task_context.reader(
    consumer="blocks:refund-review",
    phase="execution",
)

graph = Agently.blocks.compile({
    "plan_id": "refund-context",
    "plan_blocks": [{
        "id": "policy",
        "plan_block_id": "context_read",
        "kind": "context_read",
        "intent": "Read the refund policy",
        "bound_inputs": {
            "operation": "read",
            "query": "refund approval",
            "explicit_refs": ["policy/refunds"],
        },
    }],
})
execution = Agently.blocks.bind_runtime(graph).create_execution(
    record_store=False,
    runtime_resources={"context_reader": reader},
)
```

`context_read` accepts only `read`, `search`, and `scoped_search`. It returns the
ContextPackage projection, locator refs, bounded evidence snippets, per-source
coverage, and a compact `continuation_available` fact. Source cursors remain
private to the TaskContext-owned reader and are never exposed by the block.
Writes, links, checkpoints, and other side effects fail closed.

Source-specific filters such as `source_kinds`, `path`, `pattern`, `method`, and
`top_n` are forwarded to the bound reader. They constrain candidate collection;
they do not replace semantic relevance selection.

## Result and evidence

`Agently.blocks.map_result(...)` projects semantic terminal outputs.
`Agently.blocks.map_evidence(...)` preserves block results, action evidence,
context refs/snippets, diagnostics, and graph lineage. Host code should use
these factual records for analysis; it must not treat a runner boolean as the
final semantic judgment.

See `examples/blocks/01_blocks_lifecycle_infrastructure_smoke.py` for a runnable
TaskContext-to-`context_read` example.
