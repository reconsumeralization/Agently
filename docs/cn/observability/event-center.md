---
title: Event Center
description: Agently RuntimeEvent 的注册、过滤、结构与 DevTools 投影约束。
keywords: Agently, EventCenter, ObservationEvent, RuntimeEvent, observation, DevTools
---

# Event Center

> 语言：[English](../../en/observability/event-center.md) · **中文**

Event Center 是 Agently 的框架级运行时事件通道。它承载 **RuntimeEvent**：模型请求、Session 应用、TriggerFlow 生命周期、Action 调用等都会通过这里把结构化事件交给自定义 hook、日志 sink 或 DevTools bridge。

它和 TriggerFlow 的 `emit` / `when` 不是一回事：`emit` / `when` 改变 flow 内部控制流；RuntimeEvent 记录发生了什么。

命名兼容：

- `RuntimeEvent` 是新代码推荐使用的框架事件模型。
- Event Center 向普通 hook 分发 `RuntimeEvent`。
- `ObservationEvent` 是 DevTools bridge 从 `RuntimeEvent` 派生出的通讯投影。
- 现有 `emit_observation` / `async_emit_observation` 继续作为兼容 alias 可用。

run 与 retry 命名：

- `agent_turn` 是一次 Agent 面向用户/调用方回合的 run lineage 类型。
- `agent.create_execution()` / `agent.start()` 会为一次 execution 创建一个
  `agent_turn` run，并把选中的 route 工作绑定到它下面。`model_request`
  route 仍由 `ModelResponse` 发出 turn completion / failure，保持旧的请求观测语义；
  `skills` 和 `dynamic_task` route 则由 AgentExecution 发出 turn completion / failure。
- `attempt_index` 描述一次请求内部的模型重试 attempt；它不是 Agent turn 计数。
- DevTools 应保持两者语义分离：从 `run.run_kind` 渲染 `agent_turn`，从 `model_request` run 的 `payload.attempt_index` 或 `run.meta.attempt_index` 读取模型重试 attempt。

## 注册 hook

```python
from agently import Agently

captured = []


async def capture(event):
    captured.append(event)


Agently.event_center.register_hook(
    capture,
    event_types="runtime.info",
    hook_name="docs.capture",
)

emitter = Agently.event_center.create_emitter("Docs")
await emitter.async_info("hello")

Agently.event_center.unregister_hook("docs.capture")
```

`event_types` 可传字符串、字符串列表或 `None`。传 `None` 时 hook 接收所有事件。同步函数也能注册；Event Center 会统一转成 async 调用。

如果 hook 要把高频 runtime 事件转发到成本较高的出口，可以让 Event Center
对这个 hook 做摘要投递：

```python
Agently.event_center.register_hook(
    capture,
    event_types="model.response.delta",
    hook_name="docs.summary_capture",
    delivery_policy={
        "mode": "summary",
        "dispatch": "await",
        "emit_interval": 0.1,
        "max_items": 20,
        "high_frequency_only": True,
    },
)
```

默认投递策略是 raw 且 awaited。摘要投递只作用于当前 hook；不会改变生产者发出的
RuntimeEvent，也不会影响其他要求 raw 事件的 hook。摘要事件会带
`meta["coalesced"]`、`coalesced_count`、`first_event_id` 和 `last_event_id`。

只有具备明确 flush/close 回收点的 best-effort 出口才应使用
`dispatch="background"`。关闭前可调用
`await Agently.event_center.async_flush(hook_name)` 来排空摘要 buffer 和已跟踪的后台投递。
Event Center 在存在后台投递或摘要 buffer 时，也会启动按需 idle flush monitor：
新事件会刷新 idle 计时，安静一段时间后触发有界 flush。这个机制是长生命周期
event loop 的兜底，不替代 CLI/script 退出前的显式 flush。

## 发送 runtime event

`model.*`、`request.*`、`action.*`、`tool.*`、`session.*`、
`agent_turn.*`、`triggerflow.*`、`execution_environment.*` 这类 Agently
官方事件类型由 core 运行时协调器产出。自定义插件和应用可以向 Event Center
发送自己的消息，但应该使用应用/插件自有命名空间，也不能依赖 Agently 官方模块
消费这些自定义消息。

内置插件通过 typed observation、handler decision 或 route stream callback
把事实报告给 core。core 运行时协调器再把这些事实映射成官方 RuntimeEvent
记录和 AgentExecution stream item。

常见路径是创建 emitter：

```python
emitter = Agently.event_center.create_emitter(
    "BillingWorker",
    base_meta={"tenant": "demo"},
)

await emitter.async_emit(
    "billing.invoice_created",
    message="invoice created",
    payload={"invoice_id": "inv-1"},
)
```

