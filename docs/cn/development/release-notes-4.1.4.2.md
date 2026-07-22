---
title: Agently 4.1.4.2 开发说明
description: TaskContext、TaskWorkspace、RecordStore 与 SkillLibrary 所有权收敛的破坏式更新。
keywords: Agently, 4.1.4.2, TaskContext, TaskWorkspace, RecordStore, SkillLibrary
---

# Agently 4.1.4.2 开发说明

4.1.4.2 是开发线破坏式架构更新。过去合并式 Workspace 与 Skills execution
ownership 被直接替换，不通过 alias 走废弃兼容。

## 新所有者边界

- `TaskContext` 负责任务信息 aggregate、source bindings、一套内部派生
  ContextIndex 生命周期与读取句柄。
- `ContextReader` 是只能由 TaskContext 创建或恢复、绑定 consumer/phase 的公开句柄；
  它负责渐进检索并生成不可变 `ContextPackage`，不是第二个 aggregate owner。
- `TaskWorkspace` 负责任务文件、路径约束、mutation policy、readback、digest 与 file refs。
- `RecordStore` 负责 records、检索索引、links、checkpoints、TriggerFlow
  snapshots/events 与 SessionMemory 持久化。本地 store 位于
  `<root>/.agently/records/records.db`。
- `SessionMemory` 负责 memory extraction/compression 与 accepted RecordStore
  写入；active recall 通过 `session_memory` ContextSource 并入任务信息，不再运行
  平行 prompt-injection pipeline。
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

`Agently.skills_executor` 只保留轻量兼容/管理 facade：install、configure、list、
inspect、read、context-pack projection 与 TaskDAG helper。已授权 Git 或本地 source
会先由 `SkillSourceProvider` 落成快照，再进入不可变 SkillLibrary 安装。它不推断
capability、不选择 route，也不执行 Skill-local strategy。
远程兼容安装默认标记为 `untrusted`；Git/local source 的选定 subpath 会拒绝逃出已物化
source root 的 symlink component。

可信精确 Skill revision 中经显式授权的 script 可以绑定成普通
`code_execution` Action。Skill 层只提供 revision/path/digest identity；执行所有权
仍属于 ActionRuntime、TaskWorkspace、语言 adapter 与 ExecutionResource。

`agent.run_skills_task(...)` 是普通 AgentExecution 的 result-shaped adapter。

Skill revision 可用、绑定到具体 ModelRequest response 的 context consumption，
以及 Action 执行证据是三类不同事实。AgentTask 不把 Skills 暴露为 planner
capability，也不接受 `skills` execution shape。`skills.revisions.bound` 只报告
revision binding，不声称 activation；`skills.context.bound` 报告真实的
response-bound context consumption。

release-pinned Skill 检查现在严格遵循这套所有权：保留的 management facade 只负责
配置、安装和检查 canonical `SkillLibrary`，随后通过 `Agently.skill_library` 解析
不可变 revision，再由 `agent.require_skills(...)` 绑定该精确 revision。旧的
`resolve_skills_plan(...)` / `prompt_bindings` 断言已移除，因为它锁定的是已删除的
SkillsExecutor planning 与 prompt-injection engine，而不是 4.1.4.2 支持的行为。

## AgentTask 与 durability

AgentTask planning、observations、verification 与 replan state 默认只保留在内存和
运行日志中。只有需要重启恢复时才设置
`options={"agent_task": {"record_store_recovery": True}}`。最终文件及其可信物理
readback 仍属于 TaskWorkspace artifact；recovery refs 属于 RecordStore。

启用恢复时，AgentTask 还会快照 TaskContext 直接条目、可重建的内建 sources、
ContextReader 披露状态、精确 ContextPackages 与 ContextConsumptions。Skill
Context 按不可变 revision reference 恢复；不受支持的自定义 ContextSource 会明确
终止 resume。
ContextSource 现在提供结构 descriptor 枚举与有界 exact readback。TaskContext 的内部
ContextIndex 构造以 revision/profile/provider 为 key 的 structural、lexical 或可选
hybrid partition；ContextReader 负责 consumer-local query offset、精确 source read、
语义选择与 ContextPackage 构造。`source_kinds` 是当前 TaskContext 实际挂载 source 的
开放 vocabulary，不是框架硬编码列表。

