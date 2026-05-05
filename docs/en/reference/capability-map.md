---
title: Capability Map
description: Decide which Agently layer your current problem belongs to.
keywords: Agently, capability map, learning path, request, TriggerFlow
---

# Capability Map

This is a navigation aid: figure out which layer your problem lives at, then jump there.

## Seven layers

| Layer | The question it answers | Where to read |
|---|---|---|
| 1. One request | Can I get one structured answer from a model? | [Quickstart](../start/quickstart.md), [Requests Overview](../requests/overview.md) |
| 2. Stable output | Do I get the fields I expect, every time? | [Schema as Prompt](../requests/schema-as-prompt.md), [Output Control](../requests/output-control.md) |
| 3. Response and memory | Can I reuse one response, or continue a bounded conversation? | [Model Response](../requests/model-response.md), [Session Memory](../requests/session-memory.md) |
| 4. Actions | Should the model call functions, MCP servers, or sandboxed commands? | [Actions Overview](../actions/overview.md), [Action Runtime](../actions/action-runtime.md) |
| 5. Knowledge and services | Do I need retrieval, HTTP, SSE, or WebSocket exposure? | [Knowledge Base](../knowledge/knowledge-base.md), [FastAPI Service Exposure](../services/fastapi.md) |
| 6. Observability and development | Do I need runtime events, DevTools, or coding-agent guidance? | [Observability Overview](../observability/overview.md), [Coding Agents](../development/coding-agents.md) |
| 7. Orchestration | Branching, concurrency, pause/resume, persistence | [TriggerFlow Overview](../triggerflow/overview.md) |

Each layer assumes the previous ones work. Skipping ahead is the most common reason something goes wrong — for example, jumping into TriggerFlow before a single request returns the right shape.

## Picking the right path

| Your situation | Where to go |
|---|---|
| Brand new to Agently | [Quickstart](../start/quickstart.md) |
| Output is unstable / sometimes missing fields | [Schema as Prompt](../requests/schema-as-prompt.md) → [Output Control](../requests/output-control.md) |
| Want field-by-field streaming UX | [Async First](../start/async-first.md) → [Output Control](../requests/output-control.md) |
| Need to reuse one response multiple ways | [Model Response](../requests/model-response.md) |
| Multi-turn chat with bounded history | [Session Memory](../requests/session-memory.md) |
| Need the model to call tools / MCP servers | [Action Runtime](../actions/action-runtime.md) |
| Building a service over agents | [FastAPI Service Exposure](../services/fastapi.md) |
| Need to inspect runtime events | [Event Center](../observability/event-center.md) → [DevTools](../observability/devtools.md) |
| Multi-stage workflow with branching | [TriggerFlow Overview](../triggerflow/overview.md) → [Patterns](../triggerflow/patterns.md) |
| Long-running flow with human approval / interrupt | [Pause and Resume](../triggerflow/pause-and-resume.md) |
| Need to save and resume execution across restarts | [Persistence and Blueprint](../triggerflow/persistence-and-blueprint.md) |
| Migrating from `.end()` / `set_result()` / old runtime_data | [TriggerFlow Compatibility](../triggerflow/compatibility.md) |

## Decision shortcuts

- "Do I need TriggerFlow?" — Only when there are explicit stages, branching, concurrency, or wait/resume. A single request with retries does not need TriggerFlow.
- "Sync or async?" — Sync for scripts and demos. Async for services, streaming UI, and TriggerFlow. See [Async First](../start/async-first.md).
- "Action or tool API?" — New code: `Agently.action` / `agent.use_actions(...)`. Existing `tool_func` / `use_tools` / `use_mcp` / `use_sandbox` keep working but are positioned as a compatibility surface; see [Action Runtime](../actions/action-runtime.md).
- "Runtime event or TriggerFlow event?" — Runtime events belong to [Event Center](../observability/event-center.md). `emit` / `when` and runtime stream belong to [TriggerFlow Events and Streams](../triggerflow/events-and-streams.md).