也可以直接发 dict：

```python
await Agently.event_center.async_emit({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "direct event",
})
```

顶层便捷 API：

```python
await Agently.async_emit_observation({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "compatibility observation API",
})

await Agently.async_emit_runtime({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "runtime API",
})
```

## Event 结构

`RuntimeEvent` 的顶层字段来自 `agently.types.data.event.RuntimeEvent`。主框架 DevTools bridge 把 RuntimeEvent 投影给 DevTools 时，`ObservationEvent` 使用相同的序列化结构：

| 字段 | 含义 |
|---|---|
| `event_id` | 事件 id，默认自动生成 |
| `event_type` | 点路径，例如 `triggerflow.execution_started` |
| `source` | 事件来源 |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `message` | 人可读消息 |
| `payload` | 事件自己的结构化数据 |
| `error` | 错误信息；传入异常时会规范化为 `ErrorInfo` |
| `run` | run lineage，包括 `run_id`、`parent_run_id`、`session_id`、`execution_id` 等 |
| `meta` | 附加元数据 |
| `timestamp` | 毫秒时间戳 |

## TriggerFlow 事件别名

Event Center 会兼容 TriggerFlow 历史事件前缀。订阅 `workflow.execution_started` 可以收到 `triggerflow.execution_started`；订阅 `trigger_flow.signal` 可以收到 `triggerflow.signal`。文档和新代码优先写 `triggerflow.*`。

## Action 兼容事件

Action Runtime 生命周期事件以 `action.*` 作为主命名空间。当当前 Action Runtime 分支兼容 tool 时，Agently 会额外发出配对的 `tool.*` 兼容事件，用于旧订阅者和旧示例：

| 主事件 | tool 兼容事件 |
|---|---|
| `action.loop_started` | `tool.loop_started` |
| `action.plan_ready` | `tool.plan_ready` |
| `action.loop_failed` | `tool.loop_failed` |
| `action.loop_completed` | `tool.loop_completed` |

配对兼容事件会带上 `meta.compat_event_alias=True`、`meta.compat_alias_for` 和 `meta.primary_event_id`，方便消费者与主 `action.*` 事件去重。

具体 action 执行使用 `action.started`、`action.completed` 和 `action.failed`。
因 policy 或 sandbox gate 在正常执行前停止的 action 使用
`action.approval_required` 或 `action.blocked`，不会再被记录成普通失败。
对于 tool-backed action，`payload.action_type` 可以是 `"tool"`；这不会改变事件 family。

## Execution Environment 事件

Execution Environment 生命周期使用 `execution_environment.*`。Provider 与 DevTools
消费者都应把这个 namespace 当作可扩展协议处理。当前 manager 事件包括 `declared`、
`approval_required`、`ensuring`、`ready`、`unhealthy`、`releasing`、`released`
和 `failed`。`unhealthy` 表示 ready handle 在复用前 health check 失败；manager 会释放它并
ensure 一个新 handle。

## 运行进展与卡死诊断

Event Center 是 runtime event 的接收与出口分发层。liveness 状态会在高成本 hook
投递前更新，因此缓慢或失败的 hook 不能阻塞卡死诊断。

AgentExecution 会把进展记录在 `async_get_meta()["diagnostics"]`：

- `diagnostics["stages"]["events"]` 保留最近的 stage 进展。
- `diagnostics["last_progress"]` 记录最后一次被接受的进展事件。
- `diagnostics["timeouts"]` 记录硬截止超时。
- `diagnostics["stalls"]` 记录 idle 无进展卡死。

调试线上或本地应用时，可以临时挂 Event Center hook，或用
`.set_settings("debug", True)` / `.set_settings("debug", "detail")` 打开控制台明细。
问题定位后，应从代码中移除临时 debug hook 和 debug settings。

## 兼容约束

RuntimeEvent 是可扩展的框架事件协议。Agently-DevTools 和自定义消费者应按 fail-open 处理：

- 忽略未知顶层字段和未知 `payload` 字段。
- 对未知 `event_type` 不报错。
- 不把 `payload` 当成严格 schema；它可以按事件类型增量扩展。
- 需要关联请求、Session 或 TriggerFlow execution 时优先读 `run`，不要从 `message` 解析。

## 另见

- [TriggerFlow 事件与流](../triggerflow/events-and-streams.md) —— flow 内部控制流与 runtime stream
- [DevTools](devtools.md) —— 现成的观测与评估 bridge
- [FastAPI 服务封装](../services/fastapi.md) —— 把 runtime stream 转给服务客户端
- [Coding Agents](../development/coding-agents.md) —— 通过 Agently Skills 给 coding agent 提供指引
