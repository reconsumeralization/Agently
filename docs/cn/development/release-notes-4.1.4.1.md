---
title: Agently 4.1.4.1 Development Notes
description: Agently 4.1.4.1 关于 AgentExecutionResult 业务数据与完整数据 reader 兼容性的开发线说明。
keywords: Agently, development notes, 4.1.4.1, AgentExecutionResult, get_data, get_full_data
---

# Agently 4.1.4.1 Development Notes

> 语言：[English](../../en/development/release-notes-4.1.4.1.md) · **中文**

Agently 4.1.4.1 是 4.1.4 发布后的开发线。本页记录已经落地的 in-development
行为。

## AgentExecution Result 视图

`AgentExecutionResult.get_data()` 现在在 direct、flat、TaskBoard route 上都表示同一层
业务结果。direct model-request route 继续返回普通解析结果；task-strategy route
如果返回带 `final_result` 的终态 envelope，`get_data()` 会暴露这个
`final_result`，并在可能时按声明的 `output(...)` contract 解析。

当调用方需要完整 route/task payload 时，使用 `get_full_data()` /
`async_get_full_data()`，其中包含 `status`、`accepted`、`artifact_status`、
`taskboard`、`completion_notes`、diagnostics 等执行内部信息。`get_text()` /
`async_get_text()` 仍读取完整 payload，因此 task-strategy 的 `final_response`
依然是优先的面向用户最终文本。

这修复了之前 AgentTask-backed execution 可能让 `get_data()` 返回内部终态
envelope，而 direct execution 返回业务对象的不一致。

## 兼容性

- Package target: `4.1.4.1` development line。
- Release manifest: `compatibility/in-development.json`。
- 既有 task 终态 envelope 字段不变；依赖这些字段的调用方应从 `get_data()`
  切到 `get_full_data()`。
