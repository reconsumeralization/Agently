---
title: Agently 4.1.3.8 Release Notes
description: Agently 4.1.3.8 的任务执行策略优化、TaskBoard 策略选择、ACP fallback 能力、输出控制兜底、观测兼容和公开类型元数据说明。
keywords: Agently, release notes, 4.1.3.8, AgentExecution, AgentTaskLoop, TaskBoard, ACP, output control, typing
---

# Agently 4.1.3.8 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.8.md) · **中文**

Agently 4.1.3.8 完成 AgentExecution-backed AgentTaskLoop 路径上的任务执行策略优化。
公开 owner 仍然是 `AgentExecution`；任务执行形态由 AgentExecution / AgentTaskLoop
策略层决定，TaskBoard 只是执行 substrate，ACP 则是既能被直接编排、也能被 recovery
policy 选择的能力。

这不是新的公开 AgentTask lifecycle，也不会把 task-shape analysis 变成硬路由。

## 推荐用法

默认任务执行模式是 `auto`。在 `auto` 中，AgentTaskLoop 会让模型先用自然语言分析任务形态，
再给出很薄的非绑定 execution hint；之后由策略层把有效执行形态解析为 `flat` 或
`taskboard`。用户显式选择优先：

```python
result = (
    agent
    .goal(
        "Prepare a migration risk report.",
        success_criteria=[
            "Cover compatibility, rollout, and rollback risks.",
            "Include evidence for each recommendation.",
        ],
    )
    .effort("medium")
    .strategy("taskboard")
    .output({
        "summary": (str, "short final summary", True),
        "risks": [(str, "one material risk")],
    })
    .get_result()
)

data = result.get_data()
meta = result.get_meta()
effective_shape = meta.get("effective_execution_strategy")
```

嵌套 `AgentExecution` 默认继承父执行的 strategy context，除非用户显式覆盖子执行。

## 核心变动

| 领域 | 变动内容 | 推荐用法 | 兼容性 / 风险 |
|---|---|---|---|
| 执行策略 | `execution_strategy` 默认是 `auto`；策略层解析出 `flat` 或 `taskboard`。`.strategy("flat" | "taskboard")` 重新具备明确执行形态含义。 | 简单任务保持 `auto`；host 明确知道形态时使用 `.strategy(...)`。 | task-shape analysis 只是 evidence 和 hint，不是硬路由。 |
| TaskBoard 路径 | TaskBoard 不再做复杂度分类；只有策略层选择它之后才执行，并保留 save/load/resume、handler diagnostics 和 card evidence contracts。 | 把 TaskBoard 当作分支或多视角任务的执行 substrate。 | 最终接受仍由 verifier 和 host guards 决定。 |
| effort 反思密度 | `effort("low" | "medium" | "high")` 映射 reflection density。low 保留 final reflection 和 planner 标记的重要过程点；medium 在大节点或 card/tick 边界反思；high 在每个可观测 Action、ACP call、card、bounded step 和 final point 反思。 | 选择能提供足够审计证据的最低 effort。 | reflection 进入 Workspace evidence、replan 和 verifier 输入，但不能单独算完成证据。 |
| ACP 能力 | ACP 是 Action 加 `ExecutionResource(kind="acp")`；可以被 planner/user 直接选择，也可以在 retry 耗尽后由 recovery 使用。 | 只有需要 ACP 时才调用 `.use_acp(...)`。 | ACP 不绕过 AgentExecution 或 AgentTaskLoop 策略。 |
| 可选依赖加载 | MCP 和 ACP 都使用 `utils.LazyImport`；没有显式 `.use_mcp(...)` 或 `.use_acp(...)` 时不会加载可选包。 | 普通 agent 保持轻依赖；在能力边界显式启用可选 runtime。 | 可选依赖缺失只在相关路径被使用时通过 LazyImport 诊断暴露。 |
| 强格式过程输出 | 强格式中间模型请求使用 Agently `.output(..., format=...)` 和恰当 parser。声明的非 JSON parser 失败时可切回 JSON，且只接受能解析成 dict 的值，并携带诊断。 | 过程契约使用 `.output(...)`，不要用关键词或本地 scorecard 替代语义判断。 | 兜底是解析恢复路径，不是语义捷径。 |
| 观测兼容 | AgentExecution 会把 flat 和 TaskBoard 过程 stream item 投影为 `agent_execution.stream` RuntimeEvent；task/TaskBoard/ACP/reflection payload 保持通用、fail-open。`model.status` 和 `model_request_telemetry` 仍是 observation-only。 | 使用 `agently-devtools >=0.1.10,<0.2.0`。 | DevTools 可以 ingest、store、query 和 replay AgentExecution、flat、TaskBoard 过程事件，但不拥有任务策略语义。 |
| 公开类型 | 包内新增 `agently/py.typed`，并为常用公开 facade 方法补齐类型。 | IDE 和 pyright-compatible 工具可以直接读取安装后的 Agently 类型。 | 少数宽类型内部表面仍作为兼容 escape hatch 保留。 |

## 兼容性

- Package version: `4.1.3.8`。
- Release manifest: `compatibility/releases/4.1.3.8.json`。
- 推荐 `agently-devtools`: `>=0.1.10,<0.2.0`。
- Development-line planning 仍保留在 `compatibility/in-development.json`，直到下一条 release line 移动。

## 延期范围

4.1.3.8 不完成 multi-task scheduling、background autonomous scheduling、
production distributed task recovery、production Redis/Postgres 或 object-storage
Workspace providers，也不完成 AgentTaskLoop 的 TriggerFlow-backed AdaptiveLoop /
BootstrapLoop packaging。
