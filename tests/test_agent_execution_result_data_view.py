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
async def test_direct_terminal_retention_keeps_small_result_inline(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent("result-view-direct-small-retention")
        .use_workspace(tmp_path / "run")
        .input("Return a small direct result.")
        .create_execution()
        .strategy("direct")
    )
    execution.result = {"reply": "small terminal result"}

    event_result, retained_refs = await prepare_agent_execution_terminal_retention(execution)

    assert event_result == execution.result
    assert retained_refs == []


@pytest.mark.asyncio
async def test_direct_large_result_terminal_retention_uses_exactly_one_canonical_ref(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent("result-view-direct-large-retention")
        .use_workspace(tmp_path / "run")
        .input("Return a large direct result.")
        .create_execution()
        .strategy("direct")
    )
    execution.result = {"reply": "x" * 5000}

    event_result, retained_refs = await prepare_agent_execution_terminal_retention(execution)

    assert len(retained_refs) == 1
    assert retained_refs[0]["kind"] == "agent_execution_terminal_result"
    assert event_result["record_id"] == retained_refs[0]["id"]
    assert "x" * 100 not in str(event_result)
    assert execution.workspace is not None
    assert await execution.workspace.get_data(retained_refs[0]) == execution.result
    anchors = await execution.workspace.retention_anchors(execution.id, anchor_type="deliverable")
    assert len(anchors) == 1
    assert anchors[0]["record_ref"] is not None
    assert anchors[0]["record_ref"]["record_id"] == retained_refs[0]["id"]


@pytest.mark.asyncio
async def test_terminal_retention_reuses_workspace_envelope_without_copying_large_result(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent("result-view-envelope-retention")
        .use_workspace(tmp_path / "run")
        .input("Reuse an existing Workspace envelope.")
        .create_execution()
        .strategy("direct")
    )
    record_ref = await execution.workspace.put_artifact_ref(
        execution.id,
        {"path": "final.md"},
        metadata={"kind": "existing_deliverable"},
    )
    envelope = await execution.workspace.ref_envelope(record_ref)
    execution.result = {"artifact_refs": [envelope], "details": "e" * 5000}

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert event_result["artifact_refs"] == [envelope]
    assert "e" * 100 not in str(event_result)
    assert execution._terminal_retained_refs == [envelope]
    assert await execution.workspace.retention_anchors(execution.id, anchor_type="deliverable") == []


@pytest.mark.asyncio
async def test_terminal_retention_accepts_only_verified_workspace_file_ref(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent("result-view-file-ref-retention")
        .use_workspace(tmp_path / "run")
        .input("Reuse a verified Workspace file ref.")
        .create_execution()
        .strategy("direct")
    )
    write_result = await execution.workspace.write_file("final.md", "verified deliverable")
    file_ref = write_result["file_refs"][0]
    execution.result = {"artifact_refs": [file_ref], "reply": "file-backed result"}

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert event_result == execution.result
    assert retained_records == []
    assert execution._terminal_retained_refs == [file_ref]


