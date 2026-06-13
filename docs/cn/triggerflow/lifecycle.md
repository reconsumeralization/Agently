---
title: TriggerFlow Lifecycle
description: 三种执行态与 5 个入口 API —— 各自做什么、何时用哪个。
keywords: Agently, TriggerFlow, lifecycle, seal, close, start, execution, auto_close
---

# Lifecycle

> 语言：[English](../../en/triggerflow/lifecycle.md) · **中文**

TriggerFlow execution 有三种状态。5 个入口 API 控制启动与结束方式。

## 三态

```text
   open  ──seal()──►  sealed  ──close()──►  closed
    │                                            │
    └───（auto_close 在空闲超时后自动触发）───────┘
```

| 状态 | 接受什么 | 还在跑什么 |
|---|---|---|
| `open` | 新外部事件（`emit`、`continue_with`） | 全部：chunk、runtime stream、已注册 task |
| `sealed` | 不再接外部 | 已接受事件、内部 `emit` 链、已注册 task 继续 drain |
| `closed` | 不接 | runtime stream 已关闭；close snapshot 已冻结 |

关键区别：`seal()` 停外部输入但让在途工作完成。`close()` 先 seal，再 drain，再冻结。

## 5 个入口 API

| API | 用途 | 返回 |
|---|---|---|
| `flow.start(...)` / `flow.async_start(...)` | 隐式 execution 语法糖；create + start + wait + close | close snapshot |
| `flow.start_execution(...)` / `flow.async_start_execution(...)` | 显式启动；你持有 execution handle | execution |
| `execution.start(...)` / `execution.async_start(...)` | 启动已创建的 execution | `auto_close=True` 返回 close snapshot；`auto_close=False` 返回 execution |
| `execution.seal()` / `execution.async_seal()` | 运行期 seal | — |
| `execution.close()` / `execution.async_close()` | 收尾 | close snapshot |

### `flow.start(...)` —— 隐式语法糖

```python
snapshot = await flow.async_start("input value")
```

内部做：`create_execution(auto_close=True, auto_close_timeout=0.0)`、启动、等到 close、返回 snapshot。

规则：

- **`auto_close=False` 非法** —— 立刻报错。
- `wait_for_result=` 的值被**忽略**并 warn。返回类型固定为 close snapshot。
- `timeout=` 当作 `auto_close_timeout` —— 最后一次活动后多久自动关闭。
- flow 用了 `pause_for(...)` 时**不要**用 `flow.start()` —— 外部没有 handle 来恢复。隐式 execution 走到 `pause_for(...)` 时 TriggerFlow 会 fail fast。改用 `flow.start_execution(...)` 或 `flow.create_execution(...)`。

### `flow.start_execution(...)` —— 显式启动

```python
execution = await flow.async_start_execution("input value")
# ... 用 handle 做事 ...
snapshot = await execution.async_close()
```

返回 execution，你决定何时 close。适合服务、SSE/WebSocket 流、人工介入、外部 `emit()`。

`wait_for_result=` 在这里也被忽略。

### `execution.start(...)` —— 启动已构建的 execution

```python
execution = flow.create_execution(auto_close=True)
snapshot = await execution.async_start("input")  # 返回 close snapshot
```

```python
execution = flow.create_execution(auto_close=False)
exec2 = await execution.async_start("input")  # 返回 execution
# ... 做事，再 ...
snapshot = await execution.async_close()
```

| `auto_close` | `async_start` 返回 |
|---|---|
| `True`（默认） | close snapshot |
| `False` | execution 本身 |

Sync `start()` 仅支持 `auto_close=True`。需要手动 close 时用 `await execution.async_start(...)`。

传给 `execution.async_start(value)` 的值是 execution 的 start input，
不会 emit 一个名为该值的自定义事件。应从 start boundary 开始运行的 chunk
用 `flow.to(handler, name=...)` 挂接。如果确实需要 `"start"` 这类自定义事件，
先启动 execution，再调用 `await execution.async_emit("start", payload)`。

### `execution.seal()` —— 停新输入，让在途完成

```python
await execution.async_seal()
```

seal 后：

- 新外部 `emit()` / `continue_with()` 被拒。
- 已接受事件、内部 `emit` 链、已注册 task 继续。
- runtime stream **不**关。
- close snapshot **不**冻结。

