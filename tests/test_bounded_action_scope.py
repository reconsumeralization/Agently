"""Host-scope protocol regressions; no model calls or external Action effects."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agently import Agently
from agently.core.application.AgentTask import AgentTask
from agently.core.runtime import bind_runtime_context
from agently.types.data import TaskBoardCard


@pytest.fixture
def scope_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = (Agently.create_agent("bounded-scope")
             .use_task_workspace(tmp_path / "files")
             .use_record_store(tmp_path / "records", mode="read_write"))
    calls: list[str] = []

    def register(action_id: str, *, tagged: bool = True, exposed: bool = True):
        def action(value: str):
            calls.append(action_id)
            return {"value": value}

        agent.action.register_action(
            action_id=action_id, desc="Return the supplied value.",
            kwargs={"value": (str, "Known value.")}, func=action,
            tags=[f"agent-{agent.name}"] if tagged else ["different-agent"],
            expose_to_model=exposed,
            returns={"value": str}, replay_safe=True,
        )

    register("scope_safe")
    register("scope_other")
    register("scope_untagged", tagged=False)
    register("scope_hidden", exposed=False)
    parent = agent.create_execution(limits={"max_model_requests": 0})
    task = AgentTask(agent, goal="Run the selected allowed memory action.",
                     success_criteria=["Only the allowed memory action runs."], execution="flat")

    def no_request():
        raise AssertionError("This deterministic path must not create a model request")

    monkeypatch.setattr(agent, "create_temp_request", no_request)
    return agent, parent, task, calls


def command(action_id: str) -> dict[str, Any]:
    return {"action_id": action_id, "action_input": {"value": "known"}}


def plan(*ids: str, required=(), scope=None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "execution_shape": "actions", "step_instruction": "Run the allowed memory action.",
        "action_commands": [command(item) for item in ids], "required_action_ids": list(required),
    }
    if scope is not None:
        result["step_scope"] = {"allowed_capability_ids": list(scope)}
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("host_ids,step_ids,target,allowed", [
    (None, None, "scope_safe", True),
    ([], [], "scope_other", True),
    (["scope_safe"], None, "scope_safe", True),
    (["scope_safe"], None, "scope_other", False),
    (None, ["scope_safe"], "scope_other", False),
    (["scope_safe", "scope_other"], ["scope_safe"], "scope_safe", True),
    (["scope_safe", "scope_other"], ["scope_safe"], "scope_other", False),
    (["scope_safe"], ["scope_other"], "scope_other", False),
    (None, None, "scope_untagged", False),
    (None, ["scope_untagged"], "scope_untagged", False),
    (["scope_untagged"], None, "scope_untagged", True),
    (None, None, "scope_hidden", False),
    (["scope_hidden"], None, "scope_hidden", False),
])
async def test_flat_real_blocks_path_enforces_host_and_step_scope(
    scope_setup, host_ids, step_ids, target, allowed
):
    agent, parent, task, calls = scope_setup
    if host_ids is not None:
        parent.use_actions(host_ids)
    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        result, meta = await task._execute_step(
            1, plan(target, required=[target], scope=step_ids), {"items": [], "diagnostics": {}, "profile": "none"}
        )
    assert calls == ([target] if allowed else [])
    assert result["status"] == ("completed" if allowed else "failed")
    if not allowed:
        assert meta["diagnostics"][0]["code"].endswith(".action_not_allowed")


@pytest.mark.asyncio
async def test_entire_batch_is_rejected_before_first_call(scope_setup):
    agent, parent, task, calls = scope_setup
    parent.use_actions("scope_safe")
    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        result, meta = await task._try_flat_preplanned_action_calls(1, plan("scope_safe", "scope_other"))
    assert result["status"] == "failed"
    assert meta["diagnostics"][0]["code"].endswith(".action_not_allowed")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("child_ids,target,allowed", [
    (["scope_safe", "scope_other"], "scope_other", False),
    (["scope_other"], "scope_other", False),
    (["scope_other"], "scope_safe", False),
    ([], "scope_other", False),
    ([], "scope_safe", True),
    (["scope_safe"], "scope_safe", True),
])
async def test_ancestor_intersection_cannot_be_widened_or_cleared(scope_setup, child_ids, target, allowed):
    agent, parent, task, calls = scope_setup
    parent.use_actions("scope_safe")
    with bind_runtime_context(agent_execution_context=parent.execution_context):
        child = agent.create_execution().use_actions(child_ids)
    with bind_runtime_context(agent_execution_context=child.execution_context, settings=agent.settings):
        result, _meta = await task._try_flat_preplanned_action_calls(1, plan(target))
    assert result["status"] == ("completed" if allowed else "failed")
    assert calls == ([target] if allowed else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("source,target", [("host", "scope_other"), ("step", "scope_other"),
                                          ("tags", "scope_untagged"), ("hidden", "scope_hidden")])
async def test_narrow_request_cannot_offer_required_as_authority(scope_setup, source, target):
    agent, parent, task, calls = scope_setup
    if source == "host":
        parent.use_actions("scope_safe")
    step_plan = plan(required=[target], scope=["scope_safe"] if source == "step" else None)
    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        result, meta = await task._try_flat_narrow_action_command_request(1, step_plan, {})
    assert result["status"] == "failed"
    assert meta["diagnostics"][0]["code"].endswith(".required_action_unavailable")
    assert meta["diagnostics"][0]["action_planning_model_requests"] == 0
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("narrow", [False, True])
@pytest.mark.parametrize("target,allowed", [("scope_safe", True), ("scope_other", False)])
async def test_taskboard_shared_carrier_preserves_parent_scope(scope_setup, narrow, target, allowed, monkeypatch):
    agent, parent, task, calls = scope_setup
    parent.use_actions("scope_safe")
    card = TaskBoardCard.from_value({
        "id": "memory-card", "objective": "Run one memory action.", "allowed_execution_shape": "actions",
        "evidence_contract": {"requires_capability_ids": [target]},
        "metadata": {} if narrow else {"action_commands": [command(target)]},
    })
    context = SimpleNamespace(card=card, dependency_results={}, planning_policy=None)
    if narrow and allowed:
        # The allowed path's schema is verified without manufacturing a model verdict.
        with bind_runtime_context(agent_execution_context=parent.execution_context):
            contracts, unavailable = task._bounded_action_contracts([target], allowed_action_ids=[target])
        assert unavailable is None and contracts[0]["action_id"] == target
        return
    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        if narrow:
            result, meta = await task._try_taskboard_narrow_action_command_request(context, card_input_payload={})
        else:
            result, meta = await task._try_taskboard_preplanned_action_calls(context)
    assert result["status"] == ("completed" if allowed else "failed")
    assert calls == ([target] if allowed else [])
    if not allowed:
        assert meta["diagnostics"][0]["code"].endswith(
            ".required_action_unavailable" if narrow else ".action_not_allowed"
        )


def test_public_host_require_actions_still_mounts_untagged_action(scope_setup):
    agent, parent, task, _calls = scope_setup
    parent.require_actions("scope_untagged")
    with bind_runtime_context(agent_execution_context=parent.execution_context):
        contracts, unavailable = task._bounded_action_contracts(["scope_untagged"])
    assert unavailable is None
    assert contracts[0]["action_id"] == "scope_untagged"


def test_options_and_compact_snapshot_are_not_new_acl(scope_setup):
    _agent, _parent, task, _calls = scope_setup
    task.options.update({"capability_constraints": {"actions": {"allowed": ["scope_safe"], "denied": ["scope_other"]}},
                         "planner_capabilities": [{"id": "scope_safe", "kind": "action"}]})
    commands, error = task._normalize_bounded_action_commands(
        raw_commands=[command("scope_other")], required_action_ids=[], unit_label="Probe"
    )
    assert error is None and commands[0]["action_id"] == "scope_other"


@pytest.mark.asyncio
async def test_authorized_calls_keep_existing_dispatch_adapter(scope_setup, monkeypatch):
    agent, parent, task, calls = scope_setup
    parent.use_actions("scope_safe")
    original = agent.action._async_execute_action_calls
    adapter_calls: list[Any] = []

    async def adapter(*args, **kwargs):
        adapter_calls.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent.action, "_async_execute_action_calls", adapter)
    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        denied, _ = await task._try_flat_preplanned_action_calls(1, plan("scope_other"))
        assert denied["status"] == "failed" and adapter_calls == []
        allowed, meta = await task._try_flat_preplanned_action_calls(2, plan("scope_safe"))
    assert allowed["status"] == "completed" and calls == ["scope_safe"]
    assert len(adapter_calls) == 1
    assert meta["logs"]["action_logs"][0]["action_id"] == "scope_safe"


def test_custom_visible_catalog_is_respected(scope_setup, monkeypatch):
    agent, _parent, task, _calls = scope_setup
    original = agent.action.get_action_list

    def custom_catalog(tags=None):
        return [item for item in original(tags=tags) if item["action_id"] != "scope_other"]

    monkeypatch.setattr(agent.action, "get_action_list", custom_catalog)
    _commands, error = task._normalize_bounded_action_commands(
        raw_commands=[command("scope_other")], required_action_ids=[], unit_label="Probe"
    )
    assert error is not None and error[0] == "action_not_allowed"


@pytest.mark.parametrize("selection", ["required", "scope", "scope_alias", "task_required", "card"])
def test_child_selection_cannot_register_or_widen_host_scope(scope_setup, selection):
    agent, parent, task, calls = scope_setup
    parent.use_actions("scope_safe")
    selected = {"execution_shape": "actions", "required_action_ids": ["scope_untagged"]}
    if selection == "scope":
        selected = {"execution_shape": "actions", "step_scope": {"allowed_capability_ids": ["scope_untagged"]}}
    elif selection == "scope_alias":
        selected = task._normalize_step_plan({"execution_shape": "actions", "allowed_action_ids": ["scope_untagged"]})
    elif selection == "task_required":
        task.options["required_action_ids"] = ["scope_untagged"]
    elif selection == "card":
        card = TaskBoardCard.from_value({"id": "one", "objective": "Memory only.",
            "allowed_execution_shape": "actions",
            "evidence_contract": {"requires_capability_ids": ["scope_untagged"]}})
        selected = task._taskboard_card_carrier_plan(card)
    before = agent.action.action_registry.get_spec("scope_untagged")["tags"][:]
    with bind_runtime_context(agent_execution_context=parent.execution_context):
        child = agent.create_execution()
        with pytest.raises(PermissionError, match="visible execution scope"):
            task._configure_step_execution(child, selected)
    assert agent.action.action_registry.get_spec("scope_untagged")["tags"] == before
    assert child.local_action_ids == [] and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["child", "preplanned", "narrow"])
@pytest.mark.parametrize("scope,legacy,allowed", [
    ({"allowed_capability_ids": []}, ["scope_safe"], True),
    ({"allowed_capability_ids": ["scope_safe"]}, ["scope_other"], False),
    ({}, ["scope_safe"], False),
    ({}, ["scope_other"], True),
])
async def test_normalized_scope_primary_precedes_legacy_alias(
    scope_setup, monkeypatch, entry, scope, legacy, allowed,
):
    agent, parent, task, calls = scope_setup
    parent.use_actions(["scope_safe", "scope_other"])
    selected = task._normalize_step_plan({
        "execution_shape": "actions", "step_scope": scope, "allowed_action_ids": legacy,
        "required_action_ids": ["scope_other"],
        "action_commands": [] if entry == "narrow" else [command("scope_other")],
    })
    assert selected["step_scope"]["allowed_capability_ids"] == scope.get("allowed_capability_ids", legacy)

    class SchemaOffered(Exception):
        """Stop only after the real narrow schema/scope check has passed."""

    def after_schema():
        raise SchemaOffered()

    monkeypatch.setattr(agent, "create_temp_request", after_schema)
    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        if entry == "child":
            child = agent.create_execution()
            if allowed:
                task._configure_step_execution(child, selected)
                assert "scope_other" in child.local_action_ids
            else:
                with pytest.raises(PermissionError, match="declared step scope"):
                    task._configure_step_execution(child, selected)
        elif entry == "preplanned":
            result, _meta = await task._try_flat_preplanned_action_calls(1, selected)
            assert result["status"] == ("completed" if allowed else "failed")
        elif allowed:
            with pytest.raises(SchemaOffered):
                await task._try_flat_narrow_action_command_request(1, selected, {})
        else:
            result, _meta = await task._try_flat_narrow_action_command_request(1, selected, {})
            assert result["status"] == "failed"
    assert calls == (["scope_other"] if entry == "preplanned" and allowed else [])
    assert parent.execution_context.model_request_count == 0


def test_planner_snapshot_does_not_mount_unoffered_action(scope_setup):
    agent, parent, task, calls = scope_setup
    parent.use_actions("scope_safe")
    task.options["planner_capabilities"] = [{"id": "scope_untagged", "kind": "action"},
                                              {"id": "scope_safe", "kind": "action"}]
    before = agent.action.action_registry.get_spec("scope_untagged")["tags"][:]
    with bind_runtime_context(agent_execution_context=parent.execution_context):
        child = agent.create_execution()
        task._configure_step_execution(child, {"execution_shape": "actions"})
    assert agent.action.action_registry.get_spec("scope_untagged")["tags"] == before
    assert set(child.local_action_ids) <= {"scope_safe"} and calls == []


def test_required_action_cannot_extend_explicit_step_scope(scope_setup):
    agent, parent, task, calls = scope_setup
    with bind_runtime_context(agent_execution_context=parent.execution_context):
        child = agent.create_execution()
        with pytest.raises(PermissionError, match="declared step scope"):
            task._configure_step_execution(child, plan(required=["scope_other"], scope=["scope_safe"]))
    assert child.local_action_ids == [] and calls == []


@pytest.mark.parametrize("child_ids,expected", [
    ([], {"scope_safe"}), (["scope_safe", "scope_other"], {"scope_safe"}), (["scope_other"], set()),
])
def test_route_and_action_loop_share_ancestor_scope(scope_setup, child_ids, expected):
    agent, parent, task, _calls = scope_setup
    parent.use_actions("scope_safe")
    with bind_runtime_context(agent_execution_context=parent.execution_context):
        child = agent.create_execution().use_actions(child_ids)
    with bind_runtime_context(agent_execution_context=child.execution_context):
        assert {item["action_id"] for item in agent._get_scoped_action_list()} == expected
        assert {item["action_id"] for item in child.action_candidates()} == expected
        assert task._bounded_action_scope() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
@pytest.mark.parametrize("variant", ["allowed", "other", "mixed", "mutated_offer", "after_success"])
async def test_loop_batch_gate_precedes_any_handler_or_side_effect(scope_setup, flow_name, variant):
    agent, parent, _task, calls = scope_setup
    parent.use_actions("scope_safe")
    handlers, observations = [], []
    planning_count = 0

    async def planning(context, request):
        nonlocal planning_count
        planning_count += 1
        if variant == "mutated_offer":
            request["action_list"].append({"action_id": "scope_other"})
        target = "scope_safe" if variant == "allowed" or (variant == "after_success" and planning_count == 1) else "scope_other"
        commands = [command(target)]
        if variant == "mixed":
            commands.insert(0, command("scope_safe"))
        return {"next_action": "execute", "use_action": True, "action_calls": commands}

    original = agent.action.action_runtime.resolve_execution_handler(None)

    async def handler(context, request):
        handlers.append(request["action_calls"])
        return await original(context, request)

    async def observe(event):
        observations.append(event)

    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        flow = agent.action._flow_controller.create_named_action_flow(flow_name)
        records = await flow.async_run(action=agent.action, prompt=parent.request.prompt,
            settings=agent.settings, action_list=agent._get_scoped_action_list(), agent_name=agent.name,
            planning_handler=planning, execution_handler=handler, runtime_observation_handler=observe,
            max_rounds=2 if variant == "after_success" else 1)
    expected = ["scope_safe"] if variant in {"allowed", "after_success"} else []
    assert calls == expected
    assert len(handlers) == (len(expected) if flow_name == "TriggerFlowActionFlow" else 0)
    assert sum(event["kind"] == "action_started" for event in observations) == len(expected)
    if variant != "allowed":
        assert records[-1]["status"] == "blocked"
        assert records[-1]["diagnostics"][0]["code"] == "action.scope.not_offered"
        assert any(event["kind"] == "plan_ready" and event.get("stream_projection", {}).get("dispatch_confirmed") is False
                   for event in observations)
        if variant == "after_success":
            assert records[0]["action_id"] == "scope_safe" and records[0]["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
@pytest.mark.parametrize("selection", ["local", "unknown", "foreign"])
async def test_host_offered_artifact_recall_keeps_real_scope_gate(scope_setup, flow_name, selection):
    agent, parent, task, _calls = scope_setup
    parent.use_actions("scope_safe")
    scope = agent.action._artifact_scope_from_agent_execution_context(parent.execution_context)
    artifact = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="memory", artifact_type="action_output", label="Memory evidence",
        value={"value": "retained"}, artifact_scope=scope)
    parent.execution_context.set_action_artifact_recall_records(
        [{"action_id": "scope_safe", "artifact_refs": [artifact]}], source="host_test")
    key = artifact["selection_key"]
    if selection == "unknown":
        key = "sel_unknown"
    elif selection == "foreign":
        other = agent.create_execution()
        foreign_scope = agent.action._artifact_scope_from_agent_execution_context(other.execution_context)
        assert foreign_scope != scope
        foreign_artifact = agent.action._artifact_manager.register_execution_artifact(
            action_call_id="foreign-memory", artifact_type="action_output", label="Foreign evidence",
            value={"value": "foreign"}, artifact_scope=foreign_scope)
        key = foreign_artifact["selection_key"]

    async def planning(context, request):
        assert "read_action_artifact" in {item["action_id"] for item in request["action_list"]}
        return {"next_action": "execute", "use_action": True, "action_calls": [{
            "action_id": "read_action_artifact", "action_input": {
                "selection_key": key}}]}

    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        assert "read_action_artifact" in task._bounded_action_scope(["scope_safe"])
        contracts, unavailable = task._bounded_action_contracts(["read_action_artifact"])
        assert unavailable is None and contracts[0]["action_id"] == "read_action_artifact"
        assert "read_action_artifact" in {item["action_id"] for item in parent.action_candidates()}
        flow = agent.action._flow_controller.create_named_action_flow(flow_name)
        records = await flow.async_run(action=agent.action, prompt=parent.request.prompt,
            settings=agent.settings, action_list=agent._get_scoped_action_list(), agent_name=agent.name,
            planning_handler=planning, execution_handler=agent.action.action_runtime.resolve_execution_handler(None),
            max_rounds=1)
    assert records[0]["action_id"] == "read_action_artifact"
    assert records[0]["success"] is (selection == "local")


@pytest.mark.asyncio
async def test_host_direct_batch_does_not_gain_model_acl(scope_setup):
    agent, parent, _task, calls = scope_setup
    parent.use_actions("scope_safe")
    with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
        records = await agent.action._async_execute_action_calls(
            action_calls=[command("scope_other")], settings=agent.settings, concurrency=1,
            agent_name=agent.name)
    assert calls == ["scope_other"] and records[0]["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["host", "false_source", "unknown", "wider", "changed", "mixed", "duplicate"])
async def test_program_transport_needs_real_current_offered_catalog(scope_setup, variant):
    from agently.core.operation.Action.ActionProgram import build_programmatic_action_catalog

    agent, parent, _task, calls = scope_setup
    parent.use_actions("scope_safe")
    specs = agent.action.get_action_list()
    selected = "scope_other" if variant == "wider" else "scope_safe"
    catalog = dict(build_programmatic_action_catalog([item for item in specs if item["action_id"] == selected]))
    runtime = agent.action.action_runtime
    runtime._retain_programmatic_catalog(catalog)
    agent.action._ensure_programmatic_action_transport(settings=agent.settings)
    if variant == "changed":
        agent.action.register_action(action_id="scope_safe", desc="Changed registration.", kwargs={"value": str},
            func=lambda value: value, returns=str, replay_safe=True, tags=[f"agent-{agent.name}"])
    handlers = []

    async def planning(context, request):
        commands = [{"action_id": "run_action_program", "action_input": {
            "program": "return None", "description": "Check transport admission.",
            "catalog_revision": "unknown" if variant == "unknown" else catalog["catalog_revision"]},
            "source_protocol": "structured_plan" if variant == "false_source" else "programmatic"}]
        if variant in {"mixed", "duplicate"}:
            if variant == "duplicate":
                commands.append(dict(commands[0]))
            commands.append(command("scope_other"))
        return {"next_action": "execute", "use_action": True, "action_calls": commands}

    async def handler(context, request):
        # Admission-only seam probe: no provider or program execution claim.
        handlers.extend(request["action_calls"])
        return [{"action_id": "run_action_program", "ok": True, "success": True,
                 "status": "success", "data": None, "result": None}]

    try:
        with bind_runtime_context(agent_execution_context=parent.execution_context, settings=agent.settings):
            flow = agent.action._flow_controller.create_named_action_flow("TriggerFlowActionFlow")
            records = await flow.async_run(action=agent.action, prompt=parent.request.prompt,
                settings=agent.settings, action_list=agent._get_scoped_action_list(), agent_name=agent.name,
                planning_handler=planning, execution_handler=handler, max_rounds=1)
        allowed = variant in {"host", "false_source"}
        assert bool(handlers) is allowed and calls == []
        if not allowed:
            assert records[0]["status"] == "blocked"
            assert runtime._programmatic_catalogs[catalog["catalog_revision"]]["leases"] == 1
    finally:
        runtime.release_programmatic_catalog(catalog["catalog_revision"])


@pytest.mark.parametrize("variant", ["owned", "foreign", "tampered", "duplicate"])
def test_default_program_batch_rejection_only_releases_own_lease(scope_setup, variant):
    from agently.core.operation.Action.ActionProgram import build_programmatic_action_catalog

    agent, _parent, _task, _calls = scope_setup
    offered = [item for item in agent.action.get_action_list() if item["action_id"] == "scope_safe"]
    catalog = dict(build_programmatic_action_catalog(offered))
    payload = {"program": "return None", "description": "Admission only.",
               "catalog_revision": catalog["catalog_revision"]}
    catalog["_planning_scope"] = {"run_id": "foreign" if variant == "foreign" else "current", "round_index": 0}
    catalog["_action_input"] = dict(payload)
    runtime = agent.action.action_runtime
    runtime._retain_programmatic_catalog(catalog)
    agent.action._ensure_programmatic_action_transport(settings=agent.settings)
    if variant == "tampered":
        payload["program"] = "return 1"
    transport = {"action_id": "run_action_program", "action_input": payload}
    batch = [transport, command("scope_other")]
    if variant == "duplicate":
        runtime._retain_programmatic_catalog(catalog)  # A separately held second lease.
        batch.insert(0, dict(transport))
    records = agent.action._check_action_scope(batch, offered, run_id="current", round_index=0)
    assert records and records[0]["status"] == "blocked"
    remaining = runtime._programmatic_catalogs.get(catalog["catalog_revision"])
    if variant in {"foreign", "duplicate"}:
        assert remaining and remaining["leases"] == 1
        runtime.release_programmatic_catalog(catalog["catalog_revision"])
    else:
        assert remaining is None
