# Agent 自动编排

Agently 4.1.3 将 `agent.start()` 作为 Agent turn 的默认用户层入口。它仍然返回
业务结果，但 Agent 可以在显式注入候选能力后，路由到普通模型响应、Actions、
Skills Executor 或 DAG-shaped execution。

```python
result = (
    agent
    .use_actions([market_data_action])
    .use_skills_packs(["equity-research"])
    .input("Review this renewal risk.")
    .output({"answer": (str, "final answer", True)})
    .start()
)
```

候选注入是边界。如果没有注册 Actions、Skills、Skills Packs 或 DAG
候选，`agent.start()` 仍然是普通模型请求。

`TaskDAG` 是 DAG 基石能力。`DynamicTask` 保留为 DAG planning/execution 之上的
兼容与便利 facade，不再是第二套推荐任务生命周期。`agent.use_dynamic_task(...)`
注册 Agent 级 DAG 候选，供后续 execution 使用。`execution.use_dynamic_task(...)`
只注册到当前 `AgentExecution` draft，因此一次 DAG route 不会泄漏到无关的
Agent run。

quick prompt 链会创建 execution-scoped draft。Agent 可以作为服务单例保存共享
settings、模型激活、Actions、Skills、Workspace 和 `define(...)` / `always=True`
prompt；同一条链里的 `.input(...)`、`.system(...)`、`.output(...)`、附件和本次
execution options 会写入隔离的 `AgentExecution` draft：

```python
results = await asyncio.gather(
    agent.input("Summarize request A").async_start(),
    agent.input("Summarize request B").async_start(),
    agent.input("Summarize request C").async_start(),
)
```

多语句 setup 应显式拿住 execution draft：

```python
execution = agent.create_execution()
execution.input("Review this renewal risk.")
execution.output({"answer": (str, "final answer", True)})
result = await execution.async_start()
```

不要再依赖 `agent.input(...); agent.output(...); await agent.async_start()`
来累计本轮 execution prompt。Agent 生命周期状态使用 `always=True`、
`set_agent_prompt(...)` 或 `agent.define(...)`。只有明确需要低层 request-builder
兼容面时才使用 `agent.create_request(...)` / `agent.request`。

已验收开发线的路由是候选驱动、确定性优先：通过 DynamicTask facade 提交的
DAG 候选优先，required Skills 候选进入 Skills route。当同时存在多个可选候选，
例如 DAG-shaped execution、model-decision Skills 和普通 Actions 时，默认由模型
选择 route；如果只有一个可选候选，则直接选择该 route。

公开 Agent API 仍由 core 持有，但路线规划和执行由 active
`AgentOrchestrator` plugin 通过 `AgentOrchestrator` protocol 承担。这样
Skills、DAG substrate 和后续 route 实现都可以替换，而不需要 core 知道内置
plugin 的内部实现。

## Goal Pursuit

当业务目标需要有边界的 planning、execution、evidence、verification 和 replan
闭环时，使用 `agent.goal(goal_or_goals, success_criteria=None)`。
`agent.goals(...)` 只是同一个入口的复数 alias。

```python
result = (
    agent
    .use_skills("website-builder", "seo-reviewer")
    .use_actions(write_file, read_file)
    .require_actions("write_file")
    .goals(
        [
            "构建一个小型产品官网。",
            "准备上线检查清单。",
        ],
        success_criteria=[
            "最终产物是一个可运行页面文件。",
            "页面内容覆盖所有输入的业务事实。",
            "执行证据包含文件写入、读回和内容检查。",
        ],
    )
    .effort(
        "high",
        budget={
            "iteration_limit": 4,
            "model_call_limit": 10,
            "wall_time_seconds": 300,
        },
        planning={"depth": "expanded", "max_plan_items": 8},
        verification={"strictness": "strict"},
        replan={"policy": "on_verification_failure", "limit": 2},
        progress={"detail": "phase"},
    )
    .start()
)
```

简单代码仍然可以只写 `.effort("low" | "medium" | "high")`。展开形式仍然属于
同一个入口：effort 只控制策略和资源强度，不决定 execution 是否进入目标追求。
`budget.iteration_limit` 会映射到 task-loop iteration budget；
`model_call_limit` 和 `wall_time_seconds` 会映射到 AgentExecution limits，除非
调用方已经显式设置了 limits。完成仍然必须同时通过 model verification 和 host guards。

`execution.step_plan` 默认是 `auto`，普通用户不需要显式写出来。它允许 Goal
Pursuit 的某一轮在下一步天然包含串行或并行子步骤时，把 DAG 作为内部
bounded-step 形态使用。只有当调用方要强制一个 bounded AgentExecution step 时才写
`execution={"step_plan": "direct"}`；当调用方希望偏向 DAG-shaped step 并限制规模时，
才写 `execution={"step_plan": "dag", "max_tasks": 6}`。DAG 结果只会回收到
AgentTaskLoop evidence；必须继续通过 model verifier 和 host guards，才算任务完成。

## AgentTask Loop

