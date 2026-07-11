---
title: Agently 4.1.3.9 Release Notes
description: Agently 4.1.3.9 release notes for Workspace retrieval, SessionMemory, AgentTask scoped retrieval, vector-index seams, and public typing hardening.
keywords: Agently, release notes, 4.1.3.9, Workspace, retrieval, SessionMemory, AgentlyMemory, AgentTask, typing
---

# Agently 4.1.3.9 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.9.md)

Agently 4.1.3.9 turns Workspace into the shared durable retrieval substrate for
records, files, AgentTask scoped evidence, and Session memory. The release keeps
raw deterministic search available while adding `workspace.retrieve(...)` for
budgeted intelligent recall, optional vector candidates, model rerank, refill,
and compact model-hot packaging.

This release also promotes the pluggable `SessionMemory` protocol and the built-in
`AgentlyMemory` sample plugin. HITL approval/suggestion/guidance/insertion work
is not part of 4.1.3.9 and moves to the 4.1.4 development line.

## Recommended Shape

Use deterministic search when the caller wants cheap exact search. Use
`retrieve(...)` when the result will feed another model request or an AgentTask
work unit:

```python
results = await workspace.retrieve(
    query="What should this task remember about the customer?",
    tags=["preference", "project"],
    scope={"memory_scope": "SESSION_MEMORY"},
    sources=["records", "files"],
    budget={"chars": 12000},
    selection="length",
)

refs = await workspace.grep("deadline", filters={"collection": "memory"})
hits = await workspace.grep_files("deadline", path="notes")
```

Durable session memory is a Session plugin backed by Workspace:

```python
session = Session()
session.use_memory(mode="AgentlyMemory", workspace=workspace)

agent = Agently.create_agent().use_workspace(workspace)
agent.activate_session(session_id="support-demo")
agent.activated_session.use_memory(mode="AgentlyMemory")
```

Embeddings remain application or plugin logic. If a backend has a real
`vector_index`, Workspace can use it; otherwise vector retrieval degrades with
diagnostics.

```python
from agently.core.workspace import LocalVectorIndex

vector_index = LocalVectorIndex(embedder, similarity="cosine")
```

