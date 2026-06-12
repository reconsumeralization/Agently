---
title: Workspace
description: 用于多轮任务信息管理的持久 Workspace record。
keywords: Agently, Workspace, records, artifacts, checkpoints, 多轮任务
---

# Workspace

Workspace 是多轮任务的持久信息边界。当任务信息需要跨 turn 保留，但不应该塞进
prompt、Session 历史或紧凑 execution state 时，使用 Workspace。

Workspace V1 是底层能力。它负责存储和索引 record；它不决定模型应该记住什么，
也不决定下一步要执行什么。默认 Agent 和 TriggerFlow execution 都内置 lazy Workspace
binding，因此标准 Agent facade 上始终可以访问 `agent.workspace`，默认
`flow.create_execution()` 也可以通过 `data.require_resource("workspace")` 使用
execution 专属 Workspace。默认 local backend 只会在代码第一次写入、读取、
checkpoint、记录 evidence 或暴露 Workspace 文件区时 materialize。

```python
agent = Agently.create_agent("repo-worker")

ref = await agent.workspace.ingest(
    content=pytest_output,
    collection="observations",
    kind="test_output",
    summary="pytest failed in route fallback test",
    scope={"task_id": "issue-123", "turn": 1},
    source={"type": "command", "name": "pytest"},
)

records = await agent.workspace.search(
    "route fallback",
    filters={"collection": "observations", "kind": "test_output"},
)

context_pack = await agent.workspace.build_context(
    goal="Fix the route fallback failure.",
    scope={"task_id": "issue-123"},
    budget={"tokens": 12000},
    profile="auto",
)
```

应用需要稳定显式 root、read-only mode 或已注册 backend provider 时，再使用
`agent.use_workspace(...)`：

```python
agent.use_workspace("./.agently/runs/issue-123")
```

独立 Workspace 可以直接创建，也可以通过 Agently factory 创建：

```python
from agently import Agently, Workspace

shared_workspace = Workspace("./.agently/projects/issue-123")
factory_workspace = Agently.create_workspace("./.agently/projects/issue-124")
```

当多个 Agent、TriggerFlow execution 或 service worker 需要共享任务信息时，优先
由应用显式创建并管理一个公共 Workspace，再把每个消费者绑定到这个 Workspace。
这是推荐的信息共享形态：Workspace 保持为持久底座，而不是隐式全局单例。

```python
shared_workspace = Agently.create_workspace("./.agently/projects/issue-123")

agent = Agently.create_agent("repo-worker").use_workspace(shared_workspace)
execution = flow.create_execution(workspace=shared_workspace)
```

`flow.create_execution()` 默认创建 execution 专属 lazy Workspace。传
`workspace=False` 可以显式关闭；传 Workspace 实例、路径或 backend 时，execution
会使用应用自己管理的共享 Workspace。

不要依赖多个默认 Workspace 之间自动通讯。如果 TriggerFlow execution 过程中需要在
隔离 Workspace 之间移动信息，应在业务逻辑里显式完成：从源 Workspace search/read，
再写入或 ingest 到目标 Workspace，并把生成的 refs link 起来。Workspace 本身不提供
跨空间 messaging 或 replication 协议。

## 存什么

observations、decisions、artifacts 和紧凑 checkpoints 都可以作为 records。大型命令
输出、生成报告、transcript 和 patch 应存为 Workspace 内容，runtime state 里只保留
record refs。

```python
checkpoint_ref = await agent.workspace.checkpoint(
    "issue-123",
    {"phase": "debugging", "refs": [ref]},
    step_id="run-tests",
)

state = await agent.workspace.get_data(checkpoint_ref)
latest = await agent.workspace.latest_checkpoint("issue-123")
history = await agent.workspace.checkpoint_history("issue-123")
```

`get(...)` 按文本读取已存内容。record 中保存 dict、list 或 checkpoint state 等
JSON-compatible 结构化数据时，使用 `get_data(...)` 取回结构化对象。

## 持久 Provider 读取

默认 local Workspace backend 也提供单节点开发和本地重启恢复所需的 durable-provider
contract。runtime state 需要引用大型内容时，使用稳定 ref envelope，而不是复制全文。

