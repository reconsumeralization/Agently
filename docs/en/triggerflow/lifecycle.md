---
title: TriggerFlow Lifecycle
description: Three execution states and the five entry APIs — what each one does and when to choose it.
keywords: Agently, TriggerFlow, lifecycle, seal, close, start, execution, auto_close
---

# Lifecycle

> Languages: **English** · [中文](../../cn/triggerflow/lifecycle.md)

A TriggerFlow execution moves through three states. Five entry APIs let you control how it starts and ends.

## Three states

```text
   open  ──seal()──►  sealed  ──close()──►  closed
    │                                            │
    └─── (auto_close fires after idle timeout) ──┘
```

| State | What it accepts | What still runs |
|---|---|---|
| `open` | new external events (`emit`, `continue_with`) | everything: chunks, runtime stream, registered tasks |
| `sealed` | nothing new from outside | already-accepted events, internal `emit` chains, registered tasks continue to drain |
| `closed` | nothing | runtime stream is closed; close snapshot is frozen |

Key distinction: `seal()` stops external input but lets in-flight work finish. `close()` does seal first, then drains and freezes.

## Five entry APIs

| API | Purpose | Returns |
|---|---|---|
| `flow.start(...)` / `flow.async_start(...)` | hidden-execution sugar; create + start + wait + close | close snapshot |
| `flow.start_execution(...)` / `flow.async_start_execution(...)` | explicit launch; you keep the execution handle | execution |
| `execution.start(...)` / `execution.async_start(...)` | start an execution you already created | close snapshot if `auto_close=True`; execution if `auto_close=False` |
| `execution.seal()` / `execution.async_seal()` | runtime seal | — |
| `execution.close()` / `execution.async_close()` | finalize | close snapshot |

### `flow.start(...)` — hidden sugar

```python
snapshot = await flow.async_start("input value")
```

What it does internally: `create_execution(auto_close=True, auto_close_timeout=0.0)`, start, wait until close, return snapshot.

Rules:

- **`auto_close=False` is illegal here** — raises immediately.
- `wait_for_result=` value is **ignored** with a warning. Return type is fixed to the close snapshot.
- `timeout=` is treated as `auto_close_timeout` — how long to wait after the last activity before auto-closing.
- If your flow uses `pause_for(...)`, do **not** use `flow.start()` — there is no handle for the outside to resume against. TriggerFlow fails fast when hidden execution sugar reaches `pause_for(...)`. Use `flow.start_execution(...)` or `flow.create_execution(...)`.
- Hidden sugar is for finite, self-closing runs when the caller only needs the
  close snapshot. A bounded async request handler may use it. Use an explicit
  execution whenever the host needs external emits, save/load, intervention,
  inspection, cancellation, pause/resume, or control over when close happens.

### `flow.start_execution(...)` — explicit launch

```python
execution = await flow.async_start_execution("input value")
# ... do something with the handle ...
snapshot = await execution.async_close()
```

Returns the execution. You decide when to close. Use it for long-lived or
externally controlled services, SSE/WebSocket streams that need disconnect or
cancellation handling, human-in-the-loop, external `emit()` callers, and any
run whose host needs the execution handle.

`wait_for_result=` is ignored here too.

### `execution.start(...)` — start a pre-built execution

```python
execution = flow.create_execution(auto_close=True)
snapshot = await execution.async_start("input")  # returns close snapshot
```

```python
execution = flow.create_execution(auto_close=False)
exec2 = await execution.async_start("input")  # returns the execution
# ... do work, then ...
snapshot = await execution.async_close()
```

| `auto_close` | `async_start` returns |
|---|---|
| `True` (default) | close snapshot |
| `False` | the execution itself |

Sync `start()` only supports `auto_close=True`. If your execution must be manually closed, use `await execution.async_start(...)` instead.

The value passed to `execution.async_start(value)` is the execution's start
input. It does not emit a custom event named by that value. Attach chunks that
should run from the start boundary with `flow.to(handler, name=...)`. If you
want a custom event such as `"start"`, start the execution and then call
`await execution.async_emit("start", payload)`.

### `execution.seal()` — stop new input, let in-flight finish

```python
await execution.async_seal()
```

After seal:

- New external `emit()` / `continue_with()` calls are rejected.
- Already-accepted events, internal `emit` chains, and registered tasks keep running.
- Runtime stream is **not** closed.
- Close snapshot is **not** frozen yet.