ContextReader 现在执行保守媒体边界。文本以及成功解析的
PDF/DOCX/XLSX/PPTX 内容可以进入 ContextPackage。图片只有在具体 consumer 显式声明
支持图片附件时才进入附件通道；具备该能力的 AgentTask 请求会通过 ModelRequest
attachment channel 绑定经过校验的图片块。二进制、未知、未解析、非法或空媒体只保留
引用或使本次选中读取失败，不会根据文件名猜测内容。

Action spec 暴露 `required_input_keys`，可从本地函数签名推导，也可由 executor/MCP
adapter 声明。native tool schema 携带同一要求；模型生成的调用缺少必填 key 时会在
dispatch 前失败。TaskBoard 的 scoped retrieval 在初始 planning 与 repair 中都保持为
ContextReader 路径；纯 retrieval support card 不再被标成 Action card。

`max_model_requests` 现在是一份原子共享的 lineage budget。descendant execution 会
消耗 ancestor allowance，而不是重新计数；child-local limit 只约束该 child subtree。

required TaskWorkspace delivery path 在 terminal verification 期间表示为 digest-pinned
暂存候选。verifier 拒绝时旧目标保持不变；验收通过后才原子提升目标并进行完整的
提升后 readback。提升、digest 或 readback 任一失败都会把任务转为 blocked。
TaskWorkspace readback 不能满足 required Action 或 Skill binding。

strict verification 判断缺少事实材料时，`replan_segment` 会先创建证据补采 card，
再创建依赖它的制品修订 card。只有出现新的稳定
`(owner, locator, content_version, range)` source identity，宿主才会调度修订；
重新读取最终制品，或仅更换 call id 重读同一份未变化 source，都会记为 setback，
不算证据进展。
补采完成后，只有新 reference 被原失败 criterion 或 material-claim check 实际引用，
才能继续一次修订；无关的新素材不能维持 replan 循环。
dependency readback evidence 会在构造 TaskBoard card prompt 前先完成规范化。prompt
projection、宿主 binding validation、acceptance index 与结果持久化复用同一个 live
ledger identity domain，因此模型合法选择的 reference 不会因宿主随后重建 ordered
evidence view 而换号。material-claim target 使用宿主拥有的稳定精确 claim identity，
不再跨轮持久化 response-local `claim_N` 位置编号。

control card 明确返回 `sufficient=false` 时，不能再被
`next_board_action=finalize` 覆盖成 completed；宿主会将其规范化为 setback，避免只有
outline manifest、没有真实 deliverable 的完成死状态。

## Workspace-backed 代码执行

`agent.enable_code_runtime(...)` 通过 provider-neutral adapter 支持 Python 3.10+、
Node.js 18+、Go 1.25+ 与 C++20。每次执行都遵循 TaskWorkspace grant -> provider
binding -> 不可变 bundle 落地 -> argv execution -> 输出读回 -> release/close。
Docker 只是一个 provider；`trusted_local` 是显式无防护 fallback，不能满足硬性隔离要求。
provider probe 会报告真实观测的 toolchain version 与 safety/isolation facts；adapter 的
最低或精确版本约束参与有序筛选，选中后的事实保留在 Action result metadata 中。
隔离 probe 使用具体布尔能力轴，而不是 provider 自报标签。expected outputs 必须是有界、
规范化的 `output/` 路径；制品缺失、cleanup 失败、超时或取消都会显式失败，并终止所属
进程或容器。

外部隔离 provider 使用有序候选描述符。具体 gVisor 与 Seatbelt 实现仍由社区贡献者
在 PR #325、#327 中持有；本开发分支只提供迁移契约，不复制任一实现。

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
