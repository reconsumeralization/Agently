import asyncio
import copy
import json
from typing import Any, Callable, cast

import pytest

from agently import Agently, TriggerFlow, TriggerFlowEventData, TriggerFlowRuntimeData
from agently.base import execution_environment
from agently.core.workspace._defaults import script_scope
from agently.types.data import RunContext
from agently.types.plugins import ExecutionSnapshotStore, RuntimeEventStore
from agently.types.trigger_flow import AGGREGATION_SCOPE_META_KEY
from agently.types.trigger_flow import TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND


def test_trigger_flow_runtime_data_alias_is_exported():
    assert TriggerFlowRuntimeData is TriggerFlowEventData


@pytest.mark.asyncio
async def test_trigger_flow_runtime_data_namespaces_and_flow_resources():
    flow = TriggerFlow()
    flow.update_runtime_resources(logger="flow-logger")

    async def prepare(data: TriggerFlowRuntimeData):
        data.state.set("draft", {"topic": data.value})
        data.flow_state.set("shared_flag", True)
        data.state.set("shared_flag", data.flow_state.get("shared_flag"))
        data.state.set("logger", data.require_resource("logger"))

    flow.to(prepare)
    result = await flow.async_start("pricing")

    assert result == {
        "draft": {"topic": "pricing"},
        "shared_flag": True,
        "logger": "flow-logger",
    }


@pytest.mark.asyncio
async def test_trigger_flow_auto_close_waits_for_in_flight_start(monkeypatch):
    flow = TriggerFlow()

    async def prepare(data: TriggerFlowRuntimeData):
        data.state.set("draft", {"topic": data.value})

    flow.to(prepare)
    execution = flow.create_execution(auto_close=True, auto_close_timeout=0.0)
    original_emit_runtime_event = execution._emit_runtime_event
    observed_idle_states = []

    async def slow_start_event(event_type, *args, **kwargs):
        if event_type in {"triggerflow.execution_started", "triggerflow.signal"}:
            observed_idle_states.append(execution.is_idle())
            await asyncio.sleep(0.08)
        return await original_emit_runtime_event(event_type, *args, **kwargs)

    monkeypatch.setattr(execution, "_emit_runtime_event", slow_start_event)

    await execution._async_run_start("pricing")
    result = await execution.async_close(timeout=1)

    assert observed_idle_states
    assert not any(observed_idle_states)
    assert result == {"draft": {"topic": "pricing"}}


def test_trigger_flow_execution_binds_lazy_default_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = TriggerFlow(name="execution-workspace-default")

    execution = flow.create_execution()
    workspace = cast(Any, execution.require_runtime_resource("workspace"))
    state = execution.save()
    expected_root = tmp_path / ".agently" / "workspaces" / "scripts" / script_scope()

    assert getattr(workspace, "is_materialized") is False
    assert workspace.root == expected_root.resolve()
    assert workspace.files_root == (expected_root / "files" / "executions" / execution.id).resolve()
    assert not workspace.root.exists()
    assert "workspace" in state["resource_keys"]


@pytest.mark.asyncio
async def test_trigger_flow_default_executions_share_physical_workspace_db_and_isolate_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = TriggerFlow(name="execution-workspace-shared-default")

    first = flow.create_execution()
    second = flow.create_execution()
    first_workspace = cast(Any, first.require_runtime_resource("workspace"))
    second_workspace = cast(Any, second.require_runtime_resource("workspace"))

    assert first_workspace.root == second_workspace.root
    assert first_workspace.files_root != second_workspace.files_root
    assert first_workspace.files_root.name == first.id
    assert second_workspace.files_root.name == second.id

    await first_workspace.put("first execution", collection="observations", kind="execution_probe")
    await second_workspace.put("second execution", collection="observations", kind="execution_probe")

    assert len(list((tmp_path / ".agently" / "workspaces" / "scripts").glob("**/workspace.db"))) == 1


def test_trigger_flow_default_workspace_uses_parent_session_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = TriggerFlow(name="execution-workspace-session-default")
    parent = RunContext.create(run_kind="agent_execution", session_id="issue-123")

    execution = flow.create_execution(parent_run_context=parent)
    workspace = cast(Any, execution.require_runtime_resource("workspace"))

    assert workspace.root == (tmp_path / ".agently" / "workspaces" / "sessions" / "issue-123").resolve()
    assert workspace.files_root == (workspace.root / "files" / "executions" / execution.id).resolve()


def test_trigger_flow_execution_can_disable_default_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    flow = TriggerFlow(name="execution-workspace-disabled")

    execution = flow.create_execution(
        runtime_resources={
            "workspace": object(),
            "tool": "kept",
        },
        workspace=False,
    )
    state = execution.save()

    assert execution.require_runtime_resource("tool") == "kept"
    assert "workspace" not in execution.get_runtime_resources()
    assert "workspace" not in state["resource_keys"]


@pytest.mark.asyncio
async def test_trigger_flow_execution_workspace_argument_uses_shared_provider(tmp_path):
    shared_workspace = Agently.create_workspace(tmp_path / "shared-execution-workspace")
    flow = TriggerFlow(name="execution-workspace-shared")

    async def remember(data: TriggerFlowRuntimeData):
        await data.async_set_state("value", data.value)

    flow.to(remember)
    execution = flow.create_execution(workspace=shared_workspace)

    snapshot = await execution.async_start("hello")
    snapshot_ref = await execution.async_save(step_id="shared-bound")
    runtime_events = await shared_workspace.query_runtime_events(execution.id)

    assert execution.require_runtime_resource("workspace") is shared_workspace
    assert snapshot["value"] == "hello"
    assert snapshot_ref["collection"] == "checkpoints"
    assert snapshot_ref["scope"]["step_id"] == "shared-bound"
    assert runtime_events[-1]["event_type"] == "triggerflow.execution_closed"


