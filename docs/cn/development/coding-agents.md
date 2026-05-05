---
title: Coding Agents
description: 用 Agently 配合 Codex、Claude Code、Cursor 等 coding agent —— 官方 Agently Skills。
keywords: Agently, coding agents, Codex, Claude Code, Cursor, Skills
---

# Coding Agents

> 语言：[English](../../en/development/coding-agents.md) · **中文**

如果你借助外部 coding agent（Codex、Claude Code、Cursor 等）写 Agently 应用，给该 agent 提供良好 Agently 上下文的规范方式是 `Agently-Skills` 伴生仓中的**官方 Agently Skills** 包。

## 什么是 Agently Skills

skill 是一个包，含：

- `SKILL.md` 描述该 skill 做什么、何时应用
- references —— coding agent 按需拉的聚焦文档
- examples —— 最小可运行片段
- validators —— agent 可跑的脚本，确认用户项目遵循推荐结构

skill **不是**纯文档。它为 coding agent 结构化：每个 skill 告诉 agent 它解决什么问题、推荐路径长什么样、如何验证用户代码在该路径上。

## 可用 skill（代表）

| Skill | 用户在做的事 |
|---|---|
| `agently-playbook` | 从零开始 —— 选合适的项目结构 |
| `agently-model-setup` | 接模型端点、环境变量、设置文件 |
| `agently-prompt-management` | 塑形请求的指令或模板 |
| `agently-output-control` | 锁结构化字段、`ensure_keys`、校验 |
| `agently-model-response` | 复用单次响应、流式输出 |
| `agently-session-memory` | 加多轮连续性 / memo |
| `agently-agent-extensions` | 加 tool 使用、MCP、FastAPI 暴露 |
| `agently-triggerflow` | 需要分支、并发、pause/resume、save/load |
| `agently-knowledge-base` | embedding + 检索回答 |
| `agently-langchain-to-agently` | 从 LangChain agent 迁移 |
| `agently-langgraph-to-triggerflow` | 从 LangGraph 编排迁移 |
| `agently-migration-playbook` | 决定先用哪个迁移 skill |

实际 skill 列表见 `Agently-Skills/skills/`。上表是快照。

## 安装

```bash
git clone https://github.com/AgentEra/Agently-Skills
```

按 coding agent 自身的 loader 指向 skill 目录：

- **Claude Code** —— `~/.claude/skills/` 或项目 `.claude/skills/`
- **Codex** —— 见 Codex 安装的 skill / context loader
- **Cursor** —— 经项目 rules / context surface 加载

skill 是纯文本 + 脚本；安装时不跑 Agently 特定的东西。

## 为什么是 skill 不是单纯文档

文档告诉人能做什么。skill 告诉 coding agent **当前**推荐什么 —— 包括哪些 API 已 deprecated、当前 lifecycle 是什么、报告"完成"前要验证什么。这让 coding agent 与框架演进对齐，不需要用户手动更新自己的 context。

特别地，skill **不得**推荐 deprecated 路径如 `.end()`、`set_result()`、`wait_for_result=`、旧 `runtime_data`。如果你发现某 skill 推荐其中之一，请向 `Agently-Skills` 提 issue。

## 何时写自己的 skill

如果团队在 Agently 之上有内部模式（特定项目布局、包装的 agent factory、自定义 action 集），考虑作私有 skill 包，按公开 Agently Skills 格式。coding agent 会跨项目一致地应用团队约定。

## 验证脚本

数个 skill 携带验证脚本（如 `validate/validate_native_usage.py`）。coding agent 在宣布任务完成前可跑它们，确认用户项目遵循推荐路径。例如 TriggerFlow 验证器检查没有 deprecated API 作为推荐起点。

## 另见

- [Action Runtime](../actions/action-runtime.md) —— skill 假设的 tool 使用架构
- [DevTools](../observability/devtools.md) —— 观测、评估和交互式 wrapper 路径
- [TriggerFlow 兼容](../triggerflow/compatibility.md) —— skill 引导的迁移路径
