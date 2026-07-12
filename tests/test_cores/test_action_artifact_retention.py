from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agently import Agently


@pytest.mark.asyncio
async def test_large_action_output_stays_in_memory_and_runtime_event_is_bounded() -> None:
    large_body = "x" * (1024 * 1024)
    agent: Any = Agently.create_agent("action-artifact-retention-large-output")
    record = agent.action._finalize_action_result(
        {
            "action_call_id": "large-action-call",
            "action_id": "large_action_output",
            "status": "success",
            "success": True,
            "ok": True,
            "result": {"body": large_body},
            "data": {"body": large_body},
        }
    )
    artifact_refs = record["artifact_refs"]
    assert len(artifact_refs) == 1
    artifact_id = artifact_refs[0]["artifact_id"]
    assert artifact_refs[0]["meta"]["artifact_scope"] == {
        "kind": "action_call",
        "id": "large-action-call",
    }
    assert len(agent.action._artifact_manager._artifacts) == 1
    assert agent.action._artifact_manager.get_artifact_value(artifact_id) == {"body": large_body}
    assert large_body not in json.dumps(record, ensure_ascii=False)

    visible_record = agent.action._to_model_visible_record(record)
    assert large_body not in json.dumps(visible_record, ensure_ascii=False)
    assert visible_record["artifact_refs"][0]["artifact_id"] == artifact_id
    assert visible_record["result"]["result_preview_meta"]["truncated"] is True

    captured: list[Any] = []
    hook_name = "test_action_artifact_retention.large_action_output"
    Agently.event_center.register_hook(
        lambda event: captured.append(event),
        event_types="action.completed",
        hook_name=hook_name,
    )
    try:
        await agent.action._flow_controller.async_emit_action_flow_observation(
            {
                "kind": "action_completed",
                "source": "ActionFlow",
                "payload": {"record": record},
            }
        )
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert len(captured) == 1
    event_payload = captured[0].payload
    event_record = event_payload["record"]
    assert large_body not in json.dumps(event_payload, ensure_ascii=False)
    assert event_record["artifact_refs"][0]["artifact_id"] == artifact_id
    assert event_record["result"]["result_preview_meta"]["truncated"] is True


