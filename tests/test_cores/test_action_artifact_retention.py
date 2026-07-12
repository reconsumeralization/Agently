from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from agently import Agently


def test_action_artifact_scope_is_private_and_returned_values_are_defensive() -> None:
    agent: Any = Agently.create_agent("action-artifact-private-scope")
    manager = agent.action._artifact_manager
    scope_a = {"kind": "agent_execution", "id": "execution-a"}
    scope_b = {"kind": "agent_execution", "id": "execution-b"}
    artifact = manager.register_execution_artifact(
        action_call_id="private-scope-call",
        artifact_type="action_output",
        label="Private scope artifact",
        value={"nested": {"value": "original"}},
        artifact_scope=scope_a,
    )
    artifact_id = artifact["artifact_id"]

    artifact["meta"]["artifact_scope"] = scope_b
    artifact["meta"]["external"] = True
    first_read = manager.get_artifact(artifact_id)
    assert first_read is not None
    assert first_read["meta"]["artifact_scope"] == scope_a
    assert "external" not in first_read["meta"]

    first_read["meta"]["artifact_scope"] = scope_b
    first_read["value"]["nested"]["value"] = "mutated"
    first_value = manager.get_artifact_value(artifact_id)
    first_value["nested"]["value"] = "mutated again"
    assert manager.get_artifact_scope(artifact_id) == scope_a
    assert manager.get_artifact_value(artifact_id) == {"nested": {"value": "original"}}
    assert manager.release_scope(scope_b) == 0
    assert manager.get_artifact_value(artifact_id) is not None
    assert manager.release_scope(scope_a) == 1
    assert manager.get_artifact_value(artifact_id) is None


def test_action_artifact_keeps_exact_private_value_and_redacts_preview_only() -> None:
    agent: Any = Agently.create_agent("action-artifact-exact-private-value")
    exact_value = {"token": "exact-secret", "nested": {"body": "full-value"}}
    artifact = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="exact-private-call",
        artifact_type="action_output",
        label="Exact private value",
        value=exact_value,
    )

    assert artifact["preview"]["token"] == "[REDACTED]"
    assert agent.action._artifact_manager.get_artifact_value(artifact["artifact_id"]) == exact_value