@pytest.mark.asyncio
async def test_trigger_flow_execution_resources_override_flow_defaults():
    flow = TriggerFlow()
    flow.update_runtime_resources(tool_name="flow-tool")

    async def inspect_resource(data: TriggerFlowRuntimeData):
        data.state.set("tool_name", data.require_resource("tool_name"))

    flow.to(inspect_resource)

    assert await flow.async_start("start") == {"tool_name": "flow-tool"}
    assert await flow.async_start("start", runtime_resources={"tool_name": "execution-tool"}) == {
        "tool_name": "execution-tool"
    }


@pytest.mark.asyncio
async def test_trigger_flow_runtime_data_set_resource_is_execution_scoped():
    flow = TriggerFlow()

    async def remember(data: TriggerFlowRuntimeData):
        data.set_resource("token", data.value)
        return data.value

    async def read_token(data: TriggerFlowRuntimeData):
        return data.require_resource("token")

    flow.to(remember).to(read_token).end()

    execution = flow.create_execution()
    await execution.async_start("alpha", wait_for_result=False)
    result = await execution.async_get_result(timeout=1)

    assert result == "alpha"

    another_execution = flow.create_execution()
    with pytest.raises(KeyError, match="missing required runtime resource"):
        another_execution.require_runtime_resource("token")


@pytest.mark.asyncio
async def test_trigger_flow_execution_save_records_resource_keys_only():
    flow = TriggerFlow()
    flow.update_runtime_resources(flow_tool=object())
    flow.declare_resource_requirement("flow_tool", metadata={"declared": True})

    async def passthrough(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(passthrough).end()
    execution = flow.create_execution(runtime_resources={"execution_logger": object()})
    await execution.async_start("ok", wait_for_result=False)
    state = execution.save()

    assert sorted(state["resource_keys"]) == ["execution_logger", "flow_tool", "workspace"]
    assert state["kind"] == TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND
    requirements = {
        requirement["key"]: requirement
        for requirement in state["resource_requirements"]
        if requirement["kind"] == "runtime_resource"
    }
    assert requirements["execution_logger"]["source"] == "execution"
    assert requirements["execution_logger"]["metadata"]["scope"] == "execution"
    assert requirements["flow_tool"]["source"] == "flow"
    assert requirements["flow_tool"]["metadata"]["scope"] == "flow"
    assert requirements["flow_tool"]["metadata"]["declared"] is True
    assert "runtime_resources" not in state
    json.dumps(state)


@pytest.mark.asyncio
async def test_trigger_flow_execution_load_requires_reinjecting_runtime_resources():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "continue?"},
            resume_event="Resume",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        service = cast(Callable[[Any], Any], data.require_resource("resume_service"))
        return service(data.value)

    flow.declare_resource_requirement("resume_service")
    flow.to(ask_feedback)
    flow.when("Resume").to(finalize).end()

    execution = await flow.async_start_execution("topic", wait_for_result=False)
    interrupt_id = next(iter(execution.get_pending_interrupts()))
    saved_state = execution.save()

    missing_resource_execution = flow.create_execution()
    report = missing_resource_execution.inspect_load(saved_state)
    assert report["ready"] is False
    assert report["missing_resource_keys"] == ["resume_service"]

    with pytest.raises(RuntimeError, match="missing resources"):
        flow.create_execution().load(saved_state, validate_resources=True)

    missing_resource_execution = flow.create_execution()
    missing_resource_execution.load(saved_state)
    with pytest.raises(KeyError, match="missing required runtime resource"):
        await missing_resource_execution.async_continue_with(interrupt_id, {"approved": True})

    restored_execution = flow.create_execution()
    load = await restored_execution.async_load(
        saved_state,
        runtime_resources={
            "resume_service": lambda payload: {
                "approved": payload["approved"],
                "source": "resource",
            }
        },
    )
    assert load["ready"] is True
    await restored_execution.async_continue_with(interrupt_id, {"approved": True})
    result = await restored_execution.async_get_result(timeout=1)

    assert result == {
        "approved": True,
        "source": "resource",
    }


@pytest.mark.asyncio
async def test_trigger_flow_async_load_restores_resource_with_importable_resolver():
    flow = TriggerFlow()
    flow.declare_resource_requirement(
        "resume_service",
        resolver="tests.trigger_flow_resource_resolvers:restore_resume_service",
        provider_kind="callable",
        config_ref="settings://resume-service",
        resolver_version="1",
        resolver_fingerprint="test-fingerprint",
    )

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            interrupt_id="approval",
            resume_to="next",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        service = cast(Callable[[Any], Any], data.require_resource("resume_service"))
        return service(data.value)

    flow.to(ask_feedback).to(finalize).end()
    execution = await flow.async_start_execution("topic", wait_for_result=False)
    saved_state = execution.save()

    requirements = {
        requirement["key"]: requirement
        for requirement in saved_state["resource_requirements"]
        if requirement["kind"] == "runtime_resource"
    }
    assert requirements["resume_service"]["resolver"] == (
        "tests.trigger_flow_resource_resolvers:restore_resume_service"
    )
    assert requirements["resume_service"]["provider_kind"] == "callable"
    assert requirements["resume_service"]["config_ref"] == "settings://resume-service"
    assert requirements["resume_service"]["resolver_version"] == "1"
    assert requirements["resume_service"]["resolver_fingerprint"] == "test-fingerprint"
    assert "runtime_resources" not in saved_state
    json.dumps(saved_state)

    report = flow.create_execution().inspect_load(saved_state)
    assert report["ready"] is False
    assert report["status"] == "pending_resources"
    assert report["pending_resolver_keys"] == ["resume_service"]
    assert report["missing_resource_keys"] == ["resume_service"]

    with pytest.raises(RuntimeError, match="missing resources: \\['resume_service'\\]"):
        flow.create_execution().load(saved_state, validate_resources=True)

    restored = flow.create_execution()
    load = await restored.async_load(saved_state)
    assert load["ready"] is True
    assert "resume_service" in load["resolved_resource_keys"]
    assert any(
        diagnostic["code"] == "triggerflow.load.resolver_resolved_resource"
        for diagnostic in load["diagnostics"]
    )

    await restored.async_continue_with("approval", {"approved": True})
    result = await restored.async_get_result(timeout=1)

    assert result == {
        "approved": True,
        "source": "resume_service",
    }


