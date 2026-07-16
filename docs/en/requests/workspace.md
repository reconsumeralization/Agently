---
title: Workspace
description: Direct file access and optional lazy durable state for Agently applications.
keywords: Agently, Workspace, files, records, recovery, memory, retrieval
---

# Workspace

`Workspace` is Agently's foundation boundary for files and optional durable
information. It has one ordinary file root:

```text
Workspace(root)
├── ordinary project and document files
└── .agently/                 # reserved Agently-private state, created on demand
```

The supplied `root` is the directory the Agent reads. There is no container
root, `files_root`, `content_root`, generated guide, or standard artifact
directory. After the boundary is selected, Workspace supplies contained file
operations; the model or application decides which ordinary directories and
files to use.

An Agent always has a lightweight Workspace binding. Its default root is the
entry script's directory, with the current working directory as the fallback.
Binding it, listing files, reading files, or searching ordinary files performs
no persistent write and does not create `.agently`.

```python
from agently import Agently

agent = Agently.create_agent("repo-reader").use_workspace("/path/to/project")

result = await agent.workspace.read_file("pyproject.toml", max_bytes=16_000)
matches = await agent.workspace.glob_files("*.py", path="agently")
hits = await agent.workspace.grep_files("create_agent", path="agently")
```

## File Permissions And Products

External Workspace files are readable and read-only by default. Use an
explicit write grant when the application is intentionally editing the root:

```python
agent.use_workspace("/path/to/project", mode="read_write")
```

On the Action surface, an approved filesystem mutation can grant a scoped
write for that action. A direct `Workspace.edit_file(...)` or
`Workspace.apply_patch(...)` call still requires `mode="read_write"` because
there is no Action approval context around a direct API call.

When a read-only Workspace creates a new file, Workspace writes it to the
current execution's private fallback area:

```text
<root>/.agently/files/<execution-id>/<requested-relative-path>
```

The requested path remains available through the ordinary logical file view,
while the trusted `file_refs` entry contains the canonical private path. An
existing external file is never silently shadowed or redirected: changing it
requires a write grant or an approved Action.

External files remain read-only when Workspace records private identity metadata
for a promoted file. That metadata write occurs only inside the reserved
`.agently` area and never grants permission to mutate the external source.

For user-facing products, prefer a meaningful subdirectory such as
`outputs/`, `reports/`, or a task-specific directory instead of writing files
directly at the root. These are application choices, not Workspace-managed
areas.

```python
write_result = await agent.workspace.write_file(
    "outputs/weekly-report.md",
    "# Weekly report\n",
)

# With the default read-only mode this resolves the current execution fallback.
readback = await agent.workspace.read_file("outputs/weekly-report.md")
trusted_ref = write_result["file_refs"][0]
```

At AgentExecution or AgentTask terminal state, execution-private fallback files
are reclaimed aggressively. Only selected final products whose trusted refs
pass physical readback are retained. Drafts, intermediate files, unselected
outputs, and failed-run residue are deleted. Ordinary external files are never
part of this cleanup.

## Lazy Private State

`.agently` is reserved for everything Workspace owns beyond the ordinary file
boundary. Components are created independently and only when used:

| Use | Private state effect |
|---|---|
| Bind, list, read, glob, or grep ordinary files | none |
| Create a file without external write permission | `.agently/files/<execution-id>/...` |
| Put/search durable records or save recovery state | lazy `.agently/workspace.db` tables |
| Perform a real vector operation | lazy embedding/vector provider state |
| Enable SessionMemory | lazy memory records/provider state |
| Install or materialize Skills | lazy Skills-owned private state |

Provider configuration alone does not initialize a database, FTS index,
embedding provider, or vector store. `workspace.capabilities()` reports which
components have actually materialized.

```python
assert agent.workspace.capabilities()["materialized_components"] == []
```

### Stable identity layers

Workspace keeps three identity layers separate when durable provenance is
needed:

- locator identity represents a normalized path or URL provenance location;
- content-version identity represents one exact digest and size observed at
  that locator; changed content creates a new version even when the locator is
  unchanged;
