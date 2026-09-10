from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from agently import Agently
from agently.builtins.plugins.ActionExecutor.ProgrammaticActionExecutor import (
    ProgrammaticActionExecutor,
)
from agently.core.application.AgentExecution import AgentExecutionContext
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.operation.Action.ActionProgram import (
    build_programmatic_action_catalog,
)
from agently.core.operation.Action.ActionRegistry import ActionRegistry
from agently.core.runtime import (
    bind_runtime_context,
    get_current_tool_phase_run_context,
)
from agently.types.data import (
    PROGRAMMATIC_ACTION_TRANSPORT_ID,
    RunContext,
    TaskWorkspaceAccessRequirement,
)


class _NoopExecutor:
    kind = "review"
    sandboxed = False

    async def execute(self, **_: Any) -> Any:
        return None


def _registry_spec(action_id: str, *, name: str | None = None) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "name": name or action_id,
        "desc": "review action",
        "kwargs": {},
        "returns": (dict, "Review result."),
    }


def test_reserved_registry_id_cannot_be_replaced_via_public_register() -> None:
    registry = ActionRegistry(name="programmatic-review")
    original = _NoopExecutor()
    replacement = _NoopExecutor()
    registry._register_reserved(_registry_spec("run_action_program"), original)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        registry.register(
            _registry_spec("run_action_program", name="replacement"),  # type: ignore[arg-type]
            replacement,
            reserved=True,  # type: ignore[call-arg]
        )

    assert registry.get_executor("run_action_program") is original


def test_action_reregistration_removes_stale_tag_scope_membership() -> None:
    registry = ActionRegistry(name="programmatic-scope-review")
    executor = _NoopExecutor()
    first = _registry_spec("scoped_read")
    first["tags"] = ["agent-a"]
    registry.register(first, executor)  # type: ignore[arg-type]
    replacement = _registry_spec("scoped_read")
    replacement["tags"] = ["agent-b"]
    registry.register(replacement, executor)  # type: ignore[arg-type]

    assert registry.list_action_ids("agent-a") == []
    assert registry.list_action_ids("agent-b") == ["scoped_read"]


def test_active_programmatic_catalog_is_not_silently_evicted() -> None:
    runtime = Agently.create_agent().action.action_runtime
    runtime._retain_programmatic_catalog({"catalog_revision": "review:0", "entries": [{"action_id": "a0"}]})

    rejected = False
    for index in range(1, 66):
        try:
            runtime._retain_programmatic_catalog(
                {
                    "catalog_revision": f"review:{index}",
                    "entries": [{"action_id": f"a{index}"}],
                }
            )
        except RuntimeError:
            rejected = True
            break

    # Capacity pressure may reject a new plan, but it must not invalidate a
    # previously issued ActionCall whose catalog has not settled or released.
    if not rejected:
        assert runtime.resolve_programmatic_catalog("review:0") is not None


def test_catalog_rejects_same_contract_action_executor_replacement() -> None:
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="replacement_review_read",
        desc="Return one review value.",
        kwargs={},
        func=lambda: {"version": "first"},
        returns={"version": (str, "Implementation version.")},
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("replacement_review_read")]  # type: ignore[list-item]
    )
    agent.action.action_runtime._retain_programmatic_catalog(catalog)

    # The model-visible contract stays byte-identical, but the host capability
    # implementation has changed after the program decision was created.
    agent.action.register_action(
        action_id="replacement_review_read",
        desc="Return one review value.",
        kwargs={},
        func=lambda: {"version": "second"},
        returns={"version": (str, "Implementation version.")},
    )
    executor = ProgrammaticActionExecutor(action=agent.action)

    with pytest.raises(ValueError, match="changed|stale|replaced"):
        executor._resolve_catalog(catalog["catalog_revision"])


