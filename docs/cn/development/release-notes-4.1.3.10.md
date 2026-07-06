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
| Acceptance dirty set | acceptance item 现在携带 dirty/cache 状态、关联 card/evidence id、verdict fingerprint、last verification ref，以及 dirty count、green count、cache hit/miss count、acceptance progress percent 等进度元数据。 | TaskBoard checkpoint/result metadata 是增量字段；已有消费者可以忽略未知字段。 | `tests/test_cores/test_task_board_contracts.py`。 |
| Verifier cache 复用 | TaskBoard final verification 在所有 acceptance fingerprint 未变化、required host guard 干净时，可以复用上一轮 green verifier verdict cache。dirty item 仍然走 verifier。 | 不新增 model-call、node-count 或 tool-call 硬上限；cache 复用由 evidence/artifact/card-result fingerprint 和 host fact 保护。 | `tests/test_agent_task_loop.py`。 |
| Scoped verifier evidence | dirty final verification 收到 scoped evidence projection，只包含 dirty acceptance item 的有界 snippet。完整 SHA、bytes 和 raw body 继续留在 EvidenceEnvelope 与 Workspace 冷记录。 | verifier prompt 变小，但不替代 canonical evidence ledger。 | TaskBoard contract 测试和 planned real-model experiments。 |
| 可恢复挫折 | control-card 的 readback、repair、patch 或 continuation 意图现在可以把当前 card 记录为 `setback`，表示可恢复的执行挫折，而不是硬 `blocked` 停止。即使旧 board revision status 是 `blocked`，frontier dispatch 仍会执行已经排程的 recovery card。 | 增量 card/projection 状态。UI 可以把 `setback` 展示为“遭遇挫折”；完成判定仍属于 verifier 和 host guard。 | `tests/test_cores/test_task_board_contracts.py`、`tests/test_agent_execution_step_contract.py`。 |
| Delta 状态表 | 公开 `type="delta"` 流现在会把 TaskBoard plan/tick 更新投影成紧凑 Markdown 状态表。行状态固定为未开始、进行中、完成、失败、降级五类。 | 这只是来自结构化 TaskBoard event 的展示投影；不改变 `instant`/`all` 原始事件，也不改变完成判定权威。 | `tests/test_agent_task_loop.py`。 |
| Final response 与降级状态 | TaskBoard 终局 result 现在为 accepted、degraded、partial outcome 提供面向用户的 `final_response`。验收通过但降级交付使用 `artifact_status="degraded"`；有用但未验收的 artifact 仍保持 `artifact_status="partial"`。 | 增量 result 字段。`final_response` 和 `degraded` 是沟通/状态字段，不是完成证据。 | `tests/test_agent_execution_step_contract.py`。 |

## 兼容性

- Package target: `4.1.3.10` development line。
- Release manifest: `compatibility/in-development.json`。
- 推荐 `agently-devtools` 版本保持当前 manifest 约定，除非后续落地
  DevTools-specific 变更。
- 新 TaskBoard 字段是 additive runtime/checkpoint metadata。DevTools 和 stream
  消费者应把它们当作 observation/projection facts，而不是质量或完成判定 owner。
