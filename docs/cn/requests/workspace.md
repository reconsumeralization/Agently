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
```

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
决定写入什么；后续 Recall 层决定给模型打包哪些上下文。

