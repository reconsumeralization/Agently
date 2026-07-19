---
title: Task context, files, and records
description: TaskContext, ContextReader, TaskWorkspace, and RecordStore ownership boundaries.
keywords: Agently, TaskContext, ContextReader, TaskWorkspace, RecordStore, progressive disclosure
---

# Task context, files, and records

Agently separates four responsibilities that were previously exposed through
one ambiguous Workspace concept.

| Owner | Responsibility | Does not own |
|---|---|---|
| `TaskContext` | Task-scoped information aggregate, direct entries, source bindings, immutable read snapshots | Files, persistence, semantic execution routing |
| `ContextReader` | Consumer/phase-bound retrieval and progressive disclosure; returns `ContextPackage` | Source storage, writes, side effects |
| `TaskWorkspace` | One explicit task file boundary: containment, mutation policy, format-aware readback, digest and file refs | Records, memory, snapshots, Skill selection |
| `RecordStore` | Durable records, retrieval indexes, links, checkpoints, TriggerFlow snapshots/events, memory persistence | Task files, prompt assembly, semantic relevance judgment |

`ContextSource` adapters make source-specific information readable without
moving ownership into `TaskContext`. Built-in adapters cover SkillLibrary,
TaskWorkspace, and RecordStore. Applications may attach their own source.

## File boundary: TaskWorkspace

```python
from agently import Agently, TaskWorkspace

task_workspace = TaskWorkspace("./project", mode="read_only")
agent = Agently.create_agent("repo-review").use_task_workspace(
    "./project",
    mode="read_only",
)
```

The configured path is the ordinary file root. Existing external files remain
read-only unless `mode="read_write"` is selected. When a read-only boundary
needs a new execution product, Agently uses the execution fallback under
`.agently/files/<execution-id>/`; it does not overwrite an existing external
file. TaskWorkspace private locator and content-version identity metadata stays
under its own `.agently` area.

Agents without an explicit path receive isolated defaults under
`<entry-directory>/.agently/task_workspaces/<agent-id>`. Two Agents therefore
do not silently share a task file boundary.

Expose file operations to the model only when the task needs them:

```python
agent.enable_task_workspace_file_actions(
    read=True,
    write=True,
    expose_to_model=True,
)
```

TaskWorkspace produces stable locator and content-version facts for host-side
readback. A short application citation alias such as `[[ref:ref_1]]` is a
request-local display alias, not durable identity. Host code validates it and
maps it back to the canonical reference identity.

## Persistence boundary: RecordStore

```python
from agently.core.storage import RecordStore

record_store = RecordStore("./project-state", mode="read_write")
agent.use_record_store(record_store)

ref = await record_store.put(
    {"status": "verified"},
    collection="observations",
    kind="review_result",
    scope={"task_id": "review-42"},
)
```

The local provider materializes records at
`<root>/.agently/records/records.db`. Binding a RecordStore does not create or
change a TaskWorkspace. `SessionMemory` and TriggerFlow durability use
RecordStore ports.

TriggerFlow can opt out of its default RecordStore view with
`record_store=False`, or bind an explicit store:

```python
execution = flow.create_execution(
    record_store=record_store,
    runtime_resources={"runtime_event_store": record_store},
    auto_close=False,
)
```

AgentTask process state stays in memory and runtime logs by default. Enable
`record_store_recovery` only when restart recovery is required. Recovery refs
are RecordStore refs; final deliverable files remain TaskWorkspace refs.

## Information delivery: TaskContext and ContextReader

```python
from agently.core.context import TaskContext
from agently.core.storage import RecordStoreContextSource
from agently.types.data import ContextBudget, ContextReadIntent

task_context = TaskContext("review-42")
task_context.put(
    role="instruction",
    content="Never modify source files during review.",
    required=True,
)
task_context.attach(
    RecordStoreContextSource(record_store),
    binding_id="review-records",
    scope="task",
)

reader = task_context.reader(
    consumer="review-planner",
    phase="planning",
    budget=ContextBudget(max_chars=6000, max_blocks=12),
)
package = await reader.async_read(
    ContextReadIntent(
        query="What evidence is relevant to the failed review?",
        filters={"source_kinds": ["record_store"]},
    )
)
```

