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
| AgentExecution strategy selector | `AgentExecution.strategy(...)` now has four recommended values: `auto`, `direct`, `flat`, and `taskboard`. `direct` forces the ordinary `model_request`/ActionLoop route without creating AgentTask; `flat` and `taskboard` explicitly enter AgentTask; `auto` keeps prompt/action runs direct unless structural task signals enter AgentTask. | Historical `task`, `task_loop`, and `long_task` spellings remain legacy compatibility only and should not be promoted for new code. | `tests/test_agent_execution_step_contract.py`, `tests/test_compatibility_registry.py`. |
| Acceptance dirty set | Acceptance items now carry dirty/cache state, linked card/evidence ids, verdict fingerprints, last verification refs, and progress metadata such as dirty count, green count, cache hit/miss count, and acceptance progress percent. | Additive TaskBoard checkpoint/result metadata. Existing consumers can ignore unknown fields. | `tests/test_cores/test_task_board_contracts.py`. |
| Verifier cache reuse | Final TaskBoard verification can reuse a clean prior green verdict cache when every acceptance fingerprint is unchanged and required host guards are clean. Dirty items still go through verifier flow. | No hard model-call or node-count cap is introduced; cache reuse is guarded by evidence/artifact/card-result fingerprints and host facts. | `tests/test_agent_task_loop.py`. |
| Scoped verifier evidence | Dirty final verification receives a scoped evidence projection with bounded snippets for dirty acceptance items. Full SHA/bytes/raw bodies remain in EvidenceEnvelope and Workspace cold records. | Verifier prompts become smaller without replacing the canonical evidence ledger. | TaskBoard contract tests and planned real-model experiments. |
| Recoverable setbacks | Control-card readback, repair, patch, or continuation intent can now record the current card as `setback`, meaning a recoverable execution setback rather than a hard `blocked` stop. Frontier dispatch still runs scheduled recovery cards even if an older board revision status is `blocked`. | Additive card/projection status. UIs may render `setback` as "recoverable setback"; completion still depends on verifier and host guards. | `tests/test_cores/test_task_board_contracts.py`, `tests/test_agent_execution_step_contract.py`. |
| Delta status projection | Public `type="delta"` streams now project Flat snapshots as linear plan/action summaries and TaskBoard plan/tick updates as readable status output. Flat plan completion states the previous completed action and the current action plan; Flat terminal output includes a concise task summary of completed work and result. TaskBoard still renders a compact board table first, then summarizes card-state changes with not started, in progress, completed, failed, and degraded display states. Process paragraphs are separated from model body deltas to avoid glued CLI text. | Display-only projection from structured AgentTask events; it does not change `instant`/`all` raw events, evidence authority, or completion authority. Rich UIs should consume `instant` and render synthetic `$delta` separately from source-addressed paths. | `tests/test_agent_task_loop.py`. |
| Final response and degradation status | TaskBoard terminal results now include a user-facing `final_response` for accepted, degraded, and partial outcomes. Accepted degraded deliveries use `artifact_status="degraded"`; useful but unaccepted artifacts remain `artifact_status="partial"`. | Additive result fields. `final_response` and `degraded` are communication/status fields, not completion evidence. | `tests/test_agent_execution_step_contract.py`. |
| Action criticality | AgentTask distinguishes step-local action requirements from task-contract required actions. Step-local read actions scope the current execution attempt, while contract-required actions still use hard required-action guards. A completed, readback-verified artifact can be accepted when only non-critical read-safe actions or action-loop diagnostics failed and the final answer discloses the limitation. | Prevents non-critical source failures from driving repeated repair loops without relaxing required actions, approval waits, grounding guards, artifact readback, or explicit success criteria. | `tests/test_agent_task_loop.py`. |
| Heartbeat stream hygiene | `agent_task.heartbeat` remains available as structured `instant`/log status, but it no longer projects into public `delta` text or synthetic `$delta` items. Nested heartbeat loops are throttled so one task emits at most one heartbeat per heartbeat interval. | Reduces redundant visible process text while preserving structured liveness diagnostics. Heartbeats still do not reset the no-progress clock or satisfy evidence/completion requirements. | `tests/test_agent_execution_step_contract.py`. |
| Runtime guidance | Task-strategy `AgentExecution` now exposes `async_add_guidance(...)` / `add_guidance(...)` for non-blocking operator context while a task is active. AgentTask records guidance to Workspace `collection="guidance"`, surfaces `guidance_items` / `guidance_refs`, and applies it at the next safe Flat or TaskBoard boundary. | Additive public method on AgentExecution. Guidance does not pause execution, does not mutate non-task route prompts, and is not EvidenceEnvelope completion evidence. | `tests/test_agent_execution_step_contract.py`, `tests/test_agent_task_loop.py`. |

## Compatibility

- Package target: `4.1.3.10` development line.
- Release manifest: `compatibility/in-development.json`.
- Recommended `agently-devtools`: unchanged from the current manifest unless a
  later DevTools-specific change lands.
- The new TaskBoard fields are additive runtime/checkpoint metadata. DevTools
  and stream consumers should treat them as observation/projection facts, not
  quality or completion owners.
