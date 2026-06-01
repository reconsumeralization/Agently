---
title: Agently 4.1.3.2 Release Notes
description: Agently 4.1.3.2 的 AgentExecution task step、runtime stall control 和 RuntimeEvent delivery release note。
keywords: Agently, release notes, 4.1.3.2, AgentExecution, RuntimeEvent, Workspace, DevTools
---

# Agently 4.1.3.2 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.2.md) · **中文**

Agently 4.1.3.2 完成 4.1.3.2 AgentExecution task-step slice，并补齐让这些 step
可以在真实多步应用中落地的 runtime visibility 和 bounded diagnostics 能力。

## 主要变化

- `agent.create_execution(...)` 保留既有 turn 行为，并新增 bounded
  `mode="task_step"` 形态，支持显式传入 `lineage`、`limits` 和 Workspace。
- AgentExecution metadata 与 stream items 现在携带 execution id、execution
  mode、lineage、diagnostics、route、logs、close snapshot 和 workspace refs，便于跨
  step 关联。
- model-request budget 会贯穿 direct model call、DynamicTask model task 和
  Skills model stage。
- runtime stall control 覆盖 AgentExecution 整体 deadline、no-progress window、
  provider stream idle wait、response materialization、ActionRuntime stages 和
  ActionFlow loop close。
- `EventCenter` 继续作为 runtime event hub。它接收 `RuntimeEvent`，完成归一化和
  投递，并在出口侧应用 raw delivery、summary delivery、async background dispatch
  等 delivery policy。
- DevTools 保留 `ObservationEvent` projection，同时适配新的 RuntimeEvent 层级和
  分组呈现语义。
- Release acceptance 现在要求先做 coverage-first 论证，再用 examples、tests 和
  文档作为证据。

## 使用形态

```python
workspace = Agently.create_workspace("issue-intake")

execution = agent.create_execution(
    mode="task_step",
    lineage={"task_id": "issue-intake", "step_id": "collect-open-issues"},
    limits={"max_model_requests": 3, "max_seconds": 60, "max_no_progress_seconds": 15},
    workspace=workspace,
)

async for item in execution.async_stream():
    print(item.meta["execution_id"], item.meta["execution_mode"], item.meta["lineage"])

await execution.async_record_workspace(
    kind="checkpoint",
    data={"status": "collected", "source": "official site search"},
)
context = await workspace.async_build_context(query="latest unprocessed issues")
```

`None` 是推荐的 unlimited budget marker。对于已经用数字表达预算的设置，`-1`
仍作为兼容形态被接受。

## 示例

- `examples/agent_auto_orchestration/20_agent_execution_task_step_workspace_loop.py`
  运行两次 task-step execution，显式传入 lineage 和 limits，把 observations 与
  checkpoints 写入 Workspace，在 step 之间重建 Workspace context，并展示真实模型运行
  中的 stream/meta correlation。
- `examples/agent_auto_orchestration/21_agent_execution_github_issue_intake.py`
  展示 DeepSeek 驱动的 issue intake workflow。Agent 获得 shell 能力，自行判断如何
  搜索，可在有用时使用本地 `gh`，并把 intake 结果写入 Workspace，不把 GitHub Issues
  API 路径硬编码进框架。

## 兼容性

- Package version: `4.1.3.2`。
- Release manifest: `compatibility/releases/4.1.3.2.json`。
- 推荐 `agently-devtools`: `>=0.1.6,<0.2.0`。
- Companion Skills guidance 已和本次 release 的 runtime、dynamic task、playbook 与调试指引对齐。

## Issue 范围

本 release 关闭 #277 和 #280 背后的 runtime visibility 与 bounded-stall 工作。更广的
provider adoption 和长跑 observation 性能监控属于 release-line follow-up，不作为
4.1.3.2 blocker。
