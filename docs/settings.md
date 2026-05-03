---
title: Settings
description: "Agently settings reference for OpenAICompatible, session, runtime logs, and core configurations."
keywords: "Agently,settings,OpenAICompatible,session,runtime logs"
---

# Settings

> Applies to: 4.0.8.1+

In production you usually want consistent behavior, shared defaults, and controllable logs. Agently settings are hierarchical: global → Agent → Request/Session. Use `set_settings` with dot-path keys.

## 1. How to configure

Global and local overrides:

```python
from agently import Agently

Agently.set_settings("runtime.show_model_logs", True)

agent = Agently.create_agent()
agent.set_settings("prompt.add_current_time", False)
```

OpenAICompatible path alias:

```python
Agently.set_settings("OpenAICompatible", {
  "base_url": "https://api.openai.com/v1",
  "api_key": "YOUR_API_KEY",
  "model": "gpt-4o-mini"
})
```

Environment placeholder substitution (load from `.env`/environment):

```python
Agently.set_settings(
  "OpenAICompatible",
  { "api_key": "${ENV.OPENAI_API_KEY}" },
  auto_load_env=True,
)
```

## 2. Core settings

### 2.1 Storage (Not available now)

| Key | Default | Description |
| --- | --- | --- |
| `storage.db_url` | `sqlite+aiosqlite:///localstorage.db` | Local storage database URL. |

### 2.2 Prompt

| Key | Default | Description |
| --- | --- | --- |
| `prompt.add_current_time` | `true` | Injects current time into the `info` section. |
| `prompt.role_mapping` | See below | Role mapping for prompt → message conversion. |
| `prompt.prompt_title_mapping` | See below | Section title mapping for prompt assembly. |

Default `prompt.role_mapping`:

```yaml
system: system
developer: developer
assistant: assistant
user: user
_: assistant
```

Default `prompt.prompt_title_mapping`:

```yaml
system: SYSTEM
developer: DEVELOPER DIRECTIONS
chat_history: CHAT HISTORY
info: INFO
tools: TOOLS
action_results: ACTION RESULTS
instruct: INSTRUCT
examples: EXAMPLES
input: INPUT
output: OUTPUT
output_requirement: OUTPUT REQUIREMENT
```

### 2.3 Response

| Key | Default | Description |
| --- | --- | --- |
| `response.streaming_parse` | `false` | Global switch for structured streaming parsing. |
| `response.streaming_parse_path_style` | `dot` | Path style for streaming output: `dot` or `slash`. |

## 3. Runtime and logs

| Key | Default | Description |
| --- | --- | --- |
| `runtime.raise_error` | `true` | Raise normal errors (otherwise log only). |
| `runtime.raise_critical` | `true` | Raise critical errors (otherwise log only). |
| `runtime.show_model_logs` | `false` | Show model request/response logs. |
| `runtime.show_tool_logs` | `false` | Show tool invocation logs. |
| `runtime.show_trigger_flow_logs` | `false` | Show TriggerFlow execution logs. |
| `runtime.httpx_log_level` | `WARNING` | `httpx/httpcore` log level. |

Debug shortcut:

| Key | Value | Description |
| --- | --- | --- |
| `debug` | `true/false` | Toggles `show_*_logs` and updates `httpx_log_level`. |

## 4. Plugin activation

Use `plugins.<Type>.activate` to choose the active implementation:

| Plugin type | Default |
| --- | --- |
| `plugins.PromptGenerator.activate` | `AgentlyPromptGenerator` |
| `plugins.ModelRequester.activate` | `OpenAICompatible` |
| `plugins.ResponseParser.activate` | `AgentlyResponseParser` |
| `plugins.ToolManager.activate` | `AgentlyToolManager` |

## 5. OpenAICompatible settings

Namespace: `plugins.ModelRequester.OpenAICompatible` (alias: `OpenAICompatible`).

For parameter precedence, `tools` behavior, and practical `temperature` setup:
[/en/openai-api-format](/en/openai-api-format)

### 5.1 Model and endpoints

| Key | Default | Description |
| --- | --- | --- |
| `model_type` | `chat` | Request type: `chat`/`completions`/`embeddings`. |
| `model` | `null` | Explicit model name; falls back to `default_model`. |
| `default_model.chat` | `gpt-4.1` | Default chat model. |
| `default_model.completions` | `gpt-3.5-turbo-instruct` | Default completions model. |
| `default_model.embeddings` | `text-embedding-ada-002` | Default embeddings model. |
| `base_url` | `https://api.openai.com/v1` | API base URL. |
| `full_url` | `null` | Full request URL (overrides `base_url + path_mapping`). |
| `path_mapping.chat` | `/chat/completions` | Chat endpoint path. |
| `path_mapping.completions` | `/completions` | Completions endpoint path. |
| `path_mapping.embeddings` | `/embeddings` | Embeddings endpoint path. |

### 5.2 Auth and network

| Key | Default | Description |
| --- | --- | --- |
| `api_key` | `null` | API key shortcut. |
| `auth` | `null` | Custom auth structure (`api_key`/`headers`/`body`). |
| `headers` | `{}` | Extra request headers. |
| `proxy` | `null` | Proxy address (passed to httpx). |
| `timeout` | See below | `httpx.Timeout` config. |
| `client_options` | `{}` | Extra args for `httpx.AsyncClient`. |

Default `timeout`:

```yaml
connect: 30.0
read: 600.0
write: 30.0
pool: 30.0
```

### 5.3 Request options

| Key | Default | Description |
| --- | --- | --- |
| `request_options` | `{}` | Request body options (e.g. `temperature`), merged with prompt `options`. |
| `stream` | `true` | Use streaming (embeddings force `false`). |

### 5.4 Response mapping

| Key | Default | Description |
| --- | --- | --- |
| `content_mapping` | See below | Map provider fields to unified events and values. |
| `content_mapping_style` | `dot` | Field path style: `dot`/`slash`. |
| `yield_extra_content_separately` | `true` | Emit extra fields as separate events. |
| `strict_role_orders` | `true` | Enforce role ordering when building messages. |
| `rich_content` | `false` | Enable rich content (attachments). |

Default `content_mapping`:

```yaml
id: id
role: choices[0].delta.role
reasoning: choices[0].delta.reasoning_content
delta: choices[0].delta.content
tool_calls: choices[0].delta.tool_calls
done: null
usage: usage
finish_reason: choices[0].finish_reason
extra_delta:
  function_call: choices[0].delta.function_call
extra_done: null
```

## 6. Session (v4.0.8.1+)

Session was redesigned in `v4.0.8.1`. Default Agent now uses `SessionExtension` for context injection and request recording.

| Key | Default | Description |
| --- | --- | --- |
| `session.max_length` | `null` | Max window length. When set to an integer, built-in `simple_cut` trims `context_window`. |
| `session.input_keys` | `null` | Selector paths for recorded input fields in `finally` hook. `null` records full request text. |
| `session.reply_keys` | `null` | Selector paths for recorded output fields in `finally` hook. `null` records full output. |

Notes:

- selectors support both `dot` and `slash` path styles
- input selectors support `.request.*` and `.agent.*` prefixes
- memo updates no longer depend on legacy `session.memo.*` configs; implement via `Session.register_*_handler()`

## 7. Compatibility and deprecations

| Item | Status | Notes |
| --- | --- | --- |
| `set_debug_console(\"ON\")` | Deprecated | Only emits warning; no runtime behavior change. |
| `ChatSessionExtension` | Deprecated | Use default `SessionExtension` instead. |

If your code still follows legacy helper style, migrate to `activate_session` / `deactivate_session` + `session.max_length` + custom strategies.
