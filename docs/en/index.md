---
title: Agently Docs
description: Agently documentation home. Quickstart, model setup, requests, actions, services, observability, and TriggerFlow.
keywords: Agently, AI agent framework, documentation, quickstart, TriggerFlow
---

# Agently Docs

> Languages: **English** · [中文](../cn/index.md)

Agently is an AI application development framework focused on stable structured outputs, observable actions, service exposure, observability, and durable workflow orchestration.

This handbook is organized as a learning path. If you have not run a single request yet, start at [Quickstart](start/quickstart.md). If you are integrating Agently into a service, jump to [Async First](start/async-first.md). If you are designing event-driven or long-running flows, go to [TriggerFlow Overview](triggerflow/overview.md).

## Learning path

1. **Get Started** — installation, first request, async-first guidance, project layout
   - [Quickstart](start/quickstart.md)
   - [Async First](start/async-first.md)
   - [Model Setup](start/model-setup.md)
   - [Settings](start/settings.md)
   - [Project Framework](start/project-framework.md)

2. **Make a single request well** — prompt, output schema, validation, response reuse, session memory
   - [Requests Overview](requests/overview.md)
   - [Prompt Management](requests/prompt-management.md)
   - [Schema as Prompt](requests/schema-as-prompt.md)
   - [Output Control](requests/output-control.md)
   - [Model Response](requests/model-response.md)
   - [Session Memory](requests/session-memory.md)
   - [Context Engineering](requests/context-engineering.md)

3. **Actions** — model-callable actions, tool compatibility, MCP, and sandbox backends
   - [Actions Overview](actions/overview.md)
   - [Action Runtime](actions/action-runtime.md)
   - [Tools Compatibility](actions/tools.md)
   - [MCP](actions/mcp.md)

4. **Knowledge and services** — retrieval-backed answers and HTTP / stream exposure
   - [Knowledge Base](knowledge/knowledge-base.md)
   - [FastAPI Service Exposure](services/fastapi.md)

5. **Observability and development** — runtime events, DevTools, and coding-agent guidance
   - [Observability Overview](observability/overview.md)
   - [Event Center](observability/event-center.md)
   - [DevTools](observability/devtools.md)
   - [Coding Agents](development/coding-agents.md)

6. **Models** — protocol layers and per-provider recipes
   - [Models Overview](models/overview.md)
   - [OpenAICompatible](models/openai-compatible.md) · [AnthropicCompatible](models/anthropic-compatible.md)
   - [Providers](models/providers/)

7. **TriggerFlow** — orchestration, lifecycle, state, persistence
   - [Overview](triggerflow/overview.md)
   - [Lifecycle](triggerflow/lifecycle.md)
   - [State and Resources](triggerflow/state-and-resources.md)
   - [Events and Streams](triggerflow/events-and-streams.md)
   - [Patterns](triggerflow/patterns.md) · [Sub-Flow](triggerflow/sub-flow.md)
   - [Persistence and Blueprint](triggerflow/persistence-and-blueprint.md)
   - [Pause and Resume](triggerflow/pause-and-resume.md)
   - [Model Integration](triggerflow/model-integration.md)
   - [Compatibility](triggerflow/compatibility.md)

8. **Playbooks and case studies** — opinionated combinations of the above
   - [Playbooks](playbooks/overview.md)
   - [Case Studies](case-studies/overview.md)

9. **Reference**
   - [Capability Map](reference/capability-map.md)
   - [Glossary](reference/glossary.md)

## Community

- WeChat group: <https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf>
- Discussions: <https://github.com/AgentEra/Agently/discussions>
- Issues: <https://github.com/AgentEra/Agently/issues>
- Twitter / X: <https://x.com/AgentlyTech>