```python
ref_envelope = await agent.workspace.ref_envelope(ref)
segment = await agent.workspace.read_bounded(ref, offset=0, limit=4096)

async for chunk in agent.workspace.stream_read(ref, chunk_size=8192):
    process(chunk["content"])
```

`ref_envelope` 包含 Workspace id、record id、collection、content ref、digest、
size、创建时间、policy labels 和 backend capability hints。`read_bounded(...)`
与 `stream_read(...)` 支持按片段读取大型 record，因此 execution state 可以只保存
refs，恢复逻辑只读取需要的部分。

当 TriggerFlow 或应用 execution 需要重启诊断，而又不能把 DevTools 当作事实来源时，
可以把 RuntimeEvent 存成持久 record：

```python
execution = flow.create_execution(workspace=agent.workspace)
snapshot_ref = await execution.async_save(step_id="review")

event_record = await agent.workspace.append_runtime_event(
    "issue-123-execution",
    {"event_type": "triggerflow.interrupt_raised", "payload": {"id": "approval"}},
    idempotency_key="approval-request-1",
    snapshot_ref=snapshot_ref,
    artifact_refs=[ref],
)

events = await agent.workspace.query_runtime_events(
    "issue-123-execution",
    sequence_from=event_record["sequence"],
)
```

RuntimeEvent 存储会保留每个 execution 内的 sequence、idempotency key、
state version、parent event id、causation id、parent signal id、aggregation
scope、operator id、interrupt id、resume request id、actor id、lease owner id、
snapshot refs、artifact refs 和 exchange id。分布式 provider 需要 fail-closed
append 顺序时使用 `expected_sequence=...`；callback 或 webhook 重试安全使用
`idempotency_key=...`。Workspace 不决定 pause/resume、approval、retry 或 DAG
readiness；这些语义仍由 TriggerFlow、PolicyApproval、ExecutionExchange 和
AgentExecution 所有。

Workspace-backed durable provider 也提供 TriggerFlow-facing snapshot CAS、lease
和 artifact-ref helpers：

```python
snapshot_ref = await agent.workspace.put_snapshot(
    execution.run_context.run_id,
    execution.save(),
    expected_state_version=previous_state_version,
)

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

artifact_ref = await agent.workspace.put_artifact_ref(
    execution.run_context.run_id,
    large_payload,
    metadata={"kind": "snapshot_payload"},
)
```

`expected_state_version=...` 会在最新 checkpoint state version 与调用方读到的
cursor 不一致时 fail closed。Lease methods 在所选 provider 内检查 owner 和 token。
local backend 提供这个单节点 durable-provider seam，用于开发和本地重启恢复；生产级
跨 worker 保证仍属于所选 backend。

## Links 与诊断

Links 用来记录 records 之间的 typed relationship，并且可以通过公开 API 查询，
不需要直接访问 backend 存储。

```python
decision_ref = await agent.workspace.put(
    {"decision": "Patch route fallback"},
    collection="decisions",
    kind="loop_decision",
    scope={"task_id": "issue-123"},
)

await agent.workspace.link(decision_ref, ref, relation="responds_to")
links = await agent.workspace.links(source=decision_ref, relation="responds_to")

capabilities = agent.workspace.capabilities()
```

`link_evidence(...)` 是 `link(...)` 上的一层便利封装，会把 execution id、
operation id、RuntimeEvent id、checkpoint id、exchange id 和 artifact refs 记录到
link metadata。Retention anchors 可在 compaction 后保留 lineage 与 summary refs：

```python
await agent.workspace.link_evidence(
    request_ref,
    result_ref,
    relation="resulted_in",
    execution_id="issue-123-execution",
    runtime_event_id=event_record["event_id"],
    checkpoint_id=checkpoint_ref["id"],
    artifact_refs=[ref],
)

await agent.workspace.add_retention_anchor(
    "issue-123-execution",
    anchor_type="compaction",
    record_ref=ref,
    preserved_event_ids=[event_record["event_id"]],
)
```