Use seal when you want to stop accepting new work but still finish what's in flight, and you'll close later (or let `auto_close` close it).

### `execution.close()` — finalize and return snapshot

```python
snapshot = await execution.async_close()
```

What close does, in order:

1. seal (if not already sealed)
2. drain pending tasks
3. close the runtime stream
4. freeze and return the close snapshot

`timeout=` on close is the **drain timeout** — the maximum wait for in-flight tasks before forcing the close. It is not the auto-close timer.

## auto_close and auto_close_timeout

`auto_close=True` (the default for `create_execution`) means the execution will close itself after `auto_close_timeout` seconds of being **idle** — no chunks running, no events to process, no pending pause.

| Source | Default `auto_close_timeout` |
|---|---|
| `flow.create_execution(...)` | `10.0` seconds |
| `flow.start(...)` / `flow.async_start(...)` (hidden sugar) | `0.0` seconds (close as soon as idle) |

`pause_for(...)` pauses the auto-close timer. After `continue_with(...)`, the idle timer starts fresh.

`close()` / `async_close()` reject pending interrupts by default. Resume them first, or explicitly cancel them with `pending_interrupts="cancel"` when shutdown should abandon the wait.

For a durable standalone ActionFlow exchange, `execution_exchange.async_respond(...)`
resumes the live execution and closes it after the final interrupt. Use
`execution_exchange.async_abandon(...)` when the host intentionally abandons
the wait. A direct execution close, abandonment, and successful final resume all
trigger the ActionFlow-owned temporary artifact-scope cleanup exactly once.

Close also releases execution-local transient aggregation state such as partial
`when(mode="and")`, `batch`, `collect`, `for_each`, and `match` bookkeeping.
These scratch keys are not part of the durable close snapshot.

`auto_close_timeout=None` disables auto-close — the execution stays alive until you call `close()` explicitly. **Don't combine `auto_close_timeout=None` with hidden sugar** — `flow.start()` would never return.

## Execution snapshot and load

`execution.save()` returns a serializable execution snapshot. For restart-safe
and host-managed recovery paths, that snapshot is the versioned top-level
TriggerFlow recovery contract:

```python
saved = execution.save()
```

The execution snapshot records:

- `schema_version`, `kind`, `snapshot_id`, and `state_version`.
- execution identity, flow name, run context, lifecycle/status, owner, heartbeat,
  and lease fields.
- runtime state, flow data, pending interrupts, intervention ledger,
  sub-flow frames, last signal, and compatible result state.
- `durable_system_state`: TriggerFlow-owned progress that must survive
  open/waiting execution load, such as partial `when(mode="and")`
  aggregation state.
- `resource_requirements`: live resource keys and ExecutionResource
  requirements needed before the restored graph can safely continue.
- `resume_ledger`: accepted `continue_with(..., resume_request_id=...)` requests
  so an external resume retry does not dispatch the graph twice.

Live resource objects are not serialized. `runtime_resources`, managed
ExecutionResource handles, clients, callbacks, and other live objects remain
outside the saved state. `runtime_resources` is only the mount point for live
objects that the host has already created, restored, and validated.

Declare resources that a future resumed chunk will need. TriggerFlow can record
resources that are already mounted, but it cannot infer a resource used only by a
later branch unless you declare it:

```python
flow.declare_resource_requirement("resume_service")
```

Workers that can import the same resource factory can persist a resolver
descriptor instead of passing the live object manually on every restart:

```python
flow.declare_resource_requirement(
    "resume_service",
    resolver="my_app.runtime_resources:create_resume_service",
    provider_kind="approval_router",
    config_ref="settings://approval-service",
    secret_ref="secret://approval-service",
    fail_policy="fail_closed",
)
```

The resolver receives a context dictionary with the resource key, requirement,
execution id, snapshot, and execution handle. It should return the live
resource or an envelope such as `{"resource": service, "health": "healthy"}`.
If the resolver is missing, unhealthy, or policy-forbidden,
`inspect_load(...)` reports typed diagnostics. `fail_policy="fail_open"`
keeps the diagnostic as a warning; the default `fail_closed` blocks strict
`async_load(...)`.

Use `inspect_load(...)` or strict `async_load(...)` before resuming:

