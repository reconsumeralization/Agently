---
title: Capability Map
description: Decide which Agently layer your current problem belongs to.
keywords: Agently, capability map, learning path, request, TaskDAG, Dynamic Task, TriggerFlow
---

# Capability Map

This is a navigation aid: figure out which layer your problem lives at, then jump there.

## Ten Layers

| Layer | The question it answers | Where to read |
|---|---|---|
| 1. One request | Can I get one structured answer from a model? | [Quickstart](../start/quickstart.md), [Requests Overview](../requests/overview.md) |
| 2. Stable output | Do I get the fields I expect, every time? | [Schema as Prompt](../requests/schema-as-prompt.md), [Output Control](../requests/output-control.md) |
| 3. Response, context, and memory | Can I reuse a response, assemble task information, continue a conversation, or persist records? | [Model Response](../requests/model-response.md), [Session Memory](../requests/session-memory.md), [Task context, files, and records](../requests/workspace.md) |
| 4. Actions and execution resources | Should the model call functions, MCP servers, or sandboxed commands with managed execution resources? | [Actions Overview](../actions/overview.md), [Action Runtime](../actions/action-runtime.md), [ExecutionResource](../actions/execution-environment.md) |
| 5. Knowledge and services | Do I need retrieval, HTTP, SSE, or WebSocket exposure? | [Knowledge Base](../knowledge/knowledge-base.md), [FastAPI Service Exposure](../services/fastapi.md) |
| 6. Observability and development | Do I need observation events, DevTools, or coding-agent guidance? | [Observability Overview](../observability/overview.md), [Coding Agents](../development/coding-agents.md) |
| 7. Agent auto-orchestration | Should one Agent turn choose among model response, Actions, or Skills? | [Agent Auto-Orchestration](../start/auto-orchestration.md) |
| 8. AgentTask strategy | Should one business task run through plan, bounded execution, evidence, verification, and replan? | [Agent Auto-Orchestration](../start/auto-orchestration.md#agenttask-strategy) |
| 9. TaskDAG / DAG substrate | Should a model or app submit, validate, customize, and execute a DAG? | [TaskDAG / Dynamic Task](../dynamic-task/README.md) |
| 10. Orchestration | Branching, concurrency, pause/resume, persistence | [TriggerFlow Overview](../triggerflow/overview.md) |

Each layer assumes the previous ones work. Skipping ahead is the most common reason something goes wrong — for example, jumping into TriggerFlow before a single request returns the right shape.

## Picking the right path

| Your situation | Where to go |
|---|---|
| Brand new to Agently | [Quickstart](../start/quickstart.md) |
| Output is unstable / sometimes missing fields | [Schema as Prompt](../requests/schema-as-prompt.md) → [Output Control](../requests/output-control.md) |
| Want field-by-field streaming UX | [Async First](../start/async-first.md) → [Output Control](../requests/output-control.md) |
| Need to reuse one response multiple ways | [Model Response](../requests/model-response.md) |
| Multi-turn chat with bounded history | [Session Memory](../requests/session-memory.md) |
| Task needs files, durable records, or bounded multi-source context | [Task context, files, and records](../requests/workspace.md) |
| Explicit workflow loop needs durable records, links, snapshots, and file deliverables | [TriggerFlow Overview](../triggerflow/overview.md) + [Task context, files, and records](../requests/workspace.md); see `examples/task_workspace/task_context_loop_foundation.py` |
| Need the model to call tools / MCP servers | [Action Runtime](../actions/action-runtime.md) |
| Need common Python / shell / TaskWorkspace / Node.js / common-language code / SQLite ability | [Action Runtime](../actions/action-runtime.md), start with `agent.enable_python(...)`, `agent.enable_shell(...)`, `agent.enable_task_workspace_file_actions(...)`, `agent.enable_nodejs(...)`, `agent.enable_code_runtime(...)`, or `agent.enable_sqlite(...)` |
| Need web search or page browse | [Action Runtime](../actions/action-runtime.md), use `from agently.builtins.actions import Search, Browse` and `agent.use_actions(...)` |
| Need managed MCP/sandbox/process/browser/SQLite lifecycle before execution | [ExecutionResource](../actions/execution-environment.md), usually for action/plugin authors |
| Deciding where a new extension belongs | [Extension Boundaries](../architecture/extension-boundaries.md) |
| Building a service over agents | [FastAPI Service Exposure](../services/fastapi.md) |
| Need to inspect observation events | [Event Center](../observability/event-center.md) → [DevTools](../observability/devtools.md) |
| Not sure whether to use ModelRequest, AgentExecution, TaskDAG, or TriggerFlow | [Execution Layer Selection](execution-layer-selection.md) |
| Need one Agent turn to choose between model response, Actions, or Skills | [Agent Auto-Orchestration](../start/auto-orchestration.md) |
| Single business task needs plan → bounded execution → evidence → verification → replan | [Agent Auto-Orchestration](../start/auto-orchestration.md#agenttask-strategy), start with `agent.create_task(...)` and consume it as an `AgentExecution` result |
| Need to inspect task-frame/Skill lowering, or the optional TaskDAG Blocks carrier | [Blocks Lifecycle](blocks-lifecycle.md) |
| Model-generated or app-generated DAG that must be planned, validated, customized, and executed | [TaskDAG / Dynamic Task](../dynamic-task/README.md) |
| Multi-stage workflow with branching | [TriggerFlow Overview](../triggerflow/overview.md) → [Patterns](../triggerflow/patterns.md) |
| Long-running flow with human approval / interrupt | [Pause and Resume](../triggerflow/pause-and-resume.md) |
| Need to save and resume execution across restarts | [Persistence and Blueprint](../triggerflow/persistence-and-blueprint.md) |
| Migrating from `.end()` / `set_result()` / old runtime_data | [TriggerFlow Compatibility](../triggerflow/compatibility.md) |

## Decision shortcuts

- "Do I need TriggerFlow?" — Only when there are explicit stages, branching, concurrency, or wait/resume. A single request with retries does not need TriggerFlow.
- "Which execution layer?" — Use [Execution Layer Selection](execution-layer-selection.md) to separate execution, context, file, and persistence owners.
- "TaskDAG, Dynamic Task, or TriggerFlow?" — Use TaskDAG modules when the graph is submitted as data and must be planned, validated, pruned, resolved to handlers, customized, and executed. Use the DynamicTask facade when ordinary app code wants one compact compatibility entrypoint. Use TriggerFlow directly when you own the workflow topology in code.
- "Where do Blocks fit?" — Blocks lower `ExecutionPlan` / `PlanBlock` instances
  to TriggerFlow-backed `ExecutionBlockGraph`. Validated TaskDAG nodes use that
  bridge only through explicit `compile_blocks(...)` / `async_run_blocks(...)`;
  the default TaskDAG path goes directly to TriggerFlow. See
  [Blocks Lifecycle](blocks-lifecycle.md).
- "Sync or async?" — Sync for scripts and demos. Async for services, streaming UI, and TriggerFlow. See [Async First](../start/async-first.md).
- "Action or tool API?" — New code: `Agently.action` / `agent.use_actions(...)`, built-in packages from `agently.builtins.actions`, plus helpers such as `agent.enable_python(...)`, `agent.enable_shell(...)`, and `agent.enable_task_workspace_file_actions(...)`; see [Action Runtime](../actions/action-runtime.md).
- "Agent start or explicit API?" — Use `agent.start()` for candidate-driven model/Action auto-orchestration with optional Skill context, and `agent.create_execution()` when the caller needs route diagnostics or process streaming. Use TaskDAG / DynamicTask directly when the application or visual automation surface owns a submitted DAG.
- "AgentTask or TriggerFlow?" — Use `agent.create_task(...)` when the model owns the task-level plan, verification, and replan loop for one business task; it returns a task-strategy `AgentExecution`, so read result/meta/stream/task refs through the AgentExecution result facade. Use TriggerFlow directly when the application owns the exact stages, branching, and wait/resume topology.
- "Executor or ExecutionResource?" — Executors run one call. ExecutionResource prepares reusable or policy-bound dependencies before that call; see [ExecutionResource](../actions/execution-environment.md).
- "Core API or syntax sugar?" — App developers should start with built-in actions and Agent Component helpers. Core managers and providers are for framework, action, and plugin developers; see [Extension Boundaries](../architecture/extension-boundaries.md).
- "Observation event or TriggerFlow event?" — Observation events belong to [Event Center](../observability/event-center.md). `emit` / `when` and runtime stream belong to [TriggerFlow Events and Streams](../triggerflow/events-and-streams.md).
