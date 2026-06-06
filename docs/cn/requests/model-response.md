---
title: 模型响应
description: 从一次 result 里读 text / data / metadata 与流式事件。
keywords: Agently, result, get_result, get_data, get_text, get_meta, generator, streaming
---

# 模型结果

> 语言：[English](../../en/requests/model-response.md) · **中文**

`agent.input(...).start()` 是便捷写法 —— 跑请求并直接返回解析后的 dict。其他更有意思的事（text、metadata、流式、复用）都走 `get_result()`。

## 两种消费方式

```python
# 方式 A：一次性，立即返回 parsed data
result = agent.input("...").output({...}).start()

# 方式 B：拿一个可复用的 result facade
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

非琐碎代码默认走方式 B。模型调用在你第一次从 `result` 消费时**懒触发**，结果**缓存**，后续读不会重发请求。`get_response()` 作为旧代码兼容别名保留，并返回同一个 result facade。

## 读取方法

| 方法 | 返回 |
|---|---|
| `result.get_text()` | 完整纯文本 |
| `result.get_data()` | 解析后的结构化 dict（用了 `output()` 时） |
| `result.get_data_object()` | Pydantic 实例（`output()` 接受 `BaseModel` 时） |
| `result.get_meta()` | usage / model 信息 / 时间等 |

每个都有 async 版本：`async_get_text()`、`async_get_data()`、`async_get_data_object()`、`async_get_meta()`。

混用没问题——它们都从同一份缓存里读：

```python
result = agent.input("...").output({...}).get_result()
data = result.get_data()        # 触发请求
text = result.get_text()        # 已缓存
meta = result.get_meta()        # 已缓存
```

`.validate(...)` 每个 result 也只跑一次——校验的就是这份缓存结果。

## 流式

`result.get_generator(type=...)`（sync）与 `get_async_generator(type=...)`（async）发流式事件。`type` 决定你看到什么：

| `type` | 你拿到的 | 适合 |
|---|---|---|
| `"delta"` | 原始 token delta | 终端打字机 UX |
| `"instant"` | 带 `path`、`delta`、`value`、`is_completed` 的结构化 `StreamingData` 事件 | 字段级 UI 更新 |
| `"streaming_parse"` | 与 `instant` 使用同一个结构化流式 parser 的兼容别名 | 兼容 / 增量 dict 读取 |
| `"specific"` | `(event, data)` 元组，按事件过滤（`delta`、`reasoning_delta`、`tool_calls` 等） | 精确订阅特定事件 |
| `"original"` | 原始 provider 事件 | 调试 / passthrough |
| `"all"` | 所有事件带类型标签 | 完整日志 |

常用类型注解可以直接从 `agently` 导入公开 stream item 类型：
`StreamingData` 对应 `instant` / `streaming_parse`，
`AgentlySpecificResultMessage` 对应 `specific`，
`AgentlyModelResultMessage` 对应 `all`。完整 typed data 命名空间仍可从
`agently.types.data` 导入。
旧的 `AgentlySpecificResponseMessage`、`AgentlyModelResponseMessage` 以及相关
`Response` 别名会继续在 `agently.types.data` 里兼容，但不会从 `agently`
根入口重新导出。推荐使用 `Result` 命名。

### Delta 例子

```python
gen = agent.input("讲个递归故事。").get_generator(type="delta")
for delta in gen:
    print(delta, end="", flush=True)
```

### Instant 例子（结构化）

```python
gen = (
    agent.input("给一个定义和三条 tips。")
    .output({
        "definition": (str, "定义", True),
        "tips": [(str, "tip", True)],
    })
    .get_generator(type="instant")
)
for item in gen:
    if item.delta:
        print(f"[{item.path}] + {item.delta}")
    if item.is_completed:
        print(f"[{item.path}] done")
