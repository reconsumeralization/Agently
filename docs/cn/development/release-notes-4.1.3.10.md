---
title: Agently 4.1.3.10 Development Notes
description: Agently 4.1.3.10 关于 TaskBoard 增量验收、verifier cache 复用、scoped evidence 和进度 telemetry 的开发线说明。
keywords: Agently, development notes, 4.1.3.10, AgentTask, TaskBoard, acceptance index, verifier cache
---

# Agently 4.1.3.10 Development Notes

> 语言：[English](../../en/development/release-notes-4.1.3.10.md) · **中文**

Agently 4.1.3.10 是 4.1.3.9 之后的当前开发线。本页记录已经落地的
in-development 行为。

## TaskBoard 增量验收

TaskBoard 现在把 acceptance index 当作增量执行投影，而不只是恢复或展示用的
orientation snapshot。这个投影仍然是 `projection_only`：它不是
`EvidenceEnvelope` evidence，也不直接判定任务完成。

| 领域 | 变动内容 | 兼容性 / 风险 | 证据 |
|---|---|---|---|
| AgentExecution strategy selector | `AgentExecution.strategy(...)` 现在推荐四个值：`auto`、`direct`、`flat`、`taskboard`。`direct` 强制走普通 `model_request`/ActionLoop route，不创建 AgentTask；`flat` 和 `taskboard` 显式进入 AgentTask；`auto` 让普通 prompt/action run 保持 direct，只有结构化 task signals 才进入 AgentTask。 | 历史 `task`、`task_loop`、`long_task` 拼写只保留为 legacy compatibility，不应作为新代码推荐入口。 | `tests/test_agent_execution_step_contract.py`、`tests/test_compatibility_registry.py`。 |
| Acceptance dirty set | acceptance item 现在携带 dirty/cache 状态、关联 card/evidence id、verdict fingerprint、last verification ref，以及 dirty count、green count、cache hit/miss count、acceptance progress percent 等进度元数据。 | TaskBoard checkpoint/result metadata 是增量字段；已有消费者可以忽略未知字段。 | `tests/test_cores/test_task_board_contracts.py`。 |
| Verifier cache 复用 | TaskBoard final verification 在所有 acceptance fingerprint 未变化、required host guard 干净时，可以复用上一轮 green verifier verdict cache。dirty item 仍然走 verifier。 | 不新增 model-call、node-count 或 tool-call 硬上限；cache 复用由 evidence/artifact/card-result fingerprint 和 host fact 保护。 | `tests/test_agent_task_loop.py`。 |
| Scoped verifier evidence | dirty final verification 收到 scoped evidence projection，只包含 dirty acceptance item 的有界 snippet。完整 SHA、bytes 和 raw body 继续留在 EvidenceEnvelope 与 Workspace 冷记录。 | verifier prompt 变小，但不替代 canonical evidence ledger。 | TaskBoard contract 测试和 planned real-model experiments。 |
| 可恢复挫折 | control-card 的 readback、repair、patch 或 continuation 意图现在可以把当前 card 记录为 `setback`，表示可恢复的执行挫折，而不是硬 `blocked` 停止。即使旧 board revision status 是 `blocked`，frontier dispatch 仍会执行已经排程的 recovery card。 | 增量 card/projection 状态。UI 可以把 `setback` 展示为“遭遇挫折”；完成判定仍属于 verifier 和 host guard。 | `tests/test_cores/test_task_board_contracts.py`、`tests/test_agent_execution_step_contract.py`。 |
| Delta 状态投影 | 公开 `type="delta"` 流现在会把 Flat snapshot 投影成线性 plan/action 摘要，把 TaskBoard plan/tick 更新投影成可读状态输出。Flat 在 plan 完成时说明上一个已完成动作和当前行动规划；Flat 终态输出完成事项与结果摘要。TaskBoard 仍保留紧凑 board 表格，并在后续输出未开始、进行中、完成、失败、降级等 card 状态变化摘要。过程段落会和模型正文 delta 保持边界，避免 CLI 文本黏连。 | 这只是来自结构化 AgentTask event 的展示投影；不改变 `instant`/`all` 原始事件，也不改变证据权威或完成判定权威。丰富 UI 应消费 `instant`，把 synthetic `$delta` 和 source-addressed path 分开渲染。 | `tests/test_agent_task_loop.py`。 |
| Final response 与降级状态 | TaskBoard 终局 result 现在为 accepted、degraded、partial outcome 提供面向用户的 `final_response`。验收通过但降级交付使用 `artifact_status="degraded"`；有用但未验收的 artifact 仍保持 `artifact_status="partial"`。 | 增量 result 字段。`final_response` 和 `degraded` 是沟通/状态字段，不是完成证据。 | `tests/test_agent_execution_step_contract.py`。 |
| Action 关键性 | AgentTask 区分 step-local action requirement 和 task-contract required action。本步只读 action 只约束当前执行尝试；契约要求的 action 仍然进入硬 required-action guard。若制品已经通过 readback、grounding guard 和验收项，且失败只来自非关键 read-safe action 或 action-loop 诊断，任务可以带限制说明完成。 | 避免非关键来源失败触发重复 repair loop，同时不放松 required action、approval 等待、grounding guard、artifact readback 或显式成功标准。 | `tests/test_agent_task_loop.py`。 |
| Heartbeat stream hygiene | `agent_task.heartbeat` 仍然作为结构化 `instant` / log 状态保留，但不再投影进公开 `delta` 文本，也不再生成 synthetic `$delta` item。嵌套 heartbeat loop 会被节流，同一个 task 每个 heartbeat interval 最多发出一条 heartbeat。 | 减少可见过程文本冗余，同时保留结构化 liveness 诊断。heartbeat 仍然不重置 no-progress clock，也不满足证据或完成要求。 | `tests/test_agent_execution_step_contract.py`。 |
| Runtime guidance | task-strategy `AgentExecution` 现在提供 `async_add_guidance(...)` / `add_guidance(...)`，用于任务运行中追加非阻塞操作员上下文。AgentTask 会把 guidance 写入 Workspace `collection="guidance"`，通过 `guidance_items` / `guidance_refs` 暴露，并在下一个 Flat 或 TaskBoard 安全边界应用。 | AgentExecution 上的增量公开方法。guidance 不暂停执行，不改写非 task route prompt，也不是 EvidenceEnvelope 完成证据。 | `tests/test_agent_execution_step_contract.py`、`tests/test_agent_task_loop.py`。 |

## 兼容性

- Package target: `4.1.3.10` development line。
- Release manifest: `compatibility/in-development.json`。
- 推荐 `agently-devtools` 版本保持当前 manifest 约定，除非后续落地
  DevTools-specific 变更。
- 新 TaskBoard 字段是 additive runtime/checkpoint metadata。DevTools 和 stream
  消费者应把它们当作 observation/projection facts，而不是质量或完成判定 owner。
