"""Declared schemas are not runtime env; all requests here stop before dispatch."""

from __future__ import annotations

from copy import deepcopy
import importlib
from typing import Any, Literal

import pytest

from agently import Agently
from agently.core.operation.Action.ActionMetadata import (
    project_action_spec_for_planning,
    sanitize_action_spec_for_metadata,
)


@pytest.mark.parametrize("field", ["kwargs", "returns"])
@pytest.mark.parametrize("schema", [
    {"env": (str, "Business environment name", True)},
    {"config": ({"env": (Literal["staging", "production"], "Target environment")}, "Config")},
    {"items": ([{"env": {"region": (str, "Business region")}}], "Deployment items")},
    {"env": ({"region": (str, "Region"), "tier": (int, "Tier")}, "Business configuration")},
])
def test_declared_schema_env_is_preserved(field: str, schema: dict[str, Any]) -> None:
    original = {"name": "configure", field: deepcopy(schema), "meta": {"env": {"TOKEN": "host-secret"}}}
    snapshot = deepcopy(original)
    projected = sanitize_action_spec_for_metadata(original)

    assert projected[field] == schema
    assert projected["meta"]["env"] == {"TOKEN": "[REDACTED]"}
    assert original == snapshot
    projected[field].clear()
    assert original == snapshot


@pytest.mark.parametrize("value, expected", [
    ({"TOKEN": "host-secret"}, {"TOKEN": "[REDACTED]"}),
    (["host-secret", "another-secret"], ["[REDACTED]", "[REDACTED]"]),
    (("host-secret",), "[REDACTED]"),
    ("host-secret", "[REDACTED]"),
    (None, None),
])
def test_runtime_env_still_redacts_at_nested_metadata_locations(value: Any, expected: Any) -> None:
    original = {
        "name": "configure",
        "kwargs": {"env": (str, "Business name")},
        "meta": {"env": value, "nested": [{"config": ({"env": value},)}]},
        "execution_resources": [{"config": {"runtime": {"env": value}}}],
    }
    snapshot = deepcopy(original)
    projected = sanitize_action_spec_for_metadata(original)

    assert projected["meta"]["env"] == expected
    assert projected["meta"]["nested"][0]["config"][0]["env"] == expected
    assert projected["execution_resources"][0]["config"]["runtime"]["env"] == expected
    assert "host-secret" not in str(projected)
    assert original == snapshot


def _register(action: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    kwargs = {"env": ({"region": (str, "Region to deploy")}, "Business target", True)}
    returns = {"env": {"region": (str, "Applied region")}}

    def forbidden(**_kwargs: Any) -> None:
        raise AssertionError("A metadata test must not execute its Action")

    action.register_action(
        action_id="schema_env_probe", desc="Describe business environment configuration.",
        kwargs=kwargs, returns=returns, func=forbidden,
        meta={"env": {"TOKEN": "host-secret"}},
    )
    return kwargs, returns


@pytest.mark.parametrize("legacy", [False, True])
def test_public_and_legacy_metadata_preserve_schema_and_registry(legacy: bool) -> None:
    agent = Agently.create_agent()
    if legacy:
        from agently.builtins.plugins.ToolManager.AgentlyToolManager import AgentlyToolManager

        with pytest.deprecated_call(match="AgentlyToolManager"):
            action = AgentlyToolManager(agent.settings)
    else:
        action = agent.action
    kwargs, returns = _register(action)
    raw = deepcopy(action.action_registry.get_spec("schema_env_probe"))

    public = action.get_action_info()["schema_env_probe"]
    assert public["kwargs"] == kwargs
    assert public["returns"] == returns
    assert public.get("required_input_keys", []) == ([] if legacy else ["env"])
    assert "host-secret" not in str(public)
    tool = action.get_tool_info()["schema_env_probe"]
    assert tool["kwargs"] == kwargs
    assert tool["returns"] == returns
    planning = project_action_spec_for_planning(public)
    assert planning["kwargs"] == kwargs
    assert planning.get("required_input_keys", []) == ([] if legacy else ["env"])
    assert "meta" not in planning
    public["kwargs"]["env"][0].clear()
    assert action.action_registry.get_spec("schema_env_probe") == raw


@pytest.mark.asyncio
async def test_default_planner_receives_real_declared_env_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agently.create_agent()
    kwargs, _ = _register(agent.action)
    runtime_module = importlib.import_module("agently.builtins.plugins.ActionRuntime.AgentlyActionRuntime")
    captured: list[dict[str, Any]] = []

    class BeforeDispatch(Exception):
        pass

    def inspect_request(request: Any, **_kwargs: Any) -> None:
        captured.append({"draft": request.prompt.get(), "text": request.prompt.to_text()})
        raise BeforeDispatch

    monkeypatch.setattr(runtime_module, "_get_model_request_result", inspect_request)
    prompt = Agently.create_prompt()
    prompt.set("input", "Describe the chosen environment.")
    with pytest.raises(BeforeDispatch):
        await agent.action.action_runtime._default_structured_planning_handler(
            {"prompt": prompt, "settings": agent.settings, "agent_name": agent.name},
            {"action_list": agent.action.get_action_list()},
        )

    assert len(captured) == 1
    sections = captured[0]["draft"]["info"]
    assert isinstance(sections, list)
    runtime_info = next(item["action_runtime"] for item in sections if "action_runtime" in item)
    offered = runtime_info["available_actions"]
    target = next(item for item in offered if item["action_id"] == "schema_env_probe")
    assert target["kwargs"] == kwargs
    assert "Region to deploy" in captured[0]["text"]
    assert "host-secret" not in captured[0]["text"]
