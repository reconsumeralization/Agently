---
title: Project Framework
description: "Agently project framework for ordinary developers: how to split settings, prompts, services, workflow, tools, and tests."
keywords: "Agently,project framework,project structure,prompts,workflow"
---

# Project Framework

Once your first request works, the next problem is not API syntax. It is where the code and prompt assets should live.

## Recommended split

- `settings/`: model settings and environment-specific config
- `prompts/`: YAML or JSON prompt assets
- `services/`: request-side business services
- `workflow/`: TriggerFlow orchestration and runtime wiring
- `tools/`: tool definitions and MCP registration
- `tests/`: request-level and workflow-level verification

## Why this split

- prompts should not be buried inside business logic
- workflow code should stay separate from one-request service code
- tools and MCP integration should remain explicit and reusable

## Next

- Request-side prompt assets: [Prompt Management Overview](/en/prompt-management/overview)
- Workflow boundary: [Workflow and Extensions Overview](/en/workflow-extensions)
- Production runtime guidance: [Async First](/en/async-support)