```python
saved = execution.save()

report = restored.inspect_load(saved)
assert report["missing_resource_keys"] == ["resume_service"]

await restored.async_load(
    saved,
    runtime_resources={"resume_service": service},
)
await restored.async_continue_with(
    interrupt_id,
    {"approved": True},
    resume_request_id="webhook-42",
    actor="approval-service",
)
```

`async_load(...)` loads the snapshot, restores declared ExecutionResource
requirements, re-ensures managed execution resources, and fails
before graph continuation if required resources are still missing. Use
`load(...)` only when all required resources are already available in the
current process and no async resource preparation is needed. Pass
`validate_resources=True` for the same fail-fast resource check. Use
`async_load(...)` for restart or worker-handoff paths that may need async
resource resolution or managed environment setup before graph continuation.
Production boundaries are covered in [Distributed Pause and Resume Boundaries](distributed-pause-resume.md).

External execution snapshot stores can persist the same snapshot by exposing
`put_snapshot(run_id, state, step_id=...)`. Durable providers can additionally
expose `get_snapshot(run_id)` for reading snapshot state,
`put_snapshot(..., expected_state_version=...)`, lease methods, and
`put_artifact_ref(...)`. A Workspace can be configured directly because it
implements the same snapshot-store port:

```python
execution = flow.create_execution(workspace=agent.workspace)
snapshot_ref = await execution.async_save(step_id="after-approval")
```

For shared task information, prefer a Workspace instance that the application
creates and owns explicitly:

```python
shared_workspace = Agently.create_workspace("./project")
execution = flow.create_execution(workspace=shared_workspace)
```

`flow.create_execution()` binds a lightweight Workspace over the entry-script
directory by default. Every execution sees the same direct ordinary file root;
new files created without external write permission are isolated under
`.agently/files/<execution-id>/`.
Pass `workspace=False` to opt out, or pass a Workspace instance, path, or
backend when the execution should use an explicitly selected Workspace. The
resolved execution-local Workspace facade is available to TriggerFlow chunks as
`runtime_resources["workspace"]` / `data.require_resource("workspace")`.

This binding does not enable RuntimeEvent persistence and does not create
`.agently`. Only real file products, records, recovery, or explicitly configured
event storage materialize private state.

It is a live resource, not serialized state. If a chunk needs an Agent to use
the same explicit information scope, bind that Agent or the single
AgentExecution to the same Workspace in application code. If a flow needs to
move data between two isolated Workspaces, do it explicitly in the flow's
business logic with
Workspace `search(...)`, `get(...)`, `get_data(...)`, `put(...)`, and
`link(...)`. Workspace itself does not provide a cross-space communication
or replication protocol.

You can also pass a store through existing execution resources when a service
does not use `workspace=...`:

```python
execution = flow.create_execution(
    runtime_resources={"snapshot_store": agent.workspace}
)
snapshot_ref = await execution.async_save(step_id="after-approval")
```

To resume from a Workspace-backed snapshot, read the stored snapshot and pass it
back to TriggerFlow's load API:

```python
saved_state = await agent.workspace.get_snapshot(execution.run_context.run_id)
assert saved_state is not None

restored = flow.create_execution(workspace=agent.workspace)
await restored.async_load(saved_state, runtime_resources={"workspace": agent.workspace})
await restored.async_continue_with(
    "approval",
    {"approved": True},
    resume_request_id="approval-webhook-1",
    actor="reviewer",
)
```

This path preserves TriggerFlow-owned pause/resume ledgers, policy-approval
waits, and `when(..., mode="and")` join progress while keeping Workspace as the
snapshot provider.

For a runnable foundation check of this path, use
`examples/trigger_flow/durable_recovery.py`. It writes a Workspace-backed
snapshot, loads it in a fresh execution, resumes with a stable
`resume_request_id`, and proves duplicate callback delivery does not execute the
downstream chunk twice.

For a service-shaped provider replacement example, use
`examples/trigger_flow/fastapi_sqlite_exchange_provider.py`. It keeps the flow
definition in a module-level `discount_approval_flow` object with top-level
`.to(...)` / `.when(...)` wiring, stores top-level execution snapshots in
SQLite, publishes approval requests through a SQLite
`ExecutionExchangeProvider`, and exposes the same start/resume path through
FastAPI.

