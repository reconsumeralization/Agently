---
title: Coding Agents
description: 用 Agently 配合 Codex、Claude Code、Cursor 等 coding agent —— 官方 Agently Skills。
keywords: Agently, coding agents, Codex, Claude Code, Cursor, Skills
---

# Coding Agents

> 语言：[English](../../en/development/coding-agents.md) · **中文**

如果你借助外部 coding agent（Codex、Claude Code、Cursor 等）写 Agently 应用，给该 agent 提供良好 Agently 上下文的规范方式是 `Agently-Skills` 伴生仓中的**官方 Agently Skills** 包。

本文讲的是 **companion repo** 这条路径，不是框架内 runtime skill 消费。如果你要的是 Agently 自己在真实任务里安装并应用外部 skills，读 [Skills Executor](skills-executor.md)。

## 什么是 Agently Skills

skill 是一个包，含：

- `SKILL.md` 描述该 skill 做什么、何时应用
- references —— coding agent 按需拉的聚焦文档
- examples —— 最小可运行片段
- validators —— agent 可跑的脚本，确认用户项目遵循推荐结构

skill **不是**纯文档。它为 coding agent 结构化：每个 skill 告诉 agent 它解决什么问题、推荐路径长什么样、如何验证用户代码在该路径上。

## Companion skills 和框架内 skill 执行的区别

这两件事要分开：

- `Agently-Skills` companion repo：给外部 coding agent 用的 skill 包
- Agently `Skills Executor`：Agently 框架内部的 runtime 能力

伴生仓不会变成你的 Agently app 运行时依赖。它仍然只是给 coding agent 的指导包。

## 当前 skills

| Skill | 用户在做的事 |
|---|---|
| `agently` | 从零开始 —— 选合适的项目结构 |
| `agently-request` | 模型接入、Prompt 管理、结构化输出、响应复用、session memory、embedding、检索 |
| `agently-runtime` | Action Runtime、内置 actions、MCP、Execution Environment、FastAPI 暴露、DevTools 接入 |
| `agently-dynamic-task` | 模型生成或应用提交的 DAG 规划、校验和执行 |
| `agently-triggerflow` | 需要分支、并发、pause/resume、save/load |
| `agently-migration` | 从 LangChain、LangGraph、LlamaIndex、CrewAI 或类似系统迁移 |

当前公开 catalog generation 是 `v2`。实际默认 skill 列表见 `Agently-Skills/skills/`，应只包含这 6 个 skills。

## 安装

```bash
git clone https://github.com/AgentEra/Agently-Skills
```

按 coding agent 自身的 loader 指向 skill 目录：

- **Claude Code** —— `~/.claude/skills/` 或项目 `.claude/skills/`
- **Codex** —— 见 Codex 安装的 skill / context loader
- **Cursor** —— 经项目 rules / context surface 加载

skill 是纯文本 + 脚本；安装时不跑 Agently 特定的东西。

如果用 CLI 安装，默认 `app` bundle 是：

```bash
for skill in \
  agently \
  agently-request \
  agently-runtime \
  agently-dynamic-task \
  agently-triggerflow
do
  npx skills add AgentEra/Agently-Skills --agent "$AGENT" --skill "$skill" -y
done
```

只有迁移项目才额外安装 `agently-migration`。历史 catalog 通过冻结归档分支保留，
而不是放在默认分支文件树里；V1 12-skill catalog 归档在
`update/archive-legacy-v1-catalog`，最后支持 Agently `4.1.1`。新项目不要把归档
catalog 加入 coding agent 的常规搜索路径。

## 为什么是 skill 不是单纯文档

文档告诉人能做什么。skill 告诉 coding agent **当前**推荐什么 —— 包括哪些 API 已 deprecated、当前 lifecycle 是什么、报告"完成"前要验证什么。这让 coding agent 与框架演进对齐，不需要用户手动更新自己的 context。

特别地，skill **不得**推荐 deprecated 路径如 `.end()`、`set_result()`、`wait_for_result=`、旧 `runtime_data`。如果你发现某 skill 推荐其中之一，请向 `Agently-Skills` 提 issue。

