---
title: 设置
description: Agently 设置如何在全局、agent 与 request 之间分层，含环境变量占位。
keywords: Agently, 设置, set_settings, 分层, 环境变量, dotenv
---

# 设置

> 语言：[English](../../en/start/settings.md) · **中文**

Agently 设置是一个分层 key-value 存储，分三个 scope：

| Scope | 设置方式 | 可见范围 |
|---|---|---|
| 全局 | `Agently.set_settings(...)` | 此调用之后创建的所有 agent / request |
| Agent | `agent.set_settings(...)` | 由该 agent 创建的所有 request |
| Request / runtime | `start(..., max_retries=...)` 等方法级参数 | 仅这一次调用 |

低 scope 覆盖高 scope；没显式覆盖的 key 沿用上层。

## 设置路径

`set_settings(...)` 的第一个参数是点路径，常用：

| 路径 | 含义 |
|---|---|
| `OpenAICompatible` | `plugins.ModelRequester.OpenAICompatible` 的别名 |
| `AnthropicCompatible` | Claude requester 的别名 |
| `plugins.ModelRequester.<Name>` | 完整路径，与上面别名等价 |
| `debug` | 打开模型请求的流式控制台日志 |
| `runtime.session_id` | 把请求绑定到指定的 session id |

也可以一次传入一个 dict，按 key 合并：

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "model": "${ENV.OPENAI_MODEL}",
})
```

## 读取设置

```python
agent_settings = agent.settings.get("plugins.ModelRequester.OpenAICompatible", {})
print(agent_settings.get("model"))
```

`settings.get(path, default)` 按点路径查找，找不到时返回 default。

## 环境变量占位

设置值的任何位置都可以写 `${ENV.<NAME>}`，读取时替换为对应环境变量。占位符由 [agently/utils/Settings.py](../../../agently/utils/Settings.py) 解析。

```python
Agently.set_settings("OpenAICompatible", {
    "api_key": "${ENV.OPENAI_API_KEY}",
})
```

## 从文件加载

非琐碎项目里，把设置放到 YAML / TOML / JSON，而不是写在 Python 内：

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

`auto_load_env=True` 会先加载工作目录下的 `.env`，然后再解析 `${ENV.*}`。

如果需要直接操作 `Settings` 对象，也可以用 `Settings().load(...)`：

```python
from agently.utils import Settings

settings = Settings()
settings.load("yaml_file", "settings.yaml", auto_load_env=True)
```

完整的项目结构示例见 [项目结构](project-framework.md)。

## Debug 开关

```python
Agently.set_settings("debug", True)
```

打印模型请求的流式日志，用于核验 prompt 槽位、output schema 与重试是否符合预期。

## 另见

- [模型设置](model-setup.md)——provider 专属配置
- [项目结构](project-framework.md)——基于文件的设置布局
