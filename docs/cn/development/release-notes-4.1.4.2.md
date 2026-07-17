---
title: Agently 4.1.4.2 开发说明
description: TaskContext、TaskWorkspace、RecordStore 与 SkillLibrary 所有权收敛的破坏式更新。
keywords: Agently, 4.1.4.2, TaskContext, TaskWorkspace, RecordStore, SkillLibrary
---

# Agently 4.1.4.2 开发说明

4.1.4.2 是开发线破坏式架构更新。过去合并式 Workspace 与 Skills execution
ownership 被直接替换，不通过 alias 走废弃兼容。

## 新所有者边界

- `TaskContext` 负责任务信息 aggregate 与 source bindings。
- `ContextReader` 负责绑定 consumer/phase 的检索和向 `ContextPackage` 的渐进式披露。
- `TaskWorkspace` 负责任务文件、路径约束、mutation policy、readback、digest 与 file refs。
- `RecordStore` 负责 records、检索索引、links、checkpoints、TriggerFlow
  snapshots/events 与 SessionMemory 持久化。本地 store 位于
  `<root>/.agently/records/records.db`。
- `SkillLibrary` 负责不可变的 Skill 与 Skill-pack 安装 revision。
- `AgentExecution` 负责任务级 Skill selection/binding，并与 AgentTask 共享同一个
  TaskContext。

移除的公开/开发概念包括 `Workspace`、`ContextBuilder`、`SkillsManager`、
SkillsExecutor plugin/strategy engine、`skill_activation`、
`workspace_operation`、`create_workspace` 与 `use_workspace`。

## Skills 应用接口

`agent.use_skills(...)`、`agent.require_skills(...)`、
`agent.use_skills_packs(...)` 直接把已安装 Skill revision 绑定到普通
AgentExecution；不存在 `skills` route。

`Agently.skills_executor` 只保留轻量兼容/管理 facade：本地 install、configure、
list、inspect、read、context-pack projection 与 TaskDAG helper。它不下载远程
source、不推断或授予 capability、不 actionize script、不选择 route，也不执行
Skill-local strategy。

`agent.run_skills_task(...)` 是普通 AgentExecution 的 result-shaped adapter。

Skill revision 可用、绑定到具体 ModelRequest response 的 context consumption，
以及 Action 执行证据是三类不同事实。AgentTask 不把 Skills 暴露为 planner
capability，也不接受 `skills` execution shape。`skills.revisions.bound` 只报告
revision binding，不声称 activation；`skills.context.bound` 报告真实的
response-bound context consumption。

## AgentTask 与 durability

AgentTask planning、observations、verification 与 replan state 默认只保留在内存和
运行日志中。只有需要重启恢复时才设置
`options={"agent_task": {"record_store_recovery": True}}`。最终文件及其可信物理
readback 仍属于 TaskWorkspace artifact；recovery refs 属于 RecordStore。

启用恢复时，AgentTask 还会快照 TaskContext 直接条目、可重建的内建 sources、
ContextReader 披露状态、精确 ContextPackages 与 ContextConsumptions。Skill
Context 按不可变 revision reference 恢复；不受支持的自定义 ContextSource 会明确
终止 resume。

required TaskWorkspace delivery path 无法读取当前物理文件时 fail closed。
TaskWorkspace readback 不能满足 required Action 或 Skill binding。

## TriggerFlow 与 Blocks

TriggerFlow 接受 `record_store=...`；`record_store=False` 关闭默认 store。
RecordStore 也可以提供显式 snapshot、runtime-event、lease 和 artifact-ref ports。
TriggerFlow 不创建 TaskWorkspace。

Blocks 保留对 caller-bound ContextReader 的只读 `context_read`。写入和其他副作用
仍属于 TaskWorkspace Actions、RecordStore、ActionRuntime、policy 或宿主代码。

## 迁移

```python
agent = (
    Agently.create_agent("review")
    .use_task_workspace("./project", mode="read_only")
    .use_record_store("./project-state", mode="read_write")
)
```

本开发线更新不为被移除的合并式 owner 提供 shim。该重构的回退 baseline 已记录
在本地开发历史与 spec evidence 中。
