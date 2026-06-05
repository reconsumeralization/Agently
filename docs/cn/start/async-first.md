---
title: Async First
description: 什么场景下 async 应该是默认路径，以及该用哪些 API。
keywords: Agently, async, async_get, get_async_generator, async_start
---

# Async First

> 语言：[English](../../en/start/async-first.md) · **中文**

Agently 在运行时层是 async-native。Sync 方法是通过 `FunctionShifter.syncify()` 从 async 方法生成的便捷封装。一旦做真实服务，async 应该是默认路径。

## 什么时候 sync 也行

- 一次性脚本、Notebook、教学示例。
- 不和别的代码共享同一个事件循环。

## 什么时候 async 是默认

- 在 FastAPI、ASGI worker、SSE / WebSocket 处理器，或任何已经在 `asyncio` 里跑的代码。
- 流式 UI——希望字段 delta 先反应到界面，而不是等整个响应。
- 把模型输出和 TriggerFlow 事件、runtime stream 或外部 pubsub 结合起来。

## 推荐组合

最值得先掌握的组合：

- `result.get_async_generator(type="instant")`——逐字段流出带 `path`、`delta`、`value`、`is_completed` 的结构化 `StreamingData` patch。
- `data.async_emit(...)`——把节点变成 TriggerFlow 信号。
- `data.async_put_into_stream(...)`——把中间状态推给 UI / SSE / 日志。

`instant` 是字段级事件，不是原始 provider token。它可以在字段还在增长时通过
`.delta` 提供部分字段文本，然后在 `.is_completed` 为 true 时发完成事件。把这些
事件当作渐进式 UI 状态；最终可靠对象在结束后用 `async_get_data()` 读取。
这类 stream handler 的入参类型可直接用 `agently` 根入口的 `StreamingData`
标注；需要完整 typed data 命名空间时也可以继续从 `agently.types.data` 导入。

## API 对照

| Sync | Async 等价 |
|---|---|
| `agent.start()` / `request.start()` | `agent.async_start()` / `request.async_start()` |
| `result.get_data()` | `result.async_get_data()` |
| `result.get_text()` | `result.async_get_text()` |
| `result.get_meta()` | `result.async_get_meta()` |
| `result.get_generator(type=...)` | `result.get_async_generator(type=...)` |
| `flow.start()` | `flow.async_start()` |
| `execution.start()` / `execution.close()` | `execution.async_start()` / `execution.async_close()` |
| `data.set_state(...)` / `data.emit(...)` | `data.async_set_state(...)` / `data.async_emit(...)` |

## 最小 async 示例

```python
import asyncio
from agently import Agently

agent = Agently.create_agent()


async def main():
    result = (
        agent
        .input("给我一个标题和两条要点。")
        .output({
            "title": (str, "标题", True),
            "items": [(str, "要点", True)],
        })
        .get_result()
    )

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            print(item.path, "+", item.delta)
        if item.is_completed:
            print(item.path, "done")

    final = await result.async_get_data()
    print(final)


asyncio.run(main())
```

`get_result()` 返回一个可复用的 `ModelResponseResult`。你可以从同一个 result 拿 text、结构化 data 和 metadata，不会重发请求——见 [模型结果](../requests/model-response.md)。

## Async + TriggerFlow

事件驱动编排时优先用：

- `flow.async_start(...)`——hidden execution 语法糖，直接返回 close snapshot。
- `flow.async_start_execution(...)`——显式启动长生命周期 execution，由你自己控制。
- chunk 内部使用 `data.async_emit(...)` 与 `data.async_put_into_stream(...)`。

详见 [TriggerFlow Lifecycle](../triggerflow/lifecycle.md)。

## 不要过度宣传 async

Async First 改善的是并发性、服务组合质量和渐进式 UX。它**不会**让单次请求的模型延迟变低——单次请求的墙钟时延由模型决定，与 sync/async 无关。
