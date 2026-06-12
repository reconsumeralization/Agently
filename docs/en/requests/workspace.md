---
title: Workspace
description: Durable Workspace records for multi-turn task information management.
keywords: Agently, Workspace, records, artifacts, checkpoints, multi-turn tasks
---

# Workspace

Workspace is the durable information boundary for multi-turn tasks. Use it when
task information should survive across turns without being copied into prompts,
Session history, or compact execution state.

Workspace V1 is a foundation API. It stores and indexes records; it does not
decide what the model should remember or what step to run next. Default Agents
and TriggerFlow executions include lazy Workspace bindings, so `agent.workspace`
and `flow.create_execution().require_runtime_resource("workspace")` are
available without setup. The default local backend is materialized only when
code first writes, reads, checkpoints, records evidence, or exposes the
Workspace file area.

```python
agent = Agently.create_agent("repo-worker")

ref = await agent.workspace.ingest(
    content=pytest_output,
    collection="observations",
    kind="test_output",
    summary="pytest failed in route fallback test",
    scope={"task_id": "issue-123", "turn": 1},
    source={"type": "command", "name": "pytest"},
)

records = await agent.workspace.search(
    "route fallback",
    filters={"collection": "observations", "kind": "test_output"},
)

context_pack = await agent.workspace.build_context(
    goal="Fix the route fallback failure.",
    scope={"task_id": "issue-123"},
    budget={"tokens": 12000},
    profile="auto",
)
```

Use `agent.use_workspace(...)` when the application needs a stable explicit
root, read-only mode, or a registered backend provider:

```python
agent.use_workspace("./.agently/runs/issue-123")
```

Standalone Workspaces can be created directly or through the Agently factory:

```python
from agently import Agently, Workspace

shared_workspace = Workspace("./.agently/projects/issue-123")
factory_workspace = Agently.create_workspace("./.agently/projects/issue-124")
```

When several Agents, TriggerFlow executions, or service workers must share task
information, create and manage a shared Workspace explicitly and bind each
consumer to that same Workspace. This is the preferred shape for application
level information sharing because the Workspace remains a durable substrate
instead of an implicit global singleton:

```python
shared_workspace = Agently.create_workspace("./.agently/projects/issue-123")

agent = Agently.create_agent("repo-worker").use_workspace(shared_workspace)
execution = flow.create_execution(workspace=shared_workspace)
```

`flow.create_execution()` creates an execution-scoped lazy Workspace by default.
Pass `workspace=False` to opt out, or pass a Workspace instance, path, or backend
when the execution should use an application-owned shared Workspace.

Do not rely on separate default Workspaces to communicate with each other. If a
TriggerFlow execution needs to move information between isolated Workspaces,
make that transfer explicit in application logic: search or read from the source
Workspace, write or ingest into the destination Workspace, then link the
resulting refs. Workspace does not provide a cross-space messaging or
replication protocol.

## What It Stores

Use records for observations, decisions, artifacts, and compact checkpoints.
Large command output, generated reports, transcripts, and patches should be
stored as Workspace content and represented in runtime state by record refs.

```python
checkpoint_ref = await agent.workspace.checkpoint(
    "issue-123",
    {"phase": "debugging", "refs": [ref]},
    step_id="run-tests",
)

state = await agent.workspace.get_data(checkpoint_ref)
latest = await agent.workspace.latest_checkpoint("issue-123")
history = await agent.workspace.checkpoint_history("issue-123")
```

`get(...)` reads stored content as text. Use `get_data(...)` when records contain
JSON-compatible structured data such as dicts, lists, or checkpoint state.

## Durable Provider Reads

The local Workspace backend also exposes the default durable-provider contract
for single-node development and restart-safe local work. Use stable reference
envelopes when runtime state should carry a compact pointer instead of copying
large content.

```python
ref_envelope = await agent.workspace.ref_envelope(ref)
segment = await agent.workspace.read_bounded(ref, offset=0, limit=4096)

async for chunk in agent.workspace.stream_read(ref, chunk_size=8192):
    process(chunk["content"])
```