TriggerFlow carries owner/lease fields in the snapshot and exposes
`claim_lease(...)` / `heartbeat_lease(...)` so a store can index and project
distributed ownership. The store still owns cross-worker atomic writes, lease
enforcement, access control, and conflict handling.
Before `continue_with(...)` accepts a resume request, callbacks delivered to an
execution whose execution-local lease has already expired fail fast without
writing a resume ledger entry; a reclaimed worker should load or claim the
execution first, then process the same stable `resume_request_id`.

Workspace-backed providers expose the corresponding lease port:

```python
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
```

When a service wants to use an execution snapshot for host-managed distributed
recovery, request a fail-closed provider check:

```python
await execution.async_save(
    step_id="after-approval",
    require_distributed_provider=True,
)
```

The selected snapshot provider must report CAS, lease, range-read, and
retention capabilities and expose the matching snapshot, lease, and artifact
methods. The execution must also have a RuntimeEvent store that reports event
sequencing. The local Workspace backend passes this fail-closed provider check
for single-node development and local restart recovery, but it is not a
production cross-worker Redis/Postgres/object-storage backend.

For durable diagnostics, bind a RuntimeEvent store through execution resources:

```python
execution = flow.create_execution(
    runtime_resources={"runtime_event_store": agent.workspace}
)
await execution.async_start(request)
events = await agent.workspace.query_runtime_events(execution.id)
```

TriggerFlow still owns event identity, pause/resume semantics, DAG readiness,
and replay validation. Workspace stores the canonical RuntimeEvent records and
snapshot refs; it does not become a workflow control plane.

Durable RuntimeEvent records include the execution-local sequence,
`state_version`, parent event/signal lineage, aggregation scope, operator id,
interrupt id, resume request id, actor id, lease owner id, snapshot ref, and
artifact refs. `pause_for(...)` writes planned, persisted, and exposed
interrupt phases; `continue_with(..., resume_request_id=...)` writes accepted,
dispatched, completed, and `dispatch_failed` resume phases. Use a stable
`resume_request_id` from the external callback, webhook, or approval request so
retry delivery remains idempotent after restart.

`pause_for(...)` can also persist an ExternalWait template for external
approval, webhook, or exchange-style waits:

```python
await data.async_pause_for(
    type="exchange", exchange_kind="approval",
    payload={"question": "approve?"},
    interrupt_id="approval",
    resume_to="next",
    channel_id="ops-approval",
    provider_id="approval-router",
    wait_mode="connected_then_disconnected",
    hot_wait_timeout=30.0,
    cold_persistence_policy="persist",
    request_payload_schema={"type": "object"},
    response_payload_schema={"type": "object", "required": ["approved"]},
    audit_metadata={"exchange_id": "approval-exchange-1"},
)
```

The template is stored under `interrupt.external_wait_request` in the
execution snapshot. If `audit_metadata.exchange_id` is present, TriggerFlow projects it
onto durable RuntimeEvent records through Workspace or any compatible runtime
event provider that accepts `exchange_id`.

When a host owns an approval router, queue, or exchange transport, bind an
execution-local `execution_exchange_provider`. The provider publishes the same
typed request after the interrupt is persisted and before it is marked exposed;
TriggerFlow remains the lifecycle owner and resumes through
`continue_with(...)`:

```python
class QueueExchangeProvider:
    async def publish_request(self, execution_id, request, *, interrupt):
        ticket = await queue.publish({
            "execution_id": execution_id,
            "request": request,
            "interrupt": interrupt,
        })
        return {
            "exchange_id": ticket["id"],
            "provider_metadata": {"queue": ticket["queue"]},
        }

execution = flow.create_execution(
    runtime_resources={"execution_exchange_provider": QueueExchangeProvider()}
)
```

The provider may return `exchange_id`, `provider_metadata`, and
`audit_metadata`; those fields are merged into
`interrupt.external_wait_request` and projected to durable RuntimeEvent records.
If provider publishing fails, TriggerFlow records `dispatch_state` as
`exposure_failed` and emits `triggerflow.interrupt_exposure_failed`.

The public `execution_exchange` facade is the host-side companion for these
requests:

- register reusable transports with
  `execution_exchange.register_provider("approval-router", provider)`;
- inspect host-renderable cards with
  `execution_exchange.project_pending_exchanges(execution)` or
  `project_execution_exchanges(execution)`;
- for connected ActionFlow / PolicyApproval waits, let the host endpoint resolve
  the live exchange with
  `await execution_exchange.async_respond(exchange_id, {"approved": True})`.

