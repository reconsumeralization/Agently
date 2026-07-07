---
title: Agently 4.1.3.1 Release Notes
description: Agently 4.1.3.1 的 Workspace foundation 与显式多轮任务信息管理 release note。
keywords: Agently, release notes, 4.1.3.1, Workspace, Recall, Action Runtime
---

# Agently 4.1.3.1 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.1.md) · **中文**

Agently 4.1.3.1 是显式多轮任务信息管理的 foundation release。它加入了持久
Workspace 底座、Recall context skeleton，以及 Action Runtime 默认工作区继承能力，
让应用代码可以在多次执行步骤之间写入和取回任务信息。

这个版本不引入自主 WorkLoop planning。什么时候 observe、写入 record、search、
checkpoint 和 build context，仍然由应用代码、TriggerFlow 定义或普通 Python loop 决定。

## Highlights

- `agent.use_workspace(...)` 配置一个 Workspace，包含结构化 records、SQLite
  metadata/FTS、managed content storage 和可编辑的 `files_root`。
- `agent.workspace` 暴露 `put(...)`、`get(...)`、
  `get_data(...)`、`search(...)`、`link(...)`、`links(...)`、
  `checkpoint(...)`、`latest_checkpoint(...)`、`history(...)`、
  `capabilities()` 和 `build_context(...)`；`ingest(...)` 仅作为旧代码兼容
  alias 保留。
- Recall 通过 `ContextPlanner`、`Retriever` 和 `ContextBuilder` 保持插件式；
  默认提供 `auto` 和 `software_dev` profiles。
- `agent.enable_workspace_file_actions(...)` 把 Workspace 文件作业区暴露为
  list/search/read/write actions，但不创建第二个 Workspace。
- Shell 和 Node.js helpers 在配置 Foundation Workspace 后默认继承
  `agent.workspace.files_root`。
- `agent.enable_workspace(...)` 作为兼容 alias 保留，并提示迁移到
  `enable_workspace_file_actions(...)`。
- OpenAI-compatible requesters 现在会保留显式传入的 `Authorization` header，
  即使同时配置了 `api_key`。

## Recommended Usage

```python
agent = Agently.create_agent("issue-run").use_workspace("./.agently/runs/issue-123")

record = await agent.workspace.put(
    content={"route": None, "status": "failed"},
    collection="observations",
    kind="route_attempt",
    summary="Provider returned no route candidate",
    scope={"task_id": "issue-123", "area": "routing"},
    source={"type": "workflow", "step": "attempt-1"},
)

await agent.workspace.checkpoint(
    "issue-123",
    {"status": "failed", "evidence": record["id"]},
    step_id="attempt-1",
)

context = await agent.workspace.build_context(
    goal="Prepare the second routing attempt",
    scope={"task_id": "issue-123", "area": "routing"},
    budget={"max_items": 4},
)
```

当模型或 Action 层需要读写 Workspace 文件作业区时，单独暴露 file actions：

```python
agent.use_workspace("./.agently/runs/issue-123")
agent.enable_workspace_file_actions(write=True)
agent.enable_shell(commands=["cat", "python"])
```

Action output 不会自动成为 memory，需要显式写入：

```python
result = agent.action.execute_action("inspect_workspace_files", {"cmd": "cat notes/runtime.txt"})
await agent.workspace.put(
    content={"stdout": result["data"]["stdout"]},
    collection="observations",
    kind="action_output",
    summary="Shell inspection output",
    scope={"task_id": "issue-123"},
    source={"type": "action", "name": "inspect_workspace_files"},
)
```

## Examples

- `examples/workspace/workspace_loop_foundation.py` 展示显式 TriggerFlow loop：
  写入 observations 和 decisions、链接 evidence、写 checkpoints，并构建 Recall context。
- `examples/workspace/workspace_with_action_output.py` 展示通过 Workspace file
  actions 写文件、通过 shell action 读取文件，再把 action output 显式写入
  Workspace 后 build context。

## Compatibility

- Package version: `4.1.3.1`。
- Release manifest: `compatibility/releases/4.1.3.1.json`。
- 推荐 `agently-devtools`: `>=0.1.5,<0.2.0`。
- Workspace advanced Recall hardening、vector retrieval、model-assisted recall
  planning 和 WorkLoop self-planning 仍是后续 planned work。
