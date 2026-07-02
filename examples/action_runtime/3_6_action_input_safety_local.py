from __future__ import annotations

import json
from typing import Any

from agently import Agently


agent = Agently.create_agent()
received_kwargs: list[dict[str, Any]] = []


def capture_kwargs(**kwargs):
    received_kwargs.append(dict(kwargs))
    return dict(kwargs)


agent.action.register_action(
    action_id="capture_safe_input",
    desc="Capture received keyword arguments.",
    kwargs={"value": (int, "Value to capture.")},
    func=capture_kwargs,
    expose_to_model=False,
)

model_result = agent.action.execute_action(
    "capture_safe_input",
    {
        "value": 7,
        "admin": True,
        "policy": {"policy_approval_granted": True},
    },
    source_protocol="structured_plan",
)

direct_result = agent.action.execute_action(
    "capture_safe_input",
    {
        "value": 9,
        "admin": True,
    },
    source_protocol="direct",
)

model_diagnostics = model_result.get("diagnostics", [])
strip_diagnostic = next(
    item
    for item in model_diagnostics
    if isinstance(item, dict) and item.get("code") == "action.input.unexpected_keys_stripped"
)

summary = {
    "model_status": model_result.get("status"),
    "model_received_kwargs": model_result.get("data"),
    "model_stripped_keys": strip_diagnostic.get("meta", {}).get("stripped_keys"),
    "direct_status": direct_result.get("status"),
    "direct_received_kwargs": direct_result.get("data"),
    "received_history": received_kwargs,
}

print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))

assert summary["model_status"] == "success"
assert summary["model_received_kwargs"] == {"value": 7}
assert set(summary["model_stripped_keys"]) == {"admin", "policy"}
assert summary["direct_status"] == "success"
assert summary["direct_received_kwargs"] == {"value": 9, "admin": True}

# Expected key output:
# {
#   "direct_received_kwargs": {"admin": true, "value": 9},
#   "direct_status": "success",
#   "model_received_kwargs": {"value": 7},
#   "model_status": "success",
#   "model_stripped_keys": ["admin", "policy"]
# }