当业务目标需要一个有边界的多轮闭环，而不是一次 direct AgentExecution 时，使用
`agent.create_task(...)`。它返回一个 task-strategy `AgentExecution` draft；
内部保留的 `AgentTask` record 运行一个由单个 Agent 持有的任务：计划、执行一个
bounded step、写入 Workspace 证据、验证、必要时 replan，最后以 complete 或
blocked 结束。

在 4.1.3.7 里，这是一个加固后的有边界公开 task-loop strategy，
不是完整未来版 AgentTask 系统。`agent.create_task_loop(...)` 是同一个长任务
strategy 的显式写法，适合代码需要把 strategy 选择说清楚的场景。两个 API 仍然
返回 `AgentExecution`；新代码应通过 `execution.get_result()` 或 execution 的
stream/meta facade 消费 data、text、stream、metadata、status 和 task refs，而不是
把 `AgentTask` 当成第二套 public lifecycle。

```python
execution = agent.create_task(
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

result = execution.get_result()

async for item in result.get_async_generator():
    if (item.meta or {}).get("stream_kind") == "progress":
        print("[PROGRESS]", item.value["message"])
    elif (item.meta or {}).get("stream_kind") == "snapshot":
        print("[SNAPSHOT]", item.path, item.value["snapshot"])

data = await result.async_get_data()
meta = await result.async_get_meta()
task_refs = result.task_refs
```

每轮会把 planning decision、execution observation、verification evidence、
evidence links 和 checkpoint 写入 Workspace。checkpoint 通过 Workspace
checkpoint-store port 写入，task evidence 关系通过 `workspace.link_evidence(...)`
记录。下一轮通过 `workspace.build_context(...)` 取得 ContextPack，因此 loop
可以把证据带入下一轮，但 Workspace 不会变成自主规划器。

AgentTask 的验证仍由模型判断拥有，但最终验收采用保守 guard。loop 会规范化
verifier 输出；当仍有 missing criteria、必需 action evidence 失败或被 blocked、
仍需 approval，或必需 final deliverable 缺失时，不会把任务标记为 complete。
这些 guard 决策会记录在 task diagnostics 中，让下一轮基于具体证据 replan，而不是
接受一个证据不足的完成声明。

task-strategy AgentExecution stream 会发出结构化结果事件，并默认发出紧凑中间状态
`snapshot` item。
自然语言 `progress` item 需要通过
`options={"agent_task": {"stream_progress": True}}` 显式打开；内置描述是模板文本，
未配置 `progress_model_key` 时不会增加模型请求或 token 消耗。设置
`progress_model_key` 后，AgentTask 会用这个单独 model key 在后台基于已产生的
snapshot 和任务元数据总结进展；主循环不会为了 progress 多产出字段，也不会等待
progress 总结完成。progress narrator 失败属于 side-channel diagnostics 和
warning 级 runtime event，不会把主 execution 标记成 `model.request_failed`。
progress model 只接收 operator-safe snapshot；底层 Workspace/SQLite fallback
等 developer diagnostics 仍保留在 snapshot 和 `task.meta()["diagnostics"]`，
但不会进入 progress model 输入。

任务终态和 artifact 验收是两件事。`completed` 表示 verifier 已验收结果
（`accepted=True`、`artifact_status="accepted"`）。`max_iterations` 仍可能留下
有用的 Workspace 文件或 checkpoint，但它只是 partial artifact
（`accepted=False`、`artifact_status="partial"`），不是已完成的业务结果。

`examples/agent_task/goal_pursuit_acceptance_matrix.py` 是这个合同的轻量
real-model smoke。它运行一个 accepted Goal Pursuit 案例和一个未验收的
evidence guard 案例，planning 和 verification 都由模型完成，最后打印 verifier
与 host guard 共同决定的终态证据。设置
`AGENT_TASK_MODEL_PROVIDER=ollama` 可以复现注释里的 `max_iterations` /
partial 输出；更严格的 provider 可能把同一个缺失 Action evidence 判断为
`blocked`。

`examples/agent_task/agently_architecture_diagram_task.py` 是同一路径下更长的
设计文档实验。它使用 `.goal(...).effort(...).strategy("task")`、仓库源码资料
Action、Workspace 文件 Actions，以及独立 Agently model judge，生成并复查一份
层次清晰的 Agently 架构图。

第一版公开 slice 有明确边界：单任务、单 Agent owner、约 2-5 次迭代，并通过
`AgentExecution` 执行 bounded step。这些 step 可以使用调用方已经在 Agent 上启用的
Actions、Skills 或 DAG 候选，也可以使用当前 execution 上临时挂载的 DAG 候选。
AgentTask 不提供多任务协同、后台自治、分布式租约、step 内 pause/resume 或
长期记忆管理。这个 slice 的崩溃恢复通过 `agent.resume(...)` /
`agent.async_resume(...)` 暴露，它会重建 task-strategy `AgentExecution`，而不是把
AgentTask 暴露成第二套公开生命周期。

### 崩溃后恢复任务