@pytest.mark.asyncio
async def test_direct_action_entry_releases_success_and_failure_scopes(tmp_path, monkeypatch) -> None:
    agent: Any = Agently.create_agent("action-artifact-direct-scope-release")
    action_id = "direct_scope_release"
    agent.action.register_action(
        action_id=action_id,
        desc="Return a large direct Action value.",
        kwargs={},
        func=lambda: {"body": "d" * (1024 * 1024)},
    )

    record = await agent.action.async_execute_action(action_id, {})
    assert record["artifact_refs"]
    returned_ref = next(
        ref for ref in record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    assert returned_ref["available"] is False
    assert returned_ref["full_value_available"] is False
    assert returned_ref.get("preview") or returned_ref.get("preview_omitted") is True
    assert returned_ref["sha256"]
    assert record["model_digest"]
    readback = agent.action.read_action_artifact(
        artifact_id=returned_ref["artifact_id"],
        action_call_id=returned_ref["action_call_id"],
    )
    assert readback["ok"] is False
    assert readback["status"] == "not_found"
    assert agent.action._artifact_manager._artifacts == {}

    captured_id = ""
    original_finalize = agent.action._finalize_action_result

    def fail_after_registration(result: Any, *, artifact_scope: dict[str, str] | None = None) -> Any:
        nonlocal captured_id
        finalized = original_finalize(result, artifact_scope=artifact_scope)
        captured_id = finalized["artifact_refs"][0]["artifact_id"]
        raise RuntimeError("finalization failed after artifact registration")

    monkeypatch.setattr(agent.action, "_finalize_action_result", fail_after_registration)
    with pytest.raises(RuntimeError, match="finalization failed"):
        await agent.action.async_execute_action(action_id, {})
    assert captured_id
    assert agent.action._artifact_manager.get_artifact_value(captured_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
async def test_standalone_action_flows_release_success_scope(
    flow_name: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    agent: Any = Agently.create_agent(f"action-artifact-standalone-{flow_name}")
    action_id = f"standalone_{flow_name}"
    agent.action.register_action(
        action_id=action_id,
        desc="Return a large standalone flow value.",
        kwargs={},
        func=lambda: {"body": "f" * (1024 * 1024)},
    )
    flow = agent.action._flow_controller.create_named_action_flow(flow_name)

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [{"action_id": action_id, "action_input": {}, "purpose": "produce output"}],
        }

    async def execution_handler(_context: dict[str, Any], request: dict[str, Any]) -> list[Any]:
        return [await request["async_call_action"](action_id, {})]

    records = await flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(),
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=2,
    )

    assert records
    returned_refs: list[dict[str, Any]] = []

    def collect_refs(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("artifact_type") == "action_output" and value.get("artifact_id"):
                returned_refs.append(value)
            for nested in value.values():
                collect_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_refs(nested)

    collect_refs(records)
    serialized_records = json.dumps(records, ensure_ascii=False)
    assert len(serialized_records.encode("utf-8")) <= 16000
    assert "f" * 100 not in serialized_records
    assert all(ref["available"] is False for ref in returned_refs)
    assert all(ref["full_value_available"] is False for ref in returned_refs)
    assert all(ref.get("preview") or ref.get("preview_omitted") is True for ref in returned_refs)
    assert all(ref.get("sha256") for ref in returned_refs)
    assert agent.action._artifact_manager._artifacts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_mode", ["failure", "cancellation"])
async def test_standalone_triggerflow_action_scope_releases_on_failure_and_cancellation(
    terminal_mode: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    agent: Any = Agently.create_agent(f"action-artifact-standalone-{terminal_mode}")
    action_id = f"standalone_{terminal_mode}"
    agent.action.register_action(
        action_id=action_id,
        desc="Create a large standalone value before terminal interruption.",
        kwargs={},
        func=lambda: {"body": terminal_mode * (1024 * 1024)},
    )
    flow = agent.action._flow_controller.create_named_action_flow("TriggerFlowActionFlow")
    artifact_registered = asyncio.Event()
    wait_forever = asyncio.Event()

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [{"action_id": action_id, "action_input": {}, "purpose": "interrupt"}],
        }

    async def execution_handler(_context: dict[str, Any], request: dict[str, Any]) -> list[Any]:
        await request["async_call_action"](action_id, {})
        assert agent.action._artifact_manager._artifacts
        artifact_registered.set()
        if terminal_mode == "failure":
            raise RuntimeError("standalone flow failure")
        await wait_forever.wait()
        return []

    run = asyncio.create_task(
        flow.async_run(
            action=agent.action,
            prompt=agent.request.prompt,
            settings=agent.settings,
            action_list=agent.action.get_action_list(),
            planning_handler=planning_handler,
            execution_handler=execution_handler,
            max_rounds=2,
        )
    )
    await artifact_registered.wait()
    if terminal_mode == "cancellation":
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    else:
        with pytest.raises(RuntimeError, match="standalone flow failure"):
            await run
    assert agent.action._artifact_manager._artifacts == {}


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

    await _finalize_terminal_execution(first_execution, terminal_status="completed")

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
async def test_custom_action_execution_handler_callback_binds_agent_execution_artifact_scope(
    tmp_path,
    monkeypatch,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.route_execution import (
        _finalize_terminal_execution,
    )

    agent: Any = Agently.create_agent("action-artifact-custom-handler-scope").use_workspace(tmp_path / "run")
    action_id = "produce_custom_handler_large_output"
    action_tag = f"agent-{agent.name}"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a large Action artifact through a custom execution handler callback.",
        kwargs={"marker": (str, "Execution scope marker.")},
        func=lambda marker: {"marker": marker, "body": marker * (1024 * 1024)},
        tags=[action_tag],
    )
    owner = agent.input("Run the custom Action execution handler.").create_execution().strategy("direct")
    concurrent = agent.input("Keep a concurrent Action scope alive.").create_execution().strategy("direct")
    callback_results: list[dict[str, Any]] = []
    registered_refs: list[dict[str, Any]] = []
    original_register_execution_artifact = agent.action._artifact_manager.register_execution_artifact

    def capture_registered_artifact(*args: Any, **kwargs: Any) -> dict[str, Any]:
        artifact_ref = original_register_execution_artifact(*args, **kwargs)
        registered_refs.append(copy.deepcopy(artifact_ref))
        return artifact_ref

    monkeypatch.setattr(
        agent.action._artifact_manager,
        "register_execution_artifact",
        capture_registered_artifact,
    )

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "purpose": "produce scoped output",
                    "action_id": action_id,
                    "action_input": {"marker": "o"},
                    "todo_suggestion": "finish",
                }
            ],
        }

    async def execution_handler(
        _context: dict[str, Any],
        request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        callback_result = await request["async_call_action"](action_id, {"marker": "o"})
        assert isinstance(callback_result, dict)
        callback_results.append(callback_result)
        return [
            {
                "action_call_id": "custom-handler-result",
                "action_id": action_id,
                "purpose": "produce scoped output",
                "status": "success",
                "success": True,
                "ok": True,
                "result": callback_result,
                "data": callback_result,
                "artifact_refs": callback_result.get("artifact_refs", []),
            }
        ]

    records = await agent.action.async_plan_and_execute(
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(tags=[action_tag]),
        agent_name=agent.name,
        parent_run_context=owner._ensure_agent_execution_run_context(),
        planning_handler=planning_handler,
        action_execution_handler=execution_handler,
        max_rounds=2,
    )
    assert callback_results
    callback_ref = next(
        ref
        for ref in registered_refs
        if ref.get("artifact_type") == "action_output"
    )
    stored = agent.action._artifact_manager.get_artifact(callback_ref["artifact_id"])
    assert stored is not None
    assert stored["meta"]["artifact_scope"] == {"kind": "agent_execution", "id": owner.id}

    concurrent_record = await agent.action.async_execute_action(
        action_id,
        {"marker": "c"},
        artifact_scope={"kind": "agent_execution", "id": concurrent.id},
    )
    concurrent_ref = next(
        ref
        for ref in concurrent_record["artifact_refs"]
        if ref.get("artifact_type") == "action_output"
    )
    owner.logs["artifact_refs"] = [callback_ref]
    owner.result = {
        "accepted": True,
        "artifact_refs": [callback_ref],
        "records": records,
        "reply": "bounded",
    }
    owner.status = "success"

    await _finalize_terminal_execution(owner, terminal_status="completed")

    assert agent.action._artifact_manager.get_artifact_value(callback_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(concurrent_ref["artifact_id"]) is not None


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
    unselected_record = await execute_for(owner, "u")
    unselected_ref = next(
        ref for ref in unselected_record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    owner.logs["artifact_refs"] = [owner_ref, unselected_ref]
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

    await _finalize_terminal_execution(owner, terminal_status="completed")

    if failure_stage == "promotion":
        assert agent.action._artifact_manager.get_artifact_value(owner_ref["artifact_id"]) is not None
    else:
        assert agent.action._artifact_manager.get_artifact_value(owner_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(unselected_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(concurrent_ref["artifact_id"]) is not None
    release_diagnostic = owner.diagnostics["action_artifact_release"]
    assert release_diagnostic["status"] == ("deferred" if failure_stage == "promotion" else "released")
    assert release_diagnostic["scope"] == {"kind": "agent_execution", "id": owner.id}
    assert release_diagnostic["preserved_artifact_ids"] == (
        [owner_ref["artifact_id"]] if failure_stage == "promotion" else []
    )
    retention_diagnostics = owner.diagnostics["workspace_retention"]["diagnostics"]
    assert retention_diagnostics
    assert all(len(str(item.get("message", ""))) <= 360 for item in retention_diagnostics)
