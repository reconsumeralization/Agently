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

Default local Workspaces are scoped to a longer-lived information domain, not
to each execution. With an active `runtime.session_id`, the physical root is
`.agently/workspaces/sessions/<session-id>`; without a session it is
`.agently/workspaces/scripts/<script-scope>`. Agent, task, and execution
records are logical partitions inside that shared backend, and editable files
use scoped subdirectories under `files/`.

When a local Workspace is materialized, Agently writes an
`AGENTLY_WORKSPACE.md` guide at the physical root and at each scoped editable
`files_root`. The root guide explains `workspace.db`, `workspace.meta.json`,
`content/`, and `files/`; the scoped file guide explains the current lineage and
which directory external agents or Actions may edit. The filename intentionally
is not `README.md` so cloned repositories and task deliverables can keep their
own README semantics.

Scoped `files_root` guides also name the standard editable file areas:

- `downloads/`: remote files materialized by Browse, Actions, or external
  providers before `read_file(...)` / `export_file(...)` handling;
- `artifacts/`: generated supporting artifacts, structured outputs, evidence
  bundles, and non-primary deliverables;
- `reports/`: user-facing readable deliverables, including long, sectioned, or
  file-backed deliverables.

Use `workspace.file_area_path(...)` when framework or application code needs a
contained path inside one of those areas:

```python
download_path = agent.workspace.file_area_path("downloads", "syllabus.pdf")
report_path = agent.workspace.file_area_path("reports", "weekly.md", create=True)
```

Temporary work that should be recovered or cleaned as scratch belongs to
`workspace.open_scratch(...)` or `workspace.scratch_root()`, not a `scratch/`
folder inside `files_root`.

