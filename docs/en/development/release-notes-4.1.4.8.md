---
title: Agently 4.1.4.8 Release Notes
description: Fluent AgentExecution typing, review and artifact delivery, Execution plugins, scoped Skills, Action runtime improvements, and release evidence.
keywords: Agently, 4.1.4.8, typing, IDE, AgentExecution, plugin, Action, Skill, Ollama
---

# Agently 4.1.4.8 Release Notes

This is an unpublished candidate. The implemented usage below does not mean all release acceptance gates have passed.

Agently 4.1.4.8 is an execution-composition and developer-experience release.
It makes one-run Agent code easier to read in an IDE while preserving the same
runtime owner: a fluent chain keeps returning one `AgentExecution`, and its
Actions, Skills, interaction handler, reviews and artifacts stay
request-local unless the caller explicitly opts into Agent defaults.

The release requires Python 3.10 or newer and `agently-stage >=0.3.8,<0.4.0`.
It recommends `agently-devtools >=0.1.11,<0.2.0` and Agently-Skills generation
V2 aligned to framework 4.1.4.8.

## Recommended Usage

This example uses an explicitly configured OpenAI-compatible service and demo account data. Connect the Action to your
account system in a real application.

```python
import os
from agently import Agently

Agently.set_settings("plugins.ModelRequester.OpenAICompatible", {
    "base_url": os.environ["MODEL_BASE_URL"],
    "auth": os.environ["MODEL_API_KEY"],
    "model": os.environ["MODEL_NAME"],
})
agent = Agently.create_agent("renewal-review")
agent.use_task_workspace("./workspace")


def load_account(account_id: str) -> dict[str, str]:
    """Demo data adapter; this does not query a real account system."""
    return {"account_id": account_id, "renewal_risk": "medium"}


execution = (
    agent
    .input({"account_id": "acct-42"})
    .info("Use observed account facts; label unknowns.")
    .use_action(load_account)
    .output({"recommendation": (str, "grounded next action", True)})
    .review()
    .artifact("reports/acct-42.json")
)

result = execution.get_result()
print(result.get_data())
print(execution.artifact_results)
```

Pylance and Pyright now retain `AgentExecution` through the Action and Skill
links in this chain. `always=True` remains the explicit Agent-default form:

```python
agent.use_actions(load_account, always=True)

one_run = agent.create_execution("plan").input("Draft the launch plan.")
```

Skills follow the same return-type rules. For registration and exact-revision
binding, see the [release-pinned Skill example](../../../examples/release_pinned_usage/03_skill_library_agent_binding.py).

The built-in values for `create_execution`, `effort`, `strategy`,
`planning_protocol`, and Action `concurrency_mode` are finite IDE suggestions.
Plugin-extensible Execution and alternate orchestrator strategy names remain open
where the public contract permits them.

## Core Changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Typing and IDE | Action and Skill fluent methods now distinguish one-run `AgentExecution` from `always=True` Agent defaults; public execution methods carry readable docstrings and finite choices. | Keep one-off `.input(...).info(...).use_action(...)` or `.use_skills(...)` in one chain. | Additive typing precision; unsupported finite values now fail static checking. | Full source Pyright, public Any allowlist, negative typing fixtures, installed-wheel smoke. |
| Result streaming | A reopened instant stream replays the accepted validation-retry attempt, and AgentExecution preserves rejected/accepted attempt order. | Treat final parsed data as authority; use `attempt_index` when presenting provisional updates. | Compatible correction of retry visibility. | `examples/release_pinned_usage/06_validate_retry_accepted_stream.py` and deterministic tests. |
| Model selection | Explicit unknown model aliases fail before provider dispatch; `resolve_model_profile` exposes a non-secret preflight view. | Validate configured model keys before starting application work. | Fail-closed for misspelled aliases when `model_pool` is configured. | Model configuration tests and `examples/model_configures/typed_settings_and_model_profiles.py`. |
| Action Runtime | `programmatic` planning can execute one bounded read-only Action micro-DAG; Actions declare `parallel` or `exclusive` concurrency. | Keep `structured_plan` as the default; opt into `programmatic` only with eligible Actions and an isolated code provider. | Explicit opt-in, policy-gated, no universal cost/latency promise. | Action runtime suites and `examples/action_runtime/4_4_programmatic_vs_structured_deepseek.py`. |
| Action delivery and debug | Terminal Action responses reuse the existing execution result; concurrent console streams display in first-delta FIFO order without serializing execution. | Use `debug=True` for readable output and EventCenter/DevTools for complete facts. | Display-only change; event and execution ordering remain authoritative. | Pinned examples 04 and 05 plus console/action tests. |
| Skills | Skill defaults and execution-local declarations freeze one exact-revision scope; scripts remain inert descriptors in `selected_resources`, not Actions or authorization grants. | Use `always=True` for Agent defaults and execution methods for one-run additions; mount execution capabilities explicitly. | Fail-closed scope; no implicit script actionization. The development-only nonempty script-candidate example is withdrawn; the compatibility facade retains the released `action_candidates: []` field. | Pinned examples 03 and 07, Skills tests, Agently-Skills V2 guidance. |
| Agent delivery policies | `interact`, `review(rules=..., on_fail=...)`, and verified TaskWorkspace `artifact` delivery are stable public methods. | Attach handlers to the execution that owns the result and artifact. | Additive; blocking review can prevent terminal success. | Examples 25 and 26 and AgentExecution handler/artifact tests. |
| Execution plugins | `create_execution(name)` returns the registered execution instance; built-ins are `auto`, `request`, `long_task`, `plan`, and `long_content`. | Choose the producer explicitly when needed; `.goal(..., turn_on_long_task=False)` declares semantic goals only. | Replaces unreleased Pattern; released Orchestrator/AgentTask entrypoints remain compatibility adapters. | Examples 26–28 and plugin identity, goal-preparation, final-policy and typing tests. |
| MCP | Playwright MCP examples cover local lifecycle and model-driven browser use. | Use ExecutionResource-owned MCP sessions and close them deterministically. | External runtime/browser dependency. | `examples/action_runtime/2_3_mcp_playwright_e2e_local.py` and `2_4_mcp_playwright_agent_qwen.py`. |
| Long content and continuation | `long_content` produces structured long prose; `LongContent` fields are generated separately and filled back into the structure; `auto_continue` only continues unfinished requests. | Declare `(LongContent, "writing requirements")`; enable `.auto_continue()` when needed. | The `"long_content"` type spelling and `.ensure_long_output()` alias remain compatible; continuation need not trigger. | `examples/basic/auto_continue.py`, `examples/agent_auto_orchestration/29_field_long_content_ollama.py`, continuation/output-control tests. |
| Execution controls | Safe-boundary pause/resume and save/load, plus same-object revision rework with retained earlier readers. | Use execution `pause/resume/save/load/rework` and async equivalents; inspect `control_capabilities` first. | Active provider/child snapshots and complete nested-budget recovery are not promised. | Unified control documentation, lifecycle/rework/snapshot tests, installed typing. |
| Audio | Independent `AudioModelRequest` provides TTS/STT with explicit Agent binding; four composed streams distinguish continuous PCM, independent speech segments, transcript blocks and textual sentence endings. | `Agently.create_audio_request(...)` → `agent.use_audio(audio)`; consume streams with `async with`. | No text Prompt reuse or implicit recording/playback; built-in native realtime STT input is not implemented. | [Audio usage](../models/audio.md), `examples/audio/tts_stt_roundtrip.py`, `examples/audio/continuous_audio.py`, audio tests. |
| Shell (unfinished scope) | Native process core and reverse Cmd delegation are implemented; three environment profiles, four approval presets and the new general Agent entry are not complete. | Existing Cmd/enable_shell retains argv semantics; do not treat it as a general Bash/PowerShell script interface. | **Pending, not a supported capability**; CrossOver probes do not replace native Windows isolation acceptance. | Shell/Cmd lifecycle tests; complete feature acceptance remains open. |

