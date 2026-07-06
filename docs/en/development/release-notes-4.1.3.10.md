---
title: Agently 4.1.3.10 Development Notes
description: Agently 4.1.3.10 development notes for TaskBoard incremental acceptance, verifier cache reuse, scoped evidence, and progress telemetry.
keywords: Agently, development notes, 4.1.3.10, AgentTask, TaskBoard, acceptance index, verifier cache
---

# Agently 4.1.3.10 Development Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.10.md)

Agently 4.1.3.10 is the current development line after 4.1.3.9. This page
records accepted in-development behavior as it lands.

## TaskBoard Incremental Acceptance

TaskBoard now treats the acceptance index as an incremental execution projection
instead of only an orientation snapshot. The projection remains
`projection_only`: it is not `EvidenceEnvelope` evidence and does not decide
completion by itself.

| Area | What changed | Compatibility / risk | Evidence |
|---|---|---|---|
| Acceptance dirty set | Acceptance items now carry dirty/cache state, linked card/evidence ids, verdict fingerprints, last verification refs, and progress metadata such as dirty count, green count, cache hit/miss count, and acceptance progress percent. | Additive TaskBoard checkpoint/result metadata. Existing consumers can ignore unknown fields. | `tests/test_cores/test_task_board_contracts.py`. |
| Verifier cache reuse | Final TaskBoard verification can reuse a clean prior green verdict cache when every acceptance fingerprint is unchanged and required host guards are clean. Dirty items still go through verifier flow. | No hard model-call or node-count cap is introduced; cache reuse is guarded by evidence/artifact/card-result fingerprints and host facts. | `tests/test_agent_task_loop.py`. |
| Scoped verifier evidence | Dirty final verification receives a scoped evidence projection with bounded snippets for dirty acceptance items. Full SHA/bytes/raw bodies remain in EvidenceEnvelope and Workspace cold records. | Verifier prompts become smaller without replacing the canonical evidence ledger. | TaskBoard contract tests and planned real-model experiments. |
| Recoverable setbacks | Control-card readback, repair, patch, or continuation intent can now record the current card as `setback`, meaning a recoverable execution setback rather than a hard `blocked` stop. Frontier dispatch still runs scheduled recovery cards even if an older board revision status is `blocked`. | Additive card/projection status. UIs may render `setback` as "recoverable setback"; completion still depends on verifier and host guards. | `tests/test_cores/test_task_board_contracts.py`, `tests/test_agent_execution_step_contract.py`. |
| Delta status tables | Public `type="delta"` streams now project TaskBoard plan/tick updates into compact Markdown status tables. Rows use five display states: not started, in progress, completed, failed, and degraded. | Display-only projection from structured TaskBoard events; it does not change `instant`/`all` raw events or completion authority. | `tests/test_agent_task_loop.py`. |
| Final response and degradation status | TaskBoard terminal results now include a user-facing `final_response` for accepted, degraded, and partial outcomes. Accepted degraded deliveries use `artifact_status="degraded"`; useful but unaccepted artifacts remain `artifact_status="partial"`. | Additive result fields. `final_response` and `degraded` are communication/status fields, not completion evidence. | `tests/test_agent_execution_step_contract.py`. |

## Compatibility

- Package target: `4.1.3.10` development line.
- Release manifest: `compatibility/in-development.json`.
- Recommended `agently-devtools`: unchanged from the current manifest unless a
  later DevTools-specific change lands.
- The new TaskBoard fields are additive runtime/checkpoint metadata. DevTools
  and stream consumers should treat them as observation/projection facts, not
  quality or completion owners.