@pytest.mark.asyncio
async def test_live_program_binding_rejects_mid_run_action_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agently.create_agent()
    calls: list[str] = []
    agent.action.register_action(
        action_id="midrun_replacement_review",
        desc="Return one review value.",
        kwargs={},
        func=lambda: calls.append("first") or {"version": "first"},
        returns={"version": (str, "Implementation version.")},
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("midrun_replacement_review")]  # type: ignore[list-item]
    )
    agent.action.action_runtime._retain_programmatic_catalog(catalog)
    retained = agent.action.action_runtime.resolve_programmatic_catalog(catalog["catalog_revision"])
    assert retained is not None
    executor = ProgrammaticActionExecutor(action=agent.action)

    async def ignore_observation(_: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(
        agent.action,
        "_async_emit_action_flow_observation",
        ignore_observation,
    )
    scope = {"kind": "action_run", "id": "midrun-replacement-review"}
    with agent.action._artifact_manager.bind_artifact_scope(scope):
        binding = executor._bindings(
            catalog=retained,
            settings=agent.settings,
            parent_action_call_id="outer-midrun-review",
        )[0][0]
        agent.action.register_action(
            action_id="midrun_replacement_review",
            desc="Return one review value.",
            kwargs={},
            func=lambda: calls.append("second") or {"version": "second"},
            returns={"version": (str, "Implementation version.")},
        )
        with pytest.raises(Exception, match="changed|stale|replaced"):
            await binding.async_handler({})

    assert calls == []


@pytest.mark.asyncio
async def test_programmatic_dispatch_preserves_declared_wildcard_kwargs() -> None:
    agent = Agently.create_agent()
    received: list[dict[str, Any]] = []

    def dynamic_read(**kwargs: Any) -> dict[str, Any]:
        received.append(dict(kwargs))
        return dict(kwargs)

    agent.action.register_action(
        action_id="wildcard_review_read",
        desc="Return dynamic review fields.",
        kwargs={"<*>": (str, "Dynamic string field.")},
        func=dynamic_read,
        returns=dict,
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("wildcard_review_read")]  # type: ignore[list-item]
    )
    assert catalog["entries"][0]["input_schema"]["additionalProperties"] == {"type": "string"}
    scope = {"kind": "action_run", "id": "wildcard-review"}
    value, record = await agent.action._async_execute_program_binding_action(
        "wildcard_review_read",
        {"dynamic_key": "kept"},
        settings=agent.settings,
        purpose="review wildcard dispatch",
        artifact_scope=scope,
    )

    assert received == [{"dynamic_key": "kept"}]
    assert value == {"dynamic_key": "kept"}
    assert record.get("status") == "success"


@pytest.mark.asyncio
async def test_program_timeout_setting_reaches_code_execution_resource(
    tmp_path: Any,
) -> None:
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="timeout_review_read",
        desc="Return one review value.",
        kwargs={},
        func=lambda: {"ok": True},
        returns={"ok": (bool, "Whether the read succeeded.")},
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("timeout_review_read")]  # type: ignore[list-item]
    )
    agent.action.action_runtime._retain_programmatic_catalog(catalog)
    agent.settings.set("action.programmatic.timeout", 2)
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="timeout-review")
    grant = workspace.issue_execution_access(
        action_call_id="outer-timeout-review",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    observed_timeouts: list[int] = []

    class _Resource:
        async def async_execute_code(self, *, timeout: int, **_: Any) -> dict[str, Any]:
            observed_timeouts.append(timeout)
            return {
                "ok": True,
                "status": "success",
                "value": None,
                "logs": [],
            }

    executor = ProgrammaticActionExecutor(action=agent.action, timeout=60)
    scope = {"kind": "action_run", "id": "timeout-review"}
    with agent.action._artifact_manager.bind_artifact_scope(scope):
        await executor.execute(
            spec={},
            action_call={
                "action_call_id": "outer-timeout-review",
                "action_input": {
                    "program": "return None",
                    "description": "review timeout",
                    "catalog_revision": catalog["catalog_revision"],
                },
                "task_workspace": workspace,
                "task_workspace_access_grants": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: grant,
                },
                "execution_resource_resources": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: _Resource(),
                },
                "execution_resource_handles": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: {
                        "provider_id": "review-provider",
                        "meta": {"provider_probes": []},
                    }
                },
            },
            policy={"network_mode": "disabled"},
            settings=agent.settings,
        )

    assert observed_timeouts == [2]


@pytest.mark.asyncio
async def test_programmatic_activation_checks_reserved_collision_before_empty_catalog() -> None:
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="run_action_program",
        desc="Application-owned collision.",
        kwargs={},
        func=lambda: {},
        returns=dict,
    )

    with pytest.raises(ValueError, match="reserved"):
        await agent.action.action_runtime._default_programmatic_planning_handler(
            {
                "prompt": agent.input("review collision").request.prompt,
                "settings": agent.settings,
                "agent_name": agent.name,
                "done_plans": [],
                "last_round_records": [],
                "round_index": 0,
                "max_rounds": 1,
            },
            {
                "action_list": agent.action.get_action_list(),
                "planning_protocol": "programmatic",
            },
        )


