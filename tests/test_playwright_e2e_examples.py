from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "action_runtime"


def load_agent_example():
    sys.path.insert(0, str(EXAMPLE_DIR))
    path = EXAMPLE_DIR / "2_4_mcp_playwright_agent_qwen.py"
    spec = importlib.util.spec_from_file_location("playwright_agent_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_playwright_target_projection_is_structural_and_fail_closed():
    module = load_agent_example()

    assert module.canonicalize_playwright_target("e6") == "e6"
    assert (
        module.canonicalize_playwright_target('textbox "New todo" [ref=e6]')
        == "e6"
    )
    with pytest.raises(ValueError, match="exactly one"):
        module.canonicalize_playwright_target("button without a ref")
    with pytest.raises(ValueError, match="exactly one"):
        module.canonicalize_playwright_target("[ref=e6] and [ref=e7]")


@pytest.mark.asyncio
async def test_playwright_execution_handler_preserves_model_choice_and_projects_ref():
    module = load_agent_example()
    calls: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []

    class FakeAction:
        async def async_execute_action(self, action_id, action_input, **kwargs):
            calls.append(
                {
                    "action_id": action_id,
                    "action_input": action_input,
                    "kwargs": kwargs,
                }
            )
            return {"action_id": action_id, "status": "success", "data": {}}

    handler = module.create_validating_execution_handler(trace)
    records = await handler(
        {"action": FakeAction(), "settings": object()},
        {
            "action_calls": [
                {
                    "action_id": "browser_type",
                    "action_input": {
                        "target": 'textbox "New todo" [ref=e6]',
                        "text": "Review Agently E2E",
                    },
                }
            ]
        },
    )

    assert records[0]["status"] == "success"
    assert calls[0]["action_id"] == "browser_type"
    assert calls[0]["action_input"] == {
        "target": "e6",
        "text": "Review Agently E2E",
    }
    assert trace[0]["model_input"]["target"].endswith("[ref=e6]")
    assert trace[0]["executed_input"]["target"] == "e6"


@pytest.mark.asyncio
async def test_playwright_execution_handler_rejects_ambiguous_ref_without_dispatch():
    module = load_agent_example()
    dispatch_count = 0

    class FakeAction:
        async def async_execute_action(self, action_id, action_input, **kwargs):
            nonlocal dispatch_count
            dispatch_count += 1
            return {"action_id": action_id, "status": "success"}

    handler = module.create_validating_execution_handler([])
    records = await handler(
        {"action": FakeAction(), "settings": object()},
        {
            "action_calls": [
                {
                    "action_id": "browser_click",
                    "action_input": {"target": "[ref=e6] and [ref=e7]"},
                }
            ]
        },
    )

    assert dispatch_count == 0
    assert records[0]["status"] == "error"
    assert "exactly one" in records[0]["error"]
