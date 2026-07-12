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

    assert event_result == {"artifact_refs": [file_ref]}
    assert "file-backed result" not in str(event_result)
    assert retained_records == []
    assert execution._terminal_retained_refs == [file_ref]


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["record_path", "envelope_digest", "file_digest", "process_record"])
async def test_terminal_retention_defers_forged_or_non_artifact_workspace_refs(tmp_path, forgery: str) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        apply_agent_execution_terminal_retention,
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent(f"result-view-forged-retention-{forgery}")
        .use_workspace(tmp_path / forgery)
        .input("Reject an untrusted terminal ref.")
        .create_execution()
        .strategy("direct")
    )
    if forgery == "process_record":
        canonical_ref = await execution.workspace.put(
            {"notes": "process only"},
            collection="observations",
            kind="process_notes",
        )
        candidate = canonical_ref
    elif forgery == "file_digest":
        write_result = await execution.workspace.write_file("final.md", "canonical file body")
        canonical_ref = write_result["file_refs"][0]
        candidate = {**canonical_ref, "sha256": "0" * 64}
    else:
        canonical_ref = await execution.workspace.put_artifact_ref(
            execution.id,
            {"path": "final.md", "body": "canonical"},
            metadata={"kind": "existing_deliverable"},
        )
        if forgery == "record_path":
            candidate = {**canonical_ref, "path": "artifacts/forged.json"}
        else:
            envelope = await execution.workspace.ref_envelope(canonical_ref)
            candidate = {**envelope, "digest": "f" * 64}
    business_result = {"artifact_refs": [candidate], "reply": "business result remains available"}
    execution.result = business_result

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)
    retention_result = await apply_agent_execution_terminal_retention(execution, status="completed")

    assert event_result["kind"] == "agent_execution_terminal_result_untrusted"
    assert retained_records == []
    assert retention_result is None
    assert execution.result == business_result
    assert execution.diagnostics["workspace_retention"]["status"] == "deferred"
    if forgery != "file_digest":
        assert await execution.workspace.search(filters={"id": canonical_ref["id"]})
    else:
        assert (await execution.workspace.read_file("final.md"))["sha256"] == canonical_ref["sha256"]


@pytest.mark.asyncio
async def test_terminal_retention_keeps_small_file_backed_body_out_of_event_carrier(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent("result-view-small-file-backed-retention")
        .use_workspace(tmp_path / "run")
        .input("Return a small file-backed body.")
        .create_execution()
        .strategy("direct")
    )
    file_body = "small file-backed terminal body probe"
    write_result = await execution.workspace.write_file("final.md", file_body)
    file_ref = write_result["file_refs"][0]
    execution.result = {
        "status": "completed",
        "final_result": file_body,
        "artifact_refs": [file_ref],
    }

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert file_body not in str(event_result)
    assert event_result["artifact_refs"] == [file_ref]
    assert "final_result" not in event_result
    assert execution._terminal_inline_result is None


@pytest.mark.asyncio
async def test_terminal_retention_uses_policy_inline_limit_during_preparation(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        apply_agent_execution_terminal_retention,
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent("result-view-policy-inline-limit-retention")
        .use_workspace(tmp_path / "run")
        .input("Apply the explicit retention threshold.")
        .create_execution(options={"workspace_retention_policy": {"inline_result_limit": 1024}})
        .strategy("direct")
    )
    execution.result = {"reply": "p" * 1500}

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)
    retention_result = await apply_agent_execution_terminal_retention(execution, status="completed")

    assert len(retained_records) == 1
    assert retained_records[0]["kind"] == "agent_execution_terminal_result"
    assert event_result["record_id"] == retained_records[0]["id"]
    assert retention_result is not None
    assert retention_result["status"] in {"applied", "noop"}
    assert execution.diagnostics["workspace_retention"]["status"] in {"applied", "noop"}


@pytest.mark.asyncio
async def test_terminal_retention_reuses_explicit_action_workspace_ref_without_duplicate(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution = (
        Agently.create_agent("result-view-action-ref-retention")
        .use_workspace(tmp_path / "run")
        .input("Reuse the explicit Action artifact ref.")
        .create_execution()
        .strategy("direct")
    )
    action_ref = await execution.workspace.put_artifact_ref(
        execution.id,
        {"artifact_id": "action-output", "path": "outputs/action.json"},
        metadata={"kind": "action_artifact"},
    )
    execution.logs["artifact_refs"] = [action_ref]
    execution.result = {"reply": "a" * 5000}

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert [ref["id"] for ref in retained_records] == [action_ref["id"]]
    assert [ref["id"] for ref in event_result["artifact_refs"]] == [action_ref["id"]]
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_terminal_result", "scope.execution_id": execution.id}
    ) == []


