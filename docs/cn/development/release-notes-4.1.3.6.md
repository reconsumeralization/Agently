---
title: Agently 4.1.3.6 Release Notes
description: Agently 4.1.3.6 的 AgentExecution ownership、Result-first 消费、OpenAI-compatible 流结束加固和 bounded task-loop slice release note。
keywords: Agently, release notes, 4.1.3.6, AgentExecution, AgentExecutionResult, AgentTaskLoop, ModelResponseResult
---

# Agently 4.1.3.6 Release Notes

> 语言：[English](../../en/development/release-notes-4.1.3.6.md) · **中文**

Agently 4.1.3.6 是 AgentExecution ownership release。它把推荐公开表面收束到
`AgentExecution` 和 `AgentExecutionResult`，加固 OpenAI-compatible 流结束路径，
并发布一个有明确边界的 single-task task-loop slice，而不是把它包装成完整未来版
AgentTask 系统。

## 变更内容

### AgentExecution 是单次 run 的 owner

Quick prompt 链现在和显式 execution 使用同一套公开 ownership 模型：

```python
execution = (
    agent
    .input("Classify this customer request.")
    .output({"category": (str, "billing, support, or sales", True)})
)

result = execution.get_result()
data = result.get_data()
meta = result.get_meta()
```

`AgentTurn`、`create_turn(...)` 和 `set_turn_prompt(...)` 仍作为旧 4.1.3 示例
和迁移路径的兼容表面保留，但不再是推荐公开生命周期。request-local prompt draft、
route choice、stream、metadata 和 result facade 现在属于 `AgentExecution`。

### 推荐 Result-first 消费

当同一次 run 可能被读取为 text、data、metadata、stream event 或 task refs 时，
使用 `get_result()`。Agent quick prompt 链返回 `AgentExecutionResult`；
直接 `ModelRequest` builder 返回 `ModelResponseResult`。

Result-named stream aliases 是现在推荐的 root-level 名称。旧的 response-named
aliases 保留在 `agently.types.data` 里作为兼容表面，并计划在 Agently 4.2 移除。

### Bounded task loop 由 AgentExecution 承载

`agent.create_task(...)` 和 `agent.create_task_loop(...)` 返回 task-strategy
`AgentExecution` draft。当前 slice 刻意保持窄边界：

- 一个 business task
- 一个 Agent owner
- bounded iteration 指引，大致 2-5 轮
- 显式启用的 Actions、Skills 或 Dynamic Task candidates
- model-owned planning、verification 和 replan
- 对 missing criteria、risky action evidence、approval-required actions 和
  final deliverables 的保守 host guards

```python
execution = agent.create_task(
    "Prepare a customer-safe incident update from the provided evidence.",
    success_criteria=[
        "Names the customer impact",
        "Separates confirmed facts from unknowns",
        "Lists the next customer-facing action",
    ],
    max_iterations=3,
)

result = execution.get_result()
data = result.get_data()
task_refs = result.task_refs
meta = result.get_meta()
```

`completed` 表示 model verification 接受结果，且 host guards 接受 artifact。
达到 `max_iterations` 仍可能返回 `accepted=false` 和 `artifact_status=partial`。
`AgentExecutionResult.resume()` 是预留表面，在 resumable strategy 落地前返回
`supported=false`。

### OpenAI-compatible stream completion 加固

Issue #287 已在 4.1.3.6 线修复。部分 OpenAI-compatible gateway 会在 `[DONE]`
前发送缺失或空 `choices` 的 usage-only final SSE chunk。
`OpenAICompatible.broadcast_response(...)` 现在会保留已累积内容并合成 terminal
message，不再对空列表取下标。

同一条 stream-end 路径现在把 `GeneratorExit` 视为 control flow，而不是 model
requester error，避免一次成功流式响应结束后额外出现空消息的
`model.requester.error`。

### Release guardrails 新增 foundation examples

Release workflow 现在要求对 release 涉及或声称的 substrate 能力执行 Foundation
example effect gate，例如 ModelRequest/ModelResponse、TriggerFlow、Dynamic
Task/TaskDAG、ActionRuntime、ExecutionEnvironment 和 provider protocols。仅有测试
不够：必须用 release candidate 运行对应核心 `examples/` 场景；涉及 model-owned 行为时，
要使用真实 DeepSeek 或本地 Ollama。

## 兼容性

- Package version: `4.1.3.6`。
- Release manifest: `compatibility/releases/4.1.3.6.json`。
- 推荐 `agently-devtools`: `>=0.1.7,<0.2.0`。
- `AgentTurn`、`create_turn(...)`、`set_turn_prompt(...)` 和
  `set_request_prompt(...)` 保留为兼容迁移表面。
- DevTools 显示问题 #288 和 #289 属于 DevTools-side 工作，不由本次 Agently
  主包 release 关闭。

## 验证摘要

- 静态类型和测试覆盖 AgentExecution result facade、task-loop result refs、
  stream/meta access、OpenAI-compatible usage-only final chunk，以及 compatibility
  registry 对齐。
- 本次触及的 ModelRequest/ModelResponse substrate 已通过 Foundation example
  effect gate。命令
  `python examples/step_by_step/05-response_result.py` 使用 DeepSeek，输出
  `result_type=ModelResponseResult`、`data_has_definition=True`、
  `meta_has_id=True`、`result_cached=True` 和
  `delta_event_count_positive=True`。
- 4.1.3.6 的 AgentExecution use-case example
  `examples/agent_auto_orchestration/22_unified_agent_execution_result.py` 已使用
  DeepSeek 跑通，输出 `quick_result_type=AgentExecutionResult`、
  `quick_category=renewal_risk`、`task_strategy=task_loop` 和
  `task_result_status=completed`。
- Agently-Skills 指引已同步同一条 foundation example gate，并通过 companion
  validation suite。

## 延期范围

这不是完整未来版 AgentTask 系统。Multi-task scheduling、distributed leases、
background autonomy、完整 durable pause/resume，以及 Workspace 自动记忆规划仍然延期。
4.1.3.7 线应继续加固由 AgentExecution 承载的 task-loop strategy，而不是替换本版
确立的公开 ownership 边界。
