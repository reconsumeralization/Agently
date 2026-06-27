---
title: Agently 4.1.3.8 Release Notes
description: Agently 4.1.3.8 release notes for task execution strategy optimization, TaskBoard strategy selection, ACP fallback capability, output-control fallback, observation compatibility, and public typing metadata.
keywords: Agently, release notes, 4.1.3.8, AgentExecution, AgentTaskLoop, TaskBoard, ACP, output control, typing
---

# Agently 4.1.3.8 Release Notes

> Languages: **English** · [中文](../../cn/development/release-notes-4.1.3.8.md)

Agently 4.1.3.8 finalizes the task-execution strategy optimization work on the
AgentExecution-backed AgentTaskLoop path. The public owner remains
`AgentExecution`; task strategy selection is decided by the AgentExecution /
AgentTaskLoop policy layer, while TaskBoard stays an execution substrate and ACP
stays a capability that can be planned directly or selected by recovery policy.

This release does not introduce a separate public AgentTask lifecycle and does
not turn task-shape analysis into a hard router.

## Recommended Shape

The default task execution mode is `auto`. In `auto`, AgentTaskLoop asks the
model for natural-language task-shape analysis plus a thin non-binding
execution hint, then policy resolves the effective execution shape to `flat` or
`taskboard`. Explicit user selection wins:

```python
result = (
    agent
    .goal(
        "Prepare a migration risk report.",
        success_criteria=[
            "Cover compatibility, rollout, and rollback risks.",
            "Include evidence for each recommendation.",
        ],
    )
    .effort("medium")
    .strategy("taskboard")
    .output({
        "summary": (str, "short final summary", True),
        "risks": [(str, "one material risk")],
    })
    .get_result()
)

data = result.get_data()
meta = result.get_meta()
effective_shape = meta.get("effective_execution_strategy")
```

Nested `AgentExecution` instances inherit the parent strategy context unless a
user explicitly overrides the child execution.

## Core Changes

| Area | What changed | Recommended usage | Compatibility / risk |
|---|---|---|---|
| Execution strategy | `execution_strategy` defaults to `auto`; policy resolves `flat` or `taskboard`. `.strategy("flat" | "taskboard")` is active again for explicit execution-shape selection. | Leave simple tasks on `auto`; use `.strategy(...)` when the host knows the desired shape. | Task-shape analysis is evidence and hint only; it is not a hard routing decision. |
| TaskBoard path | TaskBoard no longer classifies complexity. It only runs after strategy policy selects it and preserves save/load/resume, handler diagnostics, and card evidence contracts. | Treat TaskBoard as a substrate for branched or multi-perspective work after policy selection. | Verifier and host guards still own final acceptance. |
| Effort reflection density | `effort("low" | "medium" | "high")` now maps to reflection density. Low keeps final reflection plus planner-marked important points; medium reflects at major nodes or card/tick boundaries; high reflects at each framework-observable action, ACP call, card, bounded step, and final point. | Pick the lowest effort level that gives enough audit evidence for the task. | Reflection records feed Workspace evidence, replan, and verifier input, but do not count as completion evidence by themselves. |
| Workspace artifact readback | AgentTask-delivered artifacts are verifier-visible only after trusted Workspace readback with `path`, `bytes`, `sha256`, bounded preview, and `file_refs`; `capability_evidence.artifacts.readback` records the trusted refs. | Use `artifact_markdown` or `artifact_manifest.sections` for deliverables and let Workspace produce the evidence chain. | Write-success/readback-failure cases report `agent_task.workspace_artifact.readback_failed` or `agent_task.workspace_artifact.readback_insufficient`, not generic budget or iteration failure. |
| TaskBoard cold readback | TaskBoard readback cards can inspect Action artifact refs and trusted Workspace file refs through bounded cold readbacks. Framework-generated readback cards scope evidence to direct dependencies plus upstream evidence cards, and continuation cards no longer recursively synthesize another readback chain for the same unresolved evidence gap. | Use readback cards for scoped cold evidence inspection; if the evidence is still insufficient, propose different executable work instead of another identical readback. | This preserves no-default-hard-cap behavior while preventing readback-only loops. |
| ACP capability | ACP is an Action plus `ExecutionResource(kind="acp")`. It can be selected directly by planner/user intent or by recovery after retry exhaustion. | Call `.use_acp(...)` only when ACP should be available; otherwise no ACP dependency is loaded. `acp_list_agents` includes common adapter-name hints such as `codex`, `claude code` / `cc`, `openclaw`, `hermes` / `hermes agent`, and `gemini`. | ACP does not bypass AgentExecution or AgentTaskLoop policy, and adapter hints are not runnable-agent evidence. |
| Optional dependency loading | MCP and ACP use `utils.LazyImport` and do not load optional packages unless `.use_mcp(...)` or `.use_acp(...)` is explicitly used. | Keep ordinary agents dependency-light; enable optional runtimes at the capability boundary. | Missing optional dependencies fail through LazyImport diagnostics only when the optional path is used. |
| Strong-format process output | Intermediate strong-format model requests use Agently `.output(..., format=...)` with the appropriate parser. If a declared non-JSON parser fails, the framework can fall back to JSON and accepts it only when the parsed value is dict-shaped with diagnostics. | Use `.output(...)` for process contracts, not keyword or local scorecard parsing. | Fallback is a parse recovery path, not a semantic shortcut. |
| Delta text stream | `get_async_generator(type="delta")` remains the public text-increment stream. Complex AgentTask / AgentExecution runs project template progress, snapshots, heartbeat status, phase status, retry markers, and terminal task result into paragraph text while `instant` keeps the structured payloads. | Use `delta` for user-facing streaming text and `instant` for structured UI state, diagnostics, or DevTools-style replay. | Existing text increments still stream as strings; process-event projection is additive. |
| Observation compatibility | AgentExecution projects flat and TaskBoard process stream items to `agent_execution.stream` RuntimeEvents; task/TaskBoard/ACP/reflection payloads stay generic and fail-open. `model.status` and `model_request_telemetry` remain observation-only. | Use `agently-devtools >=0.1.10,<0.2.0`. | DevTools can ingest, store, query, and replay AgentExecution, flat, and TaskBoard process events without owning task strategy semantics. |
| Public typing | The package now ships `agently/py.typed` and adds typing to common public facade methods. | IDEs and pyright-compatible tooling can inspect installed Agently types directly. | Remaining broad internal surfaces stay compatibility escape hatches. |

## Compatibility

- Package version: `4.1.3.8`.
- Release manifest: `compatibility/releases/4.1.3.8.json`.
- Recommended `agently-devtools`: `>=0.1.10,<0.2.0`.
- Development-line planning remains in `compatibility/in-development.json` until
  the next release line moves on.

## Deferred Scope

4.1.3.8 does not complete multi-task scheduling, background autonomous
scheduling, production distributed task recovery, production Redis/Postgres or
object-storage Workspace providers, or TriggerFlow-backed AdaptiveLoop /
BootstrapLoop packaging for AgentTaskLoop.