- reference identity is the task/application-owned stable selection key that
  points to eligible evidence without asking a model to copy paths, URLs, or
  canonical storage ids.

These identities use short type-prefixed Base62 values, expand as needed, and
are never reused within their owning scope. The same short value in another
Workspace or task remains distinct because the canonical scope includes its
Workspace/task identity. Unreachable old versions, segments, and payloads are
removed by reference-closure cleanup, while the allocator high-water mark is
retained so deleted ids are not recycled.

Identity allocation is private implementation behavior, not a new public
Workspace API. Ordinary external reads remain zero-state. Explicit promotion,
durable records, recovery, or accepted-artifact retention may write bounded
private identity metadata under `.agently`; this filesystem-first metadata does
not require the record database unless the selected feature already uses it.

## Durable Records

Use records for information that should intentionally survive the current
execution: application knowledge, selected observations, decisions, compact
checkpoints, memory, or other semantic state. Do not copy every task tick,
prompt envelope, model response, or RuntimeEvent into records by default.

```python
ref = await agent.workspace.put(
    content={"status": "failed", "test": "route_fallback"},
    collection="observations",
    kind="test_result",
    summary="route fallback test failed",
    scope={"task_id": "issue-123"},
    source={"type": "command", "name": "pytest"},
)

record = await agent.workspace.get_data(ref)
refs = await agent.workspace.grep(
    "route fallback",
    filters={"collection": "observations", "kind": "test_result"},
)
```

`put(...)` is the canonical record-write API. It creates only the record tables
needed for that operation. FTS is opt-in through `indexed=True`; vector
indexing is opt-in through `vector=True` and requires configured embedding and
vector providers.

Use stable refs and bounded reads instead of copying large values into runtime
state:

```python
envelope = await agent.workspace.ref_envelope(ref)
segment = await agent.workspace.read_bounded(ref, offset=0, limit=4096)

async for chunk in agent.workspace.stream_read(ref, chunk_size=8192):
    consume(chunk["content"])
```

`workspace.link(...)` and `workspace.links(...)` connect durable records.
`workspace.link_evidence(...)` adds execution, operation, event, checkpoint,
exchange, and artifact identity facts without making Workspace the owner of
execution policy.

## Retrieval

Choose the retrieval surface from the caller's real need:

- `grep(...)`: deterministic durable-record search;
- `grep_files(...)`: deterministic ordinary-file search, using `rg` when
  available;
- `search(...)` / `search_files(...)`: compatibility result shapes with
  automatic packaging for broad candidate pools;
- `retrieve(...)`: shared record/file retrieval with budgets, optional vector
  candidates, optional model rerank, and trusted refs;
- `build_context(...)`: creates a bounded `ContextPackage` for a later model
  request.

```python
package = await agent.workspace.retrieve(
    "deadline and owner",
    sources=["records", "files"],
    filters={"collection": "project_notes"},
    file_options={"path": "notes", "pattern": "*.md"},
    budget={"chars": 6000, "item_chars": 1200},
    selection="length",
    rerank=False,
)

context = await agent.workspace.build_context(
    goal="Summarize the current release risks.",
    scope={"task_id": "release-42"},
    budget={"tokens": 8000},
    profile="auto",
)
```

### Retrieval references in natural-language answers

When a later model answer cites selected retrieval results, keep full source
records in host code. Give the model one short trusted `ref_id` per source plus
only the title/snippet facts it needs, and require inline tokens in the
application-level form `[[ref:<ref_id>]]`. For durable AgentTask output, use the
task-owned stable reference identity, for example `[[ref:ref_2]]`. An evidence
ledger `cite_as` value such as `e1` is a request-local display alias: normalize
it to its offered stable `ref_*` identity before the model response leaves that
exact ledger view. Never persist or later guess an `(eN)` position.

Do not use a bare `${ref_id}` token: `${...}` already belongs to Agently prompt
and TaskDAG placeholder families. The `[[ref:...]]` protocol is an application
rendering convention, not a new Workspace or Agently public API.

