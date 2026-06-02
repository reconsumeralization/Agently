---
title: Agently 4.1.3.3 Release Notes
description: Agently 4.1.3.3 release notes for typed settings/options, model profiles, API key pool failover, runtime handler ownership, core package refactors, and image input.
keywords: Agently, release notes, 4.1.3.3, typed settings, model profiles, API key pool, image input
---

# Agently 4.1.3.3 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.3.md)

Agently 4.1.3.3 is a release-line hardening slice for the 4.1.4 AgentTask
target. It closes the public configuration issues in #274 and #276, makes model
request retries and runtime event ownership easier to reason about, and adds a
small `.image(...)` convenience API for common VLM requests.

## What Changed

- `agent.create_execution(...)` now accepts dict-compatible typed
  `options=...`. The first routed consumer is `routes.skills.effort`, so Skills
  auto-orchestration can use the same `fast` / `normal` / `max` effort control
  as explicit Skills calls.
- Model routing now supports the recommended layered shape:
  `model_pool -> model_profiles -> api_key_pools`. A business model key can
  resolve to a provider profile with provider name, model, base URL,
  request/client options, and an API key pool.
- `api_key_pools` now separates request-time key selection from provider-error
  failover. Selection and failover both support built-in policies and custom
  handlers.
- Model requester plugins now cooperate with core through handler contracts.
  Official runtime events for framework-owned flows are emitted by core, while
  provider plugins return observations, errors, and decisions for core to map.
- The built-in model requester packages and `agently/core` layout were
  reorganized into package directories with stable public exports. Existing
  public imports continue to work.
- `agent.image(...)` and `request.image(...)` now build VLM image input from a
  question plus local files or remote URLs. Local PNG, JPEG, WebP, GIF, and BMP
  files are converted to `data:<mime>;base64,...` image URLs.
- `output_format="instant"` documentation now clearly describes immediate
  field streaming, its value, and its relationship to structured output modes.

## Usage Shapes

Skills route effort through execution options:

```python
from agently.types.options import ExecutionOptions, SkillsRouteOptions

execution = agent.create_execution(
    options=ExecutionOptions(
        routes={"skills": SkillsRouteOptions(effort="normal")},
    ),
)
```

Layered model routing:

```python
agent.set_settings("model_pool", {"skills.reason": "deepseek.reasoner"})
agent.set_settings("model_profiles", {
    "deepseek.reasoner": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-reasoner",
        "api_key_pool": "deepseek.prod",
    },
})
agent.set_settings("api_key_pools", {
    "deepseek.prod": {
        "selection": {"strategy": "round_robin"},
        "failover": {"strategy": "try_next", "retry_status_codes": [429]},
        "keys": [
            {"id": "primary", "value": "${ENV.DEEPSEEK_API_KEY}"},
            {"id": "secondary", "value": "${ENV.DEEPSEEK_API_KEY_2}"},
        ],
    },
})
```

VLM image input:

```python
result = (
    agent
    .image(
        question="Compare these two screenshots and list the visible differences.",
        files=["./before.png", "./after.png"],
    )
    .start()
)
```

## Compatibility

- Package version: `4.1.3.3`.
- Release manifest: `compatibility/releases/4.1.3.3.json`.
- Recommended `agently-devtools`: `>=0.1.6,<0.2.0`.
- Existing dict-based settings, legacy `model_pool`, `key_pool_strategy`, and
  `key_pool` shapes remain compatible.
- `.attachment([...])` remains the low-level rich-content input surface. The new
  `.image(...)` API is convenience syntax for question plus image sources.

## Issue Scope

This release marks #274 and #276 as solved on the development line. It prepares
the configuration and request/runtime substrate for 4.1.4 AgentTask V1, but it
does not implement AgentTask itself.