应用开发中如果发现框架能力缺失、行为与 docs、examples、Skills 指导或预期的
模型应用责任边界不符、API 未暴露或使用不友好，或某项本应由 Agently 承担的
责任只能由业务代码通过 workaround、补丁、胶水、私有 wrapper 或重复机制弥补，
应生成简洁规范的 issue 说明。建议到
[`github.com/AgentEra/Agently`](https://github.com/AgentEra/Agently/issues)
提报，并包含业务场景、期望的框架责任、实际行为、当前 workaround、最小复现或
受影响 docs/examples，以及兼容性问题。
issue 必须把遭遇问题时的具体场景讲清楚，说明当时尝试解决的是哪一类模型应用
开发问题。涉密时可以脱敏或省略具体业务细节，但仍要描述应用类别、workflow
形态、决策点和期望由框架承担的责任，方便维护者理解问题。
人工提交时，只把 issue 内容和提交方式提供给使用者。自动提交前必须先询问用户；
如果用户确认自动提交，先检查本地 GitHub 提交权限和能力、本地复现问题仍存在，
并复核 Agently docs、examples、Skills 指导和 API 用法，确认不是遗漏信息或不当
使用造成的问题。创建远端 issue 前必须清理正文，确保不包含 secret、token、
客户数据或本机绝对路径。

新增框架 deprecation 时，必须通过 `agently.utils.DeprecationWarnings.warn_deprecated_once(...)` 或 `agently.utils.warn_deprecated_once(...)` alias 搭配稳定 API key 发 warning。不要直接新增 `warnings.warn(..., DeprecationWarning, ...)`；deprecated API warning 设计为每个 Python 进程内每个 API 只发一次，并遵守 `runtime.show_deprecation_warnings`。

## 4.1 之后的默认推荐

当你审计或编写面向 Agently `4.1+` 的指导时，coding agent 应默认偏向这些用法：

- API 形态：遵守奥卡姆剃刀原则。如无必要，勿增实体、方法、facade 或兼容补丁；已有表面能清晰承载语义时优先复用。若只是命名表意不清，优先建议窄别名或文档澄清，而不是再加一个容易重叠的方法。
- 结构化输出：固定必填叶子直接写在 `.output(...)` 的 `(TypeExpr, "description", True)` 里。只有空值必须触发重试时才用 `(TypeExpr, "description", "not_null")`。手动 `ensure_keys=` 只留给条件路径或运行时决定的路径。
- Actions：新代码从 `@agent.action_func` 和 `agent.use_actions(...)` 起步。`tool_func`、`use_tool`、`use_tools` 是兼容别名，不是首选推荐。
- TriggerFlow lifecycle：把 `close()` / `async_close()` 和 close snapshot 视为规范收尾路径。不要把 `.end()`、`set_result()`、`get_result()`、`wait_for_result=` 当正常起点。
- TriggerFlow state：单次 execution 的数据用 `get_state(...)` / `set_state(...)`。`flow_data` 是有意共享时才使用的风险作用域，不是普通状态存储。
- Settings 加载：provider 配置落文件时，优先 `Agently.load_settings("yaml_file", path, auto_load_env=True)`。`Agently.set_settings(...)` 留给内联覆盖。
- 执行风格：服务、流式、工作流默认 async-first。sync API 视为脚本、REPL 或兼容桥接层。
- Result 复用：一次模型调用如果要同时消费文本、结构化数据、metadata 或流式更新，优先 `get_result()` 复用同一个 result，而不是重复发请求。
- 任务执行质量：当一个目标驱动任务必须使用某个能力（Action、Skill、Skill pack 或 DynamicTask）时，不要靠 prompt 里的强指令或业务特例去强制或检查它。把要求表达为框架契约：让能力对规划器可见（`planner_capabilities`），用会传到 ActionRuntime 边界的结构化 `step_scope` 约束 action 步骤范围，并用结构化的 `capability_evidence_requirements` 声明完成证据要求。Skills 步骤如果可能产出很长的 artifact 文本，应配置 Skills route 的输出格式，而不是强行让大段原始内容走 JSON streaming。若 Skills 步骤需要文件写入/读回、shell、HTTP 或其它副作用，必须通过 route/effort 配置显式授权 action/tool 范围；当 React 策略应在这些副作用成功后停止时，声明 required side-effect actions，并为宿主 actions 声明 `action_succeeded` 证据。Skills 提供指导，ActionRuntime 负责可调用执行和证据。上一轮 Workspace context 必须优先保留 action evidence，而不是被大块执行元数据挤掉；`execution.step_plan="auto"` 在某个 DAG shape 失败后应避开同类 DAG 重试，只有明确需要 DAG 契约时才使用显式 `step_plan="dag"`。AgentTaskLoop 的 host guard 会拿这个要求与执行证据做确定性比对；prompt 只是说明，不是保障。场景特定的检查（视觉指纹、领域名称、来源选择）只留在 example 和测试里，绝不进入框架路径。

## 何时写自己的 skill

如果团队在 Agently 之上有内部模式（特定项目布局、包装的 agent factory、自定义 action 集），考虑作私有 skill 包，按公开 Agently Skills 格式。coding agent 会跨项目一致地应用团队约定。

## 验证脚本

数个 skill 携带验证脚本（如 `validate/validate_native_usage.py`）。coding agent 在宣布任务完成前可跑它们，确认用户项目遵循推荐路径。例如 TriggerFlow 验证器检查没有 deprecated API 作为推荐起点。

功能验收通过还要求完成 spec 对齐：把相关 spec 更新为最终实现方案，已完整落地的 planned spec 移入 `spec/implemented/`，并在同一工作项里更新 `spec/README.md`。

用户可见 feature work 必须为功能所对应的场景新增或更新 examples。example 应在声明环境中可运行，使用当前推荐 API，并通过输出、断言或注释把关键运行时行为展示出来。`Expected key output` 注释应保留一次实际运行中的稳定关键值，而不是只写“可以看到 X”一类泛化描述。当输出本身不足以解释行为时，可在 example 注释中补充简短工作原理或 ASCII 流程图。

对 Agently `4.1.3` 开发线，如果任务涉及默认 `agent.start()` 路由、`agent.create_execution()` 或 Agent 过程流式输出，需要纳入 `examples/agent_auto_orchestration/`。该目录中的本地 smoke 脚本只能作为基础设施检查；模型应用或验收结论仍必须来自真实 DeepSeek 或本地 Ollama 示例。对 4.1.2.5 基础能力线，把 `examples/cookbook/`、`examples/action_runtime/`、`examples/execution_resource/`、`examples/builtin_actions/`、`examples/trigger_flow/`、`examples/dynamic_task/` 和 `examples/fastapi/` 视为推荐起点；`examples/archived/` 只作为兼容参考。

汇报 API、推荐用法、examples 或兼容线变化时，应给出能直观看出新用法的简短样例代码。能用当前用法或 before/after 片段说明时，优先用代码片段而不是抽象描述。

## 另见

- [Action Runtime](../actions/action-runtime.md) —— skill 假设的 tool 使用架构
- [DevTools](../observability/devtools.md) —— 观测、评估和交互式 wrapper 路径
- [TriggerFlow 兼容](../triggerflow/compatibility.md) —— skill 引导的迁移路径
