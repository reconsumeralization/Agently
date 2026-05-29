---
title: Workspace
description: 用于多轮任务信息管理的持久 Workspace record。
keywords: Agently, Workspace, records, artifacts, checkpoints, 多轮任务
---

# Workspace

Workspace 是多轮任务的持久信息边界。当任务信息需要跨 turn 保留，但不应该塞进
prompt、Session 历史或紧凑 execution state 时，使用 Workspace。

Workspace V1 是底层能力。它负责存储和索引 record；它不决定模型应该记住什么，
也不决定下一步要执行什么。

```python
agent = (
    Agently.create_agent("repo-worker")
    .use_workspace("./.agently/runs/issue-123")
)

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
    profile="software_dev",
)
```

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

`capabilities()` 会报告当前 backend 的 content、metadata、checkpoint、text index、
policy 和 vector index 组件。替换 local backend 或插件调试时可以用它确认当前 wiring。

## Action 边界

`agent.use_workspace(...)` 同时定义 `agent.workspace.content_root`。类文件系统的
Action helper 在没有显式 root 或 cwd 时会继承这个边界。

```python
agent.enable_workspace(write=True)
agent.enable_shell(commands=["pwd", "pytest"])
agent.enable_nodejs()
```

如果某个 action 必须使用独立目录，显式传入 `root=` 或 `cwd=`。

## 不是记忆策略

Workspace V1 不暴露 `remember(...)`、`observe(...)`、`decide(...)` 这类可被模型调用
的记忆动词。这些属于未来 Action、Recall 或 WorkLoop 层的高阶接口。V1 中，应用代码
决定写入什么；Recall 骨架通过可插拔 planner、retriever 和 context-builder profile
把已存 records 打包成 `ContextPack`。

## 插件边界

Workspace 暴露 content、metadata、checkpoint、text index、policy 和 vector index
等底层 backend seam。默认本地 backend 是 filesystem content + SQLite metadata/FTS
+ `NoopVectorIndex`。Recall 暴露 `RecallPlanner`、`Retriever` 和 `ContextBuilder`；
高级模型辅助规划、向量检索、rerank 和 compression 预期作为插件叠加在这个底座上。

`examples/trigger_flow/workspace_loop_foundation.py` 展示了一个显式 TriggerFlow
loop：写入结构化 observations，把 decisions link 到 evidence，checkpoint 紧凑状态，
并通过 Recall 生成 ContextPack。