AgentTaskLoop 在每次迭代完成后都会持久化一份可恢复快照。若进程崩溃，可以恢复成一个
新的 `AgentExecution` 并从下一次迭代继续。已完成的迭代不会重复执行：

```python
execution = await agent.async_resume("issue-123")   # 或 agent.resume("issue-123")
result = await execution.async_start()              # 从第 N+1 次迭代继续
meta = await execution.async_get_meta()
```

恢复会从 Workspace 读取该任务最新快照，还原迭代历史与累计的 required 能力进度；若不存在
可恢复快照则抛出 `ValueError`。崩溃时正在执行中的那次迭代会被重新规划，因此非
replay-safe 的 step 副作用由宿主负责。当 result 带有可恢复的 `task_refs` 时，
`AgentExecutionResult.resume()` 会委托同一个 Agent resume facade；否则返回不支持恢复的
响应。`resume_task(...)` 只保留为 `resume(...)` 的兼容别名。

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
execution = agent.create_execution()
execution.input("Run the reviewed graph.")
execution.use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

execution 对象沿用模型 response 的消费风格：`get_data`、`get_text`、
`get_meta`、`get_generator` 以及对应 async 方法。
execution stream 产出的是 `agently.types.data` 里的
`AgentExecutionStreamData`。它保留熟悉的 `path`、`value`、`delta`、
`is_complete` 字段，并增加过程级事件需要的 route metadata。

`create_execution()` 创建一个 AgentExecution draft。只有 prompt 的 draft 会作为
直接模型请求执行。当开发者自己编写循环，或 task strategy 需要一个有边界的单步执行时，
用 `lineage` 和 `limits` 表达边界：

```python
execution = agent.input("Try one bounded fix step.").create_execution(
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

这仍然只是一次 AgentExecution，不是多轮循环本身。`lineage` 提供稳定关联，
`limits` 提供跨普通模型 route、TaskDAG model task、Skills model stage
共享的模型请求预算计数。无限预算用 `None` 表达。

如果有边界的 execution 超出模型请求预算，Agently 会抛出
`AgentExecutionLimitExceeded`，可以从 `agently.core` 根导出或
`agently.core.application.AgentExecution` 引入。execution meta 仍然可以检查，
并会记录 `status="blocked"`，以及 `diagnostics` 里的 limit event。

对于卡住的执行，`limits.max_seconds` 是整个 AgentExecution 的硬截止时间。
`limits.max_no_progress_seconds` 是 idle stall 边界：route selection、模型流、
TaskDAG、Skills、ActionRuntime 任何被接受的运行进展都会刷新计时。如果任一边界
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

`async_get_meta()` 会包含 `lineage`、`limits`、
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
item.meta["lineage"]["task_id"]
```

默认 Agent 带有 lazy Workspace binding；也可以在 `create_execution()` 之前用
`agent.use_workspace(...)` 覆盖为显式 root 或 provider。AgentExecution 仍然不会自动
决定什么应该进入记忆；调用方应从 execution 侧显式持久化：

```python
workspace_record = await execution.async_record_workspace(
    collection="observations",
    kind="agent_execution_observation",
    content={"result": data},
    checkpoint=True,
)
```

这个 helper 会通过 execution 绑定的 Workspace provider surface 写入。请求
checkpoint 时，它会使用 checkpoint-store port，并在 AgentExecution record 与
checkpoint 之间写入 evidence link。record id、checkpoint id 和 evidence link id
都可以从 `meta["workspace_refs"]` 读取。Workspace 保持 durable substrate，不需要
理解 AgentExecution 策略语义。下一步再由调用方调用 `workspace.build_context(...)`。

开发排障时，可以挂 EventCenter observation hook，或临时打开控制台明细：

```python
Agently.event_center.register_hook(print, event_types=None, hook_name="debug")
agent.set_settings("debug", "detail")
```

这只用于调试 route selection、model request、ActionRuntime 或 Workspace 持久化。
问题定位后，应从示例和生产代码中移除 debug hook/settings。

## 提交式 DAG 输入

通过 DynamicTask facade 路由的提交式 DAG，其 task `inputs` 继续使用 DAG 运行时
占位符，例如 `${INIT.ticket}` 和 `${DEPS.lookup}`。在 Agent route 里，graph input
按以下顺序解析：

```text
use_dynamic_task(graph_input=...)
> execution prompt snapshot 的 input slot
> {"target": task_target}
```

因此普通 Agent prompt 写法可以直接喂给提交式 DAG，而不需要另造一套映射面：

```python
execution = agent.create_execution()
execution.input({"ticket": "TICKET-OK"})
execution.use_dynamic_task(mode="submitted", plan=graph, handlers=handlers)
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

TaskDAG model 节点会把结构化输出字段映射到稳定 path：

```python
async for item in execution.get_async_generator(type="instant"):
    if item.path == "task_dag.tasks.reply.fields.reply" and item.delta:
        print(item.delta, end="", flush=True)
```

这样保留 ModelResponseResult `instant` 的字段级 delta 语义，同时让过程流 path
由 Agent execution route 统一拥有。
