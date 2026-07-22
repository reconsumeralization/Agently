---
title: Project Framework
description: Topology-first, concise project layouts for Agently applications and services.
keywords: Agently, project layout, topology, TriggerFlow, Dynamic Task, FastAPI, FastMCP
---

# Project Framework

A project tree is an implementation result, not the starting design. Plan the
real owners and consumers first, then create the smallest file layout that
carries those boundaries.

For a complete runnable example, see the Agently-Skills
[`skills/agently/assets/project-template`](https://github.com/AgentEra/Agently-Skills/tree/main/skills/agently/assets/project-template)
asset. It intentionally demonstrates several optional boundaries at once. Copy
it selectively; it is not a mandatory scaffold for a small application.

## Plan topology before files

For every non-trivial linear, branching, concurrent, or looped model
application, record four ledgers before creating modules:

1. **Owner and invariant ledger**: which model, host, Action, flow, storage,
   transport, or human owner makes each decision, and what must remain true.
2. **Planned node ledger**: each logical ModelRequest or host stage, its input,
   exact output schema, evidence boundary, lifecycle, and split reason.
3. **Planned edge ledger**: each value, state, signal, effect, and user
   projection, including its producer, validation or transformation, and
   consumer.
4. **Production-necessity ledger**: why every node and field exists, who
   consumes it, its visibility and retention, failure behavior, and whether a
   claimed quality benefit is hypothetical, observed, or A/B verified.

A node in the plan is not automatically a Python file. Several small host
validations can stay beside their owning Chunk; one reusable external contract
may deserve a module. Choose files from ownership boundaries, not diagram-box
count.

Runtime graphs, events, traces, and artifacts validate the planned topology;
they do not replace planning. A graph can show activation without proving that
the right field reached its consumer.

## Start with the minimum honest layout

### One request family

```text
project/
├── app.py
├── SETTINGS.yaml
├── prompts/
│   └── request.yaml
└── tests/
```

Keep the request in `app.py` when it is small and has one consumer. Add a
request module only when it owns a reusable Prompt/output contract or meaningful
host validation. Do not add TriggerFlow, `services/`, `domain/`, `tools/`, or
empty packages for appearance.

Use the model for semantic work such as intent recognition, routing, planning,
trade-offs, response generation, and quality judgment. Keep schema and enum
validation, trusted-key membership, authorization, hard policy, lifecycle,
canonical identity reconstruction, and side effects host-owned. Model
participation does not require a separate ModelRequest for every semantic step.

### Stable multi-stage workflow

```text
project/
├── app.py
├── SETTINGS.yaml
├── TOPOLOGY.md
├── prompts/
├── workflows/
│   ├── main_flow.py
│   └── chunks/
└── tests/
```

Developer-owned stable topology belongs to TriggerFlow. `TOPOLOGY.md` carries
the four ledgers; `main_flow.py` owns graph and execution lifecycle; each
justified Chunk owns one independently observable business stage. Do not add a
separate join Chunk when `for_each(...).end_for_each()` already returns the
joined list and no transformation, partial-failure, or policy boundary exists.

Map ordered and independent work before implementation. Use async APIs and
bounded concurrency for independent stages. Serial execution is appropriate
only for a real data dependency, ordering rule, side-effect safety boundary, or
external capacity limit. Put pressure controls with their real owners: host
admission, TriggerFlow execution, `batch`/`for_each`, model scheduling, client
pools, or blocking-code thread pools.

### Submitted or model-generated DAG

```text
project/
├── app.py
├── SETTINGS.yaml
├── task_dag/
│   ├── contracts.py
│   ├── handlers.py
│   └── runtime.py
└── tests/
```

When a plan is runtime data, use TaskDAG / Dynamic Task. Validate and resolve
the DAG through the TaskDAG path, then let `TaskDAGExecutor.async_run(...)` use
the TriggerFlow substrate. Do not compile unvalidated submitted or
model-generated plan data directly into new TriggerFlow definitions. Blocks is
an explicit opt-in only when Blocks lifecycle evidence or
`ExecutionBlockGraph` output is required.

## Add delivery adapters only when needed

An application that exposes both HTTP and MCP can add:

```text
services/
├── contracts.py      # shared approved public projection, when actually shared
├── api.py            # direct FastAPI inbound adapter
└── mcp_server.py     # direct FastMCP server adapter
```

Both transports should validate admission, issue host-owned task identity, call
the same async application entry point, and return the same approved public
projection. They must not become workflow-policy owners.

```python
from uuid import uuid4


# services/api.py
@app.post("/analysis", response_model=AnalysisResponse)
async def analyze(request: AnalysisRequest) -> AnalysisResponse:
    task_id = f"analysis-{uuid4().hex}"
    run = await run_analysis(
        request.question,
        task_id=task_id,
        max_concurrency=request.max_concurrency,
    )
    return project_analysis_run(task_id, run)


# services/mcp_server.py
@mcp.tool
async def analyze_with_mcp(
    question: str,
    max_concurrency: int = 4,
) -> dict[str, object]:
    request = AnalysisRequest(
        question=question,
        max_concurrency=max_concurrency,
    )
    task_id = f"analysis-{uuid4().hex}"
    run = await run_analysis(
        request.question,
        task_id=task_id,
        max_concurrency=request.max_concurrency,
    )
    return project_analysis_run(task_id, run).model_dump()
```

The runnable template contains complete imports, settings lifespans, task-local
paths, and in-process transport tests. The abbreviated example above shows only
the ownership relationship.

`FastAPIHelper` remains available when its packaged task/stream protocol is the
public contract you want. It is not deprecated, but direct FastAPI is the
default for an ordinary typed HTTP route. MCP client consumption already
belongs to Agently Action management; do not add another local MCP-client
service or a forwarding-only registration wrapper.

## Keep Prompt and output contracts explicit

Keep stable Prompt contracts in YAML or JSON when they evolve independently:

- `input`: current runtime facts;
- `info`: authoritative facts, API/schema documentation, signatures,
  docstrings, evidence, and offered key sets;
- `instruct`: transformation and call rules;
- `output`: the exact machine-consumable result.

Describe every downstream-consumed field with type, meaning, requiredness,
enum or format, range, nullability, and cross-field constraints. Host code must
still validate the result before an external call or side effect.

When the model selects a host record, give it one trusted selection key and
only task-relevant facts. Validate that key against the offered set and rebuild
canonical ids and metadata in host code. Do not make the model copy UUIDs,
multiple ids, URLs, or unrelated metadata.

Do not request or store hidden chain-of-thought. A bounded task-specific process
field is acceptable only when its semantic role, evidence boundary, type and
bounds, consumer, visibility, retention, failure behavior, and quality-evidence
status are explicit. A generic unconsumed `reasoning`, `analysis`, or `thinking`
field is not a quality mechanism.

## Keep related information local

Co-locate information that changes together and serves the same consumer. For
both people and coding agents, minimize the cross-file lookup count and nesting
depth required to understand one request, invariant, or side effect. Do not
move a one-use schema, constant, helper, or class elsewhere merely to make the
local code shorter or the directory tree look formally layered.

This is not a reason to create a god module. Split unrelated responsibilities,
and extract a boundary when it owns real reuse, independent versioning/review,
policy or lifecycle, non-trivial representation translation, or dynamic
composition. Keep an extracted owner directly discoverable from its call site.
Readable cohesion is the goal, not maximum inlining.

## Remove unowned wrappers

Before adding a Service, Manager, Factory, request wrapper, repository facade,
or adapter, require at least one real owner:

- authorization, validation, policy, or safety;
- lifecycle, state, cleanup, retry, concurrency, or transaction scope;
- a stable external contract or non-trivial representation translation;
- multiple consumers of the exact same contract;
- an already released compatibility boundary.

Otherwise inline it. Remove renaming-only functions, forwarding-only managers,
empty packages, unused output nodes, and duplicate facades. Concision is an
ownership and consumer property, not a universal line-count limit.

## Result, state, and evidence boundaries

- Await `async_get_data()` directly when nobody consumes progressive output.
  Never drain an `instant` generator into a no-op loop.
- Treat consumed `instant` fields as provisional; irreversible effects wait for
  the final parsed and validated result.
- Keep per-run data in TriggerFlow execution state. `flow_data` remains shared
  on the flow object even though save/load serializes and replaces its value.
- Use an explicit execution handle for observation, external emit, pause/resume,
  save/load, intervention, cancellation, or host-controlled close.
- Let trace record bounded facts and Eval judge semantic quality. Do not repeat
  full prompts, deltas, secrets, or raw metadata in every event.

Test deterministic contracts first: settings, Prompt/output schema, host
validation, TriggerFlow state and joins, TaskDAG admission, service projection,
and trace allowlists. Mocks prove wiring, not model semantics; executable Prompt
or semantic-behavior changes need explicit criteria and the smallest authorized
representative real-model check.

## See also

- [Settings](settings.md)
- [Prompt Management](../requests/prompt-management.md)
- [Schema as Prompt](../requests/schema-as-prompt.md)
- [TriggerFlow Overview](../triggerflow/overview.md)
- [Dynamic Task](../dynamic-task/README.md)
- [FastAPI Service Exposure](../services/fastapi.md)
