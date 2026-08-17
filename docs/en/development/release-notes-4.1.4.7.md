---
title: Agently 4.1.4.7 Release Notes
description: Automatic mixed sync/async Stage routing for TriggerFlow integrations.
keywords: Agently, 4.1.4.7, Agently-Stage, TriggerFlow, sync, async, carrier
---

# Agently 4.1.4.7 Release Notes

Agently 4.1.4.7 raises the minimum Stage dependency to
`agently-stage >=0.3.7,<0.4.0` and resolves the mixed sync/async runtime bug
exposed by [Agently #347](https://github.com/AgentEra/Agently/issues/347),
[Agently-Stage #24](https://github.com/AgentEra/Agently-Stage/issues/24), and
[Agently-Stage #25](https://github.com/AgentEra/Agently-Stage/issues/25).

The correction belongs primarily to Stage: synchronous and asynchronous scopes
now distinguish inherited logical execution lineage from the physical thread
and event loop that a call can safely block. Stage 0.3.7 also propagates every
upstream carrier in a transitive synchronous wait chain, preventing a nested
scope from selecting a loop that is indirectly waiting for it. This supersedes
the incomplete 0.3.6 routing fix. Agently adds end-to-end TriggerFlow
regression contracts and pins later installations to the complete correction.

The public PyPI baseline is 4.1.4.6. This release is a focused runtime
integration patch over that published version.

## Developer-visible changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|
| Sync TriggerFlow chunks | A provider-owned sync wrapper may use `with Stage()` for an async SDK and then call `data.set_state(...)`, `append_state(...)`, or `del_state(...)`. | Keep the provider's synchronous interface; use native async chunks only when the work must remain on the caller-owned loop. | No provider API rewrite or awareness of TriggerFlow's private Stage use is required. | `tests/test_cores/test_trigger_flow_execution_state.py`; `examples/trigger_flow/automatic_stage_sync_provider.py` |
| Stage dependency | The minimum compatible Stage is `0.3.7` within the existing `<0.4.0` line. | Install `agently==4.1.4.7` (or a later compatible release). | Resolves dependency selection for future installs; direct Stage users should not downgrade below 0.3.7. | `pyproject.toml`; `poetry.lock`; `tests/test_stage_support_contract.py` |
| `FunctionShifter.syncify/asyncify` | Deprecated names and warnings remain; scalar calls delegate to `Stage.as_sync/as_async`. | Keep existing imports and call forms, or migrate to `Stage.as_sync/as_async`. | Additive runtime correction; the deprecated façade remains supported. | `tests/test_utils/test_function_shifter.py` |
| Internal bridges | `default_stage_call_bridge` remains the owner where Agently requires lightweight bridging, injected lifecycle, streams, or explicit `managed=True` settlement. | Do not replace every bridge call with a scoped adapter. | No semantic change to these existing boundaries. | `agently/utils/FunctionShifter.py`; Stage support contracts |
| Workflow ownership | TriggerFlow still owns workflow state, lifecycle, persistence, concurrency, and errors. | Continue using TriggerFlow's public execution APIs. | Stage carrier details remain private and are not serialized. | `compatibility/releases/4.1.4.7.json` |

## Provider-owned synchronous interface

Tool and Function providers may keep a deliberate synchronous method even when
the underlying implementation is async:

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

Stage automatically chooses a physically safe carrier. The provider is not
responsible for discovering whether TriggerFlow already uses Stage internally.

This remains a synchronous boundary and blocks its calling worker. Native async
chunks should still use `await` and `data.async_set_state(...)`. Work that owns
an object bound to the caller's event loop must stay on that owner loop and be
awaited there.

## Stage surface

Agently does not re-export Stage. Applications that directly need Stage's
scoped adapters import it from `agently_stage`:

```python
from agently_stage import Stage

sync_search = Stage.as_sync(search_tool.search)
async_transform = Stage.as_async(transform)
```

Each adapter invocation owns one automatic Stage scope and waits for the result
and Stage-owned settlement. `StageCallBridge` remains the advanced API for
stream conversion, injected Stage/executor ownership, independent close, and
lightweight adaptation.

## Upgrade

No application source migration is required for the reported call chain.
Install the release normally:

```bash
pip install -U "agently==4.1.4.7"
```

An existing Agently installation that already permits the Stage 0.3
compatibility line may independently upgrade Stage to 0.3.7, but installing
Agently 4.1.4.7 is the supported way to carry the minimum dependency forward.