## Core Changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Workspace retrieval | Added `workspace.retrieve(...)` as the shared intelligent retrieval surface for records and files. It builds keyword/tag candidates, can include file candidates, may use vector or hybrid candidates when a non-noop `vector_index` exists, applies structure-gated model rerank, refills dropped candidates, and packages by length budget or `top_n`. | Use `retrieve(...)` for model-hot context and AgentTask scoped evidence. Use `selection="length"` by default; use `top_n` when count matters more than total text budget. | Retrieval hits are evidence snippets or refs, not completion proof. Failed or empty evidence supports missing-data claims only. | `tests/test_cores/test_workspace.py`, `docs/*/requests/workspace.md`, `spec/experiments/agent-task-workspace-retrieval/round-009/`. |
| Deterministic search | `workspace.grep(...)` and `workspace.grep_files(...)` are the explicit low-cost deterministic surfaces. `workspace.search(...)` and `workspace.search_files(...)` keep compatibility return shapes and can choose deterministic or retrieval-backed internals without changing the public result contract. | Use `grep(...)` / `grep_files(...)` for debugging, exact filters, and cheap searches. Keep `search(...)` callers when compatibility is more important than choosing a strategy manually. | Search aliases do not trigger model rerank by default. | Workspace compatibility tests and docs. |
| Record packaging | Selected structured records now use representation packaging. `record_representation="auto"` preserves short structures while omitting cold fields from model-hot snippets, projects long/noisy records, and records projection diagnostics plus raw readback refs. | Leave `auto` on for ordinary retrieval. Force `budget={"record_representation": "raw"}` only when downstream code truly needs raw structure in the model-hot view. | Raw Workspace records remain the source of truth for readback; model-hot projections are compact views. | Workspace projection tests and round-009 retrieval experiments. |
| Vector seam | Added provider-neutral `LocalVectorIndex(embedder, similarity="cosine" | "dot" | "l2")`. Workspace chooses hybrid/vector candidates only when configuration or policy indicates a vector preference and the backend has a non-noop index. | Keep embedding providers in business code, custom backends, or plugins. Pass a vector index to Workspace/backend rather than hard-coding provider settings in framework code. | The default local backend remains `NoopVectorIndex`; vector requests without a real index degrade with diagnostics. | Vector-mode tests, installed-package smoke, docs. |
| Rerank policy | Candidate strategy and rerank are separate. Default rerank is structure-gated: it runs only for broad, noisy, cross-source, or distractor-heavy candidate pools. | Use `rerank=None` for the default gate, `rerank=False` to avoid model cost, and `rerank=True` only when the caller knows semantic pruning is needed. | If rerank fails after retry, Workspace degrades to deterministic order and records diagnostics. | Rerank/drop/refill tests and experiment reports. |
| Session memory | Added the `SessionMemory` protocol and built-in `AgentlyMemory` plugin. `AgentlyMemory` stores Workspace records in `collection="memory"` with `kind="global_memory"` or `kind="session_memory"`, injects `GLOBAL_MEMORY` / `SESSION_MEMORY`, and keeps provenance, tags, scope, and optional vector metadata. | Use `session.use_memory(mode="AgentlyMemory", workspace=workspace)` or bind through an Agent session with `agent.workspace`. Configure body schema and prompt strategy under `session.memory.AgentlyMemory.*`. | V1 supports one selected memory mode per Session. It does not add cross-Workspace user profiles or automatic sync. | `tests/test_cores/test_session.py`, `examples/basic/session_workspace_memory.py`, `docs/*/requests/session-memory.md`. |
| AgentTask scoped retrieval | Flat and TaskBoard scoped retrieval query groups lower through Blocks `workspace_operation.search`, while execution uses `Workspace.retrieve(...)` for records/files and injects a body-light evidence ledger plus readback refs. | Let AgentTask retrieve bounded Workspace/file evidence before broad reads. Use structured `evidence_use` ids when output claims depend on retrieved facts. | AgentTask retrieval remains evidence production, not semantic acceptance. Final claims still need verifier/readback support. | `tests/test_cores/test_blocks_plugin.py`, `tests/test_agent_task_loop.py`, experiment rounds 007-009. |
| Public typing | Public typing now covers the new Workspace vector export and dict-compatible TaskBoard update helpers. `TaskBoardGraph.with_cards(...)` and `TaskBoardRevision.next_revision(...)` accept common mapping payloads as well as dataclass values. | Let user code pass dict-shaped payloads on common public update methods; use dataclasses when stronger structure is already available. | Broad internal compatibility escape hatches remain intentional. | Full pyright over `agently/`, `tests/`, and `examples`; installed-package pyright smoke. |

## Compatibility

- Package version: `4.1.3.9`.
- Release manifest: `compatibility/releases/4.1.3.9.json`.
- Recommended `agently-devtools`: `>=0.1.10,<0.2.0`.
- The next development-line manifest is `compatibility/in-development.json` and
  targets `4.1.4`.

## Acceptance Evidence

- Source typing: `python -m pyright --pythonpath "$(python -c 'import sys; print(sys.executable)')" agently tests examples`.
- Full test suite: `python -m pytest -q`.
- Clean installed-package smoke with `uv`: imports Workspace retrieval,
  `LocalVectorIndex`, Session memory dependencies, TaskBoard dict-compatible
  helpers, LazyImport missing-dependency diagnostics, and installed `py.typed`.
- Companion guidance: `../Agently-Skills` request guidance updated for Workspace
  retrieval and Session memory.

## Deferred To 4.1.4

4.1.3.9 does not complete HITL approval/suggestion/guidance/insertion flows,
long-task task-execution memory beyond the released Session/Workspace substrate,
or additional observation/runtime refinements that were not accepted for this
Workspace release.