@pytest.mark.asyncio
async def test_trigger_flow_load_missing_resolver_fails_closed():
    flow = TriggerFlow()
    flow.declare_resource_requirement(
        "resume_service",
        resolver="tests.trigger_flow_missing_resolvers:create",
    )

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(type="human_input", interrupt_id="approval", resume_to="next")

    flow.to(ask_feedback).end()
    execution = await flow.async_start_execution("topic", wait_for_result=False)
    saved_state = execution.save()

    report = flow.create_execution().inspect_load(saved_state)
    assert report["ready"] is False
    assert report["missing_resource_keys"] == ["resume_service"]
    assert any(
        diagnostic["code"] == "triggerflow.load.missing_resolver"
        for diagnostic in report["diagnostics"]
    )

    with pytest.raises(RuntimeError, match="resource resolver failed"):
        await flow.create_execution().async_load(saved_state)


@pytest.mark.asyncio
async def test_trigger_flow_load_unhealthy_resource_fail_closed_and_fail_open():
    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(type="human_input", interrupt_id="approval", resume_to="next")

    fail_closed_flow = TriggerFlow()
    fail_closed_flow.declare_resource_requirement(
        "resume_service",
        resolver="tests.trigger_flow_resource_resolvers:unhealthy_resume_service",
        fail_policy="fail_closed",
    )
    fail_closed_flow.to(ask_feedback).end()
    fail_closed_execution = await fail_closed_flow.async_start_execution("topic", wait_for_result=False)
    fail_closed_state = fail_closed_execution.save()

    with pytest.raises(RuntimeError, match="resource resolver failed"):
        await fail_closed_flow.create_execution().async_load(fail_closed_state)

    fail_open_flow = TriggerFlow()
    fail_open_flow.declare_resource_requirement(
        "resume_service",
        resolver="tests.trigger_flow_resource_resolvers:unhealthy_resume_service",
        fail_policy="fail_open",
    )
    fail_open_flow.to(ask_feedback).end()
    fail_open_execution = await fail_open_flow.async_start_execution("topic", wait_for_result=False)
    fail_open_state = fail_open_execution.save()

    report = fail_open_flow.create_execution().inspect_load(fail_open_state)
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["pending_resolver_keys"] == ["resume_service"]

    load = await fail_open_flow.create_execution().async_load(fail_open_state)
    assert load["ready"] is True
    assert load["unresolved_resource_keys"] == ["resume_service"]
    assert any(
        diagnostic["code"] == "triggerflow.load.unhealthy_resource"
        and diagnostic["severity"] == "warning"
        for diagnostic in load["diagnostics"]
    )


@pytest.mark.asyncio
async def test_trigger_flow_pause_for_validates_external_wait_template():
    execution = TriggerFlow().create_execution(auto_close=False)

    with pytest.raises(ValueError, match="wait_mode"):
        await execution.async_pause_for(wait_mode="background")

    with pytest.raises(ValueError, match="hot_wait_timeout"):
        await execution.async_pause_for(hot_wait_timeout=-1)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_mode", ["connected", "disconnected"])
async def test_trigger_flow_pause_for_persists_connected_and_disconnected_wait_modes(wait_mode):
    execution = TriggerFlow().create_execution(auto_close=False)
    interrupt_id = f"approval-{ wait_mode }"

    try:
        await execution.async_pause_for(
            type="exchange", exchange_kind="approval",
            interrupt_id=interrupt_id,
            wait_mode=wait_mode,
            channel_id=f"{ wait_mode }-channel",
            provider_id="approval-provider",
            hot_wait_timeout=10,
            cold_persistence_policy="persist",
        )
        request = execution.get_pending_interrupts()[interrupt_id]["external_wait_request"]
        assert request["exchange_kind"] == "approval"
        assert request["wait_mode"] == wait_mode
        assert request["channel_id"] == f"{ wait_mode }-channel"
        assert request["provider_id"] == "approval-provider"
        assert request["dispatch_state"] == "exposed"

        saved_state = execution.save()
        saved_request = saved_state["interrupts"][interrupt_id]["external_wait_request"]
        assert saved_request["wait_mode"] == wait_mode
        assert saved_request["cold_persistence_policy"] == "persist"

        restored = TriggerFlow().create_execution(auto_close=False)
        restored.load(saved_state)
        restored_request = restored.get_pending_interrupts()[interrupt_id]["external_wait_request"]
        assert restored_request["wait_mode"] == wait_mode
        assert restored_request["channel_id"] == f"{ wait_mode }-channel"
    finally:
        await execution.async_close(pending_interrupts="cancel")


