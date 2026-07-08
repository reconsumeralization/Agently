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

## AgentExecution Facade 与生命周期

Agent quick-prompt 链，例如 `agent.input(...).output(...).start()`，会为当前表达式
创建新的 `AgentExecution`，循环里不会再复用已完成执行的旧结果。

显式拿到的 `AgentExecution` 仍然只表示一次独立执行。它开始之后，再调用
`input(...)` 或 `output(...)` 等 prompt/config mutator 会抛出生命周期错误，而不是从
已完成 run record 静默创建第二次执行。下一轮请求应通过 `agent.input(...)`、
`agent.create_execution(...)` 或 `execution.create_execution(...)` 创建新的 execution。

execution facade 现在补齐早期基础示例依赖的 reader：
`get_data_object()`、`get_key_result()`、`wait_keys(...)` 以及
`when_key(...).start_waiter()`、`streaming_print()`。
`get_generator(type="specific")` 与 `ModelRequestResult` 保持一致，返回
`(event, data)` 元组；`get_generator(type="instant")` 保留结构化
`full_data` 快照；public delta stream 不再打印 provider 原始
`original_delta` chunk；`execution.get_prompt_text()` 在执行前后都可用于
prompt inspection。

## Public Typing Gate

`compatibility/public-typing-allowlist.json` 记录当前公开表面里有意保留的
`Any` 兼容边界。release gate 会自动扫描列出的 public surface，因此新增公开方法默认必须完整标注
参数和返回类型；如果确实需要 `Any`，必须在同一个 release 中加入带 owner、reason、
narrowing plan 和 expiry 的 allowlist 记录。

## 兼容性

- Package target: `4.1.4.1` development line。
- Release manifest: `compatibility/in-development.json`。
- 既有 task 终态 envelope 字段不变；依赖这些字段的调用方应从 `get_data()`
  切到 `get_full_data()`。
- 已完成 execution 上的 prompt/config 链式调用现在 fail fast；新的服务代码与示例都应按请求创建新的
  execution。
- public typing allowlist 是例外记录，不是允许公开方法清单。