Long-form declarations and continuation settings are independent. Reuse the configured Agent above:

```python
from agently import LongContent

execution = agent.input("Write a chapter-organized operations manual.").output({
    "body": (LongContent, "Develop the requested content without inventing business constraints."),
}).auto_continue()
```

The built-in SQLite RecordStore and vector store now close connections when each
operation exits, preserving commit, rollback and error propagation. This fixes
connection leaks without changing public calls, read-only policy or lazy creation.

Built-in RecordStore context revisions now follow the visible scope: unrelated
writes outside that view no longer invalidate its Reader, while visible changes
still require refresh. Page and exact reads use one read-only transaction's
revision. ContextSource excludes out-of-scope records; public RecordStore reads
do not acquire new permission rules. Custom providers/read adapters retain their
existing path. Revision checks still scan metadata; cost is not independent of
record count.

Structured task-repair requirements and evidence identities now survive the
projection into subsequent planning, including saved/restored iteration summaries.
Requirements already lost from historical snapshots are not reconstructed.

## Examples Added For This Release

- `examples/agent_auto_orchestration/25_agent_execution_delivery_review_ollama.py`
  proves real local-Qwen generation, model-backed review, required blocking handler
  review, and physical artifact readback.
- `examples/agent_auto_orchestration/26_plan_execution_interaction_ollama.py`
  demonstrates connected clarification and a host-validated plan artifact.
- `examples/agent_auto_orchestration/27_long_content_execution_artifact_ollama.py`
  demonstrates section planning/writing, host-ordered assembly, artifact verification,
  and advisory review.
- `examples/agent_auto_orchestration/28_missing_goal_preparation_ollama.py`
  demonstrates conditional goal interpretation for an explicitly selected long task.
- Release-pinned examples 06 and 07 protect accepted retry streams and
  execution-scoped Skill composition without requiring a model service.

The Ollama examples default to `qwen`; set
`AGENT_EXECUTION_OLLAMA_MODEL` or `OLLAMA_DEFAULT_MODEL` to select another local
Qwen model.

Early refactor checkpoint (not current final acceptance): the 26/27 runs completed framework delivery but
semantic inspection found an invented attendance threshold and an expanded
deployment restriction, respectively. Example 28 prepared its missing contract
but timed out in later production. These are retained Prompt-audit findings,
not semantic release acceptance. Unified execution controls now cover settled outer
pause/resume/save/load, cancellation/close, supplementary information and same-object
rework revisions with retained readers and cumulative budgets. Active-child/provider
checkpoints, disconnected clarification and nested-budget restoration remain unsupported.
See [execution controls](../start/auto-orchestration.md#execution-controls).
This checkpoint does not establish release readiness.

## Compatibility And Release Gate

- Package version: `4.1.4.8`.
- Release manifest: `compatibility/releases/4.1.4.8.json`.
- Required runtime: `agently-stage >=0.3.8,<0.4.0` (published 0.3.8 verified).
- Optional observation companion: `agently-devtools >=0.1.11,<0.2.0` using
  `agently-devtools.observation-runtime.v1`.
- Agently-Skills: V2 catalog, aligned framework version `4.1.4.8`.

Local Ollama/Qwen runs are supplementary release evidence under the repository
policy. Before final release recommendation, the release PR must also record the
required online-model Foundation and pinned-example checks, or an explicit
maintainer waiver with residual risk. No online API quota is consumed merely by
building or testing this candidate.

Install after publication:

```bash
pip install -U "agently==4.1.4.8"
```