@pytest.mark.asyncio
async def test_direct_program_decisions_have_distinct_host_freshness_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("agently.builtins.plugins.ActionRuntime.AgentlyActionRuntime")
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="review_lookup",
        desc="Read one review record.",
        kwargs={"key": (str, "Record key.")},
        func=lambda key: {"key": key},
        returns={"key": (str, "Record key.")},
    )
    programs = iter(
        [
            "return await actions.review_lookup({'key': 'first'})",
            "return await actions.review_lookup({'key': 'second'})",
        ]
    )

    class _Reader:
        def __init__(self, program: str) -> None:
            self.program = program

        async def async_get_data(self) -> dict[str, Any]:
            return {
                "next_action": "execute",
                "description": "review lookup",
                "program": self.program,
            }

    def fake_get_result(_request: Any, *, parent_run_context: Any = None) -> Any:
        assert parent_run_context is None
        return SimpleNamespace(result=_Reader(next(programs)))

    monkeypatch.setattr(runtime_module, "_get_model_request_result", fake_get_result)
    action_list = agent.action.get_action_list()

    async def decide(user_input: str) -> dict[str, Any]:
        return await agent.action.action_runtime._default_programmatic_planning_handler(
            {
                "prompt": agent.input(user_input).request.prompt,
                "settings": agent.settings,
                "agent_name": agent.name,
                "done_plans": [],
                "last_round_records": [],
                "round_index": 0,
                "max_rounds": 1,
            },
            {"action_list": action_list, "planning_protocol": "programmatic"},
        )

    first = await decide("read first")
    second = await decide("read second")
    first_revision = first["action_calls"][0]["action_input"]["catalog_revision"]
    second_revision = second["action_calls"][0]["action_input"]["catalog_revision"]

    assert first_revision != second_revision
    runtime = agent.action.action_runtime
    runtime.release_programmatic_catalog(first_revision)
    assert runtime.resolve_programmatic_catalog(first_revision) is None
    # Releasing/replaying A again must not consume B's independent lease.
    runtime.release_programmatic_catalog(first_revision)
    assert runtime.resolve_programmatic_catalog(second_revision) is not None


@pytest.mark.asyncio
async def test_known_global_approval_policy_is_excluded_before_model_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("agently.builtins.plugins.ActionRuntime.AgentlyActionRuntime")
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="approval_policy_review_read",
        desc="Return one review value.",
        kwargs={},
        func=lambda: {"ok": True},
        returns={"ok": (bool, "Whether the read succeeded.")},
    )
    agent.settings.set("action.policy.global", {"approval_mode": "always"})

    def model_must_not_run(*_: Any, **__: Any) -> Any:
        raise AssertionError("known approval-pending catalog reached model planning")

    monkeypatch.setattr(runtime_module, "_get_model_request_result", model_must_not_run)
    decision = await agent.action.action_runtime._default_programmatic_planning_handler(
        {
            "prompt": agent.input("review approval policy").request.prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "done_plans": [],
            "last_round_records": [],
            "round_index": 0,
            "max_rounds": 1,
        },
        {
            "action_list": agent.action.get_action_list(),
            "planning_protocol": "programmatic",
        },
    )

    assert decision["next_action"] == "response"
    assert decision["action_calls"] == []


def test_program_source_is_not_copied_into_runtime_observation() -> None:
    agent = Agently.create_agent()
    marker = "REVIEW_PROGRAM_SOURCE_MUST_STAY_COLD"
    program = f"value = {marker!r}\nreturn {{'ok': bool(value)}}"
    observation = agent.action._to_runtime_visible_observation(
        {
            "kind": "plan_ready",
            "payload": {
                "decision": {
                    "next_action": "execute",
                    "use_action": True,
                    "action_calls": [
                        {
                            "purpose": "review",
                            "action_id": "run_action_program",
                            "action_input": {
                                "program": program,
                                "description": "review",
                                "catalog_revision": "sha256:review",
                            },
                            "source_protocol": "programmatic",
                        }
                    ],
                }
            },
        }
    )

    assert marker not in json.dumps(observation, ensure_ascii=False)