AgentExecution hosts do not need to read raw TriggerFlow interrupts for the UI.
When an ActionFlow run is owned by an AgentExecution, pending and resolved
exchanges are emitted as stream items with path `exchange.pending` or
`exchange.resolved`, `meta.stream_kind == "exchange"`, and a value shaped as
`{"action": "...", "exchanges": [ExecutionExchangeView, ...]}`.

For a minimal provider smoke that does not call a model or external service, see
`examples/step_by_step/11-triggerflow-23_execution_exchange_provider.py`.

For long-running executions, keep large payloads behind provider refs and store
only compaction facts in the execution snapshot by configuring a host-owned
reducer policy. The reducer receives the bounded RuntimeEvent
records selected by TriggerFlow and returns serializable summary facts plus any
large payload that should be stored behind a provider artifact ref:

```python
async def compact_execution_state(context):
    records = context["records"]
    return {
        "summary": f"compacted {len(records)} runtime events",
        "artifact": {"event_ids": [record["event_id"] for record in records]},
        "retained_lineage_anchors": [{
            "anchor_id": "root-after-compaction",
            "sequence": context["sequence_from"],
            "event_id": records[0]["event_id"],
        }],
        "load_read_limit": 100,
    }

execution.set_compaction_policy(
    min_runtime_events=100,
    reducer=compact_execution_state,
    artifact_kind="snapshot_payload",
)
await execution.async_save(step_id="auto-compacted")
```

`inspect_load(...)` reports retained lineage anchor mismatches, missing
required artifact refs, and invalid load read limits as snapshot
diagnostics. TriggerFlow records the execution facts and provider refs; the
Workspace or enterprise provider owns artifact storage, retention anchors, and
bounded runtime-event reads.

`execution.inspect_load(...)` reports typed recovery diagnostics for
invalid snapshots, missing resources, accepted-but-not-dispatched resume
requests, dispatched-but-not-completed resume requests, expired lease warnings,
active lease owner conflicts, DAG join state mismatches, TaskDAG graph
fingerprint mismatch, and durable RuntimeEvent sequence or parent-signal
lineage problems when event records are inspected.

## Picking the right entry

| Situation | Use |
|---|---|
| Finite, self-closing run; all inputs known; caller only needs close snapshot | `flow.start(...)` / `flow.async_start(...)` |
| Service/request path that needs an execution handle, external events, cancellation, or controlled close | `flow.start_execution(...)` |
| Need `pause_for(...)` (human approval, async webhook) | `flow.create_execution(auto_close=False)` + `execution.async_start(...)` + manual `close()` |
| Need to save and resume across restarts | `create_execution(...)` + `execution.save()` / `load()` |

## A quick decision example

```python
# This flow pauses for user input — DO NOT use flow.start()
flow = TriggerFlow(name="approval")
async def ask(data):
    return await data.async_pause_for(type="exchange", exchange_kind="approval", resume_to="next")
async def commit(data):
    await data.async_set_state("approved", data.input)
flow.to(ask).to(commit)

execution = flow.create_execution(auto_close=False)
await execution.async_start(None)
# ... wait for an external system to call execution.async_continue_with(...) ...
snapshot = await execution.async_close()
```

If you'd written `await flow.async_start(None)` instead, TriggerFlow would raise when `pause_for(...)` is reached because the hidden execution has no resumable handle.

If you need to stop a waiting execution without resuming it, make that explicit:

```python
snapshot = await execution.async_close(pending_interrupts="cancel")
```

## Compatibility parameters

| Parameter | Status |
|---|---|
| `wait_for_result=True` / `False` | **value is ignored**, warning emitted; return type is governed by `auto_close` |
| `set_result()` / `get_result()` / `.end()` | deprecated; see [Compatibility](compatibility.md) |
| `runtime_data` (`get_runtime_data` / `set_runtime_data` etc.) | deprecated alias of `state`; see [State and Resources](state-and-resources.md) |

## See also

- [State and Resources](state-and-resources.md) — what makes it into the snapshot
- [Execution Result](execution-result.md) — reading snapshots, state, compatibility results, and metadata through one facade
- [Pause and Resume](pause-and-resume.md) — `pause_for` and `continue_with`
- [Distributed Pause and Resume Boundaries](distributed-pause-resume.md) — host-managed recovery and live object ownership
- [Persistence and Blueprint](persistence-and-blueprint.md) — `save` / `load`
- [Compatibility](compatibility.md) — migration from older APIs
