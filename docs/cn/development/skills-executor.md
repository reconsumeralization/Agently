---
title: Skills Executor
description: 通过 Agently 安装和执行标准 SKILL.md Skill。
keywords: Agently, Skills Executor, SKILL.md, skills, run_skills_task, use_skills
---

# Skills Executor

> 语言： [English](../../en/development/skills-executor.md) · **中文**

Agently Skills 遵循标准 Skills 目录：`SKILL.md` 是能力定义，`scripts/`、
`references/`、`assets/` 是可选资源目录。Agently 不定义额外的 Skill
作者清单。

```markdown
---
name: release-review
description: Use when checking release readiness and rollback risk.
---

# Release Review

Follow this checklist before recommending a release or rollback...
```

## 安装

`install_skills(...)` 会把标准 Skill 目录复制到本地 registry。安装后的 Skill
根目录仍然直接包含 `SKILL.md`。Agently 只在安装副本内添加 `.agently/`
管理目录。

```text
.agently/skills/release-review/
|-- SKILL.md
|-- scripts/
|-- references/
|-- assets/
`-- .agently/
    |-- install.json
    |-- decision_card.json
    |-- resource_index.json
    `-- checksums.json
```

`.agently/` 文件用于加速路由、检查和资源索引，不是 Skill 能力定义。派生文件缺失或过期时，Agently 会重建，或直接回退读取 `SKILL.md`。

根目录下的 `skill.yaml`、`skill.json`、`agently.skill.yaml` 等非标准清单会被拒绝。`scripts/`、`references/`、`assets/` 里的同名文件只作为普通资源处理。

## 选择

用 `use_skills(...)` 将已安装 Skills 暴露为可选 route candidates。模型先看到简短 decision cards；只有 Skills route 真正执行时才注入完整 guidance。

```python
agent = Agently.create_agent("ops-assistant")
agent.use_skills(["release-review"], mode="model_decision")
```

需要检查将会使用哪些 Skills 时，调用 `resolve_skills_plan(...)`。Required
Skills 保持调用方顺序；多个可选候选由模型排序。

```python
plan = await agent.async_resolve_skills_plan(
    "Should this release be blocked?",
    skills=["release-review", "incident-triage"],
    mode="model_decision",
)
```

## 执行

当任务必须通过 selected Skills 回答时，用 `run_skills_task(...)`。执行是
prompt-first：Agently 会把 selected Skills 的完整 `SKILL.md` guidance、
decision cards、资源摘要和任务放进一次模型请求。

```python
execution = await agent.async_run_skills_task(
    "Review this release and give a go/no-go recommendation.",
    skills=["release-review"],
    mode="required",
)

print(execution.status)
print(execution.output)
print(execution.skill_logs)
```

安装 Skill 不会自动执行 bundled scripts 或资源。脚本和资源只有在宿主应用显式通过 Action 或 Execution Environment 授权时才能被使用。

## 配置

builtin Skills Executor plugin 不发布 stage/action 执行默认配置。标准 Skills
执行是 prompt-first，Skill 的适用性来自 `SKILL.md`；Agently 的 `.agently/`
文件只是描述性的安装元数据。

框架级 `skills.*` 配置仍可调整宿主行为，例如普通 prompt 是否披露可选 Skill
候选的完整 guidance。有 plugin defaults 时会先加载 plugin defaults，框架配置
是最终应用级默认值。两层配置都不能替代 `SKILL.md` 成为 Skill 能力定义。

## API Summary

- `Agently.skills_executor.install_skills(...)`
- `Agently.skills_executor.install_skills_pack(...)`
- `Agently.skills_executor.inspect_skills(...)`
- `agent.use_skills(...)`
- `agent.use_skills_packs(...)`
- `agent.resolve_skills_plan(...)`
- `agent.run_skills_task(...)`

`SkillContract` 描述已安装的标准 Skill、Agently 安装元数据、decision card、
资源索引和 checksums，不包含框架自创的 stage 声明。
