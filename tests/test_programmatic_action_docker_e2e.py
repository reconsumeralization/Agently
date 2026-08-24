from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
)
from agently.core.operation.Action.ActionProgram import build_programmatic_action_catalog
from agently.types.data import PROGRAMMATIC_ACTION_TRANSPORT_ID


@pytest.mark.asyncio
async def test_programmatic_action_full_docker_binding_path(tmp_path: Path) -> None:
    probe = DockerExecutionResource()
    availability = probe.inspect_availability()
    if not availability.get("available"):
        pytest.skip("local Docker daemon is unavailable")
    image = "python:3.12-slim"
    if not probe.inspect_image(image).get("exists"):
        pytest.skip(f"required local Docker image is unavailable: {image}")

    agent = Agently.create_agent()
    agent.set_settings("code_execution.providers", ["docker"])
    agent.set_settings("task_workspace.root", str(tmp_path / "workspace"))
    agent.set_settings("task_workspace.mode", "read_write")
    agent.set_action_loop(planning_protocol="programmatic", max_rounds=2)
    observed: list[str] = []

    def lookup_record(record_id: str) -> dict[str, Any]:
        observed.append(record_id)
        return {"record_id": record_id, "score": 7}

    tag = f"agent-{agent.name}"
    agent.action.register_action(
        action_id="lookup_record",
        desc="Look up one deterministic test record.",
        kwargs={"record_id": (str, "Record id")},
        func=lookup_record,
        returns={
            "record_id": (str, "Record id"),
            "score": (int, "Record score"),
        },
        tags=[tag],
        side_effect_level="read",
        replay_safe=True,
        expose_to_model=True,
    )
    action_list = agent.action.get_action_list(tags=[tag])
    catalog = build_programmatic_action_catalog(action_list)
    agent.action.action_runtime._retain_programmatic_catalog(dict(catalog))
    agent.action._ensure_programmatic_action_transport(settings=agent.settings)

    async def plan_handler(context, request):
        _ = request
        if not context.get("done_plans"):
            return {
                "next_action": "execute",
                "use_action": True,
                "action_calls": [
                    {
                        "purpose": "lookup record in isolated program",
                        "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
                        "action_input": {
                            "program": (
                                "record = await actions.lookup_record({'record_id': 'r1'})\n"
                                "print('selected', record['record_id'])\n"
                                "return {'selected_id': record['record_id'], 'score': record['score']}"
                            ),
                            "description": "lookup record",
                            "catalog_revision": catalog["catalog_revision"],
                        },
                        "source_protocol": "programmatic",
                        "todo_suggestion": "respond",
                    }
                ],
            }
        return {"next_action": "response", "action_calls": []}

    records = await agent.action.async_plan_and_execute(
        prompt=agent.input("Look up r1").request.prompt,
        settings=agent.settings,
        action_list=action_list,
        agent_name=agent.name,
        planning_handler=plan_handler,
        max_rounds=2,
        planning_protocol="programmatic",
        timeout=20,
    )

    assert observed == ["r1"], json.dumps(records, ensure_ascii=False, default=str)
    assert len(records) == 1
    assert records[0].get("action_id") == PROGRAMMATIC_ACTION_TRANSPORT_ID
    assert records[0].get("status") == "success"
    assert catalog["catalog_revision"] not in agent.action.action_runtime._programmatic_catalogs


@pytest.mark.asyncio
async def test_programmatic_action_direct_generated_call_uses_docker_binding(
    tmp_path: Path,
) -> None:
    probe = DockerExecutionResource()
    availability = probe.inspect_availability()
    if not availability.get("available"):
        pytest.skip("local Docker daemon is unavailable")
    image = "python:3.12-slim"
    if not probe.inspect_image(image).get("exists"):
        pytest.skip(f"required local Docker image is unavailable: {image}")

    agent = Agently.create_agent()
    agent.set_settings("code_execution.providers", ["docker"])
    agent.set_settings("task_workspace.root", str(tmp_path / "workspace"))
    agent.set_settings("task_workspace.mode", "read_write")
    observed: list[str] = []

    def lookup_record(record_id: str) -> dict[str, Any]:
        observed.append(record_id)
        return {"record_id": record_id, "score": 7}

    agent.action.register_action(
        action_id="lookup_record",
        desc="Look up one deterministic test record.",
        kwargs={"record_id": (str, "Record id")},
        func=lookup_record,
        returns={
            "record_id": (str, "Record id"),
            "score": (int, "Record score"),
        },
        tags=[f"agent-{agent.name}"],
        side_effect_level="read",
        replay_safe=True,
        expose_to_model=True,
    )
    action_list = agent.action.get_action_list(tags=[f"agent-{agent.name}"])
    catalog = build_programmatic_action_catalog(action_list)
    agent.action.action_runtime._retain_programmatic_catalog(dict(catalog))
    agent.action._ensure_programmatic_action_transport(settings=agent.settings)

    record = await agent.action.async_execute_action(
        PROGRAMMATIC_ACTION_TRANSPORT_ID,
        {
            "program": (
                "record = await actions.lookup_record({'record_id': 'r1'})\n"
                "return {'selected_id': record['record_id'], 'score': record['score']}"
            ),
            "description": "lookup record",
            "catalog_revision": catalog["catalog_revision"],
        },
        settings=agent.settings,
        purpose="lookup record in isolated program",
        source_protocol="programmatic",
    )

    assert observed == ["r1"], json.dumps(record, ensure_ascii=False, default=str)
    assert record.get("status") == "success"
    assert catalog["catalog_revision"] not in agent.action.action_runtime._programmatic_catalogs
