---
title: Agently 4.1.4 Release Notes
description: Agently 4.1.4 final upgrade notes from 4.1.3, covering AgentExecution, AgentTask, TaskBoard, Workspace, TriggerFlow, Skills, ActionRuntime, model runtime, observability, and typing.
keywords: Agently, release notes, 4.1.4, AgentExecution, AgentTask, TaskBoard, Workspace, TriggerFlow, SkillsExecutor, ActionRuntime
---

# Agently 4.1.4 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.4.md)

Agently 4.1.4 upgrades execution ownership, long-task delivery, durable context,
runtime orchestration, capability control, and observable model/action execution.

## Core Outcome

Agently 4.1.4 makes `AgentExecution` the stable public run surface and puts
long-task execution, Workspace evidence, ActionRuntime capabilities,
TriggerFlow orchestration, and runtime observation behind one consistent shape:

```text
business input
  -> AgentExecution
  -> direct / flat / taskboard strategy
  -> Actions / Skills / Workspace / TaskDAG / TriggerFlow
  -> EvidenceEnvelope + Workspace readback
  -> verifier + host guards
  -> final_response + structured result + RuntimeEvents
```

## Key Sample Code

### Direct AgentExecution

```python
result = (
    agent
    .input("Summarize the renewal risk and recommend the next action.")
    .output({
        "summary": (str, "short business summary", True),
        "risk_level": (str, "low / medium / high", True),
        "next_action": (str, "recommended next action", True),
    })
    .strategy("direct")
    .get_result()
)

data = result.get_data()
text = result.get_text()
meta = result.get_meta()
```

### Task Strategy With Workspace Evidence

```python
result = (
    agent
    .use_workspace("./.agently/tasks/migration-risk")
    .goal(
        "Prepare a migration risk report.",
        success_criteria=[
            "Cover compatibility, rollout, and rollback risks.",
            "Ground each recommendation in available evidence.",
            "Produce a final artifact that can be read back from Workspace.",
        ],
    )
    .effort("medium")
    .strategy("auto")
    .output({
        "executive_summary": (str, "final summary", True),
        "top_risks": ([str], "material migration risks", True),
        "recommended_plan": (str, "recommended rollout plan", True),
    })
    .get_result()
)

final_text = result.get_text()
task_payload = result.get_data()
task_meta = result.get_meta()
```

### Explicit TaskBoard Delivery

```python
execution = agent.create_task(
    goal="Complete the vendor security questionnaire.",
    success_criteria=[
        "Every required question has an answer.",
        "Each answer is grounded in supplied policy evidence.",
        "The final Markdown file is written and read back from Workspace.",
    ],
    execution="taskboard",
    workspace="./.agently/tasks/security-questionnaire",
)

execution.output({
    "final_file": (str, "Workspace path for the final Markdown file", True),
    "summary": (str, "short completion summary", True),
})

result = execution.get_result()

async for item in result.get_async_generator(type="instant"):
    render_status(item.path, item.value)

answer = await result.async_get_text()
data = await result.async_get_data()
```

### Runtime Guidance During A Task

```python
import asyncio

execution = agent.create_task(
    goal="Prepare the incident handoff.",
    success_criteria=["The handoff reflects the latest operator context."],
    execution="flat",
    workspace="./.agently/tasks/incident-handoff",
)

run_task = asyncio.create_task(execution.async_get_data())

await execution.async_add_guidance(
    "Use the newly uploaded incident note as the primary source.",
    author="operator",
)

data = await run_task
meta = await execution.async_get_meta()
guidance_refs = meta["task_refs"]["workspace_refs"]["guidance"]
```

### Workspace Records And Retrieval

```python
workspace = Agently.create_workspace("./.agently/support-memory")

await workspace.put(
    collection="memory",
    kind="project_note",
    content="Customer prefers staged rollout with rollback checkpoints.",
    tags=["customer", "rollout"],
    source={"type": "operator_note"},
)

context = await workspace.retrieve(
    query="What rollout constraints should the migration report remember?",
    tags=["customer", "rollout"],
    sources=["records", "files"],
    budget={"chars": 12000},
    selection="length",
)

exact_hits = await workspace.grep(
    "rollback",
    filters={"collection": "memory", "kind": "project_note"},
)
```

