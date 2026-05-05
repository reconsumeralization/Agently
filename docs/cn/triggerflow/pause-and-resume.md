---
title: Pause 与 Resume
description: 用 pause_for 暂停 chunk 等待人工 / 外部事件，用 continue_with 恢复。
keywords: Agently, TriggerFlow, pause_for, continue_with, interrupt, human-in-the-loop
---

# Pause 与 Resume

> 语言：[English](../../en/triggerflow/pause-and-resume.md) · **中文**

`pause_for(...)` 让 chunk 自我挂起，把控制权交回框架等外部事件。execution 保持 alive 但空闲。pause_for 期间 auto-close 暂停。外部调 `continue_with(...)`（或 `resume_event` 通过 `emit` 到达）后 chunk 醒，得到 payload 作为 await 返回。

## 用 pause_for 挂起

```python
async def ask(data: TriggerFlowRuntimeData):
    return await data.async_pause_for(
        type="human_input",
        payload={"question": f"批准 {data.input} 的操作？"},
        resume_event="ApprovalGiven",
    )
```

`pause_for` 做：

- 记录一个唯一 id 的 interrupt。
- 暂停该 execution 的 auto-close 计时。
- 返回框架。chunk 协程挂起。
- interrupt 通过 `execution.get_pending_interrupts()` 暴露。
- 外部 `continue_with(interrupt_id, payload)`（或 `emit(resume_event, payload)` 匹配）后，被 await 的调用返回 payload。

| 参数 | 含义 |
|---|---|
| `type=` | 字符串标签（如 `"human_input"`、`"approval"`、`"webhook"`）。应用据此决定如何呈现 interrupt。 |
| `payload=` | 给负责恢复方的结构化细节（UI 渲染问题、webhook 接收方等）。 |
| `resume_event=` | 可选。设了之后，`emit` 该事件也能恢复该 pause（与直接 `continue_with` 并行）。 |
| `interrupt_id=` | 可选。自己指定 id；否则框架生成。 |

## 用 continue_with 恢复

```python
interrupt_id = next(iter(execution.get_pending_interrupts()))
await execution.async_continue_with(interrupt_id, {"approved": True})
```

payload 成为被挂起的 `await data.async_pause_for(...)` 调用的返回值。chunk 从那里继续。

如果指定了 `resume_event="ApprovalGiven"`，这也行：

```python
await execution.async_emit("ApprovalGiven", {"approved": True})
```

第一个匹配的 interrupt 被恢复。

## 完整例子

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="approval")

    async def ask(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            payload={"question": f"批准工单 {data.input} 退款？"},
            resume_event="ApprovalGiven",
        )

    async def commit(data: TriggerFlowRuntimeData):
        await data.async_set_state("decision", data.input)

    flow.to(ask).to(commit)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("T-001")

    # 真实系统里 UI / webhook 后续调 continue_with。
    # 这里在同一协程里恢复仅作 demo。
    interrupt_id = next(iter(execution.get_pending_interrupts()))
    await execution.async_continue_with(interrupt_id, {"approved": True})

    snapshot = await execution.async_close()
    print(snapshot["decision"])  # {'approved': True}


asyncio.run(main())
```

注意：这个 flow 用了 `pause_for(...)`。必须用 `flow.create_execution(...)`（或 `flow.start_execution(...)`），**不要**用 `flow.start(...)` —— 隐式 execution 没有外部可用的 handle 来调 `continue_with`。

## 跨进程重启的 pause

`pause_for(...)` 与 `save` / `load` 配合得很好：

```python
execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")
# 此时已碰到 pause_for；存在 pending interrupt

saved = execution.save()
# 持久化 saved

# 后续在另一进程 / worker：
restored = flow.create_execution(
    auto_close=False,
    runtime_resources={...},   # chunk 需要的全部重新注入
)
restored.load(saved)
interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(interrupt_id, {"approved": True})
snapshot = await restored.async_close()
```

interrupt 是 saved state 的一部分，新进程知道有什么待处理。详见 [持久化与 Blueprint](persistence-and-blueprint.md)。

## 多个并发 pause

单 execution 可有多个未决 interrupt（如两个并行分支各等人工输入）。`get_pending_interrupts()` 返回全部；`continue_with(id, payload)` 一次解一个。

需要指定 id 时，给 `pause_for(...)` 传 `interrupt_id="my-id"`，`continue_with` 用同 id。

## Pause vs emit

| 模式 | 用途 |
|---|---|
| `pause_for` + `continue_with` | chunk 需要**带着** payload 返回并从那里继续 |
| `emit` + `when(...)` | 单独的 handler 在事件发生时跑；原 chunk 不必等 |

人工介入用 pause —— chunk 逻辑依赖人工回应。fan-out 副作用用 emit/when。

## auto_close 互动

只要存在未决 `pause_for`，`auto_close=True` 不触发。`continue_with` 解掉最后一个 pending interrupt 后 execution 重新进入空闲，auto-close 计时从零重启。

希望等待时永不 auto-close 用 `auto_close_timeout=None`（记得显式 `close()`）。

## 另见

- [Lifecycle](lifecycle.md) —— 恢复后何时 seal/close
- [持久化与 Blueprint](persistence-and-blueprint.md) —— 跨 pause 保存
- [State 与 Resources](state-and-resources.md) —— `load()` 后重新注入 `runtime_resources`
