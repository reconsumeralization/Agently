---
title: 观测概览
description: Event Center RuntimeEvent、TriggerFlow stream/control event、DevTools 与 coding-agent 指引的边界。
keywords: Agently, observability, Event Center, ObservationEvent, RuntimeEvent, DevTools, TriggerFlow
---

# 观测概览

> 语言：[English](../../en/observability/overview.md) · **中文**

Agently 里有几种看起来像“事件”的接口。它们相关，但职责不同。

| 接口 | 归属 | 用途 | 去读 |
|---|---|---|---|
| RuntimeEvent | Event Center | 框架级事件，例如模型请求、Session、Action 调用、TriggerFlow lifecycle。DevTools 接收由 RuntimeEvent 派生出的 ObservationEvent 投影。 | [Event Center](event-center.md) |
| TriggerFlow `emit` / `when` | TriggerFlow execution | 单个 execution 内的控制流信号 | [TriggerFlow 事件与流](../triggerflow/events-and-streams.md) |
| TriggerFlow runtime stream | TriggerFlow execution | 给 UI、SSE、日志或 wrapper 消费的 live data item | [TriggerFlow 事件与流](../triggerflow/events-and-streams.md) |
| DevTools | `agently-devtools` companion package | 可视化运行、上传 observation、执行 evaluation、暴露交互式 wrapper | [DevTools](devtools.md) |
| Coding-agent 指引 | `Agently-Skills` companion repo | 给 Codex、Claude Code、Cursor 等工具提供当前框架指引 | [Coding Agents](../development/coding-agents.md) |

## 判断方法

- 想消费框架 RuntimeEvent、不改变业务行为，用 Event Center。
- 事件会改变 flow 里的下一步走向，用 TriggerFlow `emit` / `when`。
- chunk 要把 live 输出推给外部消费者，用 TriggerFlow runtime stream。
- 想要现成的观测、评估或交互 UI，用 DevTools。

RuntimeEvent 的源码结构在 [`agently/types/data/event.py`](../../../agently/types/data/event.py)，事件分发在 [`agently/core/Runtime/EventCenter.py`](../../../agently/core/Runtime/EventCenter.py)。DevTools 示例在 [`examples/devtools/`](../../../examples/devtools/)。