需要「停接新工作但让在途事完成」、稍后再 close（或让 `auto_close` 关）时用 seal。

### `execution.close()` —— 收尾返回 snapshot

```python
snapshot = await execution.async_close()
```

close 顺序：

1. seal（如未 seal）
2. drain 待办 task
3. 关 runtime stream
4. 冻结并返回 close snapshot

close 上的 `timeout=` 是 **drain timeout** —— 在途 task 的最大等待时间，与 auto-close 计时无关。

## auto_close 与 auto_close_timeout

`auto_close=True`（`create_execution` 的默认值）表示 execution 在**空闲** `auto_close_timeout` 秒后自动 close —— 没 chunk 在跑、没事件待处理、没 pending pause。

| 来源 | 默认 `auto_close_timeout` |
|---|---|
| `flow.create_execution(...)` | `10.0` 秒 |
| `flow.start(...)` / `flow.async_start(...)`（隐式糖） | `0.0` 秒（一空闲就 close） |

`pause_for(...)` 暂停 auto-close 计时。`continue_with(...)` 后空闲计时重新开始。

`close()` / `async_close()` 默认拒绝关闭仍有 pending interrupt 的 execution。应先恢复这些 interrupt；如果关闭时就是要放弃等待，必须显式传 `pending_interrupts="cancel"`。

close 还会释放 execution-local 的 transient aggregation state，例如未完成的
`when(mode="and")`、`batch`、`collect`、`for_each` 和 `match` bookkeeping。
这些 scratch key 不属于 durable close snapshot。

`auto_close_timeout=None` 关掉 auto-close —— execution 一直存活直到显式 `close()`。**不要把 `auto_close_timeout=None` 与隐式糖一起用** —— `flow.start()` 会永远不返回。

## Execution snapshot 与 load

`execution.save()` 返回可序列化的 execution snapshot。为了支撑可重启和
宿主管理恢复路径，这个 snapshot 本身就是带版本的 TriggerFlow 恢复契约：

```python
saved = execution.save()
```

execution snapshot 记录：

- `schema_version`、`kind`、`snapshot_id`、`state_version`。
- execution identity、flow name、run context、lifecycle/status、owner、
  heartbeat 与 lease 字段。
- runtime state、flow data、pending interrupts、intervention ledger、
  sub-flow frames、last signal 与兼容 result state。
- `durable_system_state`：TriggerFlow 自身需要跨 open/waiting execution
  load 保存的进度，例如未完成的 `when(mode="and")` 聚合状态。
- `resource_requirements`：恢复后继续执行前必须满足的 live resource key 与
  execution-environment requirement。
- `resume_ledger`：已接受的 `continue_with(..., resume_request_id=...)`
  请求，避免外部 resume 重试重复 dispatch 图。

live resource 对象不会被序列化。`runtime_resources`、受管
execution-environment handle、client、callback 以及其他 live object 都不进入
saved state。`runtime_resources` 只是把宿主已经创建、恢复并校验过的 live object
挂到 execution 的入口。

未来恢复后才会用到的资源需要显式声明。TriggerFlow 能记录已经挂载的资源，但
无法从未执行到的分支里推断出未来会调用哪个 resource：

```python
flow.declare_resource_requirement("resume_service")
```

如果各个 worker 都能导入同一个 resource factory，也可以持久化 resolver
descriptor，而不是每次重启都手动传入 live object：

```python
flow.declare_resource_requirement(
    "resume_service",
    resolver="my_app.runtime_resources:create_resume_service",
    provider_kind="approval_router",
    config_ref="settings://approval-service",
    secret_ref="secret://approval-service",
    fail_policy="fail_closed",
)
```

resolver 会收到一个 context dict，里面包含 resource key、requirement、
execution id、snapshot 和 execution handle。resolver 应返回 live resource，
或返回 `{"resource": service, "health": "healthy"}` 这样的 envelope。resolver
缺失、unhealthy 或 policy-forbidden 时，`inspect_load(...)` 会返回 typed
diagnostics。`fail_policy="fail_open"` 会把诊断降为 warning；默认
`fail_closed` 会阻断严格的 `async_load(...)`。

恢复前用 `inspect_load(...)` 或严格的 `async_load(...)` 检查：