@pytest.mark.asyncio
async def test_trigger_flow_execution_exchange_provider_publishes_external_wait_request(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "exchange-provider")
    published: list[dict[str, Any]] = []

    class ExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            published.append(
                {
                    "execution_id": execution_id,
                    "request": request,
                    "interrupt": interrupt,
                }
            )
            return {
                "exchange_id": "exchange-provider-1",
                "provider_metadata": {"published": True, "transport": "test"},
                "audit_metadata": {"provider_request_id": "provider-request-1"},
            }

    flow = TriggerFlow(name="execution-exchange-provider")

    async def ask_for_approval(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            interrupt_id="approval",
            resume_to="next",
            channel_id="ops",
            provider_id="exchange-provider",
            wait_mode="disconnected",
        )

    flow.to(ask_for_approval)
    execution = flow.create_execution(
        auto_close=False,
        runtime_resources={
            "workspace": workspace,
            "execution_exchange_provider": ExchangeProvider(),
        },
    )

    try:
        await execution.async_start({"request": "publish"})
        pending = execution.get_pending_interrupts()["approval"]
        request = copy.deepcopy(pending["external_wait_request"])
        snapshot_state = execution.save()
        runtime_events = await workspace.query_runtime_events(execution.id)
    finally:
        await execution.async_close(pending_interrupts="cancel")

    assert len(published) == 1
    assert published[0]["execution_id"] == execution.id
    assert published[0]["request"]["dispatch_state"] == "persisted"
    assert published[0]["request"]["channel_id"] == "ops"
    assert published[0]["interrupt"]["id"] == "approval"
    assert request["dispatch_state"] == "exposed"
    assert request["exchange_id"] == "exchange-provider-1"
    assert request["provider_metadata"] == {"published": True, "transport": "test"}
    assert request["audit_metadata"]["provider_request_id"] == "provider-request-1"
    saved_request = snapshot_state["interrupts"]["approval"]["external_wait_request"]
    assert saved_request["exchange_id"] == "exchange-provider-1"
    assert saved_request["provider_metadata"]["published"] is True
    assert any(
        event["event_type"] == "triggerflow.interrupt_exposed"
        and event["exchange_id"] == "exchange-provider-1"
        for event in runtime_events
    )


