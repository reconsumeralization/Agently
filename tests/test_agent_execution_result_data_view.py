from __future__ import annotations

import copy
from typing import Any, cast

import pytest

from agently import Agently
from agently.types.data import TaskWorkspaceFileRef


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
    execution: Any = (
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
async def test_direct_terminal_retention_keeps_small_result_inline(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
        Agently.create_agent("result-view-direct-small-retention")
        .use_task_workspace(tmp_path / "run")
        .input("Return a small direct result.")
        .create_execution()
        .strategy("direct")
    )
    execution.result = {"reply": "small terminal result"}

    event_result, retained_refs = await prepare_agent_execution_terminal_retention(execution)

    assert event_result == execution.result
    assert retained_refs == []


@pytest.mark.asyncio
async def test_direct_large_result_is_not_copied_into_workspace(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
        Agently.create_agent("result-view-direct-large-memory-only")
        .use_task_workspace(tmp_path / "run")
        .input("Return a large direct result.")
        .create_execution()
        .strategy("direct")
    )
    execution.result = {"reply": "x" * 70_000}

    event_result, retained_refs = await prepare_agent_execution_terminal_retention(execution)

    assert event_result["kind"] == "agent_execution_terminal_result_omitted"
    assert retained_refs == []
    assert execution.result["reply"] == "x" * 70_000
    assert not (tmp_path / "run" / ".agently").exists()


@pytest.mark.asyncio
async def test_direct_terminal_cleanup_keeps_only_verified_file_ref(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        apply_agent_execution_terminal_retention,
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
        Agently.create_agent("result-view-direct-file-cleanup")
        .use_task_workspace(tmp_path / "run", mode="read_only")
        .input("Return a file-backed result.")
        .create_execution()
        .strategy("direct")
    )
    draft = await execution.task_workspace.write_file("working/draft.md", "discard")
    final = await execution.task_workspace.write_file("deliverables/final.md", "retain")
    execution.result = {"artifact_refs": [final["file_refs"][0]]}
    execution.status = "success"

    _event_result, retained_refs = await prepare_agent_execution_terminal_retention(execution)
    cleanup = await apply_agent_execution_terminal_retention(execution, status="completed")

    assert retained_refs == [final["file_refs"][0]]
    assert cleanup is not None and cleanup["status"] == "applied"
    assert not (tmp_path / "run" / draft["path"]).exists()
    assert (tmp_path / "run" / final["path"]).read_text(encoding="utf-8") == "retain"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["limit", "general"])
async def test_agent_execution_error_projection_is_shared_and_utf8_bounded(
    tmp_path,
    error_kind: str,
) -> None:
    from agently.core.application.AgentExecution import AgentExecutionLimitExceeded

    captured: list[Any] = []

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id:
            captured.append(event)

    hook_name = f"test_agent_execution_result_data_view.bounded_error.{error_kind}"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    execution: Any = (
        Agently.create_agent(f"result-view-bounded-error-{error_kind}")
        .use_task_workspace(tmp_path / error_kind)
        .input("Raise one oversized error.")
        .create_execution()
        .strategy("direct")
    )
    oversized_message = "oversized-error-body:" + ("界" * 20000)

    async def fake_route(**_kwargs: Any) -> tuple[str, Any]:
        if error_kind == "limit":
            raise AgentExecutionLimitExceeded(
                oversized_message,
                limit_name="max_probe",
                limit_value=1,
                used=2,
            )
        raise RuntimeError(oversized_message)

    execution._async_execute_route = fake_route  # type: ignore[method-assign]
    try:
        with pytest.raises((AgentExecutionLimitExceeded, RuntimeError)):
            await execution.async_get_data()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    error_item = next(item for item in execution.stream.items if item.path == "error")
    diagnostic = execution.diagnostics["errors"][-1]
    terminal_error = execution.close_snapshot["terminal_result"]["error"]
    assert error_item.value == diagnostic == terminal_error
    assert len(str(error_item.value).encode("utf-8")) <= 4096
    assert len(str(execution.close_snapshot["terminal_result"]).encode("utf-8")) <= 4096
    assert "界" * 2000 not in str(error_item.value)
    terminal_event = next(
        event for event in captured if event.event_type in {"agent_execution.failed", "agent_execution.cancelled"}
    )
    assert len(str(terminal_event.payload).encode("utf-8")) <= 4096

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
