---
title: Agently 4.1.4.7 Release Notes
description: Stage 0.3.8 integration, provider-neutral isolated code execution, reasoning observations, and validation diagnostics.
keywords: Agently, 4.1.4.7, Agently-Stage, TriggerFlow, gVisor, Seatbelt, Landlock, reasoning, validation
---

# Agently 4.1.4.7 Release Notes

Agently 4.1.4.7 is a runtime integration and diagnostics release over the
public PyPI 4.1.4.6 baseline. It:

- requires `agently-stage >=0.3.8,<0.4.0` for safe mixed sync/async routing
  and Python 3.14 task-factory compatibility;
- registers built-in gVisor, macOS Seatbelt, and Linux Landlock candidates
  behind the existing provider-neutral `code_execution` selection contract;
- preserves provider-supplied reasoning as result and RuntimeEvent facts,
  including explicit reasoning-token usage when the provider reports it;
- makes ModelRequest validation failures and retry transitions readable in
  simple and detailed console diagnostics.

The isolation provider classes are built into the package and registered
inactive. Their external mechanisms are probed only when explicitly selected.
They add no gVisor, Seatbelt, or Landlock Python dependency.

## Core changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Sync TriggerFlow chunks | A provider-owned sync wrapper may use `with Stage()` for an async SDK and then re-enter `data.set_state(...)`, `append_state(...)`, or `del_state(...)`. Stage 0.3.8 retains the 0.3.7 transitive-wait routing fix and forwards Python 3.14 task-factory keyword arguments. | Keep deliberate provider sync interfaces; use native async chunks when work must remain on a caller-owned loop. | No provider API rewrite or awareness of TriggerFlow's private Stage use is required. | TriggerFlow execution-state tests; Stage support contracts; `examples/trigger_flow/automatic_stage_sync_provider.py` |
| Stage governance | Stage is the required-runtime companion and Agently now requires `>=0.3.8,<0.4.0`. | Install Agently normally; import Stage directly from `agently_stage` only when the application itself needs Stage adapters. | Stage types and carrier state remain private to the mechanism layer and are not serialized or re-exported by Agently. | Published Stage 0.3.8 artifact; dependency lock; compatibility manifest |
| Provider-neutral code execution | Built-in provider ids `gvisor`, `seatbelt`, and `landlock` participate in the existing ordered provider-candidate protocol. Provider modules are registered inactive; external binaries/kernel capabilities are probed when selected. | Select candidates through `enable_code_runtime(..., providers=[...], isolation=...)`. | No new third-party Python dependency. Every explicit provider fails closed without falling back to Docker or `trusted_local`. Capabilities are observed axes, not inferred from names. | Provider-neutral selection tests and provider-specific conformance/integration tests |
| gVisor | The `gvisor` candidate verifies Docker and an active `runsc` runtime before executing. | Use `providers=["gvisor"]` with `isolation="required"` when the host is configured for runsc. | Missing, malformed, or unusable runsc configuration is terminal; no runc/host fallback occurs. | gVisor provider and integration tests; Ubuntu workflow |
| macOS Seatbelt | The `seatbelt` candidate derives writable rules only from the TaskWorkspace grant and denies network access by default. | Use `providers=["seatbelt"]` with `isolation="preferred"` on macOS. | The initial profile permits broad host reads and reports `host_filesystem_restricted=false`; use Docker/gVisor when host-read isolation is required. | Seatbelt bug and integration tests; bilingual execution-resource docs |
| Linux Landlock | The `landlock` candidate applies `PR_SET_NO_NEW_PRIVS` plus ABI-aware filesystem rules through a provider-owned helper. | Use `providers=["landlock"]` with `isolation="preferred"` on a supported Linux kernel. | Landlock restricts filesystem access but not processes, networks, or general syscalls; unsupported kernels fail closed. | Landlock isolation/integration tests; Ubuntu workflow |
| Reasoning observations | `ModelRequestResult.get_data(type="all")` retains `reasoning_delta` and nullable `reasoning`. Event Center publishes `model.reasoning.delta` and `model.reasoning.completed`. Explicit provider `reasoning_tokens` remains a separate usage detail. | Treat these facts as observation/audit data and keep them out of prompts, routing, retry, and quality decisions. | Missing reasoning stays null/unknown. Agently never infers hidden chain-of-thought or estimates reasoning tokens from text. | Model-request observation and structured-output tests; DevTools companion tests |
| Validation diagnostics | Simple model logs show validator, reason, attempt, and retry transition. Detail logs may add bounded validation context and traceback tails without repeating the response or reason. | Use `debug=True` for concise diagnostics or `debug="detail"` for bounded deep inspection. | Logging remains observational; deterministic validation and retry contracts remain authoritative. | Runtime console/event tests and settings docs |
| Workflow ownership | TriggerFlow still owns workflow state, lifecycle, persistence, concurrency, and errors. | Continue using TriggerFlow's public execution APIs. | Stage carrier details remain process-local and are not serialized. | Compatibility manifest and TriggerFlow lifecycle tests |

## Provider-owned synchronous interface

Tool and Function providers may retain a deliberate synchronous method even
when their SDK is async:

```python
from agently_stage import Stage


def search(query: str):
    with Stage() as stage:
        return stage.get(search_tool.search, query)


def chunk(data):
    result = search(data.input)
    data.set_state("search_result", result, emit=False)
    return result
```

This remains a blocking sync boundary. Native async chunks should still use
`await` and `data.async_set_state(...)`.

Agently does not re-export Stage. Direct scalar adapters use:

```python
from agently_stage import Stage

sync_search = Stage.as_sync(search_tool.search)
async_transform = Stage.as_async(transform)
```

`StageCallBridge` remains the advanced surface for stream conversion, injected
Stage/executor ownership, independent close, and explicitly lightweight
adaptation.

## Provider-neutral isolation selection

Use the existing generic API rather than provider-specific Agent methods:

```python
agent.enable_code_runtime(
    language="python",
    providers=["gvisor"],
    isolation="required",
)
```

For partial host mechanisms, select `seatbelt` on macOS or `landlock` on Linux
with `isolation="preferred"`. The selected handle and Action result report
observed toolchain, safety, isolation-axis, and fallback facts.

## Reasoning and validation diagnostics

Reasoning-capable results can be consumed without mixing reasoning into the
answer parser:

```python
result = agent.input("Explain the decision.").get_result()
all_data = result.get_data(type="all")
print(all_data.get("reasoning"))
```

Enable bounded validation diagnostics when investigating a failed output
contract:

```python
agent.set_settings("debug", "detail")
```

DevTools `0.1.11` adds the matching reasoning view, explicit reasoning-token
aggregation, and bounded run-partitioned observation ingest pipeline while
keeping `agently-devtools.observation-runtime.v1`.

## Upgrade

Install the framework and the matching optional DevTools release:

```bash
pip install -U "agently==4.1.4.7"
pip install -U "agently-devtools==0.1.11"
```

- Python: `>=3.10`
- Agently-Stage: `>=0.3.8,<0.4.0`
- Recommended Agently DevTools: `>=0.1.11,<0.2.0`
- Skills authoring protocol: `agently-skills.authoring.v2`
- DevTools observation protocol: `agently-devtools.observation-runtime.v1`

No application source migration is required for the Stage call chain.
Applications choosing a new isolation provider must satisfy its documented
host prerequisites and must not treat a provider name as proof of stronger
isolation than the reported capability axes.
