---
title: Agently 4.1.3.4 Release Notes
description: Agently 4.1.3.4 的结构化输出解析修复、请求重试、流错误传播、运行时能力策略和 AgentTaskLoop first public slice release note。
keywords: Agently, release notes, 4.1.3.4, structured output, AgentTaskLoop, PolicyApproval, Skills Executor
---

# Agently 4.1.3.4 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.4.md) · **中文**

Agently 4.1.3.4 是一版 release-line hardening slice。主发布理由是提升本地和
云端 OpenAI-compatible 模型上的结构化输出可靠性；同时包含运行时能力策略加固，
以及面向 4.1.4 AgentTask 目标的 bounded AgentTaskLoop 第一版公开 slice。

## 结构化输出加固

`.output(..., format="auto")` 现在只根据 schema 结构选择格式。它不会检查字段名、
业务语义、分词、关键词或模型输出。

- flat string-only dict schema 选择 `xml_field`；
- string 字段和 typed non-string 字段混合的 schema 选择 `hybrid`；
- all-complex、all-control、non-dict 和 dense machine contract 保持 `json`；
- `flat_markdown` 保留为显式兼容模式，不再作为 auto/default 路径；
- `yaml_literal` 需要显式 opt-in，默认不进入 auto。

新增和修订的 parser 格式：

- `xml_field`：XML-like field envelope，使用自定义边界 parser，不是严格 XML parser；
- `hybrid`：文本字段用 Markdown section，typed 字段用 fenced JSON value；
- `yaml_literal`：在显式 Agently boundary 内输出 YAML，长文本使用 literal scalar；
- `flat_markdown`：保留给显式 legacy 用法，不再作为推荐默认路径。

Reasoning 归一在各格式 parser 之前完成。provider-native reasoning 字段和 payload
之前完整的外层 `<think>...</think>` 会进入既有 reasoning events；payload、代码块和
普通文本内部的 `<think>` 内容会保留。

## 请求与流式可靠性

`OpenAICompatible` 现在会在输出开始前重试瞬时传输错误。默认重放一次
（`request_retry.max_attempts = 2`），且不改变模型、prompt 或输出格式。一旦输出已经
开始，Agently 不会自动重放，避免重复 partial content。

response materialization 现在会通过 `get_text()`、`get_data()`、`get_meta()` 传播明确
的 stream/provider 构造错误，而不是继续等待 materialization timeout。

## 运行时能力策略

Skills capability execution 现在使用框架统一的 `PolicyApproval` 表面。Skill
capability needs 会记录在 `SkillExecutionPlan` 中，host policy 可以配置 auto-load
能力面，高风险能力仍然走 approval 或 fail-closed。

内置 Search 在前序 backend 失败但 fallback provider 恢复时返回 `partial_success`。
`partial_success` 仍然是可继续使用的证据，不等同于 Action failure。

## AgentTaskLoop 第一版公开 Slice

`agent.create_task(...)` 提供一个 bounded single-Agent task loop：规划一个 step、
通过 `AgentExecution` 执行、写入 Workspace 证据、验证、必要时 replan，最后以
completed、blocked 或超过限制后的 partial 结束。

这明确只是 first public slice，不是完整的 4.1.4 AgentTask 目标。它不提供多任务协同、
后台自治、分布式租约或长期记忆管理。适用场景是 host 控制 workspace、limits 和已启用
capabilities 的 bounded single-agent workflow。

## 兼容性说明

- Package version: `4.1.3.4`。
- Release manifest: `compatibility/releases/4.1.3.4.json`。
- Agently 推荐 `agently-devtools >=0.1.6,<0.2.0`。
- Agently-Skills 使用 authoring protocol `agently-skills.authoring.v2` 和标准
  `SKILL.md` package。
- 下一批 development-line manifest 是 `compatibility/in-development.json`，目标版本
  为 `4.1.3.5`。
