---
title: Agently 4.1.3.7 Release Notes
description: Agently 4.1.3.7 的 AgentExecution-backed AgentTaskLoop 加固、goal/effort 配置、Skills context-pack DAG 支持和 release-blocker runtime 修复说明。
keywords: Agently, release notes, 4.1.3.7, AgentExecution, AgentTaskLoop, SkillsExecutor, ActionRuntime, TriggerFlow, Workspace
---

# Agently 4.1.3.7 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.7.md) · **中文**

Agently 4.1.3.7 加固由 `AgentExecution` 承载的 AgentTaskLoop 路径。本次 release
继续保持 `AgentExecution` 作为推荐 Agent run owner，同时让 goal pursuit 更清晰、
更容易验证，并与 Skills、Actions、DAG-shaped bounded steps、Workspace evidence 和
release diagnostics 对齐。

这不是新的公开 `AgentTask` lifecycle，也不是 TriggerFlow distributed recovery
里程碑。未来的 AdaptiveLoop / BootstrapLoop packaging、multi-task scheduling、
background autonomy 和 resumable AgentExecution strategies 仍然明确延期。

## 推荐用法

围绕同一个 execution draft 配置 Prompt、Skills、Actions、goals 和 effort：

```python
result = (
    agent
    .use_skills("website-builder", "seo-reviewer")
    .use_actions(write_file, read_file)
    .goal(
        [
            "Build a small product website.",
            "Include brand introduction, product features, target users, and contact information.",
        ],
        success_criteria=[
            "The final artifact is a runnable page file.",
            "The page content covers every supplied business fact.",
            "Execution evidence includes file write, readback, and content inspection.",
        ],
    )
    .effort(
        "medium",
        budget={"iteration_limit": 3, "model_call_limit": 10},
        execution={"step_plan": "auto"},
        verification={"strictness": "normal"},
    )
    .start()
)

data = result.get_data()
task_refs = result.task_refs
meta = result.get_meta()
```

`agent.goal(goal_or_goals, success_criteria=None)` 和复数可读性 alias
`agent.goals(...)` 返回 task-strategy `AgentExecution` drafts。详细
`effort(...)` 控制 planning depth、budget、verification、replan、progress 和可选
DAG-shaped bounded steps；它不授予权限，也不会绕过 host policy。

## 核心变动

| 领域 | 变动内容 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|---|---|
| Goal Pursuit API | Goal 和 success criteria 收敛到 execution-first 配置表面。 | `agent.goal([...], success_criteria=[...]).effort(...).start()` | 推荐形态增强；`AgentTask` 仍不是主要公开 lifecycle。 | AgentTaskLoop tests、release manifest、docs、examples。 |
| Effort 与 bounded steps | `effort(...)` 承载 budget、planning、verification、replan、progress 和 `execution.step_plan` 指引。 | 普通任务用 preset；host 需要显式边界时再加详细 sections。 | `execution.step_plan` 只指导策略；DAG 完成只是 step evidence，不是 task acceptance。 | `tests/test_agent_task_loop.py`、auto-orchestration docs、compatibility manifest。 |
| Verification 与 replan | Goal Pursuit 暴露 configured、planned、executing、evidence、verified、guarded、replan 和 terminal phase concepts。 | 通过 `AgentExecutionResult` 消费 result、stream、task refs 和 metadata。 | 完成仍需要 model verifier 接受并通过 host guards。 | AgentTaskLoop tests 和 `examples/agent_task/goal_pursuit_acceptance_matrix.py`。 |
| Skills context packs | SkillsExecutor 暴露 task-aware context-pack 和 TaskDAG resolver 支持。 | 常规使用 `agent.use_skills(...)`；custom planner 需要时调用 context-pack APIs。 | SkillsExecutor 仍是能力 adapter，不直接执行 bundled scripts。 | SkillsExecutor tests、主仓 compatibility metadata、Agently-Skills validation。 |
| ActionRuntime blockers | Action metadata 会隐藏 execution-environment secrets，action-loop timeout 覆盖完整 loop，显式 action-loop reply 不会再次进入 ActionRuntime，空 native tool-call plan 返回诊断。 | 使用当前 Action APIs，并用 `agent.action.summarize_records(...)` 查看 host-facing records。 | 修复 release blockers，不改变 ActionRuntime ownership。 | ActionRuntime tests 和 compatibility manifest。 |
| DevTools companion | DevTools UI、playground 和 runtime observation 语义对齐 `agent_execution` 命名。 | Agently 4.1.3.7 必须和 `agently-devtools` 0.1.9 同步发布，主仓 manifest 推荐保持 `>=0.1.9,<0.2.0`。 | 需要同步 companion release；不能在主仓发布时保留过期 DevTools 推荐版本。 | 主仓 compatibility manifest、DevTools package metadata、DevTools tests。 |
| TriggerFlow 与 Workspace foundation | 当前 manifests 记录支撑本 release line 的 TriggerFlow runtime integrity 和 Workspace durable-provider contracts。 | 把 TriggerFlow recovery 和 Workspace provider seams 视为 foundation capability；不要把未来 distributed recovery 当作已完成 AgentTask 工作。 | Production distributed recovery 仍明确延期。 | TriggerFlow / Workspace tests、examples 和 specs。 |

## 兼容性

- Package version: `4.1.3.7`。
- Release manifest: `compatibility/releases/4.1.3.7.json`。
- 推荐 `agently-devtools`: `>=0.1.9,<0.2.0`。
- `AgentTurn`、`create_turn(...)`、`set_turn_prompt(...)`、
  `set_request_prompt(...)`、`one_turn`、`task_step` 和 `task_scope` 不属于
  4.1.3.7 当前推荐表面。
- `AgentExecutionResult.resume()` 仍是预留表面，在 resumable strategy 落地前返回
  `supported=false`。

## 验证摘要

- Static typing 和确定性测试覆盖 AgentExecution chaining、AgentTaskLoop
  verification/replan、progress 和 snapshot streams、task refs、Workspace
  evidence links、Skills context-pack integration、ActionRuntime release
  blockers、TriggerFlow runtime/resource behavior、Workspace provider
  contracts，以及 compatibility registry 对齐。
- Goal Pursuit release example
  `examples/agent_task/goal_pursuit_acceptance_matrix.py` 记录了 2026-06-12
  的真实本地 Ollama 运行，覆盖一个 accepted task 和一个 max-iteration partial
  task。关键输出包括 `accepted.status="completed"`、
  `accepted.artifact_status="accepted"`、`partial.status="max_iterations"`、
  `partial.accepted=false`，以及 `partial.guard_reasons` 包含
  `missing_criteria_present`。
- Agently-Skills 指引已同步同一套 4.1.3.7 usage shape；promotion 前应继续运行
  companion validation suite。

## 延期范围

4.1.3.7 明确不完成 multi-task scheduling、background autonomy、distributed
lease ownership、完整 durable pause/resume、production external storage
providers，也不完成 AgentTaskLoop 的 TriggerFlow-backed AdaptiveLoop /
BootstrapLoop packaging。这些仍是未来架构工作。