@pytest.mark.asyncio
async def test_selected_action_artifact_is_promoted_once_and_unselected_artifact_stays_temporary(
    tmp_path,
    monkeypatch,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    agent = Agently.create_agent("result-view-selected-action-artifact").use_workspace(tmp_path / "run")
    execution = agent.input("Promote only the accepted Action artifact.").create_execution().strategy("direct")
    artifact_scope = {"kind": "agent_execution", "id": execution.id}
    selected_value = {"body": "s" * (1024 * 1024)}
    unselected_value = {"body": "u" * (1024 * 1024)}
    selected_record = agent.action._finalize_action_result(
        {
            "action_call_id": "selected-call",
            "action_id": "selected_action_artifact",
            "status": "success",
            "success": True,
            "result": selected_value,
            "data": selected_value,
        },
        artifact_scope=artifact_scope,
    )
    unselected_record = agent.action._finalize_action_result(
        {
            "action_call_id": "unselected-call",
            "action_id": "unselected_action_artifact",
            "status": "success",
            "success": True,
            "result": unselected_value,
            "data": unselected_value,
        },
        artifact_scope=artifact_scope,
    )
    selected_ref = selected_record["artifact_refs"][0]
    unselected_ref = unselected_record["artifact_refs"][0]
    execution.logs["artifact_refs"] = [selected_ref, unselected_ref]
    execution.result = {
        "accepted": True,
        "artifact_refs": [selected_ref],
        "reply": "r" * 5000,
    }

    put_values: list[Any] = []
    original_put_artifact_ref = execution.workspace.put_artifact_ref

    async def capture_put_artifact_ref(*args: Any, **kwargs: Any) -> Any:
        value = args[1] if len(args) > 1 else kwargs["artifact"]
        put_values.append(value)
        return await original_put_artifact_ref(*args, **kwargs)

    monkeypatch.setattr(execution.workspace, "put_artifact_ref", capture_put_artifact_ref)

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert put_values == [selected_value]
    assert len(retained_records) == 1
    assert retained_records[0]["kind"] == "agent_execution_action_artifact"
    assert [ref["id"] for ref in event_result["artifact_refs"]] == [retained_records[0]["id"]]
    assert selected_value["body"] not in str(event_result)
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_terminal_result", "scope.execution_id": execution.id}
    ) == []
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_action_artifact", "scope.execution_id": execution.id}
    ) == retained_records
    assert unselected_ref["artifact_id"] not in str(
        await execution.workspace.search(filters={"collection": "artifacts"})
    )


@pytest.mark.asyncio
async def test_selected_action_artifact_defers_when_store_identity_no_longer_matches(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    agent = Agently.create_agent("result-view-selected-action-artifact-mismatch").use_workspace(tmp_path / "run")
    execution = agent.input("Reject the replaced Action artifact.").create_execution().strategy("direct")
    record = agent.action._finalize_action_result(
        {
            "action_call_id": "selected-call",
            "action_id": "selected_action_artifact",
            "status": "success",
            "success": True,
            "result": {"body": "s" * (1024 * 1024)},
            "data": {"body": "s" * (1024 * 1024)},
        },
        artifact_scope={"kind": "agent_execution", "id": execution.id},
    )
    selected_ref = record["artifact_refs"][0]
    agent.action._artifact_manager.register_external_artifact_ref(
        action_call_id="replacement-call",
        artifact_type="external_ref",
        label="Replacement with a colliding artifact id",
        ref={
            "artifact_id": selected_ref["artifact_id"],
            "path": "external/replacement.json",
            "bytes": 1,
            "sha256": "0" * 64,
        },
    )
    execution.logs["artifact_refs"] = [selected_ref]
    execution.result = {"accepted": True, "artifact_refs": [selected_ref], "reply": "r" * 5000}

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert event_result["kind"] == "agent_execution_terminal_result_untrusted"
    assert execution._terminal_retention_deferred is True
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_action_artifact", "scope.execution_id": execution.id}
    ) == []


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