```

`item` 暴露 `.path`（如 `"tips[0]"`）、`.wildcard_path`（`"tips[*]"`）、
`.value`、`.delta`、`.is_completed` 和 `.event_type`。用 `.delta` 更新正在增长
的字段；只有下游动作必须等字段关闭时，才用 `.is_completed` /
`event_type=="done"` 做触发条件。
`.is_complete` 仍作为 stream event 兼容别名保留，但已废弃，并将在 Agently
4.2 移除。

### 高价值模式：先流式更新 UI，再读取最终可靠结果

当应用可以在完整回答结束前展示或路由单个结构化字段时，用 `instant`。流式事件用于
渐进式 UI 状态；最终业务对象仍然应该来自 `async_get_data()`。

```python
import asyncio
from collections import defaultdict
from agently import Agently

agent = Agently.create_agent()


async def stream_triage_card(ticket_text: str):
    result = (
        agent
        .input(ticket_text)
        .output(
            {
                "status_summary": (str, "给用户看的一句话状态", True),
                "risk_flags": [(str, "明确风险点", True)],
                "next_actions": [(str, "支持团队下一步动作", True)],
                "customer_reply": (str, "发给客户的回复", True),
            },
            format="json",
        )
        .get_result()
    )

    ui_state: dict[str, str] = defaultdict(str)

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            # 把字段级 patch 推给 UI / SSE / WebSocket。
            ui_state[item.path] += item.delta
            print({"path": item.path, "delta": item.delta})
        if item.is_completed:
            print({"path": item.path, "status": "done", "value": item.value})

    # 不会发第二次请求：这里读取的是同一个 result 的最终缓存解析结果。
    final_data = await result.async_get_data()
    return final_data


asyncio.run(stream_triage_card(
    "Ticket T-104: enterprise billing export failed twice; CFO waiting."
))
```

服务里优先用 async 消费。同步 `get_generator(type="instant")` 适合脚本和
notebook。

### Specific 例子（事件）

```python
gen = agent.input("打个招呼。").get_generator(type="specific")
for event, data in gen:
    if event == "delta":
        print(data, end="", flush=True)
    elif event == "reasoning_delta":
        print("[reasoning]", data, end="", flush=True)
    elif event == "tool_calls":
        print("[tool call]", data)
```

### Reasoning 事件

有些 provider 会用原生 response 字段提供 reasoning。有些本地或 OpenAI-compatible
reasoning 模型可能把开头的外层 `<think>...</think>` 放进普通 content。Agently
会在结构化解析前统一归一：

- `reasoning_delta` / `reasoning_done` 承载 reasoning 文本。
- `delta` / `done` 只承载 parser 应消费的 answer payload。
- `original_delta` / `original_done` 保留 provider 原始内容，不做改写。
- 只归一位于 answer payload 之前的完整外层 `<think>...</think>`。字段、代码块或
  长文本 payload 内部的 `<think>` 会作为普通 answer 内容保留。

## Async 流式

同样的 generator 改 async：

```python
import asyncio

async def main():
    result = agent.input("...").output({...}).get_result()
    async for item in result.get_async_generator(type="instant"):
        if item.is_completed:
            print(item.path, item.value)

asyncio.run(main())
```

服务和 TriggerFlow 场景应走 async —— 见 [Async First](../start/async-first.md)。

## 并发

因为 `get_result()` 只在你消费时才发请求，可以先建多个 result，再并发消费：

```python
import asyncio

async def ask(prompt):
    r = agent.input(prompt).get_result()
    return await r.async_get_text()

results = await asyncio.gather(
    ask("总结递归。"),
    ask("给一个 Python 例子。"),
)
```

这是标准 async 模式，Agently 没有特别封装。

## 能复用就别重发

```python
# 不好——同一请求跑了三次
text = agent.input("...").start()
data = agent.input("...").output({...}).start()
meta = agent.input("...").output({...}).get_result().get_meta()

# 好——跑一次，读三种视图
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

## 另见

- [Async First](../start/async-first.md) —— 何时切到 `get_async_generator(...)`
- [输出控制](output-control.md) —— 「模型返回」与「你读到」之间发生了什么
- [Schema as Prompt](schema-as-prompt.md) —— `output()` 能接受什么
