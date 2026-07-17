---
title: Skills 与 AgentExecution
description: SkillLibrary、TaskContext 渐进式披露和轻量 SkillsExecutor 兼容 facade。
keywords: Agently, Skills, SkillLibrary, AgentExecution, TaskContext, SkillsExecutor
---

# Skills 与 AgentExecution

真实世界的 Skill 是带 revision 的知识与工作方法包。`SKILL.md` 提供指导，索引资源
可以提供 references、examples、assets 或 scripts。Skill 不是执行 route、策略
引擎、Action 授权或 workflow。

## 所有权

| 层 | 所有者 |
|---|---|
| 安装、解析、revision、resolve、list、pack membership | `SkillLibrary` |
| 任务级 selector 意图、精确 revision binding、required/model-decision mode | `AgentExecution` |
| guidance/resources 渐进式披露 | `TaskContext` + `SkillContextSource` + `ContextReader` |
| ModelRequest、AgentTask、TaskDAG、workflow、副作用 | 原有执行所有者 |
| 已发布的管理形态兼容调用 | `Agently.skills_executor` 轻量 facade |

`SkillLibrary` 安装不可变、content-addressed revision。execution 绑定精确
revision，不绑定可变目录 alias。Skill description 可以交给语义模型 selector；
本地代码不得用关键词表或正则从自由文本任务中选择 Skill。

## 推荐 Agent API

```python
contract = Agently.skills_executor.install_skills(
    "./skills/release-review",
    trust_level="local",
    update=True,
)

execution = (
    agent
    .use_skills([contract["skill_id"]], mode="required")
    .input("审查 3.2.0 发布候选。")
    .output({
        "decision": (str, "GO 或 NO-GO", True),
        "risks": ([str], "有证据支撑的发布风险", True),
    })
)
result = await execution.async_get_data()
```

`mode="required"` 以 fail-closed 方式绑定所选 revision。
`mode="model_decision"` 下，AgentExecution 用结构化 `ModelRequest` 从宿主发放
的 key 中选择，校验后绑定 revision；未知或重复 key 会 fail closed。

`agent.require_skills(...)` 是 required mode 的便捷方法。
`agent.use_skills_packs(...)` 把已安装的不可变 pack 展开为固定 revision refs。

revision 可用不等于已经被消费。只有披露后的 context package 绑定到一个具体的
ModelRequest response 时，AgentTask 才记录 Skill context consumption。该记录属于
上下文证据，不是可执行的 planner capability，也不是 Action evidence。

## `Agently.skills_executor` 仍然负责什么

facade 只保留已经发布的 Skill 管理和投影调用：

- 配置 SkillLibrary root 和允许的 trust label；
- 安装、列出、检查、读取本地 Skill package；
- 安装、列出、检查本地 Skill pack；
- 构造兼容 context-pack projection；
- 提供 TaskDAG `skill` resolver helper。

它不负责 route selection、effort strategy、stage、React loop、runtime chain、
Blocks lowering、script execution、capability inference、Action mounting、网络
下载或审批。远程 source 必须先由有权限的宿主代码物化，再按本地目录安装。

```python
pack = await Agently.skills_executor.async_build_context_pack(
    task="准备发布审查",
    skills=[contract["skill_id"]],
    include_references=True,
)
```

该方法创建临时 TaskContext，并使用与普通 execution 相同的 ContextReader
contract。`actionize_scripts=True` 会被忽略并产生 diagnostic；Skill script
仍是 descriptor，只有明确拥有授权和副作用责任的 Action/runtime 才能执行。

## 已发布的执行便捷 adapter

`agent.run_skills_task(...)` 与 `agent.async_run_skills_task(...)` 保留为普通
AgentExecution 的 result-shaped adapter：

```python
compat = await agent.async_run_skills_task(
    "审查 3.2.0 发布候选。",
    skills=[contract["skill_id"]],
    mode="required",
    output={"decision": (str, "GO 或 NO-GO", True)},
)
print(compat.execution.id, compat.output)
```

adapter 不选择 `skills` route。execution 和其他请求一样使用
`model_request`，或调用方显式指定的 AgentTask strategy。需要 stream、meta、
TaskContext diagnostics、retry 或 lifecycle control 的新代码应直接使用
AgentExecution API。

## 上下文限制与渐进式披露

安装 Skill 不会把全部资源复制进每次 prompt。required `SKILL.md` guidance
优先交付；resource index 与显式 refs 支持后续 bounded read。上下文过大时，
reader 返回 omissions、diagnostics 和可继续读取的 refs，不会把合成 summary
伪装成完整 source。

按 consumer 和 phase 返回一份或多份有界信息块。完整文件和原始 evidence
留在 SkillLibrary、TaskWorkspace 或 RecordStore；hot model path 只携带当前任务
相关的 package。

## 副作用

Skill 描述工作方法。Host code、ActionRuntime、ExecutionResource、
TaskWorkspace、RecordStore、TaskDAG 与 TriggerFlow 继续承担原有责任。Skill
不能静默授予 filesystem、network、MCP、credential 或 process 权限。