`ref_envelope` includes the Workspace id, record id, collection, content ref,
digest, size, creation time, policy labels, and backend capability hints.
`read_bounded(...)` and `stream_read(...)` read large records by segment so
execution state can keep refs while restore code reads only the required
portion.

Runtime events can be stored as durable records when a TriggerFlow or
application execution wants restart diagnostics without using DevTools as the
source of truth:

```python
execution = flow.create_execution(workspace=agent.workspace)
snapshot_ref = await execution.async_save(step_id="review")

event_record = await agent.workspace.append_runtime_event(
    "issue-123-execution",
    {"event_type": "triggerflow.interrupt_raised", "payload": {"id": "approval"}},
    idempotency_key="approval-request-1",
    snapshot_ref=snapshot_ref,
    artifact_refs=[ref],
)

events = await agent.workspace.query_runtime_events(
    "issue-123-execution",
    sequence_from=event_record["sequence"],
)
```

RuntimeEvent storage preserves per-execution sequence, idempotency key,
state version, parent event id, causation id, parent signal id, aggregation
scope, operator id, interrupt id, resume request id, actor id, lease owner id,
snapshot refs, artifact refs, and exchange id. Use `expected_sequence=...`
when a distributed provider needs fail-closed append ordering, and use
`idempotency_key=...` for callback or webhook retry safety. Workspace does not
decide pause/resume, approval, retry, or DAG readiness; those semantics remain
owned by TriggerFlow, PolicyApproval, ExecutionExchange, and AgentExecution.

Workspace-backed durable providers also expose TriggerFlow-facing snapshot CAS,
lease, and artifact-ref helpers:

```python
snapshot_ref = await agent.workspace.put_snapshot(
    execution.run_context.run_id,
    execution.save(),
    expected_state_version=previous_state_version,
)

lease = await agent.workspace.claim_lease(
    execution.run_context.run_id,
    "worker-1",
    ttl=30.0,
    expected_state_version=snapshot_state_version,
)
await agent.workspace.heartbeat_lease(
    execution.run_context.run_id,
    "worker-1",
    lease["lease_token"],
)

artifact_ref = await agent.workspace.put_artifact_ref(
    execution.run_context.run_id,
    large_payload,
    metadata={"kind": "snapshot_payload"},
)
```

`expected_state_version=...` fails closed when the latest checkpoint state
version does not match the caller's read cursor. Lease methods enforce owner
and token checks inside the selected provider. The local backend provides this
single-node durable-provider seam for development and local restart recovery;
production cross-worker guarantees still belong to the selected backend.

## Links And Diagnostics

Links record typed relationships between records and can be queried without
accessing backend storage directly.

```python
decision_ref = await agent.workspace.put(
    {"decision": "Patch route fallback"},
    collection="decisions",
    kind="loop_decision",
    scope={"task_id": "issue-123"},
)

await agent.workspace.link(decision_ref, ref, relation="responds_to")
links = await agent.workspace.links(source=decision_ref, relation="responds_to")

capabilities = agent.workspace.capabilities()
```

`link_evidence(...)` is a convenience wrapper over `link(...)` that records
execution id, operation id, RuntimeEvent id, checkpoint id, exchange id, and
artifact refs in link metadata. Retention anchors can preserve lineage and
summary refs across compaction:

```python
await agent.workspace.link_evidence(
    request_ref,
    result_ref,
    relation="resulted_in",
    execution_id="issue-123-execution",
    runtime_event_id=event_record["event_id"],
    checkpoint_id=checkpoint_ref["id"],
    artifact_refs=[ref],
)

await agent.workspace.add_retention_anchor(
    "issue-123-execution",
    anchor_type="compaction",
    record_ref=ref,
    preserved_event_ids=[event_record["event_id"]],
)
```

