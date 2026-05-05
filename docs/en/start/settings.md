---
title: Settings
description: How Agently settings layer from global to agent to request, with env placeholders.
keywords: Agently, settings, set_settings, hierarchy, env, dotenv
---

# Settings

Agently settings are a hierarchical key-value store. Three scopes:

| Scope | Set with | Visible to |
|---|---|---|
| Global | `Agently.set_settings(...)` | every agent and request created after the call |
| Agent | `agent.set_settings(...)` | requests built from that agent |
| Request / runtime | `start(..., max_retries=...)` and similar method-level args | one call only |

Lower-scope keys override higher-scope keys; keys you don't override inherit through.

## Setting paths

The first argument to `set_settings(...)` is a dotted path. Common paths:

| Path | Meaning |
|---|---|
| `OpenAICompatible` | shorthand alias resolved to `plugins.ModelRequester.OpenAICompatible` |
| `AnthropicCompatible` | shorthand for the Claude requester |
| `plugins.ModelRequester.<Name>` | full path; same as the shorthand |
| `debug` | enable streaming console logs of the model request |
| `runtime.session_id` | bind a request to an explicit session id |

You can also pass a single dict and Agently merges by key:

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "model": "${ENV.OPENAI_MODEL}",
})
```

## Reading settings back

```python
agent_settings = agent.settings.get("plugins.ModelRequester.OpenAICompatible", {})
print(agent_settings.get("model"))
```

`settings.get(path, default)` walks the dotted path; missing keys return the default.

## Env placeholders

Anywhere in a settings value, `${ENV.<NAME>}` is replaced with the matching environment variable when the settings are read. The pattern is parsed by [agently/utils/Settings.py](../../../agently/utils/Settings.py).

```python
Agently.set_settings("OpenAICompatible", {
    "api_key": "${ENV.OPENAI_API_KEY}",
})
```

## Loading from files

For non-trivial projects, keep settings in YAML / TOML / JSON instead of inline Python:

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

`auto_load_env=True` loads any `.env` in the working directory before resolving `${ENV.*}` placeholders.

If you need a standalone `Settings` object, use `Settings().load(...)`:

```python
from agently.utils import Settings

settings = Settings()
settings.load("yaml_file", "settings.yaml", auto_load_env=True)
```

A typical layout for a project that uses files is in [Project Framework](project-framework.md).

## Debug toggle

```python
Agently.set_settings("debug", True)
```

Prints the streaming model request log to the console. Useful for verifying that prompt slots, output schema, and retries are doing what you expect.

## See also

- [Model Setup](model-setup.md) — provider-specific recipes
- [Project Framework](project-framework.md) — file-based settings layout
