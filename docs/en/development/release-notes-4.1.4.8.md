---
title: Agently 4.1.4.8 Release Notes
description: Fluent AgentExecution typing, review and artifact delivery, beta Patterns, scoped Skills, Action runtime improvements, and release evidence.
keywords: Agently, 4.1.4.8, typing, IDE, AgentExecution, Pattern, Action, Skill, Ollama
---

# Agently 4.1.4.8 Release Notes

Agently 4.1.4.8 is an execution-composition and developer-experience release.
It makes one-run Agent code easier to read in an IDE while preserving the same
runtime owner: a fluent chain keeps returning one `AgentExecution`, and its
Actions, Skills, interaction handler, reviews and artifacts stay
request-local unless the caller explicitly opts into Agent defaults.

The release requires Python 3.10 or newer and `agently-stage >=0.3.8,<0.4.0`.
It recommends `agently-devtools >=0.1.11,<0.2.0` and Agently-Skills generation
V2 aligned to framework 4.1.4.8.

## Recommended Usage

This example uses local Ollama and demo account data. Connect the Action to your
account system in a real application.

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key": "ollama-local",
    "model": "qwen3.5:9b",
    "model_type": "chat",
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

one_run = agent.input("Draft the launch plan.").pattern("plan")
```

Skills follow the same return-type rules. For registration and exact-revision
binding, see the [release-pinned Skill example](../../../examples/release_pinned_usage/03_skill_library_agent_binding.py).

The built-in values for `pattern`, `effort`, `strategy`,
`planning_protocol`, and Action `concurrency_mode` are finite IDE suggestions.
Plugin-extensible Pattern and alternate orchestrator strategy names remain open
where the public contract permits them.

## Core Changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Typing and IDE | Action and Skill fluent methods now distinguish one-run `AgentExecution` from `always=True` Agent defaults; public execution methods carry readable docstrings and finite choices. | Keep one-off `.input(...).info(...).use_action(...)` or `.use_skills(...)` in one chain. | Additive typing precision; unsupported finite values now fail static checking. | Full source Pyright, public Any allowlist, negative typing fixtures, installed-wheel smoke. |
| Result streaming | A reopened instant stream replays the accepted validation-retry attempt, and AgentExecution preserves rejected/accepted attempt order. | Treat final parsed data as authority; use `attempt_index` when presenting provisional updates. | Compatible correction of retry visibility. | `examples/release_pinned_usage/06_validate_retry_accepted_stream.py` and deterministic tests. |
| Model selection | Explicit unknown model aliases fail before provider dispatch; `resolve_model_profile` exposes a non-secret preflight view. | Validate configured model keys before starting application work. | Fail-closed for misspelled aliases when `model_pool` is configured. | Model configuration tests and `examples/model_configures/typed_settings_and_model_profiles.py`. |
| Action Runtime | `programmatic` planning can execute one bounded read-only Action micro-DAG; Actions declare `parallel` or `exclusive` concurrency. | Keep `structured_plan` as the default; opt into `programmatic` only with eligible Actions and an isolated code provider. | Explicit opt-in, policy-gated, no universal cost/latency promise. | Action runtime suites and `examples/action_runtime/4_4_programmatic_vs_structured_deepseek.py`. |
| Action delivery and debug | Terminal Action responses reuse the existing execution result; concurrent console streams display in first-delta FIFO order without serializing execution. | Use `debug=True` for readable output and EventCenter/DevTools for complete facts. | Display-only change; event and execution ordering remain authoritative. | Pinned examples 04 and 05 plus console/action tests. |
| Skills | Skill defaults and execution-local declarations freeze one exact-revision scope; script discovery returns inert candidates until explicit host authorization. | Use `always=True` for Agent defaults and execution methods for one-run additions. | Fail-closed scope; no implicit script actionization. | Pinned examples 03 and 07, Skills tests, Agently-Skills V2 guidance. |
| Agent delivery policies | `interact`, `review(rules=..., on_fail=...)`, and verified TaskWorkspace `artifact` delivery are stable public methods. | Attach handlers to the execution that owns the result and artifact. | Additive; blocking review can prevent terminal success. | Examples 25 and 26 and AgentExecution handler/artifact tests. |
| Patterns | Bundled `plan` and `long_content` whole-request Patterns are available through `.pattern(...)`. | Opt in per execution and keep the final business value on AgentExecution. | Beta; bundled multi-request Patterns reject incompatible delivery contracts before dispatch. | Examples 26 and 27 plus Pattern isolation and contract tests. |
| MCP | Playwright MCP examples cover local lifecycle and model-driven browser use. | Use ExecutionResource-owned MCP sessions and close them deterministically. | External runtime/browser dependency. | `examples/action_runtime/2_3_mcp_playwright_e2e_local.py` and `2_4_mcp_playwright_agent_qwen.py`. |

## Examples Added For This Release

- `examples/agent_auto_orchestration/25_agent_execution_delivery_review_ollama.py`
  proves real local-Qwen generation, model-backed review, required blocking handler
  review, and physical artifact readback.
- `examples/agent_auto_orchestration/26_plan_pattern_interaction_ollama.py`
  proves one connected clarification exchange and a validated plan artifact.
- `examples/agent_auto_orchestration/27_long_content_pattern_artifact_ollama.py`
  proves section planning/writing, host-ordered assembly, artifact verification,
  and advisory review.
- Release-pinned examples 06 and 07 protect accepted retry streams and
  execution-scoped Skill composition without requiring a model service.

The Ollama examples default to `qwen3.5:9b`; set
`AGENT_PATTERN_OLLAMA_MODEL` or `OLLAMA_DEFAULT_MODEL` to select another local
Qwen model.

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
