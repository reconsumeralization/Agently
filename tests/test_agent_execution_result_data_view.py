from __future__ import annotations

import copy
from typing import Any, cast

import pytest

from agently import Agently
from agently.types.data import WorkspaceFileRef


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

    execution: Any = (
        Agently.create_agent("result-view-direct-large-retention")
        .use_workspace(tmp_path / "run")
        .input("Return a large direct result.")
        .create_execution()
        .strategy("direct")
    )
    execution.result = {"reply": "x" * 5000}

    event_result, retained_refs = await prepare_agent_execution_terminal_retention(execution)

    assert len(retained_refs) == 1
    retained_record = cast(dict[str, Any], retained_refs[0])
    assert retained_record["kind"] == "agent_execution_terminal_result"
    assert event_result["record_id"] == retained_record["id"]
    assert "x" * 100 not in str(event_result)
    assert execution.workspace is not None
    assert await execution.workspace.get_data(retained_record) == execution.result
    anchors = await execution.workspace.retention_anchors(execution.id, anchor_type="deliverable")
    assert len(anchors) == 1
    assert anchors[0]["record_ref"] is not None
    assert anchors[0]["record_ref"]["record_id"] == retained_record["id"]


@pytest.mark.asyncio
async def test_terminal_retention_reuses_workspace_envelope_without_copying_large_result(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
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

    assert retained_records == [envelope]
    assert event_result["artifact_refs"] == [envelope]
    assert "e" * 100 not in str(event_result)
    assert execution._terminal_retained_refs == [envelope]
    assert await execution.workspace.retention_anchors(execution.id, anchor_type="deliverable") == []


@pytest.mark.asyncio
async def test_terminal_retention_accepts_only_verified_workspace_file_ref(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
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

    assert event_result == {"artifact_refs": [file_ref], "artifacts": [file_ref]}
    assert "file-backed result" not in str(event_result)
    assert retained_records == [file_ref]
    assert execution._terminal_retained_refs == [file_ref]


@pytest.mark.asyncio
async def test_terminal_retention_accepts_verified_zero_byte_workspace_file_ref(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        apply_agent_execution_terminal_retention,
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
        Agently.create_agent("result-view-empty-file-ref-retention")
        .use_workspace(tmp_path / "run")
        .input("Reuse a verified empty Workspace file ref.")
        .create_execution()
        .strategy("direct")
    )
    write_result = await execution.workspace.write_file("empty.txt", "")
    file_ref = write_result["file_refs"][0]
    assert file_ref["bytes"] == 0
    execution.result = {"artifact_refs": [file_ref], "reply": "empty deliverable"}
    execution.status = "success"
    execution.route_info["selected_route"] = "model_request"

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)
    retention = await apply_agent_execution_terminal_retention(execution, status="completed")

    assert event_result == {"artifact_refs": [file_ref], "artifacts": [file_ref]}
    assert retained_records == [file_ref]
    assert execution._terminal_retained_refs == [file_ref]
    assert retention is not None
    assert retention["status"] in {"applied", "noop"}
    assert (await execution.workspace.read_file("empty.txt"))["bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_bytes", [True, False, None, "0"])
async def test_terminal_selector_rejects_non_integer_or_missing_zero_byte_size(
    tmp_path,
    invalid_bytes: Any,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
        Agently.create_agent(f"result-view-invalid-empty-file-ref-{invalid_bytes!r}")
        .use_workspace(tmp_path / str(invalid_bytes))
        .input("Reject an invalid empty Workspace file selector.")
        .create_execution()
        .strategy("direct")
    )
    file_ref = dict((await execution.workspace.write_file("empty.txt", ""))["file_refs"][0])
    if invalid_bytes is None:
        file_ref.pop("bytes")
    else:
        file_ref["bytes"] = invalid_bytes
    execution.result = {"artifact_refs": [file_ref]}

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert event_result["kind"] == "agent_execution_terminal_result_untrusted"


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["record_path", "envelope_digest", "file_digest", "process_record"])
async def test_terminal_retention_defers_forged_or_non_artifact_workspace_refs(tmp_path, forgery: str) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        apply_agent_execution_terminal_retention,
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
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

    execution: Any = (
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

    assert retained_records == [file_ref]
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

    execution: Any = (
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
    retained_record = cast(dict[str, Any], retained_records[0])
    assert retained_record["kind"] == "agent_execution_terminal_result"
    assert event_result["record_id"] == retained_record["id"]
    assert retention_result is not None
    assert retention_result["status"] in {"applied", "noop"}
    assert execution.diagnostics["workspace_retention"]["status"] in {"applied", "noop"}


@pytest.mark.asyncio
async def test_terminal_retention_reuses_explicit_action_workspace_ref_without_duplicate(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
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

    assert [ref.get("id") for ref in retained_records] == [action_ref["id"]]
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

    agent: Any = Agently.create_agent("result-view-selected-action-artifact").use_workspace(tmp_path / "run")
    execution: Any = agent.input("Promote only the accepted Action artifact.").create_execution().strategy("direct")
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
        "artifact_refs": [{"selection_key": selected_ref["selection_key"]}],
        "reply": "r" * 5000,
    }
    execution.status = "success"
    execution.route_info["selected_route"] = "model_request"

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
    retained_record = cast(dict[str, Any], retained_records[0])
    assert retained_record["kind"] == "agent_execution_action_artifact"
    assert [ref["id"] for ref in event_result["artifact_refs"]] == [retained_record["id"]]
    assert selected_value["body"] not in str(event_result)
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_terminal_result", "scope.execution_id": execution.id}
    ) == []
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_action_artifact", "scope.execution_id": execution.id}
    ) == retained_records
    assert unselected_ref["selection_key"] not in str(
        await execution.workspace.search(filters={"collection": "artifacts"})
    )


