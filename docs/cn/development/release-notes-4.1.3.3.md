---
title: Agently 4.1.3.3 Release Notes
description: Agently 4.1.3.3 的 typed settings/options、model profiles、API key pool failover、runtime handler ownership、core package refactor 和 image input release note。
keywords: Agently, release notes, 4.1.3.3, typed settings, model profiles, API key pool, image input
---

# Agently 4.1.3.3 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.3.md) · **中文**

Agently 4.1.3.3 是面向 4.1.4 AgentTask 目标的 release-line hardening slice。
它关闭 #274 和 #276 里的公开配置问题，让模型请求 retry 与 runtime event 归属更清晰，
并为常见 VLM 请求新增轻量的 `.image(...)` 便捷 API。

## 主要变化

- `agent.create_execution(...)` 现在支持 dict-compatible typed `options=...`。
  第一批 route consumer 是 `routes.skills.effort`，因此 Skills auto-orchestration
  可以和显式 Skills 调用一样使用 `fast` / `normal` / `max` effort 控制。
- 模型路由支持推荐的分层形态：`model_pool -> model_profiles -> api_key_pools`。
  业务模型 key 可以解析到包含 provider、model、base URL、request/client options
  和 API key pool 的 provider profile。
- `api_key_pools` 现在区分独立请求的 key selection 与 provider 错误后的 failover。
  selection 和 failover 都支持内置策略与自定义 handler。
- Model requester plugins 通过 handler contracts 与 core 配合。框架官方 runtime
  events 由 core 发送，provider plugins 返回 observations、errors 和 decisions，
  再由 core 映射为官方事件。
- 内置 model requester packages 与 `agently/core` 布局已整理为 package directories，
  并保留稳定 public exports。既有 public imports 继续可用。
- `agent.image(...)` 和 `request.image(...)` 可以用“问题 + 本地文件或远程 URL”
  构造 VLM 图片输入。本地 PNG、JPEG、WebP、GIF、BMP 会转成
  `data:<mime>;base64,...` image URL。
- `output_format="instant"` 文档现在明确说明即时字段流、价值，以及它与结构化输出模式的关系。

## 使用形态

通过 execution options 传递 Skills route effort：

```python
from agently.types.options import ExecutionOptions, SkillsRouteOptions

execution = agent.create_execution(
    options=ExecutionOptions(
        routes={"skills": SkillsRouteOptions(effort="normal")},
    ),
)
```

分层模型路由：

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

VLM 图片输入：

```python
result = (
    agent
    .image(
        question="对比这两张截图，列出可见差异。",
        files=["./before.png", "./after.png"],
    )
    .start()
)
```

## 兼容性

- Package version: `4.1.3.3`。
- Release manifest: `compatibility/releases/4.1.3.3.json`。
- 推荐 `agently-devtools`: `>=0.1.6,<0.2.0`。
- 既有 dict settings、legacy `model_pool`、`key_pool_strategy` 和 `key_pool`
  形态继续兼容。
- `.attachment([...])` 仍是底层 rich-content 输入面。新的 `.image(...)` 是“问题 +
  图片来源”的便捷写法。

## Issue 范围

本 release 在开发线上标记 #274 和 #276 已解决。它为 4.1.4 AgentTask V1 准备配置、
请求与 runtime substrate，但不实现 AgentTask 本身。