@pytest.mark.asyncio
async def test_trigger_flow_execution_exchange_provider_failure_marks_exposure_failed():
    class FailingExchangeProvider:
        async def publish_request(self, execution_id, request, *, interrupt):
            raise RuntimeError("exchange provider offline")

    execution = TriggerFlow().create_execution(
        auto_close=False,
        runtime_resources={"execution_exchange_provider": FailingExchangeProvider()},
    )

    with pytest.raises(RuntimeError, match="exchange provider offline"):
        await execution.async_pause_for(
            type="exchange", exchange_kind="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    pending = execution.get_pending_interrupts()["approval"]
    request = pending["external_wait_request"]
    assert request["dispatch_state"] == "exposure_failed"
    assert request["audit_metadata"]["error"] == "exchange provider offline"


@pytest.mark.asyncio
async def test_trigger_flow_condition_handler_can_use_runtime_resources():
    flow = TriggerFlow()

    def should_take_left(data: TriggerFlowRuntimeData):
        return bool(data.require_resource("take_left"))

    async def left(data: TriggerFlowRuntimeData):
        data.state.set("branch", "left")

    async def right(data: TriggerFlowRuntimeData):
        data.state.set("branch", "right")

    flow.if_condition(should_take_left).to(left).else_condition().to(right).end_condition()

    assert await flow.async_start("x", runtime_resources={"take_left": True}) == {"branch": "left"}
    assert await flow.async_start("x", runtime_resources={"take_left": False}) == {"branch": "right"}


@pytest.mark.asyncio
async def test_trigger_flow_config_round_trip_with_runtime_resources():
    flow = TriggerFlow(name="runtime-resources-config")

    async def render(data: TriggerFlowRuntimeData):
        renderer = cast(Callable[[Any], Any], data.require_resource("renderer"))
        data.state.set("rendered", renderer(data.value))

    flow.to(render)
    config = flow.get_flow_config()

    assert "renderer" not in json.dumps(config)

    restored = TriggerFlow()
    restored.register_chunk_handler(render)
    restored.load_flow_config(config)

    result = await restored.async_start(
        "hello",
        runtime_resources={"renderer": lambda value: value.upper()},
    )
    assert result == {"rendered": "HELLO"}


@pytest.mark.asyncio
async def test_trigger_flow_execution_environment_injects_managed_resource():
    flow = TriggerFlow()

    async def calculate(data: TriggerFlowRuntimeData):
        sandbox = data.require_resource("managed_python")
        assert sandbox is not None
        data.state.set("calculated", sandbox.run("result = value + 1")["result"])

    flow.to(calculate)

    result = await flow.async_start(
        41,
        execution_environments=[
            {
                "kind": "python",
                "scope": "execution",
                "resource_key": "managed_python",
                "config": {"base_vars": {"value": 41}},
            }
        ],
    )

    assert result == {"calculated": 42}
    assert execution_environment.list(scope="execution") == []


@pytest.mark.asyncio
async def test_trigger_flow_save_records_managed_execution_environment_keys():
    flow = TriggerFlow()

    async def hold_resource(data: TriggerFlowRuntimeData):
        data.state.set("has_resource", data.require_resource("managed_python") is not None)

    flow.to(hold_resource).end()

    execution = flow.create_execution(
        auto_close=False,
        execution_environments=[
            {
                "requirement_id": "managed-python-save-test",
                "kind": "python",
                "scope": "execution",
                "resource_key": "managed_python",
            }
        ],
    )
    await execution.async_start("start", wait_for_result=False)
    state = execution.save()

    assert "managed_python" in state["managed_resource_keys"]
    assert "managed-python-save-test" in state["execution_environment_requirement_ids"]
    requirements = state["resource_requirements"]
    environment_requirements = [
        requirement
        for requirement in requirements
        if requirement["kind"] == "execution_environment_requirement"
    ]
    assert environment_requirements[0]["metadata"]["resource_key"] == "managed_python"
    assert environment_requirements[0]["metadata"]["requirement"]["requirement_id"] == "managed-python-save-test"

    await execution.async_close()
    assert execution_environment.list(scope="execution") == []


@pytest.mark.asyncio
async def test_trigger_flow_async_load_restores_managed_execution_environment():
    flow = TriggerFlow()

    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(type="exchange", exchange_kind="approval", interrupt_id="approval", resume_to="next")

    async def use_environment(data: TriggerFlowRuntimeData):
        sandbox = data.require_resource("managed_python")
        assert sandbox is not None
        data.state.set("answer", sandbox.run("result = value + 1")["result"])

    flow.to(pause).to(use_environment)
    execution = flow.create_execution(
        auto_close=False,
        execution_environments=[
            {
                "requirement_id": "managed-python-load-test",
                "kind": "python",
                "scope": "execution",
                "resource_key": "managed_python",
                "config": {"base_vars": {"value": 41}},
            }
        ],
    )
    await execution.async_start("start")
    saved_state = execution.save()
    await execution.async_close(pending_interrupts="cancel")

    restored = flow.create_execution(auto_close=False)
    load = await restored.async_load(saved_state)
    assert load["ready"] is True
    assert load["pending_environment_resource_keys"] == []

    await restored.async_continue_with("approval", {"approved": True})
    snapshot = await restored.async_close()

    assert snapshot["answer"] == 42
    assert execution_environment.list(scope="execution") == []


@pytest.mark.asyncio
async def test_trigger_flow_execution_async_save_uses_snapshot_store():
    class Store:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        async def put_snapshot(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
            self.calls.append({"run_id": run_id, "state": state, "step_id": step_id})
            return {"id": "snapshot-1", "run_id": run_id, "step_id": step_id}

    flow = TriggerFlow(name="snapshot-store")
    execution = flow.create_execution()
    execution.set_state("value", 1)
    store = Store()

    ref = await execution.async_save(store)

    assert ref == {
        "id": "snapshot-1",
        "run_id": execution.run_context.run_id,
        "step_id": f"state:{ execution._state_version }",
    }
    assert store.calls[0]["state"]["kind"] == TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND
    assert store.calls[0]["state"]["execution_id"] == execution.id


@pytest.mark.asyncio
async def test_trigger_flow_runtime_workspace_resource_binds_durable_provider_ports(tmp_path):
    agent = Agently.create_agent("runtime-workspace-provider").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    flow = TriggerFlow(name="runtime-workspace-provider")

    async def remember(data: TriggerFlowRuntimeData):
        await data.async_set_state("value", data.value)

    flow.to(remember)
    execution = flow.create_execution(runtime_resources={"workspace": workspace})

    snapshot = await execution.async_start("hello")
    snapshot_ref = await execution.async_save(step_id="auto-bound")
    runtime_events = await workspace.query_runtime_events(execution.id)

    assert snapshot["value"] == "hello"
    assert snapshot_ref["collection"] == "checkpoints"
    assert snapshot_ref["scope"]["step_id"] == "auto-bound"
    assert runtime_events
    assert runtime_events[0]["event_type"] == "triggerflow.definition_declared"
    assert runtime_events[-1]["event_type"] == "triggerflow.execution_closed"


@pytest.mark.asyncio
async def test_trigger_flow_runtime_workspace_resource_materializes_lazy_provider(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = Agently.create_agent("runtime-lazy-workspace-provider")
    workspace = agent.workspace
    assert getattr(workspace, "is_materialized") is False

    flow = TriggerFlow(name="runtime-lazy-workspace-provider")

    async def remember(data: TriggerFlowRuntimeData):
        await data.async_set_state("value", data.value)

    flow.to(remember)
    execution = flow.create_execution(runtime_resources={"workspace": workspace})

    snapshot = await execution.async_start("hello")
    snapshot_ref = await execution.async_save(step_id="lazy-bound")
    runtime_events = await workspace.query_runtime_events(execution.id)

    assert snapshot["value"] == "hello"
    assert getattr(workspace, "is_materialized") is True
    assert snapshot_ref["collection"] == "checkpoints"
    assert snapshot_ref["scope"]["step_id"] == "lazy-bound"
    assert runtime_events[-1]["event_type"] == "triggerflow.execution_closed"


@pytest.mark.asyncio
async def test_trigger_flow_workspace_snapshot_restores_pause_continue_provider_path(tmp_path):
    agent = Agently.create_agent("runtime-workspace-provider-pause").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    flow = TriggerFlow(name="runtime-workspace-provider-pause")

    async def ask_for_approval(data: TriggerFlowRuntimeData):
        await data.async_set_state("draft", {"topic": data.value}, emit=False)
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            payload={"question": "approve?"},
            interrupt_id="approval",
            resume_to="next",
            channel_id="ops-approval-channel",
            provider_id="approval-router",
            wait_mode="connected_then_disconnected",
            hot_wait_timeout=30.0,
            cold_persistence_policy="persist",
            request_payload_schema={"type": "object", "required": ["question"]},
            response_payload_schema={"type": "object", "required": ["approved"]},
            audit_metadata={"exchange_id": "approval-exchange-1"},
        )

    async def finalize(data: TriggerFlowRuntimeData):
        await data.async_set_state(
            "final",
            {
                "draft": data.get_state("draft"),
                "approval": data.value,
            },
            emit=False,
        )

    flow.to(ask_for_approval).to(finalize)
    execution = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    await execution.async_start("pricing")
    assert "approval" in execution.get_pending_interrupts()

    snapshot_ref = await execution.async_save(step_id="waiting-approval")
    latest_ref = await workspace.latest_snapshot(execution.run_context.run_id)
    assert latest_ref is not None
    assert latest_ref["id"] == snapshot_ref["id"]
    snapshot_state = await workspace.get_data(latest_ref)
    assert snapshot_state["interrupts"]["approval"]["status"] == "waiting"
    pending_wait = snapshot_state["interrupts"]["approval"]["external_wait_request"]
    assert pending_wait["request_id"] == "approval"
    assert pending_wait["exchange_kind"] == "approval"
    assert pending_wait["channel_id"] == "ops-approval-channel"
    assert pending_wait["provider_id"] == "approval-router"
    assert pending_wait["wait_mode"] == "connected_then_disconnected"
    assert pending_wait["hot_wait_timeout"] == 30.0
    assert pending_wait["dispatch_state"] == "exposed"
    assert pending_wait["cold_persistence_policy"] == "persist"
    assert pending_wait["request_payload_schema"] == {"type": "object", "required": ["question"]}
    assert pending_wait["response_payload_schema"] == {"type": "object", "required": ["approved"]}
    assert pending_wait["audit_metadata"]["exchange_id"] == "approval-exchange-1"
    assert pending_wait["audit_metadata"]["type"] == "exchange"
    assert pending_wait["audit_metadata"]["exchange_kind"] == "approval"

    restored = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    load = await restored.async_load(
        snapshot_state,
        runtime_resources={"workspace": workspace},
    )
    assert load["ready"] is True
    assert restored.id == execution.id

    await restored.async_continue_with(
        "approval",
        {"approved": True},
        resume_request_id="approval-webhook-1",
        actor="reviewer",
    )
    snapshot = await restored.async_close()
    resumed_ref = await restored.async_save(step_id="after-approval")
    resumed_state = await workspace.get_data(resumed_ref)
    runtime_events = await workspace.query_runtime_events(restored.id)
    event_types = [event["event_type"] for event in runtime_events]

    assert snapshot["final"] == {
        "draft": {"topic": "pricing"},
        "approval": {"approved": True},
    }
    assert (
        resumed_state["resume_ledger"]["approval"]["approval-webhook-1"]["status"]
        == "completed"
    )
    completed_wait = resumed_state["interrupts"]["approval"]["external_wait_request"]
    assert completed_wait["dispatch_state"] == "completed"
    assert completed_wait["callback_idempotency_key"] == "approval-webhook-1"
    assert completed_wait["actor_id"] == "reviewer"
    assert completed_wait["channel_id"] == "ops-approval-channel"
    assert completed_wait["provider_id"] == "approval-router"
    assert "triggerflow.interrupt_planned" in event_types
    assert "triggerflow.interrupt_persisted" in event_types
    assert "triggerflow.interrupt_exposed" in event_types
    assert "triggerflow.resume_request_accepted" in event_types
    assert "triggerflow.resume_dispatched" in event_types
    assert "triggerflow.resume_completed" in event_types
    assert "triggerflow.interrupt_raised" in event_types
    assert "triggerflow.execution_resumed" in event_types
    assert any(
        event["event_type"] == "triggerflow.interrupt_planned"
        and event["exchange_id"] == "approval-exchange-1"
        for event in runtime_events
    )
    resume_records = [
        event
        for event in runtime_events
        if event["resume_request_id"] == "approval-webhook-1"
    ]
    assert resume_records
    assert {event["actor_id"] for event in resume_records} == {"reviewer"}
    assert any(event["interrupt_id"] == "approval" for event in resume_records)


@pytest.mark.asyncio
async def test_trigger_flow_compaction_metadata_loads_and_diagnoses_mismatches(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "compacted")
    flow = TriggerFlow(name="runtime-compaction-snapshot")

    async def gate(data: TriggerFlowRuntimeData):
        await data.async_set_state("draft", {"topic": data.value}, emit=False)
        return await data.async_pause_for(type="exchange", exchange_kind="approval", interrupt_id="approval", resume_to="next")

    async def finalize(data: TriggerFlowRuntimeData):
        await data.async_set_state("final", {"approval": data.value}, emit=False)

    flow.to(gate).to(finalize)
    execution = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    await execution.async_start("pricing")
    artifact_ref = await workspace.put_artifact_ref(
        execution.id,
        {"summary": "large snapshot payload"},
        metadata={"kind": "snapshot_payload", "summary": "large snapshot payload"},
    )
    execution._record_retained_lineage_anchor(
        "anchor-1",
        sequence=1,
        event_id="event-root",
        parent_signal_id="signal-root",
        metadata={"reason": "compacted root lineage"},
    )
    execution._record_compaction_segment(
        "segment-1",
        sequence_from=1,
        sequence_to=3,
        summary="events 1-3 compacted",
        artifact_refs=[artifact_ref],
        retained_anchor_ids=["anchor-1"],
        reducer="tests.trigger_flow_resource_resolvers:restore_resume_service",
    )
    execution._record_snapshot_artifact_ref(
        artifact_ref,
        kind="snapshot_payload",
        metadata={"reason": "large-state externalized"},
    )
    execution._set_load_read_limit(2)

    bounded_records = await execution._async_read_runtime_events_for_load(sequence_from=1)
    assert len(bounded_records) == 2

    saved_state = execution.save()
    compaction = saved_state["compaction"]
    assert compaction["segments"][0]["segment_id"] == "segment-1"
    assert compaction["segments"][0]["reducer"] == "tests.trigger_flow_resource_resolvers:restore_resume_service"
    assert compaction["retained_lineage_anchors"][0]["anchor_id"] == "anchor-1"
    assert compaction["artifact_refs"][0]["kind"] == "snapshot_payload"
    assert compaction["load_policy"]["runtime_event_read_limit"] == 2
    json.dumps(saved_state)

    report = flow.create_execution().inspect_load(
        saved_state,
        runtime_resources={"workspace": workspace},
    )
    assert report["ready"] is True
    assert report["compaction"]["segments"][0]["segment_id"] == "segment-1"

    restored = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    load = await restored.async_load(
        saved_state,
        runtime_resources={"workspace": workspace},
    )
    assert load["ready"] is True
    assert restored.save()["compaction"]["segments"][0]["segment_id"] == "segment-1"

    await restored.async_continue_with("approval", {"approved": True})
    snapshot = await restored.async_close()
    assert snapshot["final"] == {"approval": {"approved": True}}

    anchor_mismatch_state = copy.deepcopy(saved_state)
    anchor_mismatch_state["compaction"]["retained_lineage_anchors"][0]["event_id"] = "changed"
    anchor_report = flow.create_execution().inspect_load(anchor_mismatch_state)
    assert anchor_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.compaction.lineage_anchor_mismatch"
        for diagnostic in anchor_report["diagnostics"]
    )

    missing_artifact_state = copy.deepcopy(saved_state)
    missing_artifact_state["compaction"]["artifact_refs"][0]["status"] = "missing"
    artifact_report = flow.create_execution().inspect_load(missing_artifact_state)
    assert artifact_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.compaction.missing_artifact"
        for diagnostic in artifact_report["diagnostics"]
    )