```python
agent = Agently.create_agent("repo-worker")

ref = await agent.workspace.put(
    content=pytest_output,
    collection="observations",
    kind="test_output",
    summary="pytest failed in route fallback test",
    scope={"task_id": "issue-123", "turn": 1},
    source={"type": "command", "name": "pytest"},
)

records = await agent.workspace.grep(
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

`workspace.grep(...)` is deterministic record search. It supports structural
filters such as `collection`, `kind`, record `id`, record `path`,
`scope.<key>`, and `meta.<key>`. Use these filters when the planner or
application already knows the relevant collection, record, path, or task scope;
they narrow retrieval without turning a search hit into semantic acceptance.
`workspace.search(...)` keeps the compatibility return shape of record refs, but
its implementation may automatically use the shared retrieval packaging path
when a broad query produces many candidates. Use `workspace.grep(...)` when the
caller requires the old deterministic candidate list.

For deterministic file search, use `workspace.grep_files(...)`. The
compatibility method `workspace.search_files(...)` keeps returning file-search
hits, but may automatically use retrieval packaging for large candidate pools.
Use `workspace.grep_files(...)` when the caller requires the old deterministic
line/file search result set:

```python
hits = await agent.workspace.grep_files(
    "deadline",
    path="notes",
    pattern="*.md",
    max_results=10,
)
```

## Retrieval

Use `workspace.retrieve(...)` when the caller wants the shared intelligent
retrieval strategy: keyword/tag candidates, optional vector candidates,
structure-gated model rerank, dropped-candidate refill, and budget packaging.

```python
results = await agent.workspace.retrieve(
    query="What should this session remember?",
    tags=["preference", "project"],
    scope={"memory_scope": "SESSION_MEMORY"},
    sources=["records", "files"],
    budget={"chars": 12000},
    selection="length",
)
```

Defaults are conservative:

- candidate source is `DBStoreProvider` keyword/FTS search plus tag retrieval;
- selection uses a character budget (`selection="length"`);
- `selection="top_n"` is available when callers need a fixed number of items;
- retrieval candidate strategy and rerank are separate decisions:
  `method="auto"` chooses the candidate source, while `rerank=None` decides
  whether model rerank is worth the extra request;
- `method="auto"` is the default. It resolves to keyword/tag retrieval unless
  the backend has both an `EmbeddingProvider` and a `VectorStoreProvider`, and Workspace
  retrieval settings express vector preference, for example
  `workspace.retrieval.candidate_strategy="hybrid"`,
  `workspace.retrieval.vector_preferred=True`, or
  `workspace.retrieval.embedding_model="<model-name>"`;
- `method="keyword"`, `method="vector"`, and `method="hybrid"` remain explicit
  caller overrides;
- the default local backend uses `db_store_provider="sqlite"` for the record DB
  and `vector_store_provider="auto"` for vectors: Chroma is selected when
  `chromadb` is importable and initialization succeeds, otherwise Workspace
  falls back to a SQLite vector table in `workspace.db`;
- record DB, embedding, and vector storage are separate providers. A
  lower-capability `DBStoreProvider` keeps the same method surface and returns
  empty/absent values for unsupported advanced features. A vector store may be
  present without an embedding provider; in that case vector mode degrades to
  deterministic candidates and records `embedding_provider_unavailable`
  diagnostics. The built-in `LocalVectorIndex(embedder)` remains as a
  compatibility adapter for callers that still want one object to own both
  embedding and local vector scoring;
- `rerank=None` uses a structural cost gate: rerank is skipped for focused
  candidate pools and used for oversized, weakly filtered, mixed-source, or
  highly dispersed pools;
- broad-pool rerank sees a bounded candidate-summary window before final
  packaging, so dropped candidates do not starve later relevant records or file
  snippets;
- selected record payloads use `record_representation="auto"` by default:
  short structured records keep a compact structure with cold fields omitted,
  while long or noisy records use deterministic model-hot projections; every
  item carries `original_ref` and `projection` metadata, and the raw Workspace
  record remains the source of truth for readback;
- cold fields are non-hot record mechanics such as `audit`, `source_system`,
  `tags`, and `noise`; they are omitted from the model-hot package, not from
  the stored Workspace record;
- callers can set `budget={"record_representation": "raw"}` or
  `budget={"record_representation": "projected"}` when they need a fixed
  representation policy;
- callers can still force rerank with `rerank=True` or disable it with
  `rerank=False`;
- if model rerank fails after retry, retrieval keeps deterministic candidates
  and records diagnostics.

`retrieve(...)` is a Workspace strategy, not a Session-memory-only feature.
Session memory, text fragments, and file retrieval can all use it.

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

`flow.create_execution()` binds the current session/script default Workspace by
default and gives the execution its own scoped file root under
`files/lineage/<root-kind>/<root-id>/.../execution/<execution-id>/files`.
Pass `workspace=False` to opt out, or pass a Workspace instance, path, or
backend when the execution should use an explicitly selected Workspace.

Do not rely on separate explicitly isolated Workspaces to communicate with each
other. If a TriggerFlow execution needs to move information between isolated
Workspaces, make that transfer explicit in application logic: search or read
from the source Workspace, write into the destination Workspace, then link the
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
`supports_lease`, `supports_artifact_refs`, `supports_physical_reclamation`, and
`supports_remote_backend`. The built-in local SQLite backend reports physical
reclamation only when exact filesystem block allocation and safe maintenance
are available. Retention accounting measures the SQLite DB/WAL/SHM files; the
stable Workspace mutation-lock file remains fixed system overhead and is never
reported as reclaimed bytes.
Distributed recovery should fail closed when the selected provider lacks the
required flags or the matching provider methods.

## Action Boundary

`agent.workspace.files_root` defines an ordinary editable working tree for
shell, Node.js, and file actions. In shared default Workspaces this is a scoped
lineage subdirectory such as
`files/lineage/<root-kind>/<root-id>/.../agent/<agent-scope>/files`,
`files/lineage/<root-kind>/<root-id>/.../execution/<execution-id>/files`, or
`files/lineage/<root-kind>/<root-id>/.../task/<task-id>/files`.
Filesystem-like action helpers inherit that boundary when no explicit root or
cwd is passed, including when the Agent is still using its lazy default
Workspace.
`agent.workspace.content_root` remains the shared managed record-content store
used by Workspace records.

```python
agent.enable_workspace_file_actions(write=True)
agent.enable_coding_agent_actions()
agent.enable_shell(commands=["pwd", "pytest"])
agent.enable_nodejs()
```

`enable_workspace_file_actions(...)` does not create a second Workspace. It
exposes list/search/read/write file actions over the current Workspace file
area. Pass `export=True` together with `write=True` when the Agent should also
receive an `export_file` action. Pass an explicit `root=` or `cwd=` only when an
action must use an independent directory.

`enable_coding_agent_actions(...)` is the Workspace-owned profile for coding
agents. It exposes `read_file`, `glob_files`, `grep_files`, `edit_file`,
`apply_patch`, and guarded `write_file` actions over the same file boundary.
Use `edit_file(...)` or `apply_patch(...)` for targeted edits, and keep shell
commands for tests, builds, git status/diff/log inspection, and read-only
diagnostics. In coding-agent mode, full-file `write_file(...)` is guarded by
prior read state or an expected SHA unless the host explicitly disables that
policy.

## File IO Handlers

Workspace file reads, writes, and exports use registered
`WorkspaceFileIOHandler` implementations. Workspace owns path containment,
deterministic file info, handler dispatch, digests, and file refs; handlers own
format-specific parsing or rendering. Workspace does not become a shell
executor, MCP client, renderer lifecycle owner, OCR engine, or model requester.

```python
await agent.workspace.write_file("notes/todo.txt", "ship docs")
read_result = await agent.workspace.read_file("notes/todo.txt", max_bytes=4096)