@pytest.mark.asyncio
async def test_selected_action_artifact_run_scope_release_is_concurrent_and_small_carrier_is_durable(
    tmp_path,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.route_execution import (
        _finalize_terminal_execution,
    )

    agent: Any = Agently.create_agent("action-artifact-retention-run-scope").use_workspace(tmp_path / "run")
    action_id = "produce_scoped_large_action_output"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a scope-owned large Action artifact.",
        kwargs={"marker": (str, "Marker used to identify the execution-owned value.")},
        func=lambda marker: {"marker": marker, "body": marker * (1024 * 1024)},
        tags=[f"agent-{agent.name}"],
    )
    first_execution = agent.input("Run the first scoped Action set.").create_execution().strategy("direct")
    second_execution = agent.input("Run the concurrent scoped Action set.").create_execution().strategy("direct")
    async def run_scoped_actions(execution: Any, markers: list[str]) -> list[dict[str, Any]]:
        return await asyncio.gather(
            *[
                agent.action.async_execute_action(
                    action_id,
                    {"marker": marker},
                    artifact_scope={"kind": "agent_execution", "id": execution.id},
                )
                for marker in markers
            ]
        )

    first_records, second_records = await asyncio.gather(
        run_scoped_actions(first_execution, ["s", "u"]),
        run_scoped_actions(second_execution, ["c"]),
    )
    first_refs = [
        next(ref for ref in record["artifact_refs"] if ref.get("artifact_type") == "action_output")
        for record in first_records
    ]
    second_ref = next(
        ref
        for ref in second_records[0]["artifact_refs"]
        if ref.get("artifact_type") == "action_output"
    )
    expected_first_scope = {"kind": "agent_execution", "id": first_execution.id}
    expected_second_scope = {"kind": "agent_execution", "id": second_execution.id}
    assert [ref["meta"]["artifact_scope"] for ref in first_refs] == [
        expected_first_scope,
        expected_first_scope,
    ]
    assert second_ref["meta"]["artifact_scope"] == expected_second_scope

    selected_ref, unselected_ref = first_refs
    first_execution.logs["artifact_refs"] = first_refs
    first_execution.result = {
        "accepted": True,
        "artifact_refs": [selected_ref],
        "reply": "small accepted wrapper",
    }
    first_execution.status = "success"

    await _finalize_terminal_execution(first_execution, failed=False)

    assert agent.action._artifact_manager.get_artifact_value(selected_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(unselected_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(second_ref["artifact_id"]) is not None
    assert first_execution.diagnostics["action_artifact_release"]["released_count"] == 4
    terminal_result = first_execution.close_snapshot["terminal_result"]
    assert selected_ref["artifact_id"] not in json.dumps(terminal_result, ensure_ascii=False)
    promoted_ref = terminal_result["artifact_refs"][0]
    assert promoted_ref["id"]
    stored = await first_execution.workspace.search(filters={"id": promoted_ref["id"]})
    assert len(stored) == 1
    readback = await first_execution.workspace.read_bounded(
        stored[0],
        offset=0,
        limit=int(stored[0]["size"]) + 1,
    )
    durable_value = json.loads(str(readback["content"]))
    assert durable_value["marker"] == "s"
    assert durable_value["body"] == "s" * (1024 * 1024)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["promotion", "terminal_event"])
async def test_action_artifact_terminal_failure_still_releases_only_owner_scope(
    tmp_path,
    monkeypatch,
    failure_stage: str,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.route_execution import (
        _finalize_terminal_execution,
    )

    agent: Any = Agently.create_agent(f"action-artifact-release-{failure_stage}").use_workspace(tmp_path / failure_stage)
    action_id = f"produce_release_failure_{failure_stage}"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a large value for terminal failure release coverage.",
        kwargs={"marker": (str, "Scope marker.")},
        func=lambda marker: {"marker": marker, "body": marker * (1024 * 1024)},
    )
    owner = agent.input("Finalize the owner scope.").create_execution().strategy("direct")
    concurrent = agent.input("Keep the concurrent scope alive.").create_execution().strategy("direct")

    async def execute_for(execution: Any, marker: str) -> dict[str, Any]:
        return await agent.action.async_execute_action(
            action_id,
            {"marker": marker},
            artifact_scope={"kind": "agent_execution", "id": execution.id},
        )

    owner_record, concurrent_record = await asyncio.gather(
        execute_for(owner, "o"),
        execute_for(concurrent, "c"),
    )
    owner_ref = next(
        ref for ref in owner_record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    concurrent_ref = next(
        ref for ref in concurrent_record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    owner.logs["artifact_refs"] = [owner_ref]
    owner.result = {"accepted": True, "artifact_refs": [owner_ref], "reply": "bounded"}
    owner.status = "success"

    if failure_stage == "promotion":
        async def fail_promotion(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("promotion unavailable " + "p" * 500)

        monkeypatch.setattr(owner.workspace, "put_artifact_ref", fail_promotion)
    else:
        owner._ensure_agent_execution_run_context()

        async def fail_terminal_event(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("terminal event unavailable " + "e" * 500)

        monkeypatch.setattr(agent, "_async_emit_agent_execution_terminal_event", fail_terminal_event)

    await _finalize_terminal_execution(owner, failed=False)

    assert agent.action._artifact_manager.get_artifact_value(owner_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(concurrent_ref["artifact_id"]) is not None
    release_diagnostic = owner.diagnostics["action_artifact_release"]
    assert release_diagnostic["status"] == "released"
    assert release_diagnostic["scope"] == {"kind": "agent_execution", "id": owner.id}
    retention_diagnostics = owner.diagnostics["workspace_retention"]["diagnostics"]
    assert retention_diagnostics
    assert all(len(str(item.get("message", ""))) <= 360 for item in retention_diagnostics)
