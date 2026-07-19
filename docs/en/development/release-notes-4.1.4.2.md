---
title: Agently 4.1.4.2 development notes
description: Breaking TaskContext, TaskWorkspace, RecordStore, and SkillLibrary ownership convergence.
keywords: Agently, 4.1.4.2, TaskContext, TaskWorkspace, RecordStore, SkillLibrary
---

# Agently 4.1.4.2 development notes

4.1.4.2 is a breaking development-line architecture update. The former
combined Workspace and Skills execution ownership is replaced rather than
deprecated through aliases.

## New owner boundaries

- `TaskContext` owns the task information aggregate, source bindings, one
  internal derived ContextIndex lifecycle, and its read handles.
- `ContextReader` is a public consumer/phase-bound handle created or restored
  only by TaskContext. It performs progressive retrieval into immutable
  `ContextPackage` values; it is not a second aggregate owner.
- `TaskWorkspace` owns task files, containment, mutation policy, readback,
  digests, and file refs.
- `RecordStore` owns records, retrieval indexes, links, checkpoints,
  TriggerFlow snapshots/events, and SessionMemory persistence. The local store
  materializes under `<root>/.agently/records/records.db`.
- `SessionMemory` owns memory extraction/compression and accepted RecordStore
  writes; active recall joins task information through a `session_memory`
  ContextSource instead of a parallel prompt-injection pipeline.
- `SkillLibrary` owns immutable installed Skill and Skill-pack revisions.
- `AgentExecution` owns task-scoped Skill selection/binding and shares its
  TaskContext with AgentTask.

Removed public/development concepts include `Workspace`, `ContextBuilder`,
`SkillsManager`, the SkillsExecutor plugin/strategy engine,
`skill_activation`, `workspace_operation`, `create_workspace`, and
`use_workspace`.

## Skills application interface

`agent.use_skills(...)`, `agent.require_skills(...)`, and
`agent.use_skills_packs(...)` bind installed Skill revisions directly to an
ordinary AgentExecution. There is no `skills` route.

`Agently.skills_executor` remains a thin compatibility/management facade for
install, configure, list, inspect, read, context-pack projection, and the
TaskDAG helper. Authorized Git or local source snapshots are materialized by a
`SkillSourceProvider` before immutable SkillLibrary installation. It does not
infer capabilities, select routes, or execute Skill-local strategies.
Remote compatibility installation defaults to `untrusted`, and selected
Git/local subpaths reject symlink components that escape the materialized
source root.

An explicitly authorized script from a trusted exact Skill revision may be
bound as an ordinary `code_execution` Action. The Skill layer supplies only
revision/path/digest identity; ActionRuntime, TaskWorkspace, language adapters,
and ExecutionResource retain execution ownership.

`agent.run_skills_task(...)` is a result-shaped adapter over ordinary
AgentExecution.

Skill revision availability, concrete ModelRequest-bound context consumption,
and Action execution evidence are separate facts. AgentTask does not expose
Skills as planner capabilities or accept a `skills` execution shape.
`skills.revisions.bound` reports revision binding without claiming activation;
`skills.context.bound` reports actual response-bound context consumption.

## AgentTask and durability

AgentTask planning, observations, verification, and replan state stay in memory
and runtime logs by default. Set
`options={"agent_task": {"record_store_recovery": True}}` only when restart
recovery is required. Final files and their trusted physical readbacks remain
TaskWorkspace artifacts; recovery refs remain RecordStore refs.

When recovery is enabled, AgentTask also snapshots TaskContext entries,
reconstructible built-in sources, ContextReader disclosure state, exact
ContextPackages, and ContextConsumptions. Skill Context resumes by immutable
revision reference; unsupported custom ContextSources fail resume explicitly.
ContextSources now expose structural descriptor enumeration and bounded exact
readback. TaskContext's internal ContextIndex builds revision/profile/provider-
keyed structural, lexical, or optional hybrid partitions; ContextReader owns
consumer-local query offsets, exact source reads, semantic selection and
ContextPackage construction. `source_kinds` is the open vocabulary of sources
actually attached to the TaskContext, not a hard-coded framework list.

Required TaskWorkspace delivery paths are represented by digest-pinned staged
candidates during terminal verification. Verifier rejection leaves the prior
target unchanged. Acceptance triggers atomic target promotion and complete
post-promotion readback; any promotion, digest, or readback failure changes the
task to blocked. TaskWorkspace readback cannot satisfy a required Action or
Skill binding.

When strict verification reports missing material evidence,
`replan_segment` now creates an evidence-reacquisition card followed by a
dependent artifact-repair card. The host requires a new stable
`(owner, locator, content_version, range)` source identity before the repair is
scheduled. Re-reading the final artifact or the same unchanged source under a
fresh call id is a setback, not evidence progress.
After reacquisition, another repair is allowed only when a newly acquired
reference is actually cited by the original failed criterion or material-claim
check; unrelated new material cannot keep a replan loop alive.
Dependency readback evidence is canonicalized before a TaskBoard card prompt is
built. Prompt projection, host binding validation, acceptance indexing and
result persistence now share that one live ledger identity domain, so a legal
model-selected reference cannot change merely because the host later rebuilds
an ordered evidence view. Material-claim targets use a host-owned stable exact
claim identity rather than response-local `claim_N` positions.

A control card that explicitly returns `sufficient=false` cannot become
completed through `next_board_action=finalize`. It normalizes to a setback, so
an outline-only manifest cannot create a completed-without-deliverable dead
state.

## Workspace-backed code execution

`agent.enable_code_runtime(...)` supports Python 3.10+, Node.js 18+, Go 1.25+,
and C++20 through provider-neutral adapters. Every run follows
TaskWorkspace grant -> provider binding -> immutable bundle materialization ->
argv execution -> output readback -> release/close. Docker is one provider;
`trusted_local` is an explicit unsafe fallback and never satisfies a hard
isolation requirement. Provider probes report observed toolchain versions and
safety/isolation facts; minimum or exact adapter constraints participate in
ordered selection, and the selected facts are preserved in Action result
metadata.
Isolation probes use concrete boolean capability axes rather than provider
self-labels. Expected outputs are bounded normalized paths under `output/`;
missing outputs, cleanup failure, timeout, and cancellation fail visibly, with
owned processes or containers terminated.

External isolation providers use ordered candidate descriptors. Concrete
gVisor and Seatbelt implementations remain contributor-owned in PR #325 and
#327; this development branch provides their migration contract without
copying either implementation.

## TriggerFlow and Blocks

TriggerFlow accepts `record_store=...`; `record_store=False` opts out. A
RecordStore may also provide explicit snapshot, runtime-event, lease, and
artifact-ref ports. TriggerFlow does not create a TaskWorkspace.

Blocks retains read-only `context_read` over a caller-bound ContextReader.
Writes and other side effects stay with TaskWorkspace Actions, RecordStore,
ActionRuntime, policy, or host code.

## Migration

```python
agent = (
    Agently.create_agent("review")
    .use_task_workspace("./project", mode="read_only")
    .use_record_store("./project-state", mode="read_write")
)
```

This development-line change intentionally provides no shim for the removed
combined owner. The rollback baseline for this refactor is recorded in the
local development history and spec evidence.
