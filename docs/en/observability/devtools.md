---
title: DevTools
description: ObservationBridge, EvaluationBridge, and InteractiveWrapper usage from agently-devtools.
keywords: Agently, DevTools, ObservationBridge, EvaluationBridge, InteractiveWrapper
---

# DevTools

> Languages: **English** · [中文](../../cn/observability/devtools.md)

`agently-devtools` is an optional companion package. It consumes Agently observation events and provides local observation, evaluation, and interactive UI workflows. It is not the source of truth for workflow structure; TriggerFlow definitions and executions remain the source of truth.

## Install and listener

```bash
pip install -U agently agently-devtools
agently-devtools start
```

Default local endpoints from [`examples/devtools/README.md`](../../../examples/devtools/README.md):

| Surface | Default |
|---|---|
| DevTools console | `http://127.0.0.1:15596/` |
| Observation ingest | `http://127.0.0.1:15596/observation/ingest` |
| Interactive wrapper UI | `http://127.0.0.1:15365/` |

## ObservationBridge

The example path is [`examples/devtools/01_observation_bridge_local.py`](../../../examples/devtools/01_observation_bridge_local.py). It creates the bridge through Agently's LazyImport helper, watches the global runtime, runs a TriggerFlow, then unregisters the bridge:

```python
from agently import Agently

bridge = Agently.create_observation_bridge(
    app_id="agently-main-examples",
    group_id="devtools-local-demo",
)
bridge.watch(Agently)

try:
    ...
finally:
    bridge.unregister()
```

Use `Agently.create_observation_bridge(target, ...)` or `bridge.watch(target)` when only selected objects should be uploaded; see [`02_observation_bridge_selective_watch.py`](../../../examples/devtools/02_observation_bridge_selective_watch.py). `bridge.watch(...)` accepts the global `Agently` object, agents, model requests/responses, TriggerFlows, TriggerFlow executions, Dynamic Task / TaskDAG selectors, Skill executions, tool functions, or `{"target_type": ..., "target_name": ...}` mappings.

For a minimal example that keeps `agently_devtools` behind Agently's LazyImport facade, see [`07_agently_observe_lazy_bridge.py`](../../../examples/devtools/07_agently_observe_lazy_bridge.py).

The lower-level DevTools package can also bind at construction time:

```python
from agently import Agently
from agently_devtools import ObservationBridge

bridge = ObservationBridge(Agently)
bridge.watch(agent)
```

The older `bridge = ObservationBridge(...); bridge.register(Agently)` form remains compatible and emits a deprecation warning.

`ObservationBridge` uploads from a background queue and coalesces high-frequency observation events such as `model.streaming` before sending them to the listener. This keeps passive observation off the request/output path. For short scripts that exit immediately after a run, call `await bridge.flush()` before process exit if you need all buffered events uploaded.

## EvaluationBridge

[`03_scenario_evaluations.py`](../../../examples/devtools/03_scenario_evaluations.py) builds a small TriggerFlow, binds it with `EvaluationBinding`, and runs `EvaluationRunner` across multiple `EvaluationCase` inputs. Use this path for repeatable scenario checks, not for request-time validation inside application code.

## InteractiveWrapper

`InteractiveWrapper` can wrap:

- a plain callable or generator: [`04_interactive_wrapper_basic.py`](../../../examples/devtools/04_interactive_wrapper_basic.py)
- an Agently Agent: [`05_interactive_wrapper_agent.py`](../../../examples/devtools/05_interactive_wrapper_agent.py)
- a TriggerFlow that streams stage updates: [`06_interactive_wrapper_trigger_flow.py`](../../../examples/devtools/06_interactive_wrapper_trigger_flow.py)

For TriggerFlow, stream progress through `data.async_put_into_stream(...)`; the wrapper consumes the runtime stream and then shows the close snapshot.

## AgentTask action observations

AgentTask may project `agent_task.action.started`, `agent_task.action.completed`, and `agent_task.action.failed` RuntimeEvent records from bounded execution Action logs. DevTools should consume them as factual action observations in the AgentTask timeline, grouped by task iteration when iteration metadata is present.

These records are for display, logging, and post-run analysis only. Do not use them as route decisions, verifier results, quality scores, semantic relevance judgments, or task-completion acceptance. Unknown fields should be ignored, and large payloads should stay summarized or ref-backed.

## Compatibility boundary

DevTools consumers should fail open:

- ignore unknown observation event types and unknown payload fields
- prefer `run` lineage fields over parsing `message`
- treat TriggerFlow graphs as derived from flow definitions and runtime metadata, not as a separate manual graph

For release management, package versions are only part of the story. Unreleased branch work uses a runtime protocol declared by Agently in `agently/compatibility.py`; DevTools should compare that protocol first, then use package version ranges as upgrade guidance. Historical published manifests live under the Agently source registry at `compatibility/releases/<framework_version>.json`. For legacy published releases `4.1.0.2` through `4.1.1`, DevTools may fall back to the built-in legacy protocol mapping because those builds predate the manifest. Outside that legacy window, missing manifest support should be treated as unverified compatibility.

See [Event Center](event-center.md) for the observation event schema and alias rules.
