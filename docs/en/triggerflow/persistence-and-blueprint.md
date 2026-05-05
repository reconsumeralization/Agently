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

`save()` captures everything needed to resume the execution where it stopped:

- the execution's `state`
- lifecycle metadata (status, timestamps, run ids)
- pending interrupt state (if `pause_for(...)` was hit)
- `resource_keys` — the names of runtime resources expected on resume, but not the live values

What it does **not** capture:

- the live `runtime_resources` themselves (they're not serializable; see [State and Resources](state-and-resources.md))
- in-flight chunks (no execution mid-coroutine; save during a settled state)

```python
execution = flow.create_execution(auto_close=False)
await execution.async_start("refund request")

saved_state = execution.save()
# persist saved_state somewhere (Redis, DB, file, etc.)
```

Restore later (possibly in a different process):

```python
restored = flow.create_execution(
    auto_close=False,
    runtime_resources={"db": new_db_client, "logger": new_logger},
)
restored.load(saved_state)

# Continue: emit, continue_with an interrupt, then close
await restored.async_emit("UserFeedback", {"approved": True})
snapshot = await restored.async_close()
```

The flow definition must be the **same flow** (or compatible) on both sides — `load()` doesn't reconstruct the chunk graph from `saved_state`; it expects the flow to already exist.

### Resuming around a pause_for

```python
execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")

# at this point the flow may have called pause_for(...)
saved = execution.save()

# ... days later, in a different worker ...
restored = flow.create_execution(
    auto_close=False,
    runtime_resources={"search_tool": new_search_function},
)
restored.load(saved)

interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(interrupt_id, {"approved": True})
snapshot = await restored.async_close()
```

`get_pending_interrupts()` returns ids of interrupts created via `pause_for(...)`. `continue_with(id, payload)` resumes the corresponding suspended chunk.

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
saved = execution.save()
redis.set(f"flow:{exec_id}", json.dumps(saved))

# later
saved = json.loads(redis.get(f"flow:{exec_id}"))
restored = flow.create_execution(auto_close=False, runtime_resources={...})
restored.load(saved)
```

**Distributed worker pickup**

Pair a blueprint (stored once) with an execution save (stored per execution):

```python
blueprint = source_flow.save_blueprint()
db.save("flow_blueprints", blueprint_id, blueprint)

# in worker
flow = TriggerFlow(name="loaded")
register_all_handlers(flow)            # whatever your registration entry is
flow.load_blueprint(db.load("flow_blueprints", blueprint_id))

execution = flow.create_execution(auto_close=False, runtime_resources=...)
execution.load(saved)
```

## See also

- [Lifecycle](lifecycle.md) — what counts as a "settled" execution to save
- [Pause and Resume](pause-and-resume.md) — `pause_for` / `continue_with`, the most common reason to save
- [State and Resources](state-and-resources.md) — what survives, what must be re-injected
