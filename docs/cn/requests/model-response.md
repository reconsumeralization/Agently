---
title: 模型结果
description: 从一次 result 里读 text / data / metadata 与流式事件。
keywords: Agently, result, get_result, get_data, get_text, get_meta, generator, streaming
---

# 模型结果

> 语言：[English](../../en/requests/model-response.md) · **中文**

`agent.input(...).start()` 是便捷写法 —— 创建 `AgentExecution`、执行它并直接返回
解析后的 data。其他更有意思的事（text、metadata、流式、复用、status 或 task
refs）都走 `get_result()`。quick prompt 链返回 `AgentExecutionResult`；直接
`agent.create_request(...).get_result()` 返回 `ModelRequestResult`。
`ModelResponseResult` 是 4.1.x 的 deprecated compatibility alias；直接构造
`ModelResponse` 也已 deprecated。

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
| `"delta"` | 文本 delta；重放替换前额外输出 `"<$retry>{reason}</$retry>"` | 终端打字机 UX |
| `"instant"` | 带 `path`、`delta`、`value`、`is_complete` 的结构化 `StreamingData` 事件 | 字段级 UI 更新 |
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

`ModelRequestResult` 是 canonical result class。历史的
`ModelResponseResult` import 只保留为 compatibility alias；新注解、文档和示例
都不应使用它。

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
    if item.is_complete:
        print(f"[{item.path}] done")
```

`item` 暴露 `.path`（如 `"tips[0]"`）、`.wildcard_path`（`"tips[*]"`）、
`.value`、`.delta`、`.is_complete` 和 `.event_type`。用 `.delta` 更新正在增长
的字段；只有下游动作必须等字段关闭时，才用 `.is_complete` /
`event_type=="done"` 做触发条件。

### AgentExecution 投影

`AgentExecutionStreamData` 是 execution 层的投影，不是
`ModelRequestResult`，也不是字符串 delta。一个 execution 持有模型请求时，模型
attempt 的事实会作为结构化 stream item 保留下来：`$status` 表达 retry、失败和
完成状态，`meta` 带有 `response_id`、`request_run_id`、`model_run_id` 与
`attempt_index`。

```python
execution = agent.input("总结这份事故更新。")
async for item in execution.get_async_generator(type="instant"):
    if item.path == "$status":
        print(item.value["status"], item.meta["response_id"])
    elif item.path == "model.delta" and item.delta:
        print(item.delta, end="", flush=True)
```

直接消费 `ModelRequestResult` delta 时的 `"<$retry>{reason}</$retry>"` 标记不会被
投影进 AgentExecution delta。execution 的消费者应读取结构化 `$status` item。

如果多个字段共用一个 CLI 输出区域，不要把 `.is_complete` 当成全局展示顺序屏障。
结构化 parser 往往是因为已经看到下一个 path 开始，才确认上一个 path 已关闭，
所以下一个 path 的首个 `.delta` 可能和上一个 path 的 done 事件几乎同时到达
consumer。Web UI、SSE 和 WebSocket 通常应把不同 `path` 渲染到各自的 UI slot。
如果 CLI 必须把多个 path 按固定阅读顺序打印到同一个终端区域，在 consumer
里维护一个很小的状态 flag 或 buffer，等前一个 path 的 `.is_complete` 事件已经
被处理后，再 flush 后一个 path 的内容。

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
        if item.is_complete:
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
        if item.is_complete:
            print(item.path, item.value)

asyncio.run(main())
```

服务和 TriggerFlow 场景应走 async —— 见 [Async First](../start/async-first.md)。

### Attempt 状态

`$status` 是框架保留的 stream path，不是模型输出字段。当显式允许 provider 在已经有
partial 输出后重放时，它用于通知 UI/SSE 消费者：

```python
result = agent.create_request().input("总结这次事故。").get_result()

async for item in result.get_async_generator(type="instant"):
    if item.path == "$status" and item.value["status"] == "failed" and item.value["retry"]:
        clear_provisional_answer()
        continue
    render_field_update(item)
```

最终的 `get_data()` 不含 `$status`。需要原始状态事件时，用 `type="all"` 或
`type="specific", specific="status"`。`reason` 给出有界的 transport/provider 实际说明；
`cancelled` 与失败请求不同。

纯文本 `delta` 消费者会在替代 attempt 的正文前收到独立的
`"<$retry>{reason}</$retry>"` 标记。它是重放边界，不是模型正文：

```python
import html

provisional_text = ""
for chunk in result.get_generator(type="delta"):
    if "<$retry>" in chunk:
        retry_reason = html.unescape(
            chunk.removeprefix("<$retry>").removesuffix("</$retry>")
        )
        provisional_text = ""
        clear_provisional_answer(retry_reason)
        continue
    provisional_text += chunk
    render_delta(chunk)
```

标记里的 reason 会对 provider 错误消息中的 `<`、`>`、`&` 做 XML text 转义。
`$status` 仍是结构化事实来源。纯文本流无法让 sentinel 完全无碰撞；必须保留模型输出中
包含 `"<$retry>"` 的文本 chunk 时，应改用 `instant`、`specific` 或 `all`。

AgentExecution 会把同一状态投影成结构化 process item，并在 `item.meta` 中加入来源
request/run lineage。它的 stream 不会发出纯文本 retry marker：

```python
execution = agent.input("总结这次事故。")

async for item in execution.get_async_generator(type="instant"):
    if item.path == "$status" and item.value["retry"]:
        clear_provisional_output(item.meta["response_id"])
        continue
    render_execution_item(item)
```

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

### 可选的请求调度

当大量并发请求（或长程任务）有触发供应商并发/速率上限的风险时，可以按 provider
限制模型请求的下发。调度是可选的；不配置时请求立即下发、重试立即重发（行为不变）。

```python
# 限制所有 provider 的在途并发与每秒下发数，可对单个 provider 覆盖。
agent.set_settings("model_request.scheduler.max_concurrency", 8)
agent.set_settings("model_request.scheduler.rate_per_second", 5)
agent.set_settings("model_request.scheduler.providers",
                   {"OpenAICompatible": {"max_concurrency": 2}})

# 重试之间退避而非立即重发（指数 + 抖动）。
agent.set_settings("model_request.retry_backoff_base", 0.5)  # 秒
agent.set_settings("model_request.retry_backoff_max", 30)
```

由于重试也走同一个 per-provider 槽位，速率限制同样会拉开重试调用的间隔，从而抑制
供应商错误风暴。

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
