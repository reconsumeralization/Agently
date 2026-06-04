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

## AgentTask Loop

当业务目标需要一个有边界的多轮闭环，而不是一次 Agent turn 时，使用
`agent.create_task(...)`。AgentTask V1 运行一个由单个 Agent 持有的任务：计划、
执行一个 bounded step、写入 Workspace 证据、验证、必要时 replan，最后以 complete
或 blocked 结束。

```python
task = agent.create_task(
    goal="将旧版 Agently 脚本迁移到当前 4.1.x API，并确保它可以运行。",
    success_criteria=[
        "原始失败已被记录。",
        "脚本不再使用不兼容的旧 API。",
        "修复后的脚本可以运行，并产出预期结构化结果。",
    ],
    workspace="./.agently/tasks/legacy-script-upgrade",
    max_iterations=4,
    verify="before_done",
    options={
        "agent_task": {
            "stream_progress": True,
            "stream_progress_background": True,
            "stream_snapshots": True,
            # 可选：用单独 model key 基于 snapshot 生成自然语言进展。
            # 不设置时使用模板 progress，不增加模型请求。
            # "progress_model_key": "cheap-progress-model",
        },
    },
)

async for item in task.stream():
    if (item.meta or {}).get("stream_kind") == "progress":
        print("[PROGRESS]", item.value["message"])
    elif (item.meta or {}).get("stream_kind") == "snapshot":
        print("[SNAPSHOT]", item.path, item.value["snapshot"])

result = await task.run()
meta = await task.meta()
```

每轮会把 planning decision、execution observation、verification evidence 和
checkpoint 写入 Workspace。下一轮通过 `workspace.build_context(...)` 取得
ContextPack，因此 loop 可以把证据带入下一轮，但 Workspace 不会变成自主规划器。

AgentTask 的验证仍由模型判断拥有，但最终验收采用保守 guard。loop 会规范化
verifier 输出；当仍有 missing criteria、必需 action evidence 失败或被 blocked、
仍需 approval，或必需 final deliverable 缺失时，不会把任务标记为 complete。
这些 guard 决策会记录在 task diagnostics 中，让下一轮基于具体证据 replan，而不是
接受一个证据不足的完成声明。

`task.stream()` 会发出结构化结果事件，并默认发出紧凑中间状态 `snapshot` item。
自然语言 `progress` item 需要通过
`options={"agent_task": {"stream_progress": True}}` 显式打开；内置描述是模板文本，
未配置 `progress_model_key` 时不会增加模型请求或 token 消耗。设置
`progress_model_key` 后，AgentTask 会用这个单独 model key 在后台基于已产生的
snapshot 和任务元数据总结进展；主循环不会为了 progress 多产出字段，也不会等待
progress 总结完成。progress narrator 失败属于 side-channel diagnostics 和
warning 级 runtime event，不会把主任务标记成 `model.request_failed`。
progress model 只接收 operator-safe snapshot；底层 Workspace/SQLite fallback
等 developer diagnostics 仍保留在 snapshot 和 `task.meta()["diagnostics"]`，
但不会进入 progress model 输入。

任务终态和 artifact 验收是两件事。`completed` 表示 verifier 已验收结果
（`accepted=True`、`artifact_status="accepted"`）。`max_iterations` 仍可能留下
有用的 Workspace 文件或 checkpoint，但它只是 partial artifact
（`accepted=False`、`artifact_status="partial"`），不是已完成的业务结果。

第一版公开 slice 有明确边界：单任务、单 Agent owner、约 2-5 次迭代，并通过
`AgentExecution` 执行 bounded step。这些 step 可以使用调用方已经在 Agent 上启用的
Actions、Skills 或 Dynamic Task 候选。AgentTask 不提供多任务协同、后台自治、分布式
租约或长期记忆管理。

当示例需要验证模型生成内容的语义质量时，应组合 deterministic smoke check 和第二个
Agently model-judge request。文件存在、问题数量、source label 可见等结构检查只能作为
smoke gate；语义验收应使用带每条规则 evidence 和 boolean 结果的 judge schema。

示例里的业务系统 fixture 可以 mock，但只能返回业务事实、记录、政策或有缺陷/不完整的
source data。不要让 mock 返回 pass/fail、隐藏标准答案或本地质量 verdict。若场景需要判断
artifact 是否正确处理了缺陷数据或冲突事实，应由 AgentTask verifier 或独立 Agently
model-judge request 基于明确规则和证据做判断。

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
`AgentExecutionLimitExceeded`，可以从 `agently.core` 根导出或
`agently.core.application.AgentExecution` 引入。execution meta 仍然可以检查，
并会记录 `status="blocked"`，以及 `diagnostics` 里的 limit event。

对于卡住的执行，`limits.max_seconds` 是整个 AgentExecution 的硬截止时间。
`limits.max_no_progress_seconds` 是 idle stall 边界：route selection、模型流、
Dynamic Task、Skills、ActionRuntime 任何被接受的运行进展都会刷新计时。如果任一边界
被超过，Agently 会抛出 `RuntimeStageStallError`，可以从 `agently.core` 根导出或
`agently.core.application.AgentExecution` 引入。
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
如果 provider 或最终响应构造在 materialization 完成前发出显式 stream error，
`get_text()` / `get_data()` / `get_meta()` 会传播该原始错误，而不是继续等到
materialization timeout。

高频 RuntimeEvent 出口应该通过 Event Center 请求摘要投递，而不是让
AgentExecution 在信号源降频：

```python
Agently.event_center.register_hook(
    handler,
    event_types="model.response.delta",
    hook_name="app.delta_summary",
    delivery_policy={"mode": "summary", "emit_interval": 0.1, "max_items": 20},
)
```

AgentExecution stream API 保持 raw。某个 hook 主动选择 summary delivery 时，
Event Center 摘要事件会包含 `meta["coalesced"]`、`coalesced_count` 和源事件 id。

`async_get_meta()` 会包含 `execution_mode`、`lineage`、`limits`、
`route`、`route_plan`、`logs`、`diagnostics` 和 `workspace_refs`。`logs` 是跨
route 稳定检查运行事实的位置，例如模型响应 id、
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