```python
saved = execution.save()

report = restored.inspect_load(saved)
assert report["missing_resource_keys"] == ["resume_service"]

await restored.async_load(
    saved,
    runtime_resources={"resume_service": service},
)
await restored.async_continue_with(
    interrupt_id,
    {"approved": True},
    resume_request_id="webhook-42",
    actor="approval-service",
)
```

`async_load(...)` 会 load snapshot、恢复声明的 execution environment
requirements、重新 ensure 受管 execution environments，并在图继续前对缺失资源
fail fast。只有当所需 resource 已经在当前进程里可用、且不需要 async
environment 准备时，才使用同步 `load(...)`；需要同样的同步 fail-fast 检查时可传
`validate_resources=True`。生产边界见 [分布式 Pause 与 Resume 边界](distributed-pause-resume.md)。

外部 execution snapshot store 只要暴露 `put_snapshot(run_id, state, step_id=...)`
即可持久化同一个 snapshot。Durable provider 还可以暴露
`get_snapshot(run_id)` 读取 snapshot state、
`put_snapshot(..., expected_state_version=...)`、lease methods 和
`put_artifact_ref(...)`。Workspace 已实现同一个 snapshot-store port，因此可以直接配置：

```python
execution = flow.create_execution(workspace=agent.workspace)
snapshot_ref = await execution.async_save(step_id="after-approval")
```

如果需要共享任务信息，优先使用由应用显式创建并管理的 Workspace 实例：

```python
shared_workspace = Agently.create_workspace("./.agently/projects/issue-123")
execution = flow.create_execution(workspace=shared_workspace)
```

`flow.create_execution()` 默认绑定当前 session/script 的默认 Workspace，并给 execution
分配 `files/executions/<execution-id>` 下的独立文件 root。传 `workspace=False` 可以显式
关闭；传 Workspace 实例、路径或 backend 时，execution 会使用显式选择的 Workspace。
解析后的 execution-local Workspace facade 会作为 `runtime_resources["workspace"]`
暴露给 TriggerFlow chunks，也可以通过 `data.require_resource("workspace")` 读取。

它是 live resource，不会被序列化进 execution state。如果某个 chunk 需要 Agent
使用同一个显式信息范围，应在业务代码里把该 Agent 或单次 AgentExecution 绑定到同一个
Workspace。如果 flow 需要在两个隔离 Workspace 之间移动数据，应在业务逻辑里显式用
Workspace `search(...)`、`get(...)`、`get_data(...)`、`put(...)`、`ingest(...)` 和
`link(...)` 完成。Workspace 本身不提供跨空间 communication 或 replication 协议。

如果服务没有使用 `workspace=...`，也可以通过已有 execution resources 传入 store：

```python
execution = flow.create_execution(
    runtime_resources={"snapshot_store": agent.workspace}
)
snapshot_ref = await execution.async_save(step_id="after-approval")
```

如果要从 Workspace-backed snapshot 恢复，先读出保存的 snapshot，再交回
TriggerFlow 的 load API：

```python
saved_state = await agent.workspace.get_snapshot(execution.run_context.run_id)
assert saved_state is not None

restored = flow.create_execution(workspace=agent.workspace)
await restored.async_load(saved_state, runtime_resources={"workspace": agent.workspace})
await restored.async_continue_with(
    "approval",
    {"approved": True},
    resume_request_id="approval-webhook-1",
    actor="reviewer",
)
```

这条路径会保留 TriggerFlow 自己拥有的 pause/resume ledger、policy approval
waits 与 `when(..., mode="and")` join progress；Workspace 仍只是 snapshot
provider。

这条路径有一个可运行的 foundation check：
`examples/trigger_flow/durable_recovery.py`。它会写入 Workspace-backed
snapshot，在新的 execution 中 load，用稳定 `resume_request_id` 恢复，并证明重复
callback 投递不会让下游 chunk 执行两次。

如果要看服务形态的 provider 替换示例，使用
`examples/trigger_flow/fastapi_sqlite_exchange_provider.py`。它把 flow
definition 放在模块级 `discount_approval_flow` 对象里，并在模块顶层声明
`.to(...)` / `.when(...)` 装配；它把顶层 execution snapshot 存到 SQLite，
通过 SQLite `ExecutionExchangeProvider` 发布 approval request，并用 FastAPI
暴露同一套 start/resume 路径。