The following pattern validates model citations, turns them into Markdown links,
emits the answer, and then emits complete application-approved source-card
details for hover cards, a source list, or reply-attached result cards. Keep raw
provider/Workspace records host-side and apply authorization and redaction
before emitting card fields:

```python
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from agently import Agently


REF_TOKEN = re.compile(r"\[\[ref:([A-Za-z0-9._:-]+)\]\]")


def prepare_refs(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
    refs_by_id: dict[str, dict[str, Any]] = {}
    model_refs: list[dict[str, str]] = []
    for index, record in enumerate(records, start=1):
        ref_id = f"r{index}"
        full_record = {**dict(record), "ref_id": ref_id}
        refs_by_id[ref_id] = full_record
        model_refs.append({
            "ref_id": ref_id,
            "title": str(record.get("title", "")),
            "snippet": str(record.get("snippet", "")),
        })
    return model_refs, refs_by_id


def build_source_card(record: Mapping[str, Any]) -> dict[str, Any]:
    # Extend this explicit frontend contract as needed; do not emit the raw record.
    fields = ("ref_id", "title", "url", "snippet", "source_name", "published_at")
    return {field: record[field] for field in fields if field in record}


def trusted_http_url(value: Any) -> str:
    url = str(value)
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError(f"unsupported source URL: {url}")
    return url


def render_refs(
    answer: str,
    refs_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    used_ids: list[str] = []

    def replace(match: re.Match[str]) -> str:
        ref_id = match.group(1)
        record = refs_by_id.get(ref_id)
        if record is None:
            raise ValueError(f"unknown retrieval ref: {ref_id}")
        used_ids.append(ref_id)
        label = str(record.get("title") or ref_id).replace("\\", "\\\\").replace("]", "\\]")
        return f"[{label}]({trusted_http_url(record.get('url'))})"

    rendered = REF_TOKEN.sub(replace, answer)
    unique_ids = list(dict.fromkeys(used_ids))
    return rendered, [build_source_card(refs_by_id[ref_id]) for ref_id in unique_ids]


async def answer_with_refs(
    question: str,
    retrieved_records: Sequence[Mapping[str, Any]],
    emit: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    model_refs, refs_by_id = prepare_refs(retrieved_records)

    def validate_refs(data: dict[str, Any], _ctx: Any) -> bool | dict[str, Any]:
        cited_ids = REF_TOKEN.findall(data.get("answer", ""))
        if not cited_ids:
            return {"ok": False, "reason": "answer must cite at least one offered ref"}
        unknown = {
            ref_id
            for ref_id in cited_ids
            if ref_id not in refs_by_id
        }
        if unknown:
            return {"ok": False, "reason": f"unknown refs: {sorted(unknown)}"}
        return True

    result = (
        Agently.create_request("retrieval-reference-answer")
        .input({"question": question, "retrieval_results": model_refs})
        .instruct([
            "Answer from retrieval_results.",
            "Cite a source inline as [[ref:<ref_id>]] using only an offered ref_id.",
            "Do not copy source URLs or hidden metadata into the answer.",
        ])
        .output({
            "answer": (
                str,
                "Natural-language answer with inline [[ref:<ref_id>]] citations",
                True,
            ),
        })
        .validate(validate_refs)
        .get_result()
    )

    # No progressive consumer here, so read final data directly.
    data = await result.async_get_data()
    rendered_answer, source_cards = render_refs(data["answer"], refs_by_id)

    await emit({"type": "answer", "text": rendered_answer})
    await emit({"type": "retrieval_refs", "items": source_cards})
```

This protocol keeps citation choice model-owned while identity validation,
link construction, authorized source-detail transport, and frontend rendering
stay deterministic and host-owned.

Structural filters narrow candidates; they do not prove semantic relevance or
task completion. Model-owned rerank and verification remain outside Workspace.

## File IO Handlers

Workspace owns path containment, deterministic file information, digests,
trusted refs, and handler dispatch. `WorkspaceFileIOHandler` implementations
own format-specific read, write, or export behavior.