materialized = await agent.workspace.materialize_file(
    "downloads/syllabus.pdf",
    pdf_bytes,
    source={"kind": "remote_download", "url": "https://example.com/syllabus.pdf"},
    media_type="application/pdf",
)

export_result = await agent.workspace.export_file(
    "report.md",
    "report.pdf",
    export_kind="markdown_pdf",
)
```

The default text handler reads UTF-8 / UTF-8-SIG text, writes plain text, and
returns bounded content with `bytes`, `sha256`, `offset`, `read_bytes`,
`truncated`, diagnostics, and file refs. Unknown binary files return
`readable=False` with diagnostics instead of replacement-character content.
`search_files` only searches files that are readable text through the same
handler registry. Search results keep the original `path`, `line`, and `text`
fields, and also include `role="evidence_snippet"`, bounded snippet counts, and
a `truncated` flag plus a nested `locator_ref` with `content_state="ref_only"`.
Use the visible snippet as evidence only within that excerpt; use the locator
as a target for a later bounded `read_file(...)` or Blocks
`workspace_operation` readback.

Blocks `workspace_operation` can also run scoped Workspace retrieval through
the compatibility `search` operation name plus bounded ref/path reads through
`read_bounded`. The `search` operation uses `workspace.retrieve(...)` as the
shared retrieval strategy for Workspace records and files, then returns typed
`locator_ref` and `evidence_snippet` facts; it does not decide whether a hit is
semantically useful or whether a task is complete. In Flat AgentTask steps,
planner-provided `scoped_retrieval.query_groups` are lowered to these Blocks
retrieval facts before the bounded `agent_step` consumes them. Query groups may
set `search_surface` to `workspace_index`, `workspace_files`, or
`workspace_index_and_files`, may carry structural filters (`collection`,
`kind`, `id`, `path`, `scope`, or `meta`), and may pass explicit retrieval
options such as `tags`, `method`, `rerank`, `selection`, `top_n`, or
`max_candidates` when the task requires them. This keeps large retained records
and files out of the hot context until a bounded retrieval or readback needs
them.
For `workspace_index`, put record collections in `filters.collection`; use
`filters.kind` only when the exact record kind is known, and do not use `path`
for collection names. Singleton filter lists are normalized to scalar values
before execution.
When AgentTask injects scoped retrieval results into a later Flat step or
TaskBoard card, it uses a compact model-hot view: bounded snippets, truncation
facts, line/range facts, and actionable locator handles stay visible, while
reconstructable provenance such as `sha256`, byte counts, handler/media details,
backend/search-engine facts, execution block ids, and full file refs remains in
the raw Workspace/Blocks evidence for programmatic audit and readback.
TaskBoard applies the same split to readback continuations: external
`target_refs` such as HTTP/HTTPS URLs become Action evidence work, while
Workspace/content paths and retained-note refs become bounded Workspace readback
cards. Intermediate readback previews keep content, path, range, and truncation
facts hot, and readback work-unit hot payloads use compact refs rather than full
provenance refs. SHA, byte counts, media/handler details, backend facts, execution
block ids, and other programmatically traceable provenance stay in cold
Workspace/Blocks evidence, final artifact audit metadata, DevTools, or runner
logs. Final verifier hot input uses path/ref handles, bounded content or
preview, and truncation status; it does not need SHA only to judge task
sufficiency.
For `workspace_files`, `query` is the content text to search, `path` is the
directory or file scope, and `pattern` is a file glob such as `*.md`, `*`, or
`**` for recursive file search. Local Workspace file search uses `rg` as a
grep-style search engine when available and falls back to bounded file scanning.
Blocks use a small bounded context around file matches by default so related
nearby facts can be visible without reading the whole file.

`materialize_file(...)` is for framework-owned or application-owned byte
materialization, such as a Browse action downloading a remote PDF into
`downloads/` before a later `read_file(...)` parses it through the handler
registry. It records `bytes`, `sha256`, `media_type`, diagnostics, and file
refs, but it does not parse PDF/Office/image content itself and does not change
the plain-text contract of `write_file(...)`.

Built-in optional handlers cover:

- PDF text extraction through optional `pypdf`;
- `.docx`, `.xlsx`, and `.pptx` extraction through optional Office packages;
- image preparation as ModelRequest-compatible attachments, with interpretation
  still owned by `.image(...)` or another VLM-capable ModelRequest path;
- HTML/Markdown export to PDF or screenshot through optional renderer
  dependencies, with network fetch disabled by default.

Optional dependency missing, unsupported file type, unsupported export kind, and
image-only/scanned PDF cases return structured diagnostics. Outside-root,
missing-path, and permission failures remain execution errors.

Custom handlers can be registered on the Workspace manager:

```python
Agently.workspace.register_file_io_handler(custom_handler)
Agently.workspace.register_file_io_handler(custom_handler, replace=True)
Agently.workspace.unregister_file_io_handler("custom-handler")
```

See:

- `examples/workspace/workspace_file_io_handlers.py` for text read/write,
  unsupported binary diagnostics, and deterministic optional export dependency
  failure;
- `examples/workspace/workspace_file_io_real_documents.py` for real text
  read/write, PDF/Office extraction, and HTML/Markdown export E2E;
- `examples/workspace/workspace_file_io_real_vlm.py` for real image attachment
  preparation plus a VLM model request. The VLM example defaults to
  `qwen3-vl-plus`, requires a real provider key, and does not mock image
  interpretation. Use `WORKSPACE_FILE_IO_VLM_ENV_FILE` when the key lives in a
  non-default dotenv file.

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
affordances for future Action, ContextBuilder, or WorkLoop layers. In V1,
application code decides what to write, and `workspace.build_context(...)`
packages stored records into a `ContextPackage` through pluggable planner,
retriever, and packager profiles.

## Plugin Seams

Workspace exposes provider seams for content, DB storage, policy, embedding,
and vector storage. The default local backend uses filesystem content plus
`db_store_provider="sqlite"` for records/FTS/links/checkpoints/runtime metadata,
and `vector_store_provider="auto"` for vector storage: Chroma is used when
`chromadb` is available, otherwise Workspace falls back to a SQLite vector
table. `DBStoreProvider` is the normalized record DB adapter surface for SQLite
today and future MySQL, Postgres, or local-file implementations; lower-capability
providers keep the same method surface and return empty/absent results for
advanced capabilities they do not support. Embedding is a separate provider
(`EmbeddingProvider`) from vector storage (`VectorStoreProvider`), so
applications can swap model-owned vectorization independently from Chroma,
SQLite, or a custom vector database. The built-in `LocalVectorIndex(embedder)`
remains available as a compatibility adapter when an application still wants one
object to own both embedding and local vector scoring.
ContextBuilder exposes
`ContextPlanner`, `WorkspaceContextRetriever`, and `ContextPackager`; advanced
model-assisted planning, vector retrieval, reranking, compression, and remote
backends are expected to arrive as plugins over this foundation.

Component providers can be registered independently:

```python
Agently.workspace.register_embedding_provider("agent", build_embedding_provider)
Agently.workspace.register_db_store_provider("sqlite", build_db_store_provider)
Agently.workspace.register_vector_store_provider("pgvector", build_pgvector_store)