TriggerFlow 会在 snapshot 中携带 owner/lease 字段，并提供
`claim_lease(...)` / `heartbeat_lease(...)` 供 store 索引和投影分布式所有权。
跨 worker 原子写入、lease enforcement、访问控制和冲突处理仍由 store 负责。
`continue_with(...)` 接受 resume request 之前，投递到 execution-local lease
已过期执行上的 callback 会 fail fast，且不会写入 resume ledger；接管后的 worker
应先 load 或 claim 这个 execution，再用同一个稳定 `resume_request_id` 处理。

Workspace-backed provider 暴露同一个 lease port：

```python
lease = await agent.workspace.claim_lease(
    execution.run_context.run_id,
    "worker-1",
    ttl=30.0,
    expected_state_version=snapshot_state_version,
)
await agent.workspace.heartbeat_lease(
    execution.run_context.run_id,
    "worker-1",
    lease["lease_token"],
)
```

服务如果要把 execution snapshot 用于宿主管理的分布式恢复，应显式要求
fail-closed provider 检查：

```python
await execution.async_save(
    step_id="after-approval",
    require_distributed_provider=True,
)
```

被选中的 snapshot provider 必须报告 CAS、lease、range-read 和 retention
能力，并暴露对应的 snapshot、lease 和 artifact methods；execution 也必须配置一个
报告 event sequencing 的 RuntimeEvent store。local Workspace backend 会通过这个
fail-closed provider check，用于单节点开发和本地重启恢复，但它不是生产级跨 worker
Redis/Postgres/object-storage backend。

如果需要持久诊断，可以在 execution 上配置 RuntimeEvent store：

```python
execution = flow.create_execution(
    runtime_resources={"runtime_event_store": agent.workspace}
)
await execution.async_start(request)
events = await agent.workspace.query_runtime_events(execution.id)
```

TriggerFlow 仍然拥有 event identity、pause/resume 语义、DAG readiness 和
replay validation。Workspace 只存 canonical RuntimeEvent records 与
snapshot refs，不会变成 workflow control plane。

持久 RuntimeEvent record 会包含 execution 内 sequence、`state_version`、
parent event/signal lineage、aggregation scope、operator id、interrupt id、
resume request id、actor id、lease owner id、snapshot ref 和 artifact refs。
`pause_for(...)` 会写入 interrupt planned、persisted、exposed 阶段；
`continue_with(..., resume_request_id=...)` 会写入 resume accepted、
dispatched、completed 和 `dispatch_failed` 阶段。外部 callback、webhook 或 approval
request 应使用稳定的 `resume_request_id`，这样重启后的重复投递仍可保持幂等。

`pause_for(...)` 也可以为外部 approval、webhook 或 exchange-style wait 写入
ExternalWait template：

```python
await data.async_pause_for(
    type="exchange", exchange_kind="approval",
    payload={"question": "approve?"},
    interrupt_id="approval",
    resume_to="next",
    channel_id="ops-approval",
    provider_id="approval-router",
    wait_mode="connected_then_disconnected",
    hot_wait_timeout=30.0,
    cold_persistence_policy="persist",
    request_payload_schema={"type": "object"},
    response_payload_schema={"type": "object", "required": ["approved"]},
    audit_metadata={"exchange_id": "approval-exchange-1"},
)
```

该 template 会保存在 execution snapshot 的 `interrupt.external_wait_request` 中。
如果提供了 `audit_metadata.exchange_id`，TriggerFlow 会把它投影到 Workspace
或兼容 runtime event provider 的 durable RuntimeEvent record `exchange_id`
字段。

当 host 拥有 approval router、queue 或 exchange transport 时，可以绑定
execution-local `execution_exchange_provider`。provider 会在 interrupt 已持久化、
但还未标记为 exposed 之前发布同一份 typed request；TriggerFlow 仍然拥有
lifecycle，恢复仍通过 `continue_with(...)`：

```python
class QueueExchangeProvider:
    async def publish_request(self, execution_id, request, *, interrupt):
        ticket = await queue.publish({
            "execution_id": execution_id,
            "request": request,
            "interrupt": interrupt,
        })
        return {
            "exchange_id": ticket["id"],
            "provider_metadata": {"queue": ticket["queue"]},
        }

execution = flow.create_execution(
    runtime_resources={"execution_exchange_provider": QueueExchangeProvider()}
)
```