`capabilities()` 会报告当前 backend 的 content、metadata、checkpoint、
RuntimeEvent storage、ref resolution、retention policy、text index、policy 和 vector
index 组件。它也会报告 `supports_event_sequence`、`supports_range_read`、
`supports_stream_read`、`supports_retention`、`supports_compaction_anchor`、
`supports_cas`、`supports_lease`、`supports_artifact_refs` 和
`supports_remote_backend` 等 capability flags。分布式恢复应在所选 provider
缺少必要 flags 或对应 provider methods 时 fail closed。

## Action 边界

`agent.workspace.files_root` 是给 shell、Node.js 和文件 action 使用的普通可编辑作业区。
类文件系统的 Action helper 在没有显式 root 或 cwd 时会继承这个边界，包括 Agent 仍在
使用 lazy default Workspace 时。`agent.workspace.content_root` 仍然是 Workspace records
使用的受管内容存储。

```python
agent.enable_workspace_file_actions(write=True)
agent.enable_shell(commands=["pwd", "pytest"])
agent.enable_nodejs()
```

`enable_workspace_file_actions(...)` 不创建第二个 Workspace；它只是把当前 Workspace
文件作业区暴露成 list/search/read/write 文件 actions。只有某个 action 必须使用独立目录时，
才显式传入 `root=` 或 `cwd=`。

文件边界 policy metadata 可以持久化用于审计，但 Workspace 不因此变成 cwd manager：

```python
await agent.workspace.record_file_policy(
    allowed_roots=[str(agent.workspace.files_root)],
    root_source="workspace",
    policy_labels=["customer-data"],
)
```

## 不是记忆策略

Workspace V1 不暴露 `remember(...)`、`observe(...)`、`decide(...)` 这类可被模型调用
的记忆动词。这些属于未来 Action、Recall 或 WorkLoop 层的高阶接口。V1 中，应用代码
决定写入什么；Recall 骨架通过可插拔 planner、retriever 和 context-builder profile
把已存 records 打包成 `ContextPack`。

## 插件边界

Workspace 暴露 content、metadata、checkpoint、RuntimeEvent storage、ref
resolution、retention、evidence links、text index、policy 和 vector index 等底层
backend seam。默认本地 backend 是 filesystem content + SQLite metadata/FTS +
`NoopVectorIndex`。Recall 暴露 `RecallPlanner`、`Retriever` 和 `ContextBuilder`；
高级模型辅助规划、向量检索、rerank、compression 和 remote backends 预期作为插件叠加在这个底座上。

只要实现 Workspace backend protocol，自定义 backend 可以直接传给
`agent.use_workspace(...)`，也可以按名称注册：

```python
Agently.workspace.register_backend_provider("audit", build_audit_backend)

agent = (
    Agently.create_agent("repo-worker")
    .use_workspace(
        "tenant-a",
        provider="audit",
        provider_options={"tenant_id": "tenant-a"},
    )
)
```

Provider factory 会收到 `root`、`create`、`mode` 和所有 `provider_options`，
并返回一个 `WorkspaceBackend`。未注册的 provider name 会 fail fast，而不是回落到
local backend。没有显式选择 provider 时，Agent 的 lazy default Workspace 会使用
`.agently/workspaces/<agent-name>-<agent-id>` 下的 local backend。测试套件已经包含一个
协议级 remote audit provider proof，覆盖与本地 backend 相同的 checkpoint、RuntimeEvent、
evidence link 和 capability 路径。这个 proof 不等于公开 Redis、Postgres 或
object-storage adapter；生产 provider 仍必须报告真实能力，并在缺少分布式恢复要求时
fail closed。
TriggerFlow 测试也会通过 provider 读回 Workspace-backed execution snapshot，并由
TriggerFlow 自己 load pause/continue、policy approval waits 与
`when(..., mode="and")` join progress，因此 Workspace 仍是 storage，不是
workflow control plane。

`examples/workspace/workspace_loop_foundation.py` 展示了一个显式 TriggerFlow
loop：写入结构化 observations，把 decisions link 到 evidence，checkpoint 紧凑状态，
并通过 Recall 生成 ContextPack。

`examples/workspace/workspace_with_action_output.py` 展示 Action 边界：file action
写入 `workspace.files_root`，shell action 读取该文件，应用代码把 action output 显式
ingest 为 Workspace observation，再通过 Recall 打包成 ContextPack。