### Session Memory

```python
from agently.core import Session

workspace = Agently.create_workspace("./.agently/support-memory")

session = Session()
session.use_memory(mode="AgentlyMemory", workspace=workspace)

agent = Agently.create_agent("support-agent").use_workspace(workspace)
agent.activate_session(session_id="support-demo")
agent.activated_session.use_memory(mode="AgentlyMemory")
```

### TriggerFlow Execution

```python
from agently import TriggerFlow, TriggerFlowRuntimeData

flow = TriggerFlow(name="approval-backed-workflow")

async def prepare(data: TriggerFlowRuntimeData):
    await data.async_set_state("ticket_id", data.input["ticket_id"])
    return {"ticket_id": data.input["ticket_id"], "amount": data.input["amount"]}

async def finish(data: TriggerFlowRuntimeData):
    decision = data.input if isinstance(data.input, dict) else {}
    await data.async_set_state("approved", bool(decision.get("approved")))

flow.to(prepare).to(finish)

execution = flow.create_execution(auto_close=False)
await execution.async_start({"ticket_id": "T-100", "amount": 1200})
state = await execution.async_close()
```

### Skills With AgentExecution

```python
result = (
    agent
    .use_workspace("./.agently/tasks/release-readiness")
    .use_skills("release-readiness-reviewer")
    .goal(
        "Review release readiness and produce a go/no-go recommendation.",
        success_criteria=[
            "Check validation evidence.",
            "Identify blocking risks.",
            "Return a structured release decision.",
        ],
    )
    .effort("medium")
    .output({
        "decision": (str, "go / no-go", True),
        "blocking_risks": ([str], "release blocking risks", True),
        "followups": ([str], "required follow-up actions", True),
    })
    .get_result()
)
```

## Final Recommended Usage

| Scenario | Final recommended usage | Primary APIs / surfaces |
|---|---|---|
| Ordinary one-shot Agent run | Keep the run direct and consume an `AgentExecutionResult`. | `agent.input(...).output(...).get_result()`; `result.get_data()`; `result.get_text()` |
| Multi-statement run setup | Create or hold one execution draft, then attach prompt, output, actions, Skills, Workspace, and strategy to that draft. | `execution = agent.create_execution()`; `execution.input(...)`; `execution.output(...)`; `execution.get_result()` |
| Long or evidence-backed task | Use AgentExecution task strategy with goal, success criteria, effort, Workspace, and `auto` strategy. | `agent.use_workspace(...).goal(..., success_criteria=[...]).effort("medium").strategy("auto").get_result()` |
| Explicit strategy control | Select `direct` for ordinary request/action execution, `flat` for linear bounded task work, and `taskboard` for board/dependency coordination. | `execution.strategy("direct")`; `execution.strategy("flat")`; `execution.strategy("taskboard")` |
| User-facing final text | Read task-strategy final text from the result text facade. | `result.get_text()`; `await result.async_get_text()` |
| Structured task status | Read task status, artifact status, task refs, completion notes, and diagnostics from structured result/meta data. | `result.get_data()`; `result.get_meta()`; `result.task_refs` |
| Durable records | Write durable records through Workspace. | `workspace.put(collection=..., kind=..., content=..., tags=[...])` |
| Model-hot retrieval context | Use Workspace intelligent retrieval for records/files that will feed a model request or AgentTask work unit. | `await workspace.retrieve(query=..., sources=["records", "files"], budget={"chars": ...})` |
| Deterministic exact search | Use deterministic grep surfaces for cheap exact lookup and diagnostics. | `await workspace.grep(...)`; `await workspace.grep_files(...)` |
| Session memory | Bind Session memory to Workspace and use the built-in memory plugin for global/session memory records. | `session.use_memory(mode="AgentlyMemory", workspace=workspace)`; `agent.activate_session(...)` |
| Workspace file work | Keep file read/search/edit/write behavior inside Workspace file actions. | `agent.enable_coding_agent_actions(...)`; Workspace file IO handlers |
| Shell and local command work | Use shell for tests, builds, git inspection, and bounded diagnostics. | `agent.enable_shell(...)`; bounded stdout/stderr artifacts |
| External Actions | Mount actions explicitly and let ActionRuntime own planning, dispatch, policy, artifacts, and observations. | `agent.use_actions(...)`; ActionRuntime records; Action artifact refs |
| Execution resources | Bind runtime capabilities as ExecutionResources. | `ExecutionResource`; built-in ACP, Bash, browser, Docker, MCP, Node.js, Python, SQLite providers |
| Human-in-the-loop work | Use ExecutionExchange and PolicyApproval-backed wait/approval surfaces. | `ExecutionExchange`; `PolicyApproval`; console / host-callback exchange providers |
| Skills usage | Select Skills through AgentExecution/Agent APIs and let SkillsExecutor build context packs and capability plans. | `agent.use_skills(...)`; Skills context packs; Skills capability policy |
| Dynamic DAG work | Use TaskDAG directly for acyclic dynamic planning and execution. | `TaskDAGExecutor.compile_blocks(...)`; `TaskDAGExecutor.async_run_blocks(...)` |
| Workflow orchestration | Use TriggerFlow for explicit branching, waiting, pause/resume, runtime streams, and durable workflow execution. | `Agently.create_trigger_flow(...)`; `TriggerFlow(...)`; `flow.create_execution(...)` |
| Runtime streams | Use `delta` for user-facing text and `instant` / structured events for UI state and diagnostics. | `get_async_generator(type="delta")`; `get_async_generator(type="instant")`; RuntimeEvents |
| DevTools observation | Observe AgentExecution, model requests, actions, TaskBoard progress, exchanges, and telemetry through DevTools. | `agently-devtools >=0.1.10,<0.2.0`; RuntimeEvent / ObservationEvent bridge |