`capabilities()` reports the active backend components for content, metadata,
checkpoint, RuntimeEvent storage, ref resolution, retention policy, text index,
policy, and vector index. It also reports capability flags such as
`supports_event_sequence`, `supports_range_read`, `supports_stream_read`,
`supports_retention`, `supports_compaction_anchor`, `supports_cas`,
`supports_lease`, `supports_artifact_refs`, and `supports_remote_backend`.
Distributed recovery should fail closed when the selected provider lacks the
required flags or the matching provider methods.

## Action Boundary

`agent.workspace.files_root` defines an ordinary editable working tree for
shell, Node.js, and file actions. Filesystem-like action helpers inherit that
boundary when no explicit root or cwd is passed, including when the Agent is
still using its lazy default Workspace. `agent.workspace.content_root` remains
the managed record-content store used by Workspace records.

```python
agent.enable_workspace_file_actions(write=True)
agent.enable_shell(commands=["pwd", "pytest"])
agent.enable_nodejs()
```

`enable_workspace_file_actions(...)` does not create a second Workspace. It
exposes list/search/read/write file actions over the current Workspace file
area. Pass an explicit `root=` or `cwd=` only when an action must use an
independent directory.

File boundary policy metadata can be persisted for audit without turning
Workspace into a cwd manager:

```python
await agent.workspace.record_file_policy(
    allowed_roots=[str(agent.workspace.files_root)],
    root_source="workspace",
    policy_labels=["customer-data"],
)
```

## Not A Memory Strategy

Workspace V1 intentionally does not expose model-callable memory verbs such as
`remember(...)`, `observe(...)`, or `decide(...)`. Those are higher-level
affordances for future Action, Recall, or WorkLoop layers. In V1, application
code decides what to write, and the Recall skeleton packages stored records into
a `ContextPack` through pluggable planner, retriever, and context-builder
profiles.

## Plugin Seams

Workspace exposes low-level backend seams for content, metadata, checkpoints,
RuntimeEvent storage, ref resolution, retention, evidence links, text index,
policy, and vector index. The default local backend is filesystem content plus
SQLite metadata/FTS and `NoopVectorIndex`. Recall exposes `RecallPlanner`,
`Retriever`, and `ContextBuilder`; advanced model-assisted planning, vector
retrieval, reranking, compression, and remote backends are expected to arrive as
plugins over this foundation.

Custom backends can be passed directly to `agent.use_workspace(...)` or
registered by name when they implement the Workspace backend protocol:

```python
Agently.workspace.register_backend_provider("audit", build_audit_backend)

agent = (
    Agently.create_agent("repo-worker")
    .use_workspace(
        "tenant-a",
        provider="audit",
        provider_options={"tenant_id": "tenant-a"},
    )
)
```

Provider factories receive `root`, `create`, `mode`, and any
`provider_options`, then return a `WorkspaceBackend`. Unregistered provider
names fail fast instead of falling back to the local backend. If no explicit
provider is selected, the Agent's lazy default Workspace uses the local backend
under `.agently/workspaces/<agent-name>-<agent-id>`. The test suite includes a
protocol-level remote audit provider proof that exercises the same checkpoint,
RuntimeEvent, evidence link, and capability paths as the local backend. That
proof is not a public Redis, Postgres, or object-storage adapter; production
providers must still report their real capabilities and fail closed when
distributed recovery requirements are missing.
TriggerFlow tests also read Workspace-backed execution snapshots through the provider and
load pause/continue, policy-approval waits, and `when(..., mode="and")`
join progress through TriggerFlow, so Workspace remains storage rather than a
workflow control plane.

See `examples/workspace/workspace_loop_foundation.py` for an explicit
TriggerFlow loop that stores structured observations, links decisions to
evidence, checkpoints compact state, and recalls a ContextPack.

See `examples/workspace/workspace_with_action_output.py` for the Action
boundary: a file action writes into `workspace.files_root`, a shell action reads
that file, application code explicitly ingests the action output as a Workspace
observation, and Recall packages it into a ContextPack.