def test_programmatic_planning_observation_is_bounded_and_program_free() -> None:
    agent = Agently.create_agent()
    marker = "PLANNING_OBSERVATION_MUST_NOT_CONTAIN_PROGRAM"
    observation = agent.action._to_runtime_visible_observation(
        {
            "kind": "plan_ready",
            "payload": {
                "decision": {
                    "next_action": "execute",
                    "use_action": True,
                    "action_calls": [
                        {
                            "purpose": "review",
                            "action_id": "run_action_program",
                            "action_input": {
                                "program": f"return {marker!r}",
                                "description": "review",
                                "catalog_revision": "sha256:review",
                            },
                            "source_protocol": "programmatic",
                        }
                    ],
                    "planning_observation": {
                        "planning_protocol": "programmatic",
                        "sdk_renderer_version": "agently.programmatic_action.python.v3",
                        "eligible_action_count": 3,
                        "ineligible_action_count": 1,
                        "sdk_bytes": 2048,
                        "contract_bytes": 1024,
                        "program_bytes": 64,
                    },
                }
            },
        }
    )

    rendered = json.dumps(observation, ensure_ascii=False)
    assert marker not in rendered
    assert observation["payload"]["decision"]["planning_observation"] == {
        "planning_protocol": "programmatic",
        "sdk_renderer_version": "agently.programmatic_action.python.v3",
        "eligible_action_count": 3,
        "ineligible_action_count": 1,
        "sdk_bytes": 2048,
        "contract_bytes": 1024,
        "program_bytes": 64,
    }


def test_programmatic_settled_observation_is_runtime_visible_but_not_model_hot() -> None:
    agent = Agently.create_agent()
    record = {
        "action_call_id": "outer-observation",
        "ok": True,
        "status": "success",
        "success": True,
        "action_id": "run_action_program",
        "purpose": "review",
        "data": {"value": {"ok": True}},
        "result": {"value": {"ok": True}},
        "meta": {
            "programmatic_observation": {
                "sdk_renderer_version": "agently.programmatic_action.python.v3",
                "eligible_action_count": 2,
                "sdk_bytes": 1500,
                "contract_bytes": 800,
                "program_bytes": 64,
                "wrapper_bytes": 3000,
                "binding_call_count": 3,
                "successful_binding_calls": 3,
                "failed_binding_calls": 0,
                "peak_active_binding_calls": 2,
                "unexpected": "must not escape",
            }
        },
    }

    with agent.action._artifact_manager.bind_artifact_scope(
        {"kind": "action_run", "id": "programmatic-observation"}
    ):
        finalized = agent.action._finalize_action_result(
            record,
            artifact_scope={"kind": "action_run", "id": "programmatic-observation"},
        )
    runtime_record = agent.action._to_runtime_visible_observation(
        {"kind": "action_completed", "payload": {"record": finalized}}
    )["payload"]["record"]
    model_record = agent.action._to_model_visible_record(finalized)

    assert runtime_record["meta"]["programmatic_observation"]["peak_active_binding_calls"] == 2
    assert "unexpected" not in runtime_record["meta"]["programmatic_observation"]
    assert "programmatic_observation" not in model_record.get("meta", {})


def test_program_source_is_not_copied_into_next_model_record() -> None:
    agent = Agently.create_agent()
    marker = "REVIEW_PROGRAM_SOURCE_MUST_STAY_COLD"
    scope = {"kind": "action_run", "id": "program-source-review"}
    raw_record = {
        "action_call_id": "outer-review",
        "ok": True,
        "status": "success",
        "success": True,
        "action_id": "run_action_program",
        "tool_name": "run_action_program",
        "purpose": "review",
        "kwargs": {
            "program": f"value = {marker!r}\nreturn {{'ok': bool(value)}}",
            "description": "review",
            "catalog_revision": "sha256:review",
        },
        "data": {"value": {"ok": True}, "logs": []},
        "result": {"value": {"ok": True}, "logs": []},
        "executor_type": "programmatic_action",
        "meta": {},
    }

    with agent.action._artifact_manager.bind_artifact_scope(scope):
        finalized = agent.action._finalize_action_result(
            raw_record,
            artifact_scope=scope,
        )
    model_record = agent.action._to_model_visible_record(finalized)

    assert marker not in json.dumps(model_record, ensure_ascii=False)


