from __future__ import annotations

from typing import Any

import pytest

from agently import Agently


def _terminal_payload(final_result: Any, *, strategy: str = "flat") -> dict[str, Any]:
    return {
        "task_id": f"{strategy}-result-view",
        "status": "completed",
        "accepted": True,
        "artifact_status": "accepted",
        "execution_strategy": strategy,
        "effective_execution_strategy": strategy,
        "iterations": 1,
        "final_result": final_result,
        "final_response": f"{strategy} final response",
    }


@pytest.mark.asyncio
async def test_direct_result_get_data_and_get_full_data_share_business_view() -> None:
    execution = (
        Agently.create_agent("result-view-direct")
        .input("Return a direct result.")
        .output({"reply": (str, "Reply", True), "path": (str, "Path", True)}, format="json")
        .create_execution()
        .strategy("direct")
    )
    route_calls = 0
    direct_payload = {"reply": "direct reply", "path": "/tmp/direct.md"}

    async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, str]]:
        nonlocal route_calls
        route_calls += 1
        return "model_request", direct_payload

    execution._async_execute_route = fake_route  # type: ignore[method-assign]

    result = execution.get_result()

    assert await result.async_get_data() == direct_payload
    assert await result.async_get_full_data() == direct_payload
    assert route_calls == 1


@pytest.mark.asyncio
async def test_task_strategy_get_data_projects_structured_final_result() -> None:
    execution = (
        Agently.create_agent("result-view-task-flat")
        .goal("Produce a structured file report.", ["Return the structured result."])
        .output({"reply": (str, "Reply", True), "path": (str, "Path", True)}, format="json")
        .strategy("flat")
    )
    full_payload = _terminal_payload('{"reply": "flat reply", "path": "/tmp/flat.md"}', strategy="flat")
    route_calls = 0

    async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, Any]]:
        nonlocal route_calls
        route_calls += 1
        return "agent_task", full_payload

    execution._async_execute_route = fake_route  # type: ignore[method-assign]

    result = execution.get_result()

    assert await result.async_get_data() == {"reply": "flat reply", "path": "/tmp/flat.md"}
    assert await result.async_get_full_data() == full_payload
    assert await result.async_get_text() == "flat final response"
    assert route_calls == 1


@pytest.mark.asyncio
async def test_taskboard_get_data_projects_structured_final_result_without_losing_full_envelope() -> None:
    execution = (
        Agently.create_agent("result-view-taskboard")
        .goal("Produce a structured board deliverable.", ["Return the structured result."])
        .output({"reply": (str, "Reply", True), "path": (str, "Path", True)}, format="json")
        .strategy("taskboard")
    )
    full_payload = _terminal_payload(
        {"reply": "taskboard reply", "path": "/tmp/taskboard.md"},
        strategy="taskboard",
    )

    async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "agent_task", full_payload

    execution._async_execute_route = fake_route  # type: ignore[method-assign]

    result = execution.get_result()

    assert await result.async_get_data() == {"reply": "taskboard reply", "path": "/tmp/taskboard.md"}
    assert await result.async_get_full_data() == full_payload
    assert await result.async_get_text() == "taskboard final response"


@pytest.mark.asyncio
async def test_task_strategy_get_data_keeps_full_envelope_when_final_result_missing() -> None:
    execution = (
        Agently.create_agent("result-view-task-partial")
        .goal("Return a partial task envelope.", ["Explain what stopped."])
        .strategy("flat")
    )
    full_payload = _terminal_payload("", strategy="flat")
    full_payload["status"] = "partial"
    full_payload["accepted"] = False
    full_payload["artifact_status"] = "partial"

    async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "agent_task", full_payload

    execution._async_execute_route = fake_route  # type: ignore[method-assign]

    result = execution.get_result()

    assert await result.async_get_data() == full_payload
    assert await result.async_get_full_data() == full_payload
    assert await result.async_get_text() == "flat final response"
