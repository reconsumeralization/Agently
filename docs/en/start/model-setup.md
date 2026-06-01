---
title: Model Setup
description: Configure model providers, env-driven settings, and per-agent overrides.
keywords: Agently, model setup, OpenAICompatible, AnthropicCompatible, env, settings
---

# Model Setup

Agently has three protocol-level request plugins shipped in `agently.builtins.plugins.ModelRequester`:

- `OpenAICompatible` — for endpoints that speak the OpenAI Chat Completions API. Covers OpenAI, DeepSeek, Qwen, Ollama, Kimi, GLM, MiniMax, Doubao, SiliconFlow, Groq, ERNIE, and Gemini-via-OpenAI.
- `OpenAIResponsesCompatible` — for endpoints that use the OpenAI Responses API shape.
- `AnthropicCompatible` — for Anthropic's native API (Claude). It is a separate requester plugin with Anthropic-specific request bodies (`anthropic_version`, `anthropic_beta`, `max_tokens`).

You pick which plugin's settings to populate by name. The plugin registry resolves the active requester from those settings.

## Global vs agent-level settings

Settings are hierarchical. `Agently.set_settings(...)` sets the global default; `agent.set_settings(...)` overrides for one agent. Keys you don't set on the agent fall through to global.

```python
from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
)

agent = Agently.create_agent()

# Override the model for this agent only
agent.set_settings(
    "OpenAICompatible",
    {"model": "qwen2.5:7b"},
)
```

See [Settings](settings.md) for the full layering rules.

## Common provider recipes

### OpenAI

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

### Claude / Anthropic

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
})
```

### DeepSeek

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "${ENV.DEEPSEEK_API_KEY}",
    "model": "${ENV.DEEPSEEK_MODEL}",
})
```

### Qwen / DashScope

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "${ENV.DASHSCOPE_API_KEY}",
    "model": "${ENV.DASHSCOPE_MODEL}",
})
```

### Ollama (local)

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen2.5:7b",
})
```

`api_key` can be omitted when the local server doesn't require one.

For more recipes (Kimi, GLM, MiniMax, Doubao, SiliconFlow, Groq, ERNIE, Gemini), see [Models](../models/overview.md) and [Providers](../models/providers/).

## Environment variables and dotenv

Use `${ENV.<NAME>}` placeholders anywhere in settings to pick up environment variables at resolution time. The placeholder is parsed by `agently/utils/Settings.py`.

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "${ENV.OPENAI_BASE_URL}",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

To auto-load `.env` files at startup, pass `auto_load_env=True` when loading from a settings file:

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

## Typed provider settings

Provider settings can be supplied as dicts or typed helper classes. The typed
class only improves construction and validation; internally Agently stores the
same dict under the provider namespace.

```python
from agently import Agently
from agently.types.settings import OpenAICompatibleSettings

Agently.set_settings(
    OpenAICompatibleSettings(
        base_url="https://api.deepseek.com/v1",
        api_key="${ENV.DEEPSEEK_API_KEY}",
        model="deepseek-chat",
        request_options={"temperature": 0},
    )
)
```

Built-in provider helpers live in `agently.types.settings`. Third-party plugins
can expose their own settings classes from their plugin package and register
them through the plugin's `SETTINGS_SCHEMAS`.

## Model profiles and key pools

For applications that route by business scenario, use the layered settings
shape:

- `model_pool`: maps a business key to a model profile id.
- `model_profiles`: stores provider, model, endpoint, request options, and the
  key pool to use.
- `api_key_pools`: stores credential pools and their selection strategy.

```python
Agently.set_settings("model_pool", {
    "support-chat": "deepseek-chat-prod",
    "reasoning": "deepseek-reason-prod",
})

Agently.set_settings("model_profiles", {
    "deepseek-chat-prod": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_pool": "deepseek-prod",
    },
    "deepseek-reason-prod": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-reasoner",
        "api_key_pool": "deepseek-prod",
        "request_options": {"temperature": 0},
    },
})

Agently.set_settings("api_key_pools", {
    "deepseek-prod": {
        "strategy": "round_robin",
        "keys": [
            {"id": "a", "value": "${ENV.DEEPSEEK_API_KEY_A}"},
            {"id": "b", "value": "${ENV.DEEPSEEK_API_KEY_B}"},
        ],
    }
})

agent = Agently.create_agent()
agent.activate_model("reasoning")
```

`fixed`, `random`, `round_robin`, and `least_used` are supported. Key selection
happens before the provider request. Agently does not automatically retry a
different credential after an auth, quota, or billing failure; applications
should decide whether switching credentials after a failed business operation
is safe.

The legacy `model_pool -> key_pool_strategy -> key_pool` form remains accepted.

## Verify connectivity

```python
result = (
    Agently.create_agent()
    .input("Say hello in one short sentence.")
    .start()
)
print(result)
```

If this returns text, model setup is working. If you get an auth or connection error, the failure is in `base_url` / `api_key` / network reachability, not in your prompt or output schema.

## See also

- [Models Overview](../models/overview.md) — protocol layer details
- [Settings](settings.md) — hierarchy and env handling
- [Project Framework](project-framework.md) — putting model config into files instead of code