## Final Upgrade Matrix

| Area | Final 4.1.4 upgrade | Final recommended usage |
|---|---|---|
| AgentExecution ownership | `AgentExecution` owns one Agent run: prompt state, action execution, task strategy, process stream, result wrapper, and run metadata. | Use `AgentExecution` as the public run surface for prompt, action, Skill, task, stream, and result consumption. |
| Strategy selection | Execution strategy is consolidated around `auto`, `direct`, `flat`, and `taskboard`. | Keep ordinary work on `auto` or `direct`; choose `flat` for linear bounded task work; choose `taskboard` for board/dependency coordination. |
| Direct route | Direct execution keeps ordinary model-request and ActionLoop runs lightweight. | Use direct route for short request/response work and simple ActionLoop tasks. |
| Flat route | Flat execution shares the AgentTask substrate and can pass remaining work to the next work unit before final verification. | Use Flat for sequential long-task work that needs evidence, readback, and final verification without board scheduling. |
| TaskBoard route | TaskBoard execution shares AgentTask foundations and adds board state, dependency state, patching, continuation, finalization, and bounded projection. | Use TaskBoard for multi-part deliverables, dependency-heavy work, fan-out/fan-in work, and long artifacts. |
| Result text | Task-strategy results expose `final_response`; `get_text()` and `async_get_text()` prefer that final response. | Use result text facades for final user-facing answers. |
| Result payloads | Execution result payloads expose terminal status, artifact status, final result data, task refs, completion notes, and diagnostics. | Use structured result/meta data for application state, audits, and UI detail panels. |
| Streams | AgentExecution streams expose process events, instant items, delta text, retry boundaries, exchange state, action observations, and terminal summaries. | Render user text from `delta`; render structured UI state from `instant` or RuntimeEvents. |
| Runtime context | Runtime context is preserved for diagnostics while model-hot task prompts keep concrete runtime timestamps out of generated artifacts. | Put business dates in caller input or source evidence. |
| Incremental acceptance | TaskBoard acceptance carries dirty/cache markers, card/evidence ids, verdict fingerprints, verification refs, counters, and progress percent. | Use acceptance metadata for task status, board UI, and verification efficiency. |
| Verifier reuse | TaskBoard final verification can reuse unchanged green verifier verdicts and scope dirty verifier input to affected acceptance items. | Let TaskBoard verify only changed acceptance areas while preserving final verifier authority. |
| Setbacks | TaskBoard cards can report `setback` for recoverable readback, repair, patch, or continuation failures. | Render setback as recoverable task state and continue through scheduled recovery work. |
| Final verification | Final verification receives pinned evidence ids, normalized verifier evidence, artifact refs, readback facts, acceptance locators, completion notes, and unresolved-criteria metadata. | Use verifier output plus host guards as the final task acceptance path. |
| Runtime guidance | Active task-strategy executions accept runtime guidance and store it as Workspace guidance records before the next safe boundary. | Use `add_guidance(...)` / `async_add_guidance(...)` for operator context during active task runs. |
| Evidence ledger | `EvidenceEnvelope.evidence_items` is the canonical grounding ledger for Flat synthesis, TaskBoard synthesis, verifier prompts, host guards, and artifact locators. | Bind output claims to evidence ids through structured outputs when source grounding matters. |
| Evidence binding | Host guards reconcile evidence handles, paths, records, URLs, artifacts, action ids, action-call ids, and provenance aliases to canonical ledger ids. | Use visible evidence handles or canonical ids in structured result fields. |
| Artifact delivery | Workspace artifact delivery records write facts, readback facts, SHA-256, byte counts, previews, file refs, manifests, targeted readbacks, and acceptance locators. | Deliver long artifacts through Workspace files and readback-backed artifact refs. |
| Binding repair | Binding repair targets unresolved evidence bindings without regenerating complete deliverables. | Use targeted repair for source-binding failures. |
| Workspace foundation | Workspace is the durable boundary for records, files, evidence links, checkpoints, runtime event storage, artifact refs, file policy metadata, retention anchors, leases, and backend capability reporting. | Bind one Workspace to Agents, TriggerFlow executions, and service workers that share durable context. |
| Local Workspace backend | The local backend uses filesystem storage plus SQLite records, WAL, busy timeout, scope indexes, lineage-aware file roots, and scoped prune. | Use local Workspace for development, local durable state, examples, and filesystem-backed artifacts. |
| Workspace writes | `workspace.put(...)` is the canonical record-write API and supports `content=...` plus profile handlers. | Write records with `workspace.put(...)`. |
| Workspace providers | Workspace backend providers can be registered and selected through the Workspace provider seam. | Register custom backends through Workspace provider registration and bind them at Agent or execution boundaries. |
| Workspace file IO | Workspace file IO owns path containment, file refs, deterministic file info, handler dispatch, text read/write, optional export handlers, and diagnostics. | Keep file IO, export, and file-action roots inside Workspace. |
| Intelligent retrieval | `workspace.retrieve(...)` provides shared intelligent retrieval for records and files with keyword/tag candidates, optional vector/hybrid candidates, rerank, refill, and budgeted packaging. | Use `retrieve(...)` when records/files are being prepared as model context or AgentTask evidence. |
| Deterministic search | `workspace.grep(...)` and `workspace.grep_files(...)` provide deterministic exact search over records and files. | Use `grep(...)` / `grep_files(...)` for exact lookup, debugging, and diagnostics. |
| Workspace store providers | Workspace separates `DBStoreProvider`, `EmbeddingProvider`, and `VectorStoreProvider`: the default DB store is SQLite, and `vector_store_provider="auto"` selects Chroma when available or the SQLite vector table fallback. | Attach record DB adapters through `db_store_provider`, embedding through `embedding_provider`, and vector storage through `vector_store_provider`. Lower-capability DB stores keep the same protocol surface and return empty/absent values for unsupported advanced features. |
| Session memory | `SessionMemory` is a plugin protocol; built-in `AgentlyMemory` stores global/session memory in Workspace records. | Use `AgentlyMemory` for Workspace-backed Session memory and scoped recall. |
| Blocks | Blocks lowers AgentTask ExecutionPlan / PlanBlock work and validated TaskDAG nodes into TriggerFlow-backed ExecutionBlockGraph. | Let AgentTask and TaskDAG use Blocks as the lowering bridge to runtime execution. |
| TaskDAG | TaskDAG owns acyclic dynamic planning, validation, resolver binding, execution, retry metadata, result adaptation, and evidence mapping. | Use TaskDAG directly for explicit DAG-shaped automation and dynamic planning. |
| TriggerFlow | TriggerFlow adds durable snapshots, pause/continue, interrupt/resume ledgers, RuntimeEvent persistence, exchange metadata, compaction policy, load inspection, resource requirements, and idempotent resume ids. | Use TriggerFlow for workflows that need explicit orchestration, waits, resume, runtime streams, and durable execution state. |
| ExecutionExchange | ExecutionExchange provides the exchange manager for approvals, decisions, control messages, clarifications, guidance, and acknowledgments. | Use exchange providers and PolicyApproval-backed wait surfaces for human-in-the-loop flows. |
| ActionRuntime | ActionRuntime separates action planning, dispatch, policy approval, execution, artifact management, resource binding, and observation records. | Mount actions explicitly and inspect ActionRuntime records for execution facts. |
| ExecutionResource | ExecutionResource owns provider-backed runtime binding for ACP, Bash, browser, Docker, MCP, Node.js, Python, and SQLite runtimes. | Bind runtime capabilities as resources instead of embedding provider mechanics in business code. |
| ACP and MCP | ACP is both an Action and `ExecutionResource(kind="acp")`; MCP-declared artifacts flow through Action artifact refs and AgentTask evidence handoff. | Enable ACP or MCP at capability boundaries and consume produced artifact refs through evidence/readback paths. |
| Workspace file actions | Coding-agent Workspace actions expose file read, glob, grep, edit, unified-diff patch, and stale-guarded write behavior. | Use Workspace file actions for repository/file tasks; use shell for tests, builds, and diagnostics. |
| Browse and Search | Browse and Search actions use policy-controlled execution, fallback behavior, bounded outputs, and explicit diagnostics. | Use Browse/Search as mounted capabilities with bounded output records. |
| SkillsExecutor | SkillsExecutor records capability needs, builds context packs, discovers/activates capabilities, and exposes TaskDAG resolver support. | Use `agent.use_skills(...)` and Skills context packs for Skill-guided AgentExecution work. |
| Skills diagnostics | Direct Skills execution emits structured abort diagnostics; react/staged strategies emit budget-exhausted diagnostics. | Surface Skills diagnostics in host logs, streams, or DevTools views. |
| Model requesters | Model requester providers are modularized into credential, handler, request-builder, response-adapter, transport, type, and plugin modules. | Configure model providers through model keys, provider settings, and requester plugins. |
| Model routing | Model routing supports layered model keys, provider fallback, API key pools, request-time key selection, and provider-error retry policies. | Use model keys and pool settings for provider fallback and key rotation. |
| Model liveness | Model response materialization has liveness deadlines for first event, stream, non-streaming response, and materialization stages. | Use liveness diagnostics to understand stalled provider stages. |
| Stream retry status | `ModelRequestResult` exposes `$status` records and plain delta retry replay markers. | Consume `$status` for structured stream state and retry markers for plain text replay boundaries. |
| Telemetry | Model request telemetry records response ids, attempts, run ids, provider/model data, request URLs, duration, usage summaries, side-channel facts, errors, and estimated input/output lengths. | Feed telemetry to DevTools and host diagnostics. |
| Structured output | Output defaults are settings-owned; released parsers include `xml_field`, `hybrid`, JSON, `yaml_literal`, and `flat_markdown`; required fields enforce meaningful values. | Use `.output(...)` and Agently output control for model-owned structured decisions. |
| Image input | VLM helpers build rich image input from local files, URLs, bytes, or structured image payloads. | Use `agent.image(...)` / request image helpers for VLM input. |
| RuntimeEvent | RuntimeEvent is the core runtime event record and EventCenter dispatches RuntimeEvents with delivery policy, coalescing, and background reclaim. | Use RuntimeEvents as the common observation feed. |
| DevTools | DevTools consumes AgentExecution streams, model status, task progress, action observations, exchange states, retry status, terminal summaries, and telemetry. | Pair Agently 4.1.4 with `agently-devtools >=0.1.10,<0.2.0`. |
| Public typing | The package ships `agently/py.typed` and expands typing across facades, protocols, TypedDicts, data contracts, callbacks, stream handlers, result wrappers, Workspace, ExecutionExchange, and TaskBoard helpers. | Use pyright/Pylance-compatible tooling against the installed package. |
| Docs and examples | Docs and examples cover AgentExecution strategy, Workspace retrieval, Session memory, Action Runtime, ExecutionResource, TriggerFlow lifecycle, Skills execution, DevTools observation, structured output, and release workflows. | Start new examples from the 4.1.4 AgentExecution, Workspace, TriggerFlow, Skills, and ActionRuntime surfaces. |
