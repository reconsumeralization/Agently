# Agently DevTools Examples

These examples show the core DevTools integration paths from the main Agently repo.

Before running them:

```bash
pip install -U agently agently-devtools
agently-devtools start
```

Examples in this folder:

- `01_observation_bridge_local.py`
  Uses `ObservationBridge()` local defaults to upload one simple TriggerFlow run.
- `02_observation_bridge_selective_watch.py`
  Uses `auto_watch=False` and `bridge.watch(...)` so only the selected flow is uploaded.
- `03_scenario_evaluations.py`
  Uses `EvaluationBridge` and `EvaluationRunner` to run a small repeatable suite.

Default local listener:

- Console: `http://127.0.0.1:15596/`
- Ingest: `http://127.0.0.1:15596/observation/ingest`

All examples avoid external model dependencies so they can be used as integration smoke tests.
