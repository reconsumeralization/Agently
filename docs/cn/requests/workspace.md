---
title: 任务上下文、文件与记录
description: TaskContext、ContextReader、TaskWorkspace 与 RecordStore 的职责边界。
keywords: Agently, TaskContext, ContextReader, TaskWorkspace, RecordStore, 渐进式披露
---

# 任务上下文、文件与记录

Agently 把过去混在 Workspace 里的职责拆成四个所有者。

| 所有者 | 负责 | 不负责 |
|---|---|---|
| `TaskContext` | 任务信息聚合、直接信息块、source binding、不可变读取快照 | 文件、持久化、语义执行路由 |
| `ContextReader` | 绑定 consumer/phase 的检索与渐进式披露；返回 `ContextPackage` | source 存储、写入、副作用 |
| `TaskWorkspace` | 明确的任务文件边界：路径约束、写入策略、格式化 readback、digest 与 file refs | records、memory、snapshot、Skill 选择 |
| `RecordStore` | 持久 records、检索索引、links、checkpoints、TriggerFlow snapshots/events、memory 持久化 | 任务文件、prompt 组装、语义相关性判断 |

`ContextSource` adapter 让不同来源的信息可被读取，但不会把来源所有权
搬进 `TaskContext`。内置 adapter 覆盖 SkillLibrary、TaskWorkspace 和
RecordStore；应用也可以挂载自己的 source。

## 文件边界：TaskWorkspace

```python
from agently import Agently, TaskWorkspace

task_workspace = TaskWorkspace("./project", mode="read_only")
agent = Agently.create_agent("repo-review").use_task_workspace(
    "./project",
    mode="read_only",
)
```

配置路径就是普通文件根目录。除非明确选择 `mode="read_write"`，现有外部
文件保持只读。只读边界需要生成新制品时，Agently 使用
`.agently/files/<execution-id>/` 下的 execution fallback，不覆盖现有外部
文件。TaskWorkspace 的 locator 与 content-version 私有身份信息只保存在它
自己的 `.agently` 区域。

未显式指定路径的 Agent 使用
`<入口目录>/.agently/task_workspaces/<agent-id>`，因此不同 Agent 默认不会
悄悄共享任务文件边界。

只有任务确实需要时，才把文件 Action 暴露给模型：

```python
agent.enable_task_workspace_file_actions(
    read=True,
    write=True,
    expose_to_model=True,
)
```

TaskWorkspace 为宿主 readback 提供稳定的 locator 与 content-version facts。
`[[ref:ref_1]]` 这类短引用只是请求内显示别名，不是持久身份；宿主必须校验
它，再映射回 canonical reference 身份。

## 持久化边界：RecordStore

```python
from agently.core.storage import RecordStore

record_store = RecordStore("./project-state", mode="read_write")
agent.use_record_store(record_store)

ref = await record_store.put(
    {"status": "verified"},
    collection="observations",
    kind="review_result",
    scope={"task_id": "review-42"},
)
```

本地 provider 把 records 写到
`<root>/.agently/records/records.db`。绑定 RecordStore 不会创建或改变
TaskWorkspace。`SessionMemory` 和 TriggerFlow durability 使用 RecordStore
ports。

TriggerFlow 可以用 `record_store=False` 关闭默认 RecordStore view，或绑定
显式 store：

```python
execution = flow.create_execution(
    record_store=record_store,
    runtime_resources={"runtime_event_store": record_store},
    auto_close=False,
)
```

AgentTask 过程状态默认只保留在内存和运行日志中。只有需要重启恢复时才启用
`record_store_recovery`。恢复引用属于 RecordStore；最终交付文件仍属于
TaskWorkspace。

## 信息交付：TaskContext 与 ContextReader

```python
from agently.core.context import TaskContext
from agently.core.storage import RecordStoreContextSource
from agently.types.data import ContextBudget, ContextReadIntent

task_context = TaskContext("review-42")
task_context.put(
    role="instruction",
    content="审查期间不得修改源文件。",
    required=True,
)
task_context.attach(
    RecordStoreContextSource(record_store),
    binding_id="review-records",
    scope="task",
)

reader = task_context.reader(
    consumer="review-planner",
    phase="planning",
    budget=ContextBudget(max_chars=6000, max_blocks=12),
)
package = await reader.async_read(
    ContextReadIntent(
        query="哪些证据与失败的审查相关？",
        filters={"source_kinds": ["record_store"]},
    )
)
```

每个 reader 固定一份 TaskContext/source revision 快照；读取开始前已经过期时应显式
refresh 或创建新 reader。如果列举候选本身推进了 source revision、但 TaskContext 结构
未变化（例如 source 首次建立惰性读视图），ContextReader 会重新固定新 revision 并重取
一次；持续或并发变化仍然 fail closed。required 和显式请求的信息块不能被静默丢弃。多个可选 prose
candidate 需要相关性判断时，使用 Agently `ModelRequest` semantic selector；
模型只返回宿主发放的 selection key，宿主校验后再重建 canonical record。

required 内容超出预算时默认仍然 fail closed。只有 Skill 或调用方显式接受有损投影
时，才可设置 `metadata={"required_overflow": "lossy_digest"}`。此时 Skill source
返回有界、`completeness="lossy"` 的结构化纲要，并保留不可变全文 ref、有序 section
refs、原始长度与省略事实；不会把静默截断伪装成完整权威指令。可选 section 仍由语义
selector 选择。只需要 required core、明确不做可选选择的 host preflight 还可以设置
`optional_selection="none"`。

AgentTask 通过同一份 context budget 传递该策略：

```python
execution = agent.goal(goal, success_criteria=criteria).strategy(
    "taskboard",
    context_budget={
        "chars": 12_000,
        "required_overflow": "lossy_digest",
    },
)
```

只有明确接受有损披露时才使用该设置；否则应换用更大/更聚焦的 consumer，或让
required Skill 在业务执行前失败。

`source_kinds` 是结构性来源过滤，不是语义路由。内置值包括
`task_workspace` 与 `record_store`；Skill source 已由安装 revision 和
execution binding 限定。

AgentTask 为每个实际 planner、worker、control card 与 verifier 请求创建独立的
reader/package。只有响应成功后才记录 `ContextConsumption`，其中保留精确的
package、response/request id、phase 与 block ids；失败请求不记录 consumption，也
不发出 `skills.context.bound`。AgentTask meta 通过 `context_packages` 与
`context_consumptions` 暴露审计信息。

## AgentExecution 所有权

每个 AgentExecution 拥有一个 TaskContext，以及 Agent 的 TaskWorkspace 与
RecordStore 的 execution-scoped view。AgentTask 复用 AgentExecution 交给它的
同一个 TaskContext 和 TaskWorkspace view。Skills 以不可变 SkillLibrary
revision 绑定为 Skill ContextSource，不会创建 Skills route 或执行引擎。

启用 `record_store_recovery` 后，持久快照同时保留 TaskContext 直接条目、可重建的
内建 source bindings、reader 披露历史、packages 与 consumptions。Skill source
按不可变 `revision_ref` 精确重建；自定义 ContextSource 不会被自动重建，会在
resume 时明确失败而不是静默消失。

普通 AgentExecution 使用
`execution.async_read_task_context(consumer_id=..., phase=..., intent=...)`
构造信息包。`intent` 可传查询字符串或 `ContextReadIntent`；省略时使用
AgentExecution 的 task target。
Blocks 的只读 `context_read` block 接收调用方已经绑定好的
`context_reader`。