def test_program_source_is_not_copied_into_actionflow_state_record() -> None:
    agent = Agently.create_agent()
    marker = "REVIEW_PROGRAM_SOURCE_MUST_STAY_COLD"
    scope = {"kind": "action_run", "id": "program-state-review"}
    raw_record = {
        "action_call_id": "outer-review",
        "ok": True,
        "status": "success",
        "success": True,
        "action_id": "run_action_program",
        "tool_name": "run_action_program",
        "purpose": "review",
        "kwargs": {
            "program": f"value = {marker!r}\nreturn {{'ok': bool(value)}}",
            "description": "review",
            "catalog_revision": "sha256:review",
        },
        "data": {"value": {"ok": True}, "logs": []},
        "result": {"value": {"ok": True}, "logs": []},
        "executor_type": "programmatic_action",
        "meta": {},
    }

    with agent.action._artifact_manager.bind_artifact_scope(scope):
        finalized = agent.action._finalize_action_result(
            raw_record,
            artifact_scope=scope,
        )
    state_record = agent.action._to_action_flow_return_records([finalized])[0]

    assert marker not in json.dumps(state_record, ensure_ascii=False)


def test_programmatic_provider_traceback_stays_out_of_hot_records() -> None:
    agent = Agently.create_agent()
    marker = "REVIEW_PROVIDER_TRACEBACK_MUST_STAY_COLD"
    scope = {"kind": "action_run", "id": "program-error-review"}
    raw_record = {
        "action_call_id": "outer-review",
        "ok": False,
        "status": "error",
        "success": False,
        "action_id": "run_action_program",
        "tool_name": "run_action_program",
        "purpose": "review",
        "kwargs": {
            "program": "raise RuntimeError('provider detail')",
            "description": "review",
            "catalog_revision": "sha256:review",
        },
        "data": {"value": None, "logs": []},
        "result": {"value": None, "logs": []},
        "executor_type": "programmatic_action",
        "error": f"Traceback: /workspace/source/main.py: {marker}",
        "meta": {},
    }

    with agent.action._artifact_manager.bind_artifact_scope(scope):
        finalized = agent.action._finalize_action_result(
            raw_record,
            artifact_scope=scope,
        )
    model_record = agent.action._to_model_visible_record(finalized)
    runtime_record = agent.action._to_runtime_visible_observation(
        {"kind": "action_failed", "payload": {"record": finalized}}
    )

    assert marker not in json.dumps(model_record, ensure_ascii=False)
    assert marker not in json.dumps(runtime_record, ensure_ascii=False)


@pytest.mark.asyncio
async def test_nested_subcall_started_and_terminal_events_share_one_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="review_read",
        desc="Return one review value.",
        kwargs={},
        func=lambda: {"ok": True},
        returns={"ok": (bool, "Whether the read succeeded.")},
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("review_read")]  # type: ignore[list-item]
    )
    agent.action.action_runtime._retain_programmatic_catalog(catalog)
    retained_catalog = agent.action.action_runtime.resolve_programmatic_catalog(catalog["catalog_revision"])
    assert retained_catalog is not None
    executor = ProgrammaticActionExecutor(action=agent.action)
    observations: list[dict[str, Any]] = []

    async def capture(observation: dict[str, Any]) -> None:
        observations.append(observation)

    monkeypatch.setattr(agent.action, "_async_emit_action_flow_observation", capture)
    outer_run = RunContext.create(run_kind="action", run_id="outer-program-review")
    scope = {"kind": "action_run", "id": "lineage-review"}
    with bind_runtime_context(tool_phase_run_context=outer_run):
        with agent.action._artifact_manager.bind_artifact_scope(scope):
            binding = executor._bindings(
                catalog=retained_catalog,
                settings=agent.settings,
                parent_action_call_id="outer-call-review",
            )[0][0]
            await binding.async_handler({})

    assert [item["kind"] for item in observations] == [
        "action_started",
        "action_completed",
    ]
    started_run = observations[0]["run"]
    completed_run = observations[1]["run"]
    assert started_run.run_id == completed_run.run_id
    assert started_run.parent_run_id == outer_run.run_id