@pytest.mark.asyncio
async def test_direct_terminal_retention_emits_small_inline_result_before_cleanup(tmp_path, monkeypatch) -> None:
    captured = []
    order: list[str] = []

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id and event.event_type.endswith("completed"):
            captured.append(event)
            order.append("event")

    hook_name = "test_agent_execution_result_data_view.small_terminal_retention"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        execution = (
            Agently.create_agent("result-view-direct-small-route-retention")
            .use_workspace(tmp_path / "run")
            .input("Return a small direct route result.")
            .create_execution()
            .strategy("direct")
        )
        direct_payload = {"reply": "small route terminal result"}

        async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, str]]:
            execution.route_info["selected_route"] = "model_request"
            execution.close_snapshot = {"status": "success", "route": "model_request"}
            return "model_request", direct_payload

        original_inspect = execution.workspace.inspect_retention

        async def tracked_inspect(*args: Any, **kwargs: Any) -> Any:
            order.append("cleanup")
            return await original_inspect(*args, **kwargs)

        monkeypatch.setattr(execution.workspace, "inspect_retention", tracked_inspect)
        execution._async_execute_route = fake_route  # type: ignore[method-assign]

        assert await execution.async_get_data() == direct_payload

        assert order == ["event", "cleanup"]
        assert len(captured) == 1
        assert captured[0].payload["close_snapshot"]["terminal_result"] == direct_payload
        assert captured[0].payload["close_snapshot"]["terminal_retained_refs"] == []
        retention = execution.diagnostics["workspace_retention"]
        assert retention["status"] in {"applied", "noop"}
        assert retention["manifest_ref"] is not None
        assert retention["manifest_ref"]["meta"]["inline_result"] == direct_payload
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_direct_large_result_route_emits_only_compact_pointer_and_keeps_business_result(tmp_path) -> None:
    captured = []

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id and event.event_type.endswith("completed"):
            captured.append(event)

    hook_name = "test_agent_execution_result_data_view.large_terminal_retention"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        execution = (
            Agently.create_agent("result-view-direct-large-route-retention")
            .use_workspace(tmp_path / "run")
            .input("Return a large direct route result.")
            .create_execution()
            .strategy("direct")
        )
        direct_payload = {"reply": "z" * 5000}

        async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, str]]:
            execution.route_info["selected_route"] = "model_request"
            execution.close_snapshot = {"status": "success", "route": "model_request"}
            return "model_request", direct_payload

        execution._async_execute_route = fake_route  # type: ignore[method-assign]

        assert await execution.async_get_data() == direct_payload

        assert len(captured) == 1
        close_snapshot = captured[0].payload["close_snapshot"]
        assert close_snapshot["terminal_result"]["record_id"] == close_snapshot["terminal_retained_refs"][0]["id"]
        assert "z" * 100 not in str(close_snapshot)
        retention = execution.diagnostics["workspace_retention"]
        assert retention["status"] in {"applied", "noop"}
        assert len(retention["manifest_ref"]["meta"]["retained_refs"]) == 1
        manifest_retained_ref = retention["manifest_ref"]["meta"]["retained_refs"][0]
        assert (manifest_retained_ref.get("id") or manifest_retained_ref.get("record_id")) == close_snapshot[
            "terminal_retained_refs"
        ][0]["id"]
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_task_route_terminal_retention_reuses_agent_task_deliverable_ref(tmp_path) -> None:
    captured = []

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id and event.event_type.endswith("completed"):
            captured.append(event)

    hook_name = "test_agent_execution_result_data_view.task_terminal_retention"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        execution = (
            Agently.create_agent("result-view-task-route-retention")
            .use_workspace(tmp_path / "run")
            .input("Return an AgentTask deliverable ref.")
            .create_execution()
            .strategy("flat")
        )
        artifact_ref = await execution.workspace.put_artifact_ref(
            "task-existing-ref",
            {"path": "final.md", "sha256": "abc", "bytes": 12},
            metadata={"kind": "agent_task_deliverable", "scope": {"task_id": "task-existing-ref"}},
        )
        await execution.workspace.add_retention_anchor(
            "task-existing-ref",
            anchor_type="deliverable",
            record_ref=artifact_ref,
        )
        task_payload = _terminal_payload({"path": "final.md"})
        task_payload["artifact_refs"] = [artifact_ref]

        async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, Any]]:
            execution.route_info["selected_route"] = "agent_task"
            execution.close_snapshot = {
                "status": "completed",
                "route": "agent_task",
                "task": {"iterations": ["internal-process-body" * 500]},
            }
            return "agent_task", task_payload

        execution._async_execute_route = fake_route  # type: ignore[method-assign]

        assert await execution.async_get_full_data() == task_payload

        assert len(captured) == 1
        retained_refs = captured[0].payload["close_snapshot"]["terminal_retained_refs"]
        assert [ref["id"] for ref in retained_refs] == [artifact_ref["id"]]
        assert "internal-process-body" not in str(captured[0].payload["close_snapshot"])
        task_anchors = await execution.workspace.retention_anchors("task-existing-ref", anchor_type="deliverable")
        assert len(task_anchors) == 1
        assert task_anchors[0]["record_ref"] is not None
        assert task_anchors[0]["record_ref"]["record_id"] == artifact_ref["id"]
        execution_anchors = await execution.workspace.retention_anchors(execution.id, anchor_type="deliverable")
        assert execution_anchors == []
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_terminal_retention_cleanup_failure_is_fail_open_for_success_and_error(tmp_path, monkeypatch) -> None:
    async def fail_cleanup(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("cleanup unavailable")

    successful = (
        Agently.create_agent("result-view-retention-cleanup-success")
        .use_workspace(tmp_path / "success")
        .input("Return success despite cleanup failure.")
        .create_execution()
        .strategy("direct")
    )

    async def successful_route(**_kwargs: Any) -> tuple[str, dict[str, str]]:
        successful.route_info["selected_route"] = "model_request"
        return "model_request", {"reply": "business success"}

    successful._async_execute_route = successful_route  # type: ignore[method-assign]
    monkeypatch.setattr(successful.workspace, "inspect_retention", fail_cleanup)

    assert await successful.async_get_data() == {"reply": "business success"}
    assert successful.status == "success"
    assert successful.diagnostics["workspace_retention"]["status"] == "deferred"
    assert successful.diagnostics["workspace_retention"]["diagnostics"][0]["code"] == (
        "agent_execution.retention.apply_failed"
    )

    failed = (
        Agently.create_agent("result-view-retention-cleanup-error")
        .use_workspace(tmp_path / "failed")
        .input("Raise the business error despite cleanup failure.")
        .create_execution()
        .strategy("direct")
    )

    async def failed_route(**_kwargs: Any) -> tuple[str, Any]:
        failed.route_info["selected_route"] = "model_request"
        raise ValueError("business failure")

    failed._async_execute_route = failed_route  # type: ignore[method-assign]
    monkeypatch.setattr(failed.workspace, "inspect_retention", fail_cleanup)

    with pytest.raises(ValueError, match="business failure"):
        await failed.async_get_data()

    assert failed.status == "error"
    assert isinstance(failed._error, ValueError)
    assert failed.diagnostics["workspace_retention"]["status"] == "deferred"


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
