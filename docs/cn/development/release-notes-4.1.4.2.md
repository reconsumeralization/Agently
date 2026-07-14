---
title: Agently 4.1.4.2 开发说明
description: Workspace 存储与生命周期破坏式简化的开发线说明。
keywords: Agently, 4.1.4.2, Workspace, AgentTask, TriggerFlow, 存储, 清理
---

# Agently 4.1.4.2 开发说明

> 语言：[English](../../en/development/release-notes-4.1.4.2.md) · **中文**

Agently 4.1.4.2 是当前开发目标。本轮对 Workspace 做破坏式重构，目标是让普通
任务接近零持久化开销，同时保留显式恢复和耐久信息场景。

## Workspace 边界

- `Workspace(root)` 直接把 `root` 作为普通文件边界。
- 默认根目录是入口脚本所在目录，无法确定时回退到当前工作目录。
- 外部文件默认可读、不可写。修改需要 `mode="read_write"` 或通过审批的文件
  Action。
- `.agently` 是保留的私有区域；fallback 文件、数据库、records、vectors、
  recovery、memory 和 Skills 状态分别按需创建。
- 无法写入外部目录的新制品进入 `.agently/files/<execution-id>/...`。不再提供
  公开 `files_root`、自动 Workspace 说明文件或框架规定的制品目录分类。

## 终态存储

AgentExecution 和 AgentTask 的完整运行过程默认保留在内存和观测输出中，不再复制
到 Workspace。终态清理只保留 trusted ref 通过物理回读的已选择 fallback 制品；
草稿、中间文件、未选择制品和校验失败的 ref 对应文件都会清理。普通外部文件永远
不是清理对象。

AgentTask 重启恢复需要显式开启：

```python
agent.create_task(
    goal="Prepare the report.",
    options={"agent_task": {"workspace_recovery": True}},
)
```

## TriggerFlow 持久化

TriggerFlow 可以绑定 Workspace 来访问文件或 records，但不会再自动把它当作
RuntimeEvent store。save、pause 和 load 在确有需要时可以激活 Workspace snapshot
恢复。持久 RuntimeEvent replay 或 Workspace audit 必须单独显式绑定
`runtime_event_store`。普通审计由日志、EventCenter sink 或 DevTools 承接。

大型 TriggerFlow close result 仍完整返回给进程内调用方，但超过有界 inline 限制后，
终态 RuntimeEvent 只记录省略说明；如果结果包含已选择文件制品，则只投影紧凑 ref。
TriggerFlow 不会仅为了搬运该事件结果而创建 Workspace record。

## State 赋值语义

`StateData.set(...)` 和下标赋值现在会完整替换旧值，包括 list、mapping、set 与空集合。
递归组合由 `StateData.update(...)` 显式表达；list 累加由 `append(...)` / `extend(...)`
显式表达。因此 TriggerFlow 的 `set_state(...)`、`async_set_state(...)` 和 flow-data
setter 会准确写入新值，不再残留旧集合成员，避免已清空队列、恢复快照和 TaskBoard
进度 mapping 在 tick 之间静默膨胀。

## 兼容性

这是开发线破坏式变更，不为已移除的中间版 Workspace 布局 API 保留 alias。进入
release preparation 之前，已发布的 4.1.4.1 manifest 和包版本保持不变；
`compatibility/in-development.json` 承载 4.1.4.2 目标。

功能分支被接受前，体积实验和完整仓库门禁仍属于待完成验收项。