@pytest.mark.asyncio
async def test_business_accepted_field_cannot_authorize_action_artifact_selection(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    agent: Any = Agently.create_agent("result-view-business-accepted-no-authority").use_workspace(tmp_path / "run")
    execution: Any = agent.input("Do not select from business data.").create_execution().strategy("direct")
    value = {"body": "private selected candidate" * 10000}
    record = agent.action._finalize_action_result(
        {
            "action_call_id": "business-accepted-call",
            "action_id": "business_accepted_candidate",
            "status": "success",
            "success": True,
            "result": value,
            "data": value,
        },
        artifact_scope={"kind": "agent_execution", "id": execution.id},
    )
    artifact_ref = record["artifact_refs"][0]
    execution.logs["artifact_refs"] = [artifact_ref]
    execution.result = {"accepted": True, "artifact_refs": [artifact_ref], "status": "error"}
    execution.status = "error"
    execution.route_info["selected_route"] = "model_request"

    _carrier, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_action_artifact", "scope.execution_id": execution.id}
    ) == []
    transfer = agent.action._artifact_manager.read_selection_transfer(
        artifact_ref["selection_key"],
        expected_scope={"kind": "agent_execution", "id": execution.id},
    )
    assert transfer is not None and transfer[1] == value


@pytest.mark.asyncio
async def test_selected_action_artifact_defers_when_store_identity_no_longer_matches(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    agent: Any = Agently.create_agent("result-view-selected-action-artifact-mismatch").use_workspace(tmp_path / "run")
    execution: Any = agent.input("Reject the replaced Action artifact.").create_execution().strategy("direct")
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
    execution.logs["artifact_refs"] = [selected_ref]
    execution.result = {
        "accepted": True,
        "artifact_refs": [{"selection_key": selected_ref["selection_key"]}],
        "reply": "r" * 5000,
    }
    execution.status = "success"
    execution.route_info["selected_route"] = "model_request"

    original_read = agent.action._artifact_manager.read_selection_transfer

    def mismatched_read(selection_key: str, *, expected_scope: dict[str, str]):
        transfer = original_read(selection_key, expected_scope=expected_scope)
        assert transfer is not None
        identity, value = transfer
        identity["selection_key"] = "sel_replaced"
        return identity, value

    agent.action._artifact_manager.read_selection_transfer = mismatched_read

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert event_result["kind"] == "agent_execution_terminal_result_untrusted"
    assert execution._terminal_retention_deferred is True
    assert await execution.workspace.search(
        filters={"kind": "agent_execution_action_artifact", "scope.execution_id": execution.id}
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection_refs",
    [
        [{"selection_key": "sel_unknown"}],
        [{"selection_key": "{key}"}, {"selection_key": "{key}"}],
        [{"artifact_id": "{artifact_id}", "action_call_id": "selected-call"}],
    ],
    ids=["unknown", "duplicate", "copied-canonical-identity"],
)
async def test_action_artifact_selection_rejects_untrusted_or_duplicated_keys(
    tmp_path,
    selection_refs: list[dict[str, str]],
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    agent: Any = Agently.create_agent("result-view-reject-action-selection").use_workspace(tmp_path / "run")
    execution: Any = agent.input("Reject an invalid selection.").create_execution().strategy("direct")
    record = agent.action._finalize_action_result(
        {
            "action_call_id": "selected-call",
            "action_id": "candidate",
            "status": "success",
            "success": True,
            "result": {"body": "s" * (1024 * 1024)},
            "data": {"body": "s" * (1024 * 1024)},
        },
        artifact_scope={"kind": "agent_execution", "id": execution.id},
    )
    offered_ref = record["artifact_refs"][0]
    execution.logs["artifact_refs"] = [offered_ref]
    execution.result = {
        "artifact_refs": [
            {
                key: (
                    offered_ref["selection_key"]
                    if value == "{key}"
                    else offered_ref.get("artifact_id", "copied-id")
                    if value == "{artifact_id}"
                    else value
                )
                for key, value in ref.items()
            }
            for ref in selection_refs
        ]
    }
    execution.status = "success"
    execution.route_info["selected_route"] = "model_request"

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert event_result["kind"] == "agent_execution_terminal_result_untrusted"
    assert execution._terminal_retention_deferred is True


@pytest.mark.asyncio
async def test_selected_action_artifact_defers_when_candidate_forges_manager_scope(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    agent: Any = Agently.create_agent("result-view-forged-action-artifact-scope").use_workspace(tmp_path / "run")
    execution: Any = agent.input("Reject the forged Action artifact scope.").create_execution().strategy("direct")
    other_execution = agent.input("Own the real Action artifact scope.").create_execution().strategy("direct")
    record = agent.action._finalize_action_result(
        {
            "action_call_id": "forged-scope-call",
            "action_id": "forged_scope_action_artifact",
            "status": "success",
            "success": True,
            "result": {"body": "s" * (1024 * 1024)},
            "data": {"body": "s" * (1024 * 1024)},
        },
        artifact_scope={"kind": "agent_execution", "id": other_execution.id},
    )
    stored_ref = record["artifact_refs"][0]
    execution.logs["artifact_refs"] = [stored_ref]
    execution.result = {
        "accepted": True,
        "artifact_refs": [{"selection_key": stored_ref["selection_key"]}],
        "reply": "bounded",
    }
    execution.status = "success"
    execution.route_info["selected_route"] = "model_request"

    event_result, retained_records = await prepare_agent_execution_terminal_retention(execution)

    assert retained_records == []
    assert event_result["kind"] == "agent_execution_terminal_result_untrusted"
    assert execution._terminal_retention_deferred is True
    transfer = agent.action._artifact_manager.read_selection_transfer(
        stored_ref["selection_key"],
        expected_scope={"kind": "agent_execution", "id": other_execution.id},
    )
    assert transfer is not None and transfer[1] is not None
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
        execution: Any = (
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
        execution: Any = (
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
        result_items = [
            item
            for item in execution.stream.items
            if item.path == "result" and item.source == "agent_execution"
        ]
        assert len(result_items) == 1
        assert result_items[0].value == close_snapshot["terminal_result"]
        assert "z" * 100 not in str(result_items[0].value)
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
        execution: Any = (
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
async def test_agent_execution_cancellation_emits_bounded_terminal_projection_and_reraises(
    tmp_path,
    monkeypatch,
) -> None:
    terminal_events: list[Any] = []

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id:
            if event.event_type.startswith("agent_execution."):
                terminal_events.append(event)

    hook_name = "test_agent_execution_result_data_view.cancelled_terminal_event"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    execution: Any = (
        Agently.create_agent("result-view-cancelled-terminal")
        .use_workspace(tmp_path / "run")
        .input("Wait until the host cancels.")
        .create_execution()
        .strategy("direct")
    )
    route_started = __import__("asyncio").Event()
    wait_forever = __import__("asyncio").Event()
    captured_lifecycle: dict[str, Any] = {}
    original_inspect = execution.workspace.inspect_retention

    async def fake_route(**_kwargs: Any) -> tuple[str, Any]:
        execution.route_info["selected_route"] = "model_request"
        route_started.set()
        await wait_forever.wait()
        return "model_request", {"reply": "never"}

    async def capture_inspect(*args: Any, **kwargs: Any) -> Any:
        captured_lifecycle.update(kwargs["lifecycle"])
        return await original_inspect(*args, **kwargs)

    execution._async_execute_route = fake_route  # type: ignore[method-assign]
    monkeypatch.setattr(execution.workspace, "inspect_retention", capture_inspect)
    try:
        run = __import__("asyncio").create_task(execution.async_get_data())
        await route_started.wait()
        run.cancel()

        with pytest.raises(__import__("asyncio").CancelledError):
            await run
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert execution.status == "cancelled"
    assert captured_lifecycle["status"] == "cancelled"
    assert captured_lifecycle["state_version"] is not None
    terminal = execution.close_snapshot["terminal_result"]
    assert terminal["status"] == "cancelled"
    assert len(str(terminal).encode("utf-8")) <= 4096
    cancellation_items = [item for item in execution.stream.items if item.path == "cancelled"]
    assert len(cancellation_items) == 1
    assert len(str(cancellation_items[0].value).encode("utf-8")) <= 4096
    terminal_types = [
        event.event_type
        for event in terminal_events
        if event.event_type
        in {"agent_execution.completed", "agent_execution.failed", "agent_execution.cancelled"}
    ]
    assert terminal_types == ["agent_execution.cancelled"]
    assert terminal_events[-1].payload["status"] == "cancelled"

    lifecycle_statuses: list[str] = []
    original_late_inspect = execution.workspace.inspect_retention

    async def capture_late_status(*args: Any, **kwargs: Any) -> Any:
        lifecycle_statuses.append(str(kwargs["lifecycle"]["status"]))
        return await original_late_inspect(*args, **kwargs)

    monkeypatch.setattr(execution.workspace, "inspect_retention", capture_late_status)
    late_record = await execution.async_record_workspace(
        purpose="deliverable",
        content={"late": "cancelled deliverable"},
    )

    assert late_record["record"]["meta"]["workspace_purpose"] == "deliverable"
    assert lifecycle_statuses == ["cancelled"]


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
        .use_workspace(tmp_path / error_kind)
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
@pytest.mark.parametrize("active_fact", ["recovery", "lease"])
async def test_agent_execution_retention_passes_active_lifecycle_facts_and_preserves_scope(
    tmp_path,
    monkeypatch,
    active_fact: str,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        apply_agent_execution_terminal_retention,
        prepare_agent_execution_terminal_retention,
    )

    execution: Any = (
        Agently.create_agent(f"result-view-active-{active_fact}")
        .use_workspace(tmp_path / active_fact)
        .input("Preserve active lifecycle state.")
        .create_execution()
        .strategy("direct")
    )
    process_ref = await execution.workspace.put(
        {"active": active_fact},
        collection="observations",
        kind="active_lifecycle_process",
    )
    execution.result = {"reply": "bounded"}
    execution.status = "success"
    execution.route_info["selected_route"] = "model_request"
    await execution.workspace.put_snapshot(
        execution.id,
        {
            "state_version": 17,
            "interrupts": (
                {"approval": {"status": "waiting"}}
                if active_fact == "recovery"
                else {}
            ),
            "intervention": {"ledger": []},
        },
    )
    if active_fact == "lease":
        await execution.workspace.claim_lease(
            execution.id,
            "agent-execution-worker",
            ttl=30,
            expected_state_version=17,
        )
    captured: dict[str, Any] = {}
    original_inspect = execution.workspace.inspect_retention

    async def capture_inspect(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs["lifecycle"])
        return await original_inspect(*args, **kwargs)

    monkeypatch.setattr(execution.workspace, "inspect_retention", capture_inspect)
    await prepare_agent_execution_terminal_retention(execution)
    retention = await apply_agent_execution_terminal_retention(execution, status="completed")

    assert captured["state_version"] == 17
    assert captured["recovery_active"] is (active_fact == "recovery")
    assert captured["lease_active"] is (active_fact == "lease")
    assert retention is not None
    assert retention["status"] == "deferred"
    assert await execution.workspace.get_data(process_ref) == {"active": active_fact}


@pytest.mark.asyncio
async def test_routed_agent_task_uses_execution_child_workspace_and_parent_reuses_canonical_ref(
    tmp_path,
    monkeypatch,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.task_strategy import (
        run_agent_task_route,
    )
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        apply_agent_execution_terminal_retention,
        prepare_agent_execution_terminal_retention,
    )
    from agently.core.application.AgentTask import AgentTask

    agent: Any = Agently.create_agent("result-view-real-routed-task-scope").use_workspace(tmp_path / "run")
    execution: Any = (
        agent.input("Produce one canonical routed task file.")
        .goal("Produce the report.", ["The report is written and retained."])
        .create_execution()
        .strategy("flat")
    )
    task_observation: dict[str, Any] = {}

    async def routed_task_run(task: AgentTask) -> Any:
        task_observation["lineage"] = [dict(node) for node in task.workspace.scope_lineage]
        task_observation["files_root"] = task.workspace.files_root
        process_ref = await task.workspace.put(
            {"stage": "task process"},
            collection="observations",
            kind="routed_task_process",
        )
        task_observation["process_ref"] = process_ref
        await task.workspace.write_file("working/process.txt", "discard routed task process")
        await task.workspace.write_file("reports/final.md", "canonical routed task body")
        readback = await task.workspace.read_file("reports/final.md", max_bytes=128)
        file_ref: WorkspaceFileRef = {
            "path": "reports/final.md",
            "bytes": readback["bytes"],
            "sha256": readback["sha256"],
            "media_type": readback.get("media_type"),
            "content_kind": readback.get("content_kind", "text"),
            "role": "workspace_artifact",
        }
        task_observation["file_ref"] = file_ref
        promoted = await task._register_terminal_deliverables([file_ref])
        task.status = "completed"
        task.result = {
            "status": "completed",
            "accepted": True,
            "artifact_status": "accepted",
            "task_id": task.id,
            "execution_strategy": "flat",
            "effective_execution_strategy": "flat",
            "final_response": "Completed. Deliverable artifact: reports/final.md.",
            "final_result": "Workspace artifact delivered at reports/final.md.",
            "artifact_refs": promoted,
            "reason": "",
            "missing_criteria": [],
        }
        task_observation["retention"] = await task._apply_terminal_workspace_retention(status="completed")
        task_observation["process_record_after"] = await task.workspace.backend.get_record(process_ref["id"])
        try:
            await task.workspace.read_file("working/process.txt")
        except FileNotFoundError:
            task_observation["process_file_deleted"] = True
        else:
            task_observation["process_file_deleted"] = False
        task_observation["final_after"] = await task.workspace.read_file("reports/final.md")
        task._completed = True
        await task._emit("result", task.result)
        await task._close_streams()
        return task.result

    monkeypatch.setattr(AgentTask, "async_run", routed_task_run)
    sibling = execution.workspace.with_scope_node(
        "tasks",
        "live-sibling",
        scope={"execution_id": execution.id, "task_id": "live-sibling"},
        search_scope={"execution_id": execution.id, "task_id": "live-sibling"},
    )
    await execution.workspace.write_file("working/parent-live.txt", "live parent state")
    await sibling.write_file("working/live.txt", "live sibling state")

    result = await run_agent_task_route(execution, {"strategy": "flat"})
    parent_after_task_retention = await execution.workspace.read_file("working/parent-live.txt")
    sibling_after_task_retention = await sibling.read_file("working/live.txt")
    execution.result = result
    execution.status = "success"
    carrier, retained_records = await prepare_agent_execution_terminal_retention(execution)
    retention = await apply_agent_execution_terminal_retention(execution, status="completed")

    lineage = task_observation["lineage"]
    assert lineage[-2:] == [
        {"kind": "executions", "id": execution.id},
        {"kind": "tasks", "id": result["task_id"]},
    ]
    assert task_observation["process_ref"]["scope"]["execution_id"] == execution.id
    assert task_observation["process_ref"]["scope"]["task_id"] == result["task_id"]
    assert task_observation["files_root"].is_relative_to(execution.workspace.files_root.parent)
    assert task_observation["retention"] is not None
    assert task_observation["retention"]["status"] in {"applied", "noop"}
    assert task_observation["process_record_after"] is None
    assert task_observation["process_file_deleted"] is True
    assert task_observation["final_after"]["content"] == "canonical routed task body"
    assert retained_records == [result["artifact_refs"][0]]
    assert carrier["artifact_refs"][0]["id"] == result["artifact_refs"][0]["id"]
    assert retention is not None
    assert await execution.workspace.get_data(result["artifact_refs"][0]) == task_observation["file_ref"]
    assert parent_after_task_retention["content"] == "live parent state"
    assert sibling_after_task_retention["content"] == "live sibling state"


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
