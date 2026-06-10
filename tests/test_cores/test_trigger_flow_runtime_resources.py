import asyncio
import copy
import json
from typing import Any, Callable, cast

import pytest

from agently import Agently, TriggerFlow, TriggerFlowEventData, TriggerFlowRuntimeData
from agently.base import execution_environment
from agently.types.plugins import CheckpointStore, RuntimeEventStore
from agently.types.trigger_flow import AGGREGATION_SCOPE_META_KEY
from agently.types.trigger_flow import TRIGGER_FLOW_CHECKPOINT_KIND


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

    assert getattr(workspace, "is_materialized") is False
    assert workspace.root.parent == (tmp_path / ".agently" / "workspaces").resolve()
    assert workspace.root.name.startswith("execution-workspace-default-")
    assert not workspace.root.exists()
    assert "workspace" in state["resource_keys"]


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
    checkpoint_ref = await execution.async_save_checkpoint(step_id="shared-bound")
    runtime_events = await shared_workspace.query_runtime_events(execution.id)

    assert execution.require_runtime_resource("workspace") is shared_workspace
    assert snapshot["value"] == "hello"
    assert checkpoint_ref["collection"] == "checkpoints"
    assert checkpoint_ref["scope"]["step_id"] == "shared-bound"
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
    assert state["checkpoint"]["kind"] == TRIGGER_FLOW_CHECKPOINT_KIND
    requirements = {
        requirement["key"]: requirement
        for requirement in state["checkpoint"]["resource_requirements"]
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
    report = missing_resource_execution.inspect_rehydration(saved_state)
    assert report["ready"] is False
    assert report["missing_resource_keys"] == ["resume_service"]

    with pytest.raises(RuntimeError, match="missing resources"):
        flow.create_execution().load(saved_state, validate_rehydration=True)

    legacy_state = copy.deepcopy(saved_state)
    legacy_state["checkpoint"].pop("resource_requirements", None)
    legacy_state["resource_keys"] = ["resume_service"]
    legacy_state["checkpoint"]["resource_keys"] = ["resume_service"]
    legacy_report = flow.create_execution().inspect_rehydration(legacy_state)
    assert legacy_report["ready"] is False
    assert legacy_report["missing_resource_keys"] == ["resume_service"]

    missing_resource_execution = flow.create_execution()
    missing_resource_execution.load(saved_state)
    with pytest.raises(KeyError, match="missing required runtime resource"):
        await missing_resource_execution.async_continue_with(interrupt_id, {"approved": True})

    restored_execution = flow.create_execution()
    rehydration = await restored_execution.async_rehydrate(
        saved_state,
        runtime_resources={
            "resume_service": lambda payload: {
                "approved": payload["approved"],
                "source": "resource",
            }
        },
    )
    assert rehydration["ready"] is True
    await restored_execution.async_continue_with(interrupt_id, {"approved": True})
    result = await restored_execution.async_get_result(timeout=1)

    assert result == {
        "approved": True,
        "source": "resource",
    }


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
    requirements = state["checkpoint"]["resource_requirements"]
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
async def test_trigger_flow_async_rehydrate_restores_managed_execution_environment():
    flow = TriggerFlow()

    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(type="approval", interrupt_id="approval", resume_to="next")

    async def use_environment(data: TriggerFlowRuntimeData):
        sandbox = data.require_resource("managed_python")
        assert sandbox is not None
        data.state.set("answer", sandbox.run("result = value + 1")["result"])

    flow.to(pause).to(use_environment)
    execution = flow.create_execution(
        auto_close=False,
        execution_environments=[
            {
                "requirement_id": "managed-python-rehydrate-test",
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
    rehydration = await restored.async_rehydrate(saved_state)
    assert rehydration["ready"] is True
    assert rehydration["pending_environment_resource_keys"] == []

    await restored.async_continue_with("approval", {"approved": True})
    snapshot = await restored.async_close()

    assert snapshot["answer"] == 42
    assert execution_environment.list(scope="execution") == []


@pytest.mark.asyncio
async def test_trigger_flow_execution_async_save_checkpoint_uses_checkpoint_store():
    class Store:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        async def put_checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
            self.calls.append({"run_id": run_id, "state": state, "step_id": step_id})
            return {"id": "checkpoint-1", "run_id": run_id, "step_id": step_id}

    flow = TriggerFlow(name="checkpoint-store")
    execution = flow.create_execution()
    execution.set_state("value", 1)
    store = Store()

    ref = await execution.async_save_checkpoint(store)

    assert ref == {
        "id": "checkpoint-1",
        "run_id": execution.run_context.run_id,
        "step_id": f"state:{ execution._state_version }",
    }
    assert store.calls[0]["state"]["checkpoint"]["kind"] == TRIGGER_FLOW_CHECKPOINT_KIND
    assert store.calls[0]["state"]["checkpoint"]["execution_id"] == execution.id


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
    checkpoint_ref = await execution.async_save_checkpoint(step_id="auto-bound")
    runtime_events = await workspace.query_runtime_events(execution.id)

    assert snapshot["value"] == "hello"
    assert checkpoint_ref["collection"] == "checkpoints"
    assert checkpoint_ref["scope"]["step_id"] == "auto-bound"
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
    checkpoint_ref = await execution.async_save_checkpoint(step_id="lazy-bound")
    runtime_events = await workspace.query_runtime_events(execution.id)

    assert snapshot["value"] == "hello"
    assert getattr(workspace, "is_materialized") is True
    assert checkpoint_ref["collection"] == "checkpoints"
    assert checkpoint_ref["scope"]["step_id"] == "lazy-bound"
    assert runtime_events[-1]["event_type"] == "triggerflow.execution_closed"


@pytest.mark.asyncio
async def test_trigger_flow_workspace_checkpoint_restores_pause_continue_provider_path(tmp_path):
    agent = Agently.create_agent("runtime-workspace-provider-pause").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    flow = TriggerFlow(name="runtime-workspace-provider-pause")

    async def ask_for_approval(data: TriggerFlowRuntimeData):
        await data.async_set_state("draft", {"topic": data.value}, emit=False)
        return await data.async_pause_for(
            type="approval",
            payload={"question": "approve?"},
            interrupt_id="approval",
            resume_to="next",
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

    checkpoint_ref = await execution.async_save_checkpoint(step_id="waiting-approval")
    latest_ref = await workspace.latest_checkpoint(execution.run_context.run_id)
    assert latest_ref is not None
    assert latest_ref["id"] == checkpoint_ref["id"]
    checkpoint_state = await workspace.get_data(latest_ref)
    assert checkpoint_state["checkpoint"]["interrupts"]["approval"]["status"] == "waiting"

    restored = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    rehydration = await restored.async_rehydrate(
        checkpoint_state,
        runtime_resources={"workspace": workspace},
    )
    assert rehydration["ready"] is True
    assert restored.id == execution.id

    await restored.async_continue_with(
        "approval",
        {"approved": True},
        resume_request_id="approval-webhook-1",
        actor="reviewer",
    )
    snapshot = await restored.async_close()
    resumed_ref = await restored.async_save_checkpoint(step_id="after-approval")
    resumed_state = await workspace.get_data(resumed_ref)
    runtime_events = await workspace.query_runtime_events(restored.id)
    event_types = [event["event_type"] for event in runtime_events]

    assert snapshot["final"] == {
        "draft": {"topic": "pricing"},
        "approval": {"approved": True},
    }
    assert (
        resumed_state["checkpoint"]["resume_ledger"]["approval"]["approval-webhook-1"]["status"]
        == "accepted"
    )
    assert "triggerflow.interrupt_raised" in event_types
    assert "triggerflow.execution_resumed" in event_types


@pytest.mark.asyncio
async def test_trigger_flow_workspace_checkpoint_restores_when_join_provider_path(tmp_path):
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
    checkpoint_ref = await execution.async_save_checkpoint(step_id="after-left")
    checkpoint_state = await workspace.get_data(checkpoint_ref)
    durable_when_states = checkpoint_state["checkpoint"]["durable_system_state"]["when_states"]
    signal_scope_keys = [
        scope_key
        for when_state in durable_when_states.values()
        for scope_key in when_state.keys()
        if str(scope_key).startswith("signal:")
    ]
    assert len(signal_scope_keys) == 1
    aggregation_scope = signal_scope_keys[0].removeprefix("signal:")

    restored = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
    rehydration = await restored.async_rehydrate(
        checkpoint_state,
        runtime_resources={"workspace": workspace},
    )
    assert rehydration["ready"] is True
    await restored.async_emit(
        "B",
        {"right": "task-1"},
        _meta={AGGREGATION_SCOPE_META_KEY: aggregation_scope},
    )
    joined_ref = await restored.async_save_checkpoint(step_id="after-join")
    joined_state = await workspace.get_data(joined_ref)
    runtime_events = await workspace.query_runtime_events(restored.id)

    assert restored.get_state("joined") == {
        "event": {
            "A": {"left": "task-1"},
            "B": {"right": "task-1"},
        }
    }
    assert joined_state["checkpoint"]["runtime_data"]["joined"] == restored.get_state("joined")
    assert any(
        (
            event["event"].get("payload", {}).get("META", {}).get(AGGREGATION_SCOPE_META_KEY)
            == aggregation_scope
        )
        for event in runtime_events
    )
    assert any(event["event_type"] == "triggerflow.signal" for event in runtime_events)


@pytest.mark.asyncio
async def test_trigger_flow_distributed_checkpoint_fails_closed_for_local_workspace(tmp_path):
    agent = Agently.create_agent("distributed-workspace-provider").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    flow = TriggerFlow(name="distributed-provider-check")
    execution = flow.create_execution()
    execution.set_checkpoint_store(cast(CheckpointStore, workspace))
    execution.set_runtime_event_store(cast(RuntimeEventStore, workspace))

    with pytest.raises(RuntimeError, match="missing capabilities: supports_cas, supports_lease"):
        await execution.async_save_checkpoint(require_distributed_provider=True)


@pytest.mark.asyncio
async def test_trigger_flow_distributed_checkpoint_accepts_capable_provider():
    class DistributedStore:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

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

        async def put_checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
            self.calls.append({"run_id": run_id, "state": state, "step_id": step_id})
            return {"id": "checkpoint-1", "run_id": run_id, "step_id": step_id}

        async def append_runtime_event(self, *args: Any, **kwargs: Any):
            return {"id": "event-1"}

    store = DistributedStore()
    flow = TriggerFlow(name="distributed-capable-provider")
    execution = flow.create_execution()
    execution.set_runtime_event_store(cast(Any, store))

    ref = await execution.async_save_checkpoint(store, require_distributed_provider=True)

    assert ref["id"] == "checkpoint-1"
    assert store.calls[0]["state"]["checkpoint"]["kind"] == TRIGGER_FLOW_CHECKPOINT_KIND
