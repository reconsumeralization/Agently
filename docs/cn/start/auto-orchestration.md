# Agent 自动编排

Agently 4.1.3 将 `agent.start()` 作为 Agent turn 的默认用户层入口。它仍然返回
业务结果，但 Agent 可以在显式注入候选能力后，路由到普通模型响应、Actions、
Skills Executor 或 Dynamic Task。

```python
result = (
    agent
    .use_actions([market_data_action])
    .use_skills_packs(["equity-research"])
    .use_dynamic_task(mode="auto", max_tasks=8)
    .input("Review this renewal risk.")
    .output({"answer": (str, "final answer", True)})
    .start()
)
```

候选注入是边界。如果没有注册 Actions、Skills、Skills Packs 或 Dynamic Task
候选，`agent.start()` 仍然是普通模型请求。

已验收开发线的路由是候选驱动、确定性优先：submitted Dynamic Task 候选优先，
required Skills 候选进入 Skills route。当同时存在多个可选候选，例如 auto Dynamic
Task、model-decision Skills 和普通 Actions 时，默认由模型选择 route；如果只有一个
可选候选，则直接选择该 route。

公开 Agent API 仍由 core 持有，但路线规划和执行由 active
`AgentOrchestrator` plugin 通过 `AgentOrchestrator` protocol 承担。这样
Skills、Dynamic Task 和后续 route 实现都可以替换，而不需要 core 知道内置
plugin 的内部实现。

## Execution 对象

当调用方需要路线诊断、多种结果视图或过程流式输出时，使用
`agent.create_execution()`：