@pytest.mark.asyncio
async def test_trigger_flow_save_snapshot_runs_compaction_policy_reducer(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "auto-compacted")
    flow = TriggerFlow(name="runtime-auto-compaction-snapshot")

    async def prepare(data: TriggerFlowRuntimeData):
        await data.async_set_state("topic", data.value, emit=False)

    async def reducer(context: dict[str, Any]):
        records = context["records"]
        return {
            "summary": f"compacted { len(records) } runtime events",
            "artifact": {
                "event_ids": [record["event_id"] for record in records],
                "sequence_from": context["sequence_from"],
                "sequence_to": context["sequence_to"],
            },
            "retained_lineage_anchors": [
                {
                    "anchor_id": "auto-anchor-1",
                    "sequence": context["sequence_from"],
                    "event_id": records[0]["event_id"],
                    "parent_signal_id": records[0].get("parent_signal_id"),
                    "metadata": {"source": "test"},
                }
            ],
            "load_read_limit": 1,
        }

    flow.to(prepare)
    execution = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    execution.set_compaction_policy(
        min_runtime_events=1,
        reducer=reducer,
        artifact_kind="snapshot_payload",
        metadata={"policy": "test-auto"},
    )

    await execution.async_start("pricing")
    snapshot_ref = await execution.async_save(step_id="auto-compacted")
    snapshot_state = await workspace.get_data(snapshot_ref)
    compaction = snapshot_state["compaction"]

    assert compaction["segments"][0]["summary"].startswith("compacted ")
    assert compaction["segments"][0]["retained_anchor_ids"] == ["auto-anchor-1"]
    assert compaction["retained_lineage_anchors"][0]["anchor_id"] == "auto-anchor-1"
    assert compaction["artifact_refs"][0]["kind"] == "snapshot_payload"
    assert compaction["policy"]["enabled"] is True
    assert compaction["policy"]["reducer_kind"] == "callable"
    assert compaction["load_policy"]["runtime_event_read_limit"] == 1

    anchors = await workspace.retention_anchors(execution.id, anchor_type="compaction")
    assert len(anchors) == 1
    assert anchors[0]["preserved_event_ids"]

    second_ref = await execution.async_save(step_id="auto-compacted-again")
    second_state = await workspace.get_data(second_ref)
    assert len(second_state["compaction"]["segments"]) == 1


