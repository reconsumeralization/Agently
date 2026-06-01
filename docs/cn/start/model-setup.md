---
title: 模型设置
description: 配置模型 provider、环境变量驱动的配置以及 agent 级覆盖。
keywords: Agently, 模型设置, OpenAICompatible, AnthropicCompatible, 环境变量
---

# 模型设置

> 语言：[English](../../en/start/model-setup.md) · **中文**

Agently 在 `agently.builtins.plugins.ModelRequester` 下提供三个协议层 Request 插件：

- `OpenAICompatible`——任何说 OpenAI Chat Completions API 的端点都走这条路径。覆盖 OpenAI、DeepSeek、Qwen、Ollama、Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE、Gemini-via-OpenAI。
- `OpenAIResponsesCompatible`——OpenAI Responses API 形态的端点。
- `AnthropicCompatible`——Anthropic 原生 API（Claude）。它是独立 requester 插件，请求体构造是 Anthropic 专属（`anthropic_version`、`anthropic_beta`、必填的 `max_tokens`）。

通过填写哪个插件名下的设置，来决定 active requester 是哪一个。

## 全局 vs Agent 级设置

设置是分层的。`Agently.set_settings(...)` 设全局默认；`agent.set_settings(...)` 只覆盖某个 agent。Agent 没显式覆盖的 key 沿用全局值。

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

# 仅这个 agent 覆盖 model
agent.set_settings(
    "OpenAICompatible",
    {"model": "qwen2.5:7b"},
)
```

详细分层规则见 [设置](settings.md)。

## 常见 provider 配置

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

### Ollama（本地）

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen2.5:7b",
})
```

本地服务不需要鉴权时可以省略 `api_key`。

更多 provider（Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE、Gemini）见 [模型概览](../models/overview.md) 与 [Providers](../models/providers/)。

## 环境变量与 dotenv

设置中任何位置使用 `${ENV.<NAME>}` 占位符，会在 settings 被读取时替换为对应环境变量。占位符由 [agently/utils/Settings.py](../../../agently/utils/Settings.py) 解析。

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "${ENV.OPENAI_BASE_URL}",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

要在启动时自动加载 `.env`，从配置文件加载时传 `auto_load_env=True`：

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

## Typed provider settings

Provider settings 可以继续用 dict，也可以用 typed helper class。typed class 只负责
构造提示和提前校验；进入 Agently 后仍会存成同一个 provider namespace 下的 dict。

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

内置 provider helper 位于 `agently.types.settings`。第三方插件可以从自己的插件包
导出 settings class，并通过插件的 `SETTINGS_SCHEMAS` 注册。

## Model profiles 与 key pools

需要按业务场景路由模型时，使用分层配置：

- `model_pool`：业务 key 到 model profile id 的映射。
- `model_profiles`：保存 provider、model、endpoint、request options 和 key pool。
- `api_key_pools`：保存 API key 池与轮换策略。

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

支持 `fixed`、`random`、`round_robin`、`least_used`。key selection 发生在 provider
请求前。Agently 不会在鉴权、额度或计费失败后自动换另一个 credential 重试；应用应由
业务侧判断失败后切换 credential 是否安全。

旧的 `model_pool -> key_pool_strategy -> key_pool` 写法继续兼容。

## 验证连通性

```python
result = (
    Agently.create_agent()
    .input("用一句话打个招呼。")
    .start()
)
print(result)
```

如果能打印出文字，说明模型配置已生效。如果出现鉴权或连接错误，问题在 `base_url` / `api_key` / 网络可达性，与 prompt 或 output schema 无关。

## 另见

- [模型概览](../models/overview.md)——协议层细节
- [设置](settings.md)——分层与环境变量
- [项目结构](project-framework.md)——把模型配置放进文件而不是写在代码里