provider 可以返回 `exchange_id`、`provider_metadata` 和 `audit_metadata`；
这些字段会合并进 `interrupt.external_wait_request`，并投影到 durable
RuntimeEvent record。若 provider 发布失败，TriggerFlow 会把 `dispatch_state`
记录为 `exposure_failed`，并发出 `triggerflow.interrupt_exposure_failed`。

长运行 execution 应把大 payload 放在 provider ref 后面，execution snapshot 中只保存
compaction facts；为此配置 host-owned reducer policy。TriggerFlow 负责按阈值选择
RuntimeEvent records；reducer 返回可序列化的
summary facts，以及需要放到 provider artifact ref 后面的大 payload：

```python
async def compact_execution_state(context):
    records = context["records"]
    return {
        "summary": f"compacted {len(records)} runtime events",
        "artifact": {"event_ids": [record["event_id"] for record in records]},
        "retained_lineage_anchors": [{
            "anchor_id": "root-after-compaction",
            "sequence": context["sequence_from"],
            "event_id": records[0]["event_id"],
        }],
        "load_read_limit": 100,
    }

execution.set_compaction_policy(
    min_runtime_events=100,
    reducer=compact_execution_state,
    artifact_kind="snapshot_payload",
)
await execution.async_save(step_id="auto-compacted")
```

`inspect_load(...)` 会把 retained lineage anchor mismatch、required
artifact ref 缺失、load read limit 非法报告为 snapshot diagnostics。
TriggerFlow 只记录 execution facts 和 provider refs；Workspace 或 enterprise
provider 负责 artifact storage、retention anchors 和 bounded runtime-event read。

`execution.inspect_load(...)` 会返回 typed recovery diagnostics，用于
区分 invalid snapshot、missing resource、accepted 但未 dispatched 的
resume request、dispatched 但未 completed 的 resume request、expired lease
warning、active lease owner conflict、DAG join state mismatch、TaskDAG graph
fingerprint mismatch，以及 durable RuntimeEvent sequence 或 parent-signal
lineage 问题。

## 选哪个入口

| 场景 | 用 |
|---|---|
| 快速脚本，输入都已知 | `flow.start(...)` / `flow.async_start(...)` |
| 持续 emit / 消费 runtime stream 的服务 | `flow.start_execution(...)` |
| 需要 `pause_for(...)`（人工审批、异步 webhook） | `flow.create_execution(auto_close=False)` + `execution.async_start(...)` + 手动 `close()` |
| 跨重启 save/load | `create_execution(...)` + `execution.save()` / `load()` |

## 决策示例

```python
# 这个 flow 暂停等用户输入 —— 不要用 flow.start()
flow = TriggerFlow(name="approval")
async def ask(data):
    return await data.async_pause_for(type="exchange", exchange_kind="approval", resume_to="next")
async def commit(data):
    await data.async_set_state("approved", data.input)
flow.to(ask).to(commit)

execution = flow.create_execution(auto_close=False)
await execution.async_start(None)
# ... 等外部系统调 execution.async_continue_with(...) ...
snapshot = await execution.async_close()
```

如果写成 `await flow.async_start(None)`，隐式 execution 没有可恢复 handle，走到 `pause_for(...)` 时会直接报错。

如果需要停止一个等待中的 execution 且不恢复它，必须显式表达取消等待：

```python
snapshot = await execution.async_close(pending_interrupts="cancel")
```

## 兼容参数

| 参数 | 状态 |
|---|---|
| `wait_for_result=True` / `False` | **值被忽略**，发 warning；返回类型由 `auto_close` 决定 |
| `set_result()` / `get_result()` / `.end()` | deprecated；见 [兼容](compatibility.md) |
| `runtime_data`（`get_runtime_data` / `set_runtime_data` 等） | `state` 的 deprecated 别名；见 [State 与 Resources](state-and-resources.md) |

## 另见

- [State 与 Resources](state-and-resources.md) —— 什么进 snapshot
- [Execution Result](execution-result.md) —— 通过统一 facade 读取 snapshot、state、兼容 result 和 metadata
- [Pause 与 Resume](pause-and-resume.md) —— `pause_for` 与 `continue_with`
- [分布式 Pause 与 Resume 边界](distributed-pause-resume.md) —— 宿主管理恢复和 live object ownership
- [持久化与 Blueprint](persistence-and-blueprint.md) —— `save` / `load`
- [兼容](compatibility.md) —— 从旧 API 迁移
