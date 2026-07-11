---
title: Persistence and Blueprint
description: save / load for execution state, save_blueprint / load_blueprint for flow definitions.
keywords: Agently, TriggerFlow, save, load, blueprint, persistence, durable
---

# Persistence and Blueprint

> Languages: **English** · [中文](../../cn/triggerflow/persistence-and-blueprint.md)

Two distinct serialization paths exist. Don't confuse them.

| Method | What it serializes | Typical use |
|---|---|---|
| `execution.save()` / `execution.load(saved)` | one **execution**'s runtime state at a moment in time | resume across process restarts, hand off to another worker |
| `flow.save_blueprint()` / `flow.load_blueprint(blueprint)` | the **flow definition** structure (chunks, branches, conditions) | distribute or version-control a flow as a config artifact |

## Execution save / load

`save()` captures a restart-safe execution snapshot:

- the execution's `state`
- lifecycle metadata (status, timestamps, run ids)
- pending interrupt state (if `pause_for(...)` was hit)
- a versioned top-level execution snapshot with TriggerFlow system progress,
  interrupt ledger, resume ledger, resource requirements, and a flow definition
  fingerprint
- `resource_keys` and `resource_requirements` — the resources
  expected on resume, but not their live values

What it does **not** capture:

- the live `runtime_resources` themselves (they're not serializable; see [State and Resources](state-and-resources.md))
- in-flight chunks (no execution mid-coroutine; save during a settled state)
- distributed-store ownership. TriggerFlow records lease metadata, while the
  durable store still owns atomic claim / compare-and-set behavior.
- live object state. Stateful sessions, browser pages, process handles, remote
  tasks, and caches need external state refs and provider restore validation.

```python
flow.declare_resource_requirement("approval_service")

execution = flow.create_execution(auto_close=False)
await execution.async_start("refund request")

saved_state = execution.save()
# persist saved_state somewhere (Redis, DB, file, etc.)
```

Restore later (possibly in a different process):

```python
report = flow.create_execution(auto_close=False).inspect_load(
    saved_state,
    runtime_resources={"approval_service": new_approval_service},
)
if not report["ready"]:
    raise RuntimeError(report["diagnostics"])

restored = flow.create_execution(auto_close=False)
await restored.async_load(
    saved_state,
    runtime_resources={"approval_service": new_approval_service},
)

# Continue: emit, continue_with an interrupt, then close.
await restored.async_emit("UserFeedback", {"approved": True})
snapshot = await restored.async_close()
```

The flow definition must be the **same flow** (or compatible) on both sides.
`save()` records `flow_definition_fingerprint`; `inspect_load(...)`
reports `status="invalid_snapshot"` when the snapshot fingerprint is missing
or does not match the current flow definition, and `load(...)` rejects that
snapshot.
`load()` doesn't reconstruct the chunk graph from `saved_state`; it expects the
flow to already exist.

`load(saved_state)` is the synchronous load boundary for snapshots whose
resources are already available in the current process. `async_load(...)` is
the recovery boundary for restart or worker-handoff paths because it validates
missing resources and can rebuild managed execution resources before the
execution is resumed.

### Resuming around a pause_for

```python
flow.declare_resource_requirement("approval_service")

execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")

# at this point the flow may have called pause_for(...)
saved = execution.save()

# ... days later, in a different worker ...
restored = flow.create_execution(auto_close=False)
await restored.async_load(
    saved,
    runtime_resources={"approval_service": new_approval_service},
)

interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(
    interrupt_id,
    {"approved": True},
    resume_request_id="approval-webhook-42",
)
snapshot = await restored.async_close()
```

`get_pending_interrupts()` returns ids of interrupts created via `pause_for(...)`. `continue_with(id, payload)` resolves one interrupt and resumes the graph according to that interrupt's `resume_to` target.
Use a stable `resume_request_id` for webhook, queue, or approval callbacks so a
duplicate delivery can be replayed without dispatching the same resume twice.

### Snapshot stores

`execution.async_save(store, ...)` writes the current snapshot to any
store that exposes `put_snapshot(run_id, state, *, step_id=None)`.
A durable snapshot store can also expose `get_snapshot(run_id)` that returns
the snapshot state to pass into `load(...)` / `async_load(...)`. TriggerFlow
supplies the snapshot contract; the production store owns durable retention,
atomic claim, lease enforcement, and conflict handling.

```python
execution.claim_lease("worker-a", lease_ttl=30)
await execution.async_save(store, run_id=execution.id)

saved = await store.get_snapshot(execution.id)
assert saved is not None
restored = flow.create_execution(auto_close=False)
await restored.async_load(
    saved,
    runtime_resources={"approval_service": approval_service},
)
```

The execution snapshot is intentionally resource-key based. Serializable
resource requirements can be persisted and inspected, but clients, callbacks,
tasks, semaphores, and coroutine frames must be recreated by the recovery host.

## Flow blueprint save / load

A blueprint serializes the **structure** of the flow — chunk references, branches, conditions — but not the chunk function bodies (those stay in code).

```python
def upper(data):
    return str(data.input).upper()

def store(data):
    return data.async_set_state("output", data.input)

source = TriggerFlow(name="source")
source.register_chunk_handler(upper)
source.register_chunk_handler(store)
source.to(upper).to(store)

blueprint = source.save_blueprint()  # dict, can be JSON / YAML serialized
```

Restore on the other end:

```python
restored = TriggerFlow(name="restored")
restored.register_chunk_handler(upper)   # same function bodies must be available
restored.register_chunk_handler(store)
restored.load_blueprint(blueprint)
```

Key constraint: any chunk used in the blueprint must be **registered by the same handler name** on the restored side. Without `register_chunk_handler(...)`, the loader can't bind names to functions and the load fails.

## Recommended service packaging

For service code, prefer this packaging shape:

1. Put the full flow definition in an importable module: a module-level
   `TriggerFlow(...)`, module-level chunks, and module-level `.to(...)` /
   `.when(...)` wiring.
2. Import that flow object from the service layer and start executions from it.
3. Create live dependencies in host-owned factories or importable resolvers.
4. Mount already-created live objects through flow or execution
   `runtime_resources` only as the final process-local attachment step.
5. Store per-request business data in execution `state`, not in `flow_data`.

```python
# my_app/policy_flow.py
from agently import TriggerFlow


policy_flow = TriggerFlow(name="policy")


@policy_flow.chunk
async def analyze(data):
    agent_factory = data.require_resource("agent_factory")
    prompts_path = data.require_resource("prompts_path")
    question = data.input
    await data.async_set_state("question", question, emit=False)
    agent = agent_factory()
    return agent.load_yaml_prompt(
        prompts_path,
        prompt_key_path="analyze",
        mappings={"question": question},
    ).start()


@policy_flow.chunk
async def answer(data):
    policy_doc = data.require_resource("policy_doc")
    question = data.get_state("question")
    response = f"{policy_doc}\n\nQ: {question}"
    await data.async_set_state("answer", response, emit=False)
    await data.async_emit("POLICY_ANSWERED", {"question": question})
    return response


@policy_flow.chunk
async def audit_answer(data):
    await data.async_set_state(
        "audit",
        {"event": data.event, "question": data.value["question"]},
        emit=False,
    )


policy_flow.to(analyze).to(answer)
policy_flow.when("POLICY_ANSWERED").to(audit_answer)
```

```python
# my_app/api.py
from my_app.policy_flow import policy_flow


snapshot = await policy_flow.async_start(
    "travel subsidy?",
    runtime_resources={
        "agent_factory": make_agent,
        "prompts_path": PROMPTS_DIR / "policy.yaml",
        "policy_doc": tenant_policy_doc,
    },
)
```

This keeps the complete workflow visible in one module. Normal Python imports
execute that module body once per process for the same module name, so repeated
`from my_app.policy_flow import policy_flow` calls do not repeat the `.to(...)`
or `.when(...)` wiring. TriggerFlow's duplicate-definition protection is a
second line of defense for cases where application code explicitly runs the
same wiring again on the same flow object. If the same function is used as two
logical stages, disambiguate the stages with `name=...`.

Use `async_start(...)` for finite request/response workflows. If the flow can
pause, wait for an external callback, or be saved and loaded later, create an
explicit execution with `flow.create_execution(auto_close=False)` so the service
can save the snapshot and resume through the execution handle.

For model-generated or application-submitted To-Do/DAG data at runtime, use
TaskDAG / DynamicTask so the graph is validated before execution. Do not compile
that runtime data directly into new TriggerFlow definitions. TriggerFlow remains
the execution substrate under `TaskDAGExecutor`; stable reusable workflow
templates owned in trusted application source code remain module-level
TriggerFlow definitions. Keep each submitted graph and its results scoped to
the owning TaskDAG run, not shared `flow_data` or mutations of the main flow.

### When to use blueprints

- Authoring flows declaratively in YAML / JSON config and loading them at startup.
- Versioning flow structure separately from handler code.
- Distributing a flow to multiple workers that already have the chunk implementations.

### When **not** to use blueprints

- For one-off scripts. Just write the flow in Python.
- For sharing flows with consumers that don't have the handler code. Blueprints are not self-contained.

## save vs save_blueprint side-by-side

```text
Flow definition (chunks, branches, conditions)
        │
        ├── save_blueprint()  →  dict describing graph structure
        │
        ▼
   create_execution()  ────►  one Execution
                                  │
                                  ├── save()  →  dict describing this execution's state
                                  │
                                  ▼
                              async_close() → close snapshot
```

Both paths return JSON-friendly dicts. Pick storage (Redis, Postgres, S3, file) at the application level — the framework doesn't ship a backend.

## Practical patterns

**Single-server resume**

```python
flow.declare_resource_requirement("approval_service")

saved = execution.save()
redis.set(f"flow:{exec_id}", json.dumps(saved))

# later
saved = json.loads(redis.get(f"flow:{exec_id}"))
restored = flow.create_execution(auto_close=False)
await restored.async_load(
    saved,
    runtime_resources={"approval_service": approval_service},
)
```

**Distributed worker pickup**

Pair a blueprint (stored once) with an execution snapshot (stored per
execution). The durable store should atomically assign ownership before a worker
loads and continues the execution:

```python
blueprint = source_flow.save_blueprint()
db.save("flow_blueprints", blueprint_id, blueprint)

# in worker
saved = await snapshot_store.claim(run_id, owner_id=worker_id)
# claim(...) is application/provider-defined; it should return the claimed
# snapshot state or a ref that the worker resolves before async_load(...).

flow = TriggerFlow(name="loaded")
register_all_handlers(flow)            # whatever your registration entry is
flow.load_blueprint(db.load("flow_blueprints", blueprint_id))

execution = flow.create_execution(auto_close=False)
await execution.async_load(
    saved,
    runtime_resources=runtime_resources_for(saved),
)
execution.claim_lease(worker_id, lease_ttl=30)
```

The snapshot gives TriggerFlow the graph state. The blueprint or imported module
gives the target process the same graph definition. `runtime_resources_for(...)`
must only mount live objects that the host has already created, restored, and
validated. Leases, store-level compare-and-set, external wait outbox ordering,
and stateful live-object restore are provider or host concerns.

## See also

- [Lifecycle](lifecycle.md) — what counts as a "settled" execution to save
- [Pause and Resume](pause-and-resume.md) — `pause_for` / `continue_with`, the most common reason to save
- [Distributed Pause and Resume Boundaries](distributed-pause-resume.md) — host-managed recovery and live object ownership
- [State and Resources](state-and-resources.md) — what survives, what must be re-injected