@pytest.mark.asyncio
async def test_action_runtime_binds_the_outer_action_run_during_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agently.create_agent()
    observed_runs: list[RunContext | None] = []

    async def capture_dispatch(
        action_id: str,
        action_input: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        observed_runs.append(get_current_tool_phase_run_context())
        return {
            "action_call_id": "outer-call-review",
            "ok": True,
            "status": "success",
            "success": True,
            "action_id": action_id,
            "tool_name": action_id,
            "kwargs": action_input,
            "data": {"ok": True},
            "result": {"ok": True},
        }

    monkeypatch.setattr(agent.action, "async_execute_action", capture_dispatch)
    outer_run = RunContext.create(run_kind="action", run_id="outer-dispatch-review")
    records = await agent.action.action_runtime._default_action_execution_handler(
        {"settings": agent.settings},
        {
            "action_calls": [
                {
                    "action_id": "run_action_program",
                    "action_input": {},
                    "source_protocol": "programmatic",
                }
            ],
            "action_run_contexts": [outer_run],
        },
    )

    assert records[0]["status"] == "success"
    assert len(observed_runs) == 1
    assert observed_runs[0] is not None
    assert observed_runs[0].run_id == outer_run.run_id


@pytest.mark.asyncio
async def test_denied_outer_approval_releases_program_catalog_lease() -> None:
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="approval_review_read",
        desc="Return one review value.",
        kwargs={},
        func=lambda: {"ok": True},
        returns={"ok": (bool, "Whether the read succeeded.")},
    )
    action_list = agent.action.get_action_list()
    catalog = build_programmatic_action_catalog(
        action_list,
        revision_seed="approval-review",
    )
    catalog["_revision_seed"] = "approval-review"  # type: ignore[typeddict-unknown-key]
    runtime = agent.action.action_runtime
    runtime._retain_programmatic_catalog(catalog)
    agent.action._ensure_programmatic_action_transport(settings=agent.settings)
    agent.settings.set("action.policy.global", {"approval_mode": "always"})
    handler_name = f"programmatic_review_deny_{uuid.uuid4().hex}"
    Agently.policy_approval.register_handler(
        handler_name,
        lambda _request: {
            "status": "denied",
            "approved": False,
            "reason": "review denial",
        },
        replace=True,
    )
    agent.settings.set("policy_approval.handler", handler_name)
    planning_count = 0

    async def planning_handler(_context: Any, _request: Any) -> dict[str, Any]:
        nonlocal planning_count
        planning_count += 1
        if planning_count > 1:
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "use_action": True,
            "action_calls": [
                {
                    "action_id": "run_action_program",
                    "purpose": "review",
                    "action_input": {
                        "program": "return {'ok': True}",
                        "description": "review",
                        "catalog_revision": catalog["catalog_revision"],
                    },
                    "source_protocol": "programmatic",
                }
            ],
        }

    try:
        records = await agent.action.async_plan_and_execute(
            prompt=agent.input("review approval").request.prompt,
            settings=agent.settings,
            action_list=action_list,
            planning_handler=planning_handler,
            max_rounds=2,
            planning_protocol="programmatic",
        )
    finally:
        Agently.policy_approval.unregister_handler(handler_name)

    assert records[0].get("status") == "blocked"
    assert runtime.resolve_programmatic_catalog(catalog["catalog_revision"]) is None


@pytest.mark.asyncio
async def test_nested_action_evidence_record_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agently.create_agent()
    tail_marker = "REVIEW_UNBOUNDED_TAIL_MUST_STAY_COLD"
    agent.action.register_action(
        action_id="large_review_read",
        desc="Return a large review value.",
        kwargs={},
        func=lambda: ("x" * 20_000) + tail_marker,
        returns=str,
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("large_review_read")]  # type: ignore[list-item]
    )
    agent.action.action_runtime._retain_programmatic_catalog(catalog)
    retained_catalog = agent.action.action_runtime.resolve_programmatic_catalog(catalog["catalog_revision"])
    assert retained_catalog is not None
    executor = ProgrammaticActionExecutor(action=agent.action)

    async def ignore_observation(_: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(
        agent.action,
        "_async_emit_action_flow_observation",
        ignore_observation,
    )
    execution_context = AgentExecutionContext(
        execution_id="programmatic-evidence-review",
        lineage={},
        limits={},
    )
    scope = {"kind": "action_run", "id": "evidence-review"}
    with bind_runtime_context(agent_execution_context=execution_context):
        with agent.action._artifact_manager.bind_artifact_scope(scope):
            binding = executor._bindings(
                catalog=retained_catalog,
                settings=agent.settings,
                parent_action_call_id="outer-call-review",
            )[0][0]
            value = await binding.async_handler({})

    assert value.endswith(tail_marker)
    assert len(execution_context.action_records) == 1
    retained = json.dumps(execution_context.action_records[0], ensure_ascii=False)
    assert tail_marker not in retained
    assert len(retained.encode("utf-8")) <= 16_000
