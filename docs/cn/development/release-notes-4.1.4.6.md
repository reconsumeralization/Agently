---
title: Agently 4.1.4.6 发布说明
description: 标准包版本入口、统一 reasoning 生命周期，以及保持 live resource 的 TriggerFlow sub-flow。
keywords: Agently, 4.1.4.6, version, reasoning, thinking, DeepSeek, Anthropic, Responses, TriggerFlow
---

# Agently 4.1.4.6 发布说明

Agently 4.1.4.6 是基于 4.1.4.5 的聚焦兼容性补丁：

- 通过标准 `__version__` 名称公开包版本；
- 统一 OpenAI-compatible Chat Completions、Anthropic-compatible Messages 和
  Responses adapter 的生成 reasoning 生命周期；
- live runtime resource 穿过 TriggerFlow sub-flow 边界时保持对象身份。

[#342](https://github.com/AgentEra/Agently/issues/342) 管理的 sandbox provider
草案不进入本次发布。

## 标准包版本入口

可以使用以下任一公开入口：

```python
import agently
from agently import Agently

assert agently.__version__ == "4.1.4.6"
assert Agently.__version__ == agently.__version__
```

4.1.4.5 误引入的 `agently.version` 和 `Agently.version` 不作为 alias 保留。

## 统一 reasoning 生命周期

Provider 特有请求参数继续放在 `request_options` 下，并原样透传。例如
OpenAI-compatible endpoint 可以这样启用 thinking：

```yaml
plugins:
  ModelRequester:
    OpenAICompatible:
      model: deepseek-v4-flash
      request_options:
        thinking:
          type: enabled
```

模型生成的 reasoning 统一映射到以下公开响应生命周期：

- `reasoning_delta`：流式 reasoning 增量；
- `reasoning_done`：只发送一次的最终 reasoning；
- `original_done`：保留 provider 原始完整响应。

OpenAI-compatible Chat Completions 现在会保留非流式和流式
`reasoning_content`。Anthropic-compatible response 会正确收敛 `thinking`
block、signature、tool-use continuation，以及空或非空的 terminal reasoning
事件。Responses adapter 会映射 reasoning summary 的 delta/done 事件和完整输出
中的 reasoning item，同时不会把 `reasoning.effort`、`reasoning.summary` 等请求
配置误当成生成内容。

这是 adapter 层的通用规范化，不是 DeepSeek 特例；使用相应兼容协议的 provider
会获得相同的生命周期行为。

## TriggerFlow sub-flow runtime resource

直接的 `resources -> resources` capture 会按对象身份传递 live object：

```python
parent_flow.to_sub_flow(
    child_flow,
    capture={"resources": {"service": "resources.service"}},
)

execution = parent_flow.create_execution(
    auto_close=False,
    runtime_resources={"service": live_service},
)
```

隔离的 child-flow template 也会按对象身份继承其 flow-level runtime resource。
client、callback、lock、event 和其他 live handle 不会被 deep-copy。普通
input/value capture 仍然复制隔离，因此该修复不会把一般 sub-flow 数据变成共享
可变状态。

保存 execution 时仍不会序列化 live resource；恢复 root execution 时，host
必须重新注入所需 resource。

## 兼容性

- Python：`>=3.10`
- Agently-Stage：`>=0.3.5,<0.4.0`
- 推荐 Agently DevTools：`>=0.1.10,<0.2.0`
- Skills authoring protocol：`agently-skills.authoring.v2`
- DevTools observation protocol：`agently-devtools.observation-runtime.v1`

4.1.4.5 引入的可选长输出续写和 Stage-backed TriggerFlow task ownership 保持不变。