@pytest.mark.asyncio
async def test_trigger_flow_workspace_snapshot_restores_when_join_provider_path(tmp_path):
    agent = Agently.create_agent("runtime-workspace-provider-join").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    flow = TriggerFlow(name="runtime-workspace-provider-join")

    async def emit_left(data: TriggerFlowRuntimeData):
        await data.async_emit("A", {"left": data.value})

    async def joined(data: TriggerFlowRuntimeData):
        await data.async_set_state("joined", data.value, emit=False)

    flow.when("Run").to(emit_left)
    flow.when(["A", "B"], mode="and").to(joined)

    execution = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    await execution.async_emit("Run", "task-1")
    snapshot_ref = await execution.async_save(step_id="after-left")
    snapshot_state = await workspace.get_data(snapshot_ref)
    durable_when_states = snapshot_state["durable_system_state"]["when_states"]
    signal_scope_keys = [
        scope_key
        for when_state in durable_when_states.values()
        for scope_key in when_state.keys()
        if str(scope_key).startswith("signal:")
    ]
    assert len(signal_scope_keys) == 1
    aggregation_scope = signal_scope_keys[0].removeprefix("signal:")

    restored = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    load = await restored.async_load(
        snapshot_state,
        runtime_resources={"workspace": workspace},
    )
    assert load["ready"] is True
    await restored.async_emit(
        "B",
        {"right": "task-1"},
        _meta={AGGREGATION_SCOPE_META_KEY: aggregation_scope},
    )
    joined_ref = await restored.async_save(step_id="after-join")
    joined_state = await workspace.get_data(joined_ref)
    runtime_events = await workspace.query_runtime_events(restored.id)

    assert restored.get_state("joined") == {
        "event": {
            "A": {"left": "task-1"},
            "B": {"right": "task-1"},
        }
    }
    assert joined_state["runtime_data"]["joined"] == restored.get_state("joined")
    assert any(
        (
            event["event"].get("payload", {}).get("META", {}).get(AGGREGATION_SCOPE_META_KEY)
            == aggregation_scope
        )
        for event in runtime_events
    )
    assert any(event["event_type"] == "triggerflow.signal" for event in runtime_events)


