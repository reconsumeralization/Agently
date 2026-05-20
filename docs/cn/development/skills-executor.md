---
title: Skills Executor
description: 通过 Agent API 暴露的 planner-selected declarative behavior loop。
keywords: Agently, Skills Executor, skills, behavior loop, run_skill_task, use_skills
---

# Skills Executor

> 语言： [English](../../en/development/skills-executor.md) · **中文**

Skills Executor 让 Agently 应用安装 declarative skill package，并把它们作为
planner-selected behavior loop 使用。

它和 `Agently-Skills` companion repo 不是一回事：

- **Skills Executor** 是 Agently 框架内的 runtime 能力。
- **Agently-Skills** 是给 Codex、Claude Code、Cursor 等外部 coding agent 用的伴生 guidance。

## 当前状态

这个功能正在 `feature/skills-executor` 分支实现。

当前实现提供：

- `Agently.skills.install(...)`、`list()`、`inspect()`、`remove()`
- `agent.use_skills(...)`：把可选 skill cards 披露给模型决策路径
- 对被选为候选的 SKILL.md package，按字符上限披露 primary guidance
- `agent.resolve_skill_plan(...)`：生成 `SkillExecutionPlan`
- `agent.run_skill_task(...)`：显式执行 skill task
- SkillCard 组合元数据，例如 `stage_roles`、`consumes`、`produces`、
  `artifact_types`、`side_effects`、`required_capabilities`、`complements`、
  `failure_modes`
- declarative `action`、`model`、`validate`、`emit` stage 处理
- `run_skill_task(...)` 底层使用 Dynamic Task DAG 执行，
  `SkillExecution.close_snapshot` 会保留编译后的 task graph 结果以及 skill/action logs

第一版把 model-owned planning 留在 plan/decision 边界后面。当前实现先使用确定性过滤，
并允许应用通过 decision handler 调整 plan。完整模型 Planner 后续应落在同一个
`SkillExecutionPlan` contract 后面。

## 用户心智模型

Skill 不是 `skill.run()` 函数，也不是 `ActionExecutor`。

```text
Agent API
  -> skill cards and policy filtering
  -> SkillExecutionPlan
  -> Dynamic Task DAG
  -> SkillExecution
  -> Actions for atomic work
```

Action 仍然是原子能力。Skill 负责把这些能力组织成一个行为 loop。

## 可选 Skills

普通 agent 请求中，只想把 skills 作为候选能力时，用 `use_skills(...)`。

```python
agent = Agently.create_agent("ops-assistant")

agent.use_skills(
    ["release-checklist", "incident-triage"],
    mode="model_decision",
)

response = await (
    agent
    .input("Should this production issue trigger rollback?")
    .get_response()
    .async_get_text()
)
```

这会披露 skill cards，但不会强制执行某个 skill。
对于 SKILL.md package，primary guidance 正文也会按每个 skill 的字符上限进入 prompt，
让模型能使用 checklist 细节，但不会自动读取或执行 scripts/resource 目录。

## 强制 Skill Task

当任务必须通过 skill loop 完成时，用 `run_skill_task(...)`。

```python
execution = await agent.async_run_skill_task(
    "prepare release notes",
    skills=["release-checklist"],
    mode="required",
)

print(execution.status)
print(execution.output)
print(execution.action_logs)
```

`required` 模式下，请求的 skills 必须被选中。缺依赖、权限被拒或 Action 不可用时，
应 fail closed。

## 最小 Skill Package

```yaml
skill_id: release-checklist
display_name: Release Checklist
purpose: Check release readiness and record a release note.
trust_level: local
activation:
  keywords: [release, rollback]
requires:
  actions: [record_release_note]
stages:
  - id: record_note
    kind: action
    action: record_release_note
    input:
      text: "${task}"
  - id: validate_note
    kind: validate
    validation:
      required_state: [record_note]
```

## 组合元数据

真实 Skill Pack 应说明它和其他 Skills 如何组合。这些字段会保存在 `SkillCard`
里，并披露给 planner。

```yaml
card:
  stage_roles: [intake, action, validation]
  consumes:
    - role: task_request
      type: text
  produces:
    - role: release_note
      type: json
  artifact_types: [json]
  side_effects:
    - kind: local_record
      policy: allowed
  required_capabilities: [record_release_note]
  complements: [repo-review]
  failure_modes: [missing_action]
```

## 边界

- scripts/helpers 必须通过受控 Actions 执行，不能作为任意 Skill Python handler。
- guidance 披露只是 prompt context，不是代码执行。
- MCP、browser、sandbox、process、credentials 等资源生命周期归 Execution Environment。
- 长跑 workflow 行为应通过 TriggerFlow-backed skill execution 表达，不应藏在 Skill package 内部。

## 已实测外部 Skill 包

`examples/skills_executor/deepseek_external_skill_cards.py` 会安装并测试：

- `../Agently-Skills/skills/agently-runtime`
- `anthropics/skills/skills/xlsx`
- `anthropics/skills/skills/webapp-testing`

这个 example 验证 DeepSeek 在 `model_decision` 模式下能收到选定 skill 的 card 和受限
primary guidance；包内 scripts 仍然只是资产，除非应用把它们绑定为受控 Actions，否则不会执行。

`examples/skills_executor/realcase_dynamic_todo_triggerflow.py` 是 diagnostic realcase
版本。它安装 `Agently-Skills`，通过 `agent.use_skills(...)` 把 `agently-playbook`、
`agently-request`、`agently-triggerflow` 披露给 DeepSeek，然后让 DeepSeek 同时生成
Todo DAG 和完整 Python TriggerFlow executor module。prompt 不硬写 TriggerFlow API 细节；
host 脚本只评估模型生成模块是否用了真实 Agently API，以及能否跑通。

`examples/skills_executor/combo_skillpack_diagnostics.py` 是组合 Skill Pack 基准样例，
覆盖：

- 英语教学课程包生成
- 股票研究材料包生成
- 旅行规划，以及写入外部工具前的人类确认边界
- 调研报告到表格、文档、PPT、PDF
- Web 应用验收测试证据包

组合基准使用本地真实 `SKILL.md` 包；如果公共 Skill Pack 源码缺失，它会显式跳过
对应 case，而不是用 mock skill 替代。传入 `--fetch-missing` 可以拉取这些公开仓库。
模型只会看到可选 SkillCards 和受限长度的主 guidance；host 评估器检查 skill 选择、
阶段切换、中间产物、side effect 边界、approval gate、fallback 和输出覆盖率。

## 相关文档

- [Coding Agents](coding-agents.md)
- [Action Runtime](../actions/action-runtime.md)
- [Execution Environment](../actions/execution-environment.md)
- [TriggerFlow Overview](../triggerflow/overview.md)
