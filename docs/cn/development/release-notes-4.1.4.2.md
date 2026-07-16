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

## AgentTask 生命周期与终态修复

AgentTask 现在运行一张带版本的 TriggerFlow 生命周期图，明确暴露 context、plan、work、
materialization、evidence、verification 与 transition 节点。阶段事件只携带 host-issued
frame/plan/work/evidence id 与单调递增的 state version；旧版本和跨任务 signal 都会 fail
closed。TaskBoard 是 work 节点里的嵌套 work producer，并与 Flat 返回同一个终态 transition。

同一个语义 terminal verifier 现在同时负责当前 terminal-carrier inventory 的
`criterion_checks` 与 `material_claim_checks`。旧的 claim inventory、source selection、
逐 claim judgment 与 empty-inventory review 模型请求链已经删除。host 为当前 carrier 的每个
精确文本片段分配 request-local `claim_key`；模型只返回该 key 与已提供的 evidence reference
id，host 再从不可变 offered map 重建 carrier id、quote、path 与 content version。

required capability evidence 会在语义 verifier 之前判断。缺失的 authored
`action_succeeded` requirement 会直接安排 Action-shaped repair，不消耗 verifier 请求；required
Action 不可用时则立即 fail closed。

可信 file-backed carrier 的 material-claim repair 在 Flat 与 TaskBoard 中统一使用
host-owned control 路径。专用结构化 ModelRequest 按 host-issued `claim_key` 返回且只返回
一组 `old_string` / `new_string` replacement；这次 repair 不会打开通用
AgentExecution/ActionRuntime round。host 会在写入前校验授权 path、当前
`content_version_id`、精确匹配数量与 claim scope。整文件 write、replace-all、旧 version
和无关编辑都会 fail closed；成功 readback 会提升为新的 content version。

TaskBoard 现在会保留 `sufficient=true` control card 在
`next_board_action=stop` 时的 completed 状态；该字段只停止 board progression，不是 card
失败信号。material-claim patch control schema 要求每个 contract `claim_key` 都对应一个
operation，TaskBoard 与 Flat 使用相同的 immutable-version guard。host 生成的 Workspace
patch/readback artifact 即使被复制或换 path 仍保持 transport role，不能支撑其后代
carrier。

card execution status 也不再被模型写错的 `evidence_use` 否决。canonical Action lifecycle
fact 决定业务操作是否成功；无效 binding 只保留为不可信 diagnostics，唯一 terminal verifier
独立负责语义 acceptance。finalizer binding 可以在 host 侧固定 canonical evidence，但不会
作为第二套 evidence-id selection domain 被复制进 terminal verifier。

Flat 与 TaskBoard 还会把 artifact body、compact inline result 和 trusted refs 作为三个
独立 carrier。显式 inline result 不会成为文件正文；绑定 manifest path 且成功的 Action
write/readback 即使在 `inline_final` 计划下也会被提升为可信 artifact，累计可信 artifact
证据可供后续 iteration 使用，terminal verification 在整个 repair 中保持同一个物理
carrier/content version。终态投影会把显式 compact summary 与 file refs 一起保留，而不是用
pointer 覆盖摘要。未知或重复 claim key、未知 evidence id 会 fail closed；模型输出不能抄写或
重定义 canonical carrier identity 与 artifact quote。

同一个 manifest path 也只有一个写入 owner。一旦成功的文件 Action 已经写入该路径，
AgentTask 只会采用当前物理 Workspace readback，不会在 materialization 阶段再用另一份
`candidate_final_result` 或 `final_result` 正文覆盖文件。后续修改必须通过另一次显式文件
Action 完成。如果 Action 报告成功但声明路径无法 readback，delivery 会 fail closed，
不会回退到模型返回正文来掩盖失败。

存在 required Workspace delivery path 时，只有该路径当前物理 readback 会作为终态
Workspace carrier；中间 working file 保留为冷侧 evidence。verifier response 不合法时，
host 会把所有失败 response section 合并为一个结构化 repair contract，并携带当前 offered
claim/evidence key 集合。retry 只重入 verification 与 transition，复用已准备的 final
candidate，因此不会重复 TaskBoard finalization 或业务 work；相同稳定协议问题第三次出现
时仍然 fail closed。成功终态发出前还会刷新 terminal-convergence diagnostics 中已解决的
记录，避免观察者在任务已接受后继续看到旧的 active issue。

## 兼容性

这是开发线破坏式变更，不为已移除的中间版 Workspace 布局 API 保留 alias。进入
release preparation 之前，已发布的 4.1.4.1 manifest 和包版本保持不变；
`compatibility/in-development.json` 承载 4.1.4.2 目标。

功能分支被接受前，体积实验和完整仓库门禁仍属于待完成验收项。