@pytest.mark.asyncio
async def test_trigger_flow_distributed_snapshot_accepts_workspace_provider(tmp_path):
    agent = Agently.create_agent("distributed-workspace-provider").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    flow = TriggerFlow(name="distributed-provider-check")
    execution = flow.create_execution(
        runtime_resources={
            "snapshot_store": cast(ExecutionSnapshotStore, workspace),
            "runtime_event_store": cast(RuntimeEventStore, workspace),
        }
    )

    ref = await execution.async_save(require_distributed_provider=True)

    assert ref["collection"] == "checkpoints"
    assert workspace.capabilities()["features"]["supports_cas"] is True
    assert workspace.capabilities()["features"]["supports_lease"] is True


@pytest.mark.asyncio
async def test_trigger_flow_distributed_snapshot_fails_closed_for_missing_provider_methods():
    class MissingLeaseMethodsStore:
        def capabilities(self):
            return {
                "features": {
                    "supports_cas": True,
                    "supports_lease": True,
                    "supports_event_sequence": True,
                    "supports_range_read": True,
                    "supports_retention": True,
                }
            }

        async def put_snapshot(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
            return {"id": "snapshot-1", "run_id": run_id, "step_id": step_id}

        async def append_runtime_event(self, *args: Any, **kwargs: Any):
            return {"id": "event-1"}

    store = MissingLeaseMethodsStore()
    flow = TriggerFlow(name="distributed-provider-method-check")
    execution = flow.create_execution(
        workspace=False,
        runtime_resources={"runtime_event_store": cast(Any, store)},
    )

    with pytest.raises(RuntimeError, match="missing methods: get_snapshot"):
        await execution.async_save(store, require_distributed_provider=True)


@pytest.mark.asyncio
async def test_trigger_flow_distributed_snapshot_accepts_capable_provider():
    class DistributedStore:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        def capabilities(self):
            return {
                "features": {
                    "supports_cas": True,
                    "supports_lease": True,
                    "supports_artifact_refs": True,
                    "supports_event_sequence": True,
                    "supports_range_read": True,
                    "supports_retention": True,
                }
            }

        async def get_snapshot(self, run_id: str):
            return None

        async def put_snapshot(
            self,
            run_id: str,
            state: dict[str, Any],
            *,
            step_id: str | None = None,
            expected_state_version: int | None = None,
        ):
            self.calls.append(
                {
                    "run_id": run_id,
                    "state": state,
                    "step_id": step_id,
                    "expected_state_version": expected_state_version,
                }
            )
            return {"id": "snapshot-1", "run_id": run_id, "step_id": step_id}

        async def claim_lease(self, *args: Any, **kwargs: Any):
            return {"lease_token": "lease-1"}

        async def heartbeat_lease(self, *args: Any, **kwargs: Any):
            return {"lease_token": "lease-1"}

        async def release_lease(self, *args: Any, **kwargs: Any):
            return {"lease_token": "lease-1"}

        async def put_artifact_ref(self, *args: Any, **kwargs: Any):
            return {"id": "artifact-1"}

        async def append_runtime_event(self, *args: Any, **kwargs: Any):
            return {"id": "event-1"}

    store = DistributedStore()
    flow = TriggerFlow(name="distributed-capable-provider")
    execution = flow.create_execution(
        runtime_resources={"runtime_event_store": cast(Any, store)}
    )

    ref = await execution.async_save(store, require_distributed_provider=True)

    assert ref["id"] == "snapshot-1"
    assert store.calls[0]["state"]["kind"] == TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND


@pytest.mark.asyncio
async def test_trigger_flow_runtime_event_store_keeps_legacy_append_signature():
    class LegacyRuntimeEventStore:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        async def append_runtime_event(
            self,
            execution_id: str,
            event: Any,
            *,
            idempotency_key: str | None = None,
            node_id: str | None = None,
            aggregation_scope: str | None = None,
        ):
            self.calls.append(
                {
                    "execution_id": execution_id,
                    "event_type": event.event_type,
                    "idempotency_key": idempotency_key,
                    "node_id": node_id,
                    "aggregation_scope": aggregation_scope,
                }
            )
            return {"id": f"event-{ len(self.calls) }"}

    store = LegacyRuntimeEventStore()
    flow = TriggerFlow(name="legacy-runtime-event-store")

    def keep_result(data: TriggerFlowRuntimeData):
        data.state.set("result", data.value)

    flow.to(keep_result)
    execution = flow.create_execution(
        workspace=False,
        runtime_resources={"runtime_event_store": cast(Any, store)},
    )

    result = await execution.async_start("pricing")

    assert result == {"result": "pricing"}
    assert store.calls
    assert all(call["idempotency_key"] for call in store.calls)
