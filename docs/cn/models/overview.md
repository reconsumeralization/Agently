---
title: 模型概览
description: Agently 如何用三个协议层 Requester 组织模型 provider。
keywords: Agently, 模型, OpenAICompatible, AnthropicCompatible, providers
---

# 模型概览

> 语言：[English](../../en/models/overview.md) · **中文**

Agently 在协议层有三个 Request 插件，外加一组按 provider 整理的配置 recipe，每个 recipe 选其中一个插件。

## 分层视图

```text
应用代码
   │
   ▼
ModelRequest  ──►  ModelResponse
   │
   ▼
ModelRequester 插件（"协议层"）
   ├── OpenAICompatible             ◄── 多数 provider（Chat Completions）
   ├── OpenAIResponsesCompatible    ◄── Responses API 变体
   └── AnthropicCompatible          ◄── Claude
   │
   ▼
HTTP 调用模型端点
```

协议插件负责构造 HTTP 请求体并解析返回。Provider 配置只是设置预设，把 base_url / model 等填到对应插件下。

## 为什么是三个插件而不是一个

旧文档曾暗示「所有 provider 都走 OpenAICompatible」——这已经不准确。`OpenAICompatible`、`OpenAIResponsesCompatible`、`AnthropicCompatible` 是相互独立的 requester 插件；每个插件都直接实现 `ModelRequester` 协议，并各自维护自己的协议映射。Anthropic 尤其会构造自己的请求体——`anthropic_version`、`anthropic_beta`、必填的 `max_tokens`，以及 Claude 期望的 `messages`/`system` 字段形态。这些差异已经足够把 Claude 配错——所以新文档把它单独成一条路径。

如果你指向 `https://api.anthropic.com`（或某个走相同协议的 Claude 兼容代理），用 [AnthropicCompatible](anthropic-compatible.md)。其他情况（OpenAI、DeepSeek、Qwen、Ollama、Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE、走 OpenAI 兼容模式的 Gemini，以及任何说 OpenAI Chat Completions API 的私有网关），用 [OpenAICompatible](openai-compatible.md)。

## 插件选择表

| 你在调用 | 用哪个插件 |
|---|---|
| OpenAI、Azure OpenAI、Gemini-via-OpenAI | `OpenAICompatible` |
| DeepSeek、Qwen、Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE | `OpenAICompatible` |
| Ollama 或任何 OpenAI 兼容的本地服务 | `OpenAICompatible` |
| Anthropic / Claude（原生 API） | `AnthropicCompatible` |
| 说 OpenAI Chat Completions API 的私有网关 | `OpenAICompatible` |
| 说 OpenAI Responses API 的私有网关 | `OpenAIResponsesCompatible` |
| 说 Anthropic Messages API 的私有网关 | `AnthropicCompatible` |

## 最小配置

```python
from agently import Agently

# OpenAI 兼容
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})

# 或 Anthropic
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
})
```

每个 provider 的 recipe（环境变量、常用 model 名、base URL）见 [Providers](providers/)。

## 用 Model Pool 切换模型

需要使用多个模型的应用，可以先用 `model_pool` 配置模型别名，再用
`activate_model(...)` 切换当前 Agent 模型。别名可以是具体的运行含义，例如
`ollama-qwen2.5` 或 `deepseek-v4`。

```python
agent.set_settings("model_pool", {
    "ollama-qwen2.5": "qwen2.5:7b",
    "deepseek-v4": "deepseek-chat",
})
agent.set_settings("key_pool", {
    "local": "ollama",
    "deepseek-main": "${ENV.DEEPSEEK_API_KEY}",
    "deepseek-backup": "${ENV.DEEPSEEK_BACKUP_API_KEY}",
})
agent.set_settings("key_pool_strategy", {
    "qwen2.5:7b": {"mode": "fixed", "pool": ["local"]},
    "deepseek-chat": {"mode": "round_robin", "pool": ["deepseek-main", "deepseek-backup"]},
})

result = (
    agent
    .activate_model("ollama-qwen2.5")
    .input("Summarize this incident.")
    .output({"summary": (str, "incident summary", True)})
    .start()
)
```

`activate_model(...)` 影响后续 Agent 自己创建和持有的请求，包括链式
`agent.input(...).start()` 和 `agent.create_execution()`。如果只想覆盖单次调用，
使用 `agent.create_request(model_key="deepseek-v4")`。

API key 会在请求时根据 `key_pool_strategy` 自动选择：`fixed`、`random`、
`round_robin` 或 `least_used`。Agently 4.1.3 不会在 provider 返回鉴权、额度或费用
类错误后自动换另一个 key 重试；这类失败会暴露给应用代码，由业务系统决定该操作是否
适合更换凭据重试。

## 插件源码位置

- [agently/builtins/plugins/ModelRequester/OpenAICompatible.py](../../../agently/builtins/plugins/ModelRequester/OpenAICompatible.py)
- [agently/builtins/plugins/ModelRequester/OpenAIResponsesCompatible.py](../../../agently/builtins/plugins/ModelRequester/OpenAIResponsesCompatible.py)
- [agently/builtins/plugins/ModelRequester/AnthropicCompatible.py](../../../agently/builtins/plugins/ModelRequester/AnthropicCompatible.py)

如果某个 provider 不在以上协议族（极少见），可以新增一个 requester 插件；但实际上几乎所有商用端点要么提供 OpenAI 兼容模式、Responses 形态，要么对齐 Anthropic 协议，所以这些内置插件覆盖大部分场景。

## 另见

- [OpenAICompatible 详情](openai-compatible.md)
- [AnthropicCompatible 详情](anthropic-compatible.md)
- [Providers](providers/)——按 provider 分组的 recipe
- [模型设置](../start/model-setup.md)——快速入门级配置
- [设置](../start/settings.md)——环境变量与分层