```python
read_result = await agent.workspace.read_file("notes/todo.txt")
write_result = await agent.workspace.write_file("outputs/todo.txt", "ship docs")

download = await agent.workspace.materialize_file(
    "sources/specification.pdf",
    pdf_bytes,
    source={"type": "browse", "url": source_url},
    media_type="application/pdf",
)

exported = await agent.workspace.export_file(
    "sources/specification.docx",
    "outputs/specification.md",
    export_kind="markdown",
)
```

`materialize_file(...)` is the binary/download boundary. `write_file(...)`
keeps its text-oriented handler contract. Workspace does not become a shell,
browser, renderer lifecycle owner, OCR engine, or model requester.

Register custom handlers through the Workspace manager:

```python
Agently.workspace.register_file_io_handler(custom_handler)
```

## File Actions, Shell, And ACP

File and coding-agent Actions inherit the direct Workspace root:

```python
agent.enable_workspace_file_actions(read=True, write=True)
agent.enable_coding_agent_actions()
```

Read Actions work against the ordinary root without creating private state.
New writes in a read-only Workspace use the current execution fallback;
existing-file mutation requests go through PolicyApproval. Coding-agent writes
also retain read-before-write or expected-SHA freshness guards.

Shell and Node.js helpers inherit the same root when no explicit root/cwd is
passed. A trusted-local shell cannot enforce a read-only filesystem boundary,
so it requires a read-write Workspace. Docker can mount the ordinary root
read-only and the current `.agently/files/<execution-id>` fallback read-write.

```python
agent.enable_shell(commands=["rg", "pytest"], sandbox="docker")
agent.use_acp(on_missing="skip")
```

ACP uses `agent.workspace.root` as its default project directory. Pass an
explicit `root=` only when the host intentionally authorizes a different
directory.

## TriggerFlow Recovery And Runtime Events

TriggerFlow may carry a Workspace runtime resource for file or record access,
but it does not use that Workspace as a RuntimeEvent store by default. A finite
run therefore creates no database merely because it executed.

Pause/resume, intervention, or explicit save/load may activate Workspace as a
snapshot carrier. This is recovery state, not an audit archive. Terminal
cleanup deletes the execution's transient recovery snapshot after a completed,
failed, or cancelled finite run unless a real recovery lifecycle still needs
it.

RuntimeEvent persistence must be enabled explicitly:

```python
execution = flow.create_execution(
    workspace=agent.workspace,
    runtime_resources={
        "runtime_event_store": agent.workspace,
    },
)
```

Use `snapshot_store` or `durable_provider` explicitly when the application
wants a selected recovery provider. `runtime_event_store` is separate on
purpose: configuring recovery must not silently turn on a full event archive.
Ordinary audit belongs in EventCenter sinks, logs, or DevTools; persist it in
Workspace only when the application has deliberately selected what to retain
and how.

## AgentTask Recovery And Retention

AgentTask planning, observations, verification, and taskboard ticks are
in-process state and observation output by default. They are not copied into
Workspace records. Enable restart recovery only when the task actually needs
it:

```python
task = agent.create_task(
    goal="Prepare the release report.",
    options={"agent_task": {"workspace_recovery": True}},
)
```

The recovery option persists a compact resumable snapshot; it does not opt the
task into a complete process or RuntimeEvent archive. Final trusted file
products still use Workspace readback and terminal selection, while
non-product process state is reclaimed.

## SessionMemory And Plugin Seams

Workspace is storage and retrieval infrastructure, not a memory strategy.
`Session.use_memory(...)` owns memory extraction, compression, retrieval-query
planning, rerank, and prompt injection; it activates Workspace persistence only
when memory is explicitly enabled.

Workspace provider seams remain available for applications that need a custom
backend:

```python
Agently.workspace.register_backend_provider("remote", build_remote_backend)
Agently.workspace.register_db_store_provider("postgres", build_db_store)
Agently.workspace.register_embedding_provider("agent", build_embedding_provider)
Agently.workspace.register_vector_store_provider("pgvector", build_vector_store)

agent.use_workspace(
    "/project",
    provider="remote",
    provider_options={"tenant": "acme"},
)
```

Providers implement storage mechanics. They do not own AgentTask continuation,
TriggerFlow topology, approval, semantic relevance, memory policy, or business
completion.
