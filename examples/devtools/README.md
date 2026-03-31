# Agently DevTools Examples

These examples show the core DevTools integration paths from the main Agently repo.

Before running them:

```bash
pip install -U agently agently-devtools
```

Observation and evaluation examples also need the local DevTools listener:

```bash
agently-devtools start
```

Examples in this folder:

**ObservationBridge** (Passive monitoring):
- `01_observation_bridge_local.py`
  Uses `ObservationBridge()` local defaults to upload one simple TriggerFlow run.
- `02_observation_bridge_selective_watch.py`
  Uses `auto_watch=False` and `bridge.watch(...)` so only the selected flow is uploaded.
- `03_scenario_evaluations.py`
  Uses `EvaluationBridge` and `EvaluationRunner` to run a small repeatable suite.

**InteractiveWrapper** (Active interaction):
- `04_interactive_wrapper_basic.py`
  Uses `InteractiveWrapper()` with a generator callable to stream text chunks into the interactive chat UI.
- `05_interactive_wrapper_agent.py`
  Uses `InteractiveWrapper()` with an Agently Agent so token output can stream into the UI when the configured model supports it.
- `06_interactive_wrapper_trigger_flow.py`
  Uses `InteractiveWrapper()` with a TriggerFlow that emits stage updates before returning the final structured result.

Default local listeners:

**ObservationBridge & EvaluationBridge** (Devtools workbench):
- Console: `http://127.0.0.1:15596/`
- Ingest: `http://127.0.0.1:15596/observation/ingest`

**InteractiveWrapper** (Interactive demo UI):
- Default port: `15365` (similar to AGENT key positions)
- General usage: `python <example_file>`
- Access the interactive UI in your browser: `http://127.0.0.1:15365/`
- The built-in page uses `/api/stream` first and falls back to `/api/chat` only when the wrapped provider does not support streaming.

All examples avoid external model dependencies so they can be used as integration smoke tests.