workspace = Agently.create_workspace(
    "./.agently/projects/support",
    db_store_provider="sqlite",
    embedding_provider="agent",
    embedding_options={"agent": embedding_agent},
    vector_store_provider="auto",
)
```

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
provider is selected, the Agent's lazy default Workspace uses the current
session or script scoped local backend. The test suite includes a protocol-level
remote audit provider proof that exercises the same checkpoint, RuntimeEvent,
evidence link, and capability paths as the local backend. That proof is not a
public Redis, Postgres, or object-storage adapter; production providers must
still report their real capabilities and fail closed when distributed recovery
requirements are missing.
TriggerFlow tests also read Workspace-backed execution snapshots through the provider and
load pause/continue, policy-approval waits, and `when(..., mode="and")`
join progress through TriggerFlow, so Workspace remains storage rather than a
workflow control plane.

See `examples/workspace/workspace_loop_foundation.py` for an explicit
TriggerFlow loop that stores structured observations, links decisions to
evidence, checkpoints compact state, and builds a ContextPackage.

See `examples/workspace/workspace_shared_default_management.py` for the default
session-scoped Workspace behavior: multiple Agents and TriggerFlow executions
share one physical `workspace.db` while execution file roots stay isolated.

See `examples/workspace/workspace_pluggable_vector_providers.py` for a runnable
provider smoke: a callable embedding provider indexes records while
`vector_store_provider="auto"` uses Chroma when available or the SQLite vector
table fallback.

See `examples/workspace/workspace_with_action_output.py` for the Action
boundary: a file action writes into `workspace.files_root`, a shell action reads
that file, application code explicitly writes the action output as a Workspace
observation, and ContextBuilder packages it into a ContextPackage.

`workspace.ingest(...)` remains as a compatibility alias for older code. New
code should use `workspace.put(...)`; when an older profile path is needed, pass
`profile=...` to `put`.

## Terminal Retention For Agent Runs

AgentExecution is the parent of a routed AgentTask. The task receives an
explicit execution-scoped Workspace view and derives a child scope; it does not
search for an execution directory or rebind the Agent-wide Workspace. Canonical
task file and record refs are handed back to AgentExecution without copying the
deliverable. In that child scope, `execution_id` remains the inherited parent
execution id and `task_id` identifies the exact Task. Task cleanup therefore
removes only Task process records/files while leaving the parent and sibling
Tasks live.

On a failed AgentTask, terminal cleanup still discards ordinary process records,
files, and checkpoints. If the task has a compact `task_id::resume` snapshot
from its last completed iteration, the task adds a recovery anchor before
cleanup so that snapshot remains available for explicit resume. Successful and
cancelled tasks do not retain that recovery point by default.

At terminal state, AgentExecution prepares one bounded carrier before emitting
the `result` stream item or terminal RuntimeEvent. Both surfaces use that same
carrier and `terminal_retained_refs` contains every canonical record, envelope,
or file ref. The in-process `AgentExecutionResult.get_data()` business value may
remain complete; event payloads and retained manifests do not duplicate a large
body. File-backed AgentTask `final_response` text points to the canonical file
and is always byte-bounded. The AgentExecution terminal finalizer is the sole
owner of `agent_execution.completed`, `agent_execution.failed`, and
`agent_execution.cancelled`; nested ModelRequest completion emits only its own
request/model lifecycle events.

`AgentExecution.async_record_workspace(...)` uses purpose-aware lifecycle
rules. `process` and `recovery` writes are accepted only while the execution is
actively running; the method neither starts a fresh execution nor waits for it
to finish. Their checkpoints always use the canonical AgentExecution id.
After terminal state, `process` and `recovery` writes are rejected.
Explicit `deliverable` writes and policy-enabled `audit` writes are accepted
only with immediate Workspace retention governance. Active recovery or lease
facts defer destructive cleanup, including cancellation cleanup.

Workspace and replaceable providers expose
`await workspace.get_retention_lifecycle(execution_id, status=...,
terminal_at=...)`. It returns the existing typed lifecycle shape using the
latest persisted snapshot state version, unresolved recovery facts, and active
lease state. Agent consumers forward this snapshot rather than synthesizing
stream counters or private runtime flags. Cancellation uses the distinct
`agent_execution.cancelled` RuntimeEvent and retention status.