```python
execution = (
    agent
    .use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
    .input("Run the reviewed graph.")
    .create_execution()
)

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

execution 对象沿用模型 response 的消费风格：`get_data`、`get_text`、
`get_meta`、`get_generator` 以及对应 async 方法。

`create_execution()` 默认使用 `mode="one_turn"`，保留普通单轮 Agent 调用
语义。当开发者自己编写循环，或未来 AgentTaskLoop 需要一个有边界的单步执行时，
使用 `mode="task_step"`，并显式传入 lineage 和 limits：

```python
execution = agent.input("Try one bounded fix step.").create_execution(
    mode="task_step",
    lineage={
        "task_id": "issue-123",
        "iteration_id": "iter-2",
        "step_id": "execute-fix",
        "parent_execution_id": "exec-prev",
    },
    limits={
        "max_model_requests": 3,
        "max_seconds": 180,
        "max_no_progress_seconds": 60,
    },
)
```

`mode="task_step"` 仍然只是一次 Agent execution，不是多轮循环本身。它增加稳定
lineage、route metadata、diagnostics，以及跨普通模型 route、Dynamic Task
model task、Skills model stage 共享的模型请求预算计数。无限预算推荐用 `None`
表达；`-1` 作为兼容写法可用，但新示例不推荐。

如果 task-step 超出模型请求预算，Agently 会抛出
`agently.core.AgentExecution.AgentExecutionLimitExceeded`。execution meta
仍然可以检查，并会记录 `status="blocked"`，以及 `diagnostics` 里的 limit event。

对于卡住的执行，`limits.max_seconds` 是整个 AgentExecution 的硬截止时间。
`limits.max_no_progress_seconds` 是 idle stall 边界：route selection、模型流、
Dynamic Task、Skills、ActionRuntime 任何被接受的运行进展都会刷新计时。如果任一边界
被超过，Agently 会抛出 `agently.core.AgentExecution.RuntimeStageStallError`。
`async_get_meta()` 仍然可检查，并记录 `status="timed_out"` 或
`status="stalled"`，以及 `diagnostics["timeouts"]` / `diagnostics["stalls"]`
和最后一次进展事件。

Provider 与 response materialization 等待有独立配置：

```python
Agently.set_settings("OpenAICompatible.stream_idle_timeout", 60.0)
Agently.set_settings("OpenAIResponsesCompatible.stream_idle_timeout", 60.0)
Agently.set_settings("response.materialization_idle_timeout", 60.0)
```

`stream_idle_timeout` 限制首个 provider stream event 之后相邻事件之间的空闲间隔。
首事件超时和 stream idle timeout 都会抛出 `RuntimeStageStallError`，在 requester
能够识别时带上 provider/model 字段。
`response.materialization_idle_timeout` 限制最终 text、data、object 或 meta 从
response parser materialize 出来的等待时间。`None` 表示无限制；`-1` 作为兼容写法可用。

高频 public delta 可以合并输出，而不影响内部 liveness 计时：

```python
execution = agent.input("Stream a concise summary.").create_execution(
    output_policy={
        "delta_emit_interval": 0.1,
        "delta_max_chars": 2048,
        "delta_max_items": 20,
        "flush_on_done": True,
    },
)
```

`delta_emit_interval=0` 保持逐条 delta 输出。正数 interval 会按兼容相邻 delta
合并，并在 interval、字符数、item 数或终止事件到达时 flush。合并后的 item 会包含
`meta["coalesced"]`、`coalesced_count` 和 timing 字段。runtime stall 计时会在
coalescing 之前刷新，因此 public output 被缓冲不会让健康的 stream 被误判为卡住。

`async_get_meta()` 会包含 `execution_mode`、`lineage`、`limits`、
`output_policy`、`route`、`route_plan`、`logs`、`diagnostics` 和
`workspace_refs`。`logs` 是跨 route 稳定检查运行事实的位置，例如模型响应 id、
ActionRuntime action records 和 artifact refs：

```python
meta = await execution.async_get_meta()
meta["route"]["selected_route"]
meta["logs"]["model_response_ids"]
meta["logs"]["action_logs"]
meta["logs"]["artifact_refs"]
```

当 `model_request` route 使用 Actions 时，execution 会通过 meta 和
`actions.<action_id>` 这类 stream event 暴露 action records。需要持久化业务证据时，
host 应读取框架 action record 或 artifact，再显式写入 Workspace；不要为了让 host
能存储结果而要求模型把 raw action stdout 再复制一遍。

每条过程流 item 也会带上关联 metadata：

```python
item.meta["execution_id"]
item.meta["execution_mode"]
item.meta["lineage"]["task_id"]
```

如果在 `create_execution()` 前配置了 `agent.use_workspace(...)`，execution 会拿到
这个 Workspace binding。AgentExecution 仍然不会自动决定什么应该进入记忆；调用方应从
execution 侧显式持久化：

```python
workspace_record = await execution.async_record_workspace(
    collection="observations",
    kind="agent_execution_observation",
    content={"result": data},
    checkpoint=True,
)
```

这个 helper 仍然通过已有的通用 Workspace API 写入，并把 record/checkpoint id 更新到
`meta["workspace_refs"]`。Workspace 保持 durable substrate，不需要理解
AgentExecution 语义。下一步再由调用方调用 `workspace.build_context(...)`。

开发排障时，可以挂 EventCenter observation hook，或临时打开控制台明细：

```python
Agently.event_center.register_hook(print, event_types=None, hook_name="debug")
agent.set_settings("debug", "detail")
```

这只用于调试 route selection、model request、ActionRuntime 或 Workspace 持久化。
问题定位后，应从示例和生产代码中移除 debug hook/settings。

## 提交式 Dynamic Task 输入

提交式 Dynamic Task DAG 的 task `inputs` 继续使用 DAG 运行时占位符，例如
`${INIT.ticket}` 和 `${DEPS.lookup}`。在 Agent route 里，graph input 按以下顺序
解析：

```text
use_dynamic_task(graph_input=...)
> execution prompt snapshot 的 input slot
> {"target": task_target}
```

因此普通 Agent prompt 写法可以直接喂给提交式 DAG，而不需要另造一套映射面：

```python
execution = (
    agent
    .use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
    .input({"ticket": "TICKET-OK"})
    .create_execution()
)
```

prompt snapshot 在 `create_execution()` 时冻结。后续再调用
`agent.input(...)` 不会改变已经创建的 execution。只有当 DAG 输入需要区别于
Agent prompt input，或调用方想显式声明优先级时，才传
`graph_input=...`。

## Skills 语义

`agent.use_skills(...)` 和 `agent.use_skills_packs(...)` 注册 route candidates。
它们默认不再表示“把完整 Skill guidance 注入普通模型请求”。完整 Skill guidance
属于真正规划或执行该 Skill 的 Skills route。如果路由没有选中 Skills，普通请求
只接收安全的能力摘要。

如果调用方必须强制执行 Skills，使用 `agent.run_skills_task(...)`。

## 过程流

Agent execution stream item 保留熟悉的 instant stream 形态：

```python
item.path
item.value
item.delta
item.event_type
item.is_complete
item.route
item.stage_id
item.task_id
item.action_id
item.graph_id
```

Executor route 会桥接 TriggerFlow runtime stream 和 ModelRequest instant
checkpoint，让服务能流式输出 route decision、plan/graph readiness、
task/action 进度、选定模型字段 delta 和最终 semantic outputs。
如果 TriggerFlow-backed route 失败，Agent execution stream 会关闭，并把原始
错误抛给消费者，而不是让 `get_async_generator(...)` 一直等待后续 item。

Dynamic Task 的 model 节点会把结构化输出字段映射到稳定 path：

```python
async for item in execution.get_async_generator(type="instant"):
    if item.path == "task_dag.tasks.reply.fields.reply" and item.delta:
        print(item.delta, end="", flush=True)
```

这样保留 ModelResponse `instant` 的字段级 delta 语义，同时让过程流 path
由 Agent execution route 统一拥有。