`TaskContext` is the only task-information aggregate and the lifecycle owner.
It creates readers with `task_context.reader(...)` and restores their exported
state with `task_context.restore_reader(...)`; constructing or restoring a
`ContextReader` independently is not supported. The reader is a public,
consumer/phase-bound handle, comparable to an execution handle owned by its
aggregate. `ContextPackage` is the immutable value returned across a request,
AgentTask, Blocks, or persistence boundary; it is not another context owner.

Each reader pins one TaskContext/source revision snapshot. If that snapshot
is already stale before a read, refresh it explicitly or create a new reader.
If candidate listing itself advances a source revision while the TaskContext
structure remains unchanged (for example, a source establishes a lazy read
view), ContextReader optimistically re-pins and recollects once. Repeated or
concurrent mutation still fails closed. Required and explicitly requested blocks
cannot be silently dropped. Optional prose relevance uses an Agently
`ModelRequest` semantic selector when more than one candidate needs judgment;
selection keys are host-issued and validated before canonical records are
reconstructed.

Each source lists a bounded `ContextSourceCandidateWindow`. For the same read
intent, a successful read advances that source's private continuation window;
selector failure, read failure, or a stale cursor does not advance it. The
returned package exposes per-binding `source_coverage` with the source scope,
returned candidate count, exhaustiveness, and whether continuation remains.
Opaque cursors stay private to the reader/source protocol and never enter model
input, Blocks projections, or `ContextPackage`.

Required content remains fail-closed when it cannot fit. A caller that has
explicitly accepted a lossy projection may request
`metadata={"required_overflow": "lossy_digest"}`. Skill sources then return a
bounded `completeness="lossy"` outline with the immutable full ref, ordered
section refs, original size, and omission facts; they do not silently truncate
the authority-bearing instructions. Optional section candidates still use the
semantic selector. A host-only preflight that intentionally wants no optional
selection may additionally set `optional_selection="none"`.

AgentTask carries the same policy in its context budget:

```python
execution = agent.goal(goal, success_criteria=criteria).strategy(
    "taskboard",
    context_budget={
        "chars": 12_000,
        "required_overflow": "lossy_digest",
    },
)
```

Use this only when the Skill or caller accepts lossy disclosure. Otherwise use
a larger/focused consumer or let the required Skill fail before business work.

`source_kinds` is structural source filtering, not semantic routing. Supported
built-in values are `task_workspace` and `record_store`; Skill bindings are
already scoped by the installed revision and execution binding.

AgentTask creates an independent reader/package for each concrete planner,
worker, control-card, and verifier request. A successful response records a
`ContextConsumption` with the exact package, response/request id, phase, and
block ids; failed requests record no consumption and emit no
`skills.context.bound` event. AgentTask meta exposes both `context_packages`
and `context_consumptions` for audit.

## AgentExecution ownership

Every AgentExecution owns one TaskContext and execution-scoped views of the
Agent's TaskWorkspace and RecordStore. AgentTask reuses the exact TaskContext
and TaskWorkspace view handed to it by AgentExecution. Skills are bound as
immutable SkillLibrary revisions through a Skill ContextSource; they do not
create a Skills route or execution engine.

With `record_store_recovery` enabled, the durable snapshot also preserves
TaskContext direct entries, reconstructible built-in source bindings, reader
disclosure history, packages, and consumptions. Skill sources are rebuilt from
their exact immutable `revision_ref`. Custom ContextSources are not
automatically reconstructible and fail resume explicitly instead of disappearing.

Use `execution.async_read_task_context(consumer_id=..., phase=..., intent=...)`
when the ordinary AgentExecution should build the package. `intent` accepts a
query string or `ContextReadIntent`; when omitted, AgentExecution uses its task
target. Blocks accepts an already
bound `context_reader` for its read-only `context_read` block.
