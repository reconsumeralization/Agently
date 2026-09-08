from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.plugins.ActionExecutor.ProgrammaticActionExecutor import (
    ProgrammaticActionExecutor,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.operation.Action.ActionRegistry import ActionRegistry
from agently.core.operation.Action.ActionDispatcher import ActionDispatcher
from agently.core.operation.Action.ActionProgram import (
    build_programmatic_action_catalog,
    build_programmatic_python_source,
)
from agently.types.data import (
    PROGRAMMATIC_ACTION_TRANSPORT_ID,
    ActionSpec,
    TaskWorkspaceAccessRequirement,
    required_code_execution_isolation,
)


class _NoopExecutor:
    kind = "test"
    sandboxed = False

    async def execute(self, **_: Any) -> Any:
        return None


def _spec(action_id: str) -> ActionSpec:
    return {
        "action_id": action_id,
        "name": action_id,
        "desc": "test",
        "kwargs": {},
        "returns": (dict, "test result"),
    }


def test_action_registry_reserved_transport_cannot_be_replaced_or_removed() -> None:
    registry = ActionRegistry(name="programmatic-test")
    executor = _NoopExecutor()
    assert not hasattr(registry, "register_reserved")
    assert not hasattr(registry, "is_reserved")
    assert not hasattr(registry, "registration_version")
    registry._register_reserved(_spec("run_action_program"), executor)

    with pytest.raises(ValueError, match="reserved"):
        registry.register(_spec("run_action_program"), executor)

    replacement = _NoopExecutor()
    with pytest.raises(ValueError, match="already reserved"):
        registry._register_reserved(_spec("run_action_program"), replacement)
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        registry.register(
            _spec("run_action_program"),
            replacement,
            reserved=True,  # type: ignore[call-arg]
        )

    assert registry.unregister("run_action_program") is False
    assert registry.get_executor("run_action_program") is executor


def test_action_loop_accepts_typed_programmatic_planning_protocol() -> None:
    agent = Agently.create_agent()

    agent.set_action_loop(planning_protocol="programmatic")

    assert agent.settings.get("action.protocol") == "programmatic"
    assert agent.settings.get("tool.protocol") == "programmatic"
    assert (
        agent.action.action_runtime.resolve_planning_protocol(
            agent.settings,
        )
        == "programmatic"
    )


def test_action_loop_rejects_unknown_planning_protocol() -> None:
    agent = Agently.create_agent()

    with pytest.raises(ValueError, match="planning_protocol"):
        agent.set_action_loop(planning_protocol="unknown")  # type: ignore[arg-type]


def test_programmatic_transport_resolves_current_provider_and_timeout_per_call() -> None:
    agent = Agently.create_agent()
    agent.set_settings("code_execution.providers", ["docker"])
    agent.action._ensure_programmatic_action_transport(settings=agent.settings)
    spec = agent.action.action_registry.get_spec(PROGRAMMATIC_ACTION_TRANSPORT_ID)
    assert spec is not None

    agent.set_settings("code_execution.providers", ["gvisor"])
    agent.set_settings("action.programmatic.timeout", 2)
    requirements = agent.action.action_dispatcher._prepare_execution_resource_requirements(
        spec=spec,
        settings=agent.settings,
        policy={"network_mode": "disabled"},
    )

    assert requirements[0].get("provider_candidates") == [{"provider_id": "gvisor", "config": {}}]
    assert requirements[0].get("policy", {}).get("timeout_seconds") == 2


def test_provider_terminal_status_is_normalized_to_action_status() -> None:
    normalize = ProgrammaticActionExecutor._outer_action_status

    assert normalize("success", ok=True) == "success"
    assert normalize("blocked", ok=False) == "blocked"
    assert normalize("timed_out", ok=False) == "error"
    assert normalize("cancelled", ok=False) == "error"


def test_programmatic_catalog_capacity_never_evicts_an_unsettled_revision() -> None:
    runtime = Agently.create_agent().action.action_runtime
    first = {"catalog_revision": "sha256:first", "entries": [{}]}
    second = {"catalog_revision": "sha256:second", "entries": [{}]}
    runtime._retain_programmatic_catalog(first, max_active_catalogs=1)

    with pytest.raises(RuntimeError, match="capacity"):
        runtime._retain_programmatic_catalog(second, max_active_catalogs=1)

    assert runtime.resolve_programmatic_catalog("sha256:first") is not None
    runtime.release_programmatic_catalog("sha256:first")
    runtime._retain_programmatic_catalog(second, max_active_catalogs=1)
    assert runtime.resolve_programmatic_catalog("sha256:second") is not None


def test_generated_program_call_can_release_its_catalog_when_discarded() -> None:
    agent = Agently.create_agent()
    revision = "sha256:discarded"
    agent.action.action_runtime._retain_programmatic_catalog({"catalog_revision": revision, "entries": []})
    calls = [
        {
            "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
            "action_input": {"catalog_revision": revision},
        }
    ]

    assert agent.release_programmatic_action_calls(calls) == 1
    assert agent.action.action_runtime.resolve_programmatic_catalog(revision) is None
    assert agent.release_programmatic_action_calls(calls) == 0


def test_programmatic_subcalls_are_treated_as_model_sourced() -> None:
    sanitized, stripped = ActionDispatcher._sanitize_policy_override(
        {
            "auto_allow": True,
            "policy_approval_granted": True,
            "allowed_cmd_prefixes": ["rm"],
        },
        source_protocol="programmatic",
    )

    assert sanitized == {}
    assert set(stripped) == {
        "auto_allow",
        "policy_approval_granted",
        "allowed_cmd_prefixes",
    }


def test_programmatic_wildcard_action_preserves_schema_allowed_dynamic_keys() -> None:
    agent = Agently.create_agent()
    received: list[dict[str, Any]] = []

    def collect(**kwargs: Any) -> dict[str, Any]:
        received.append(dict(kwargs))
        return dict(kwargs)

    agent.action.register_action(
        action_id="wildcard_read",
        desc="Collect dynamic integer fields.",
        kwargs={"<*>": (int, "Dynamic integer value")},
        func=collect,
        returns={"<*>": (int, "Dynamic integer value")},
        meta={"host_only_input_keys": ["privileged"]},
        expose_to_model=True,
    )
    result = agent.action.execute_action(
        "wildcard_read",
        {"alpha": 1, "beta": 2, "privileged": 9},
        source_protocol="programmatic",
    )

    assert result.get("status") == "success"
    assert received == [{"alpha": 1, "beta": 2}]


def test_programmatic_transport_carrier_never_retains_raw_program_source() -> None:
    agent = Agently.create_agent()
    marker = "RAW_PROGRAM_MUST_NOT_ENTER_FLOW_STATE"
    scope = {"kind": "action_run", "id": "carrier-test"}
    with agent.action._artifact_manager.bind_artifact_scope(scope):
        finalized = agent.action._finalize_action_result(
            {
                "action_call_id": "outer",
                "ok": True,
                "status": "success",
                "success": True,
                "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
                "kwargs": {
                    "program": f"return {marker!r}",
                    "description": "carrier test",
                    "catalog_revision": "sha256:test",
                },
                "data": {"value": {"ok": True}, "logs": []},
                "result": {"value": {"ok": True}, "logs": []},
                "executor_type": "programmatic_action",
            },
            artifact_scope=scope,
        )
    carrier = agent.action._to_action_flow_return_records([finalized])[0]

    assert marker not in str(carrier)
    assert carrier.get("kwargs") is None
    digest = carrier.get("result")
    assert isinstance(digest, dict)
    assert digest.get("result_preview") == {
        "value": {"ok": True},
        "logs": [],
    }

    normalized_again = agent.action._normalize_execution_records(
        [carrier],
        [
            {
                "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
                "action_input": {
                    "program": f"return {marker!r}",
                    "description": "carrier test",
                    "catalog_revision": "sha256:test",
                },
            }
        ],
        artifact_scope=scope,
    )[0]
    carrier_again = agent.action._to_action_flow_return_records([normalized_again])[0]
    assert marker not in str(carrier_again)
    repeated_digest = carrier_again.get("result")
    assert isinstance(repeated_digest, dict)
    assert repeated_digest.get("result_preview") == {
        "value": {"ok": True},
        "logs": [],
    }


def test_programmatic_carrier_preserves_small_nested_value_with_large_cold_artifact() -> None:
    agent = Agently.create_agent()
    program_marker = "PROGRAM_SOURCE_MUST_STAY_COLD_WITH_NESTED_VALUE"
    sdk_marker = "SDK_SOURCE_MUST_STAY_COLD_WITH_NESTED_VALUE"
    scope = {"kind": "action_run", "id": "nested-carrier-test"}
    outer_value = [
        {"user_id": "u1", "spent": 600.0, "limit": 500.0},
        {"user_id": "u2", "spent": 1200.0, "limit": 1000.0},
    ]
    with agent.action._artifact_manager.bind_artifact_scope(scope):
        finalized = agent.action._finalize_action_result(
            {
                "action_call_id": "outer-nested",
                "ok": True,
                "status": "success",
                "success": True,
                "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
                "kwargs": {
                    "program": f"return {program_marker!r}",
                    "description": "nested carrier test",
                    "catalog_revision": "sha256:nested",
                },
                "data": {
                    "value": outer_value,
                    "logs": [],
                    "logs_truncated": False,
                    "subcall_evidence": [
                        {
                            "action_call_id": f"nested-{index}",
                            "action_id": "lookup",
                            "status": "success",
                            "success": True,
                        }
                        for index in range(6)
                    ],
                },
                "result": {"value": outer_value, "logs": []},
                "executor_type": "programmatic_action",
                "artifacts": [
                    {
                        "artifact_type": "programmatic_action_sdk",
                        "label": "cold SDK",
                        "media_type": "text/x-python",
                        "value": sdk_marker + ("x" * 8_000),
                    }
                ],
            },
            artifact_scope=scope,
        )
    carrier = agent.action._to_action_flow_return_records([finalized])[0]

    serialized = json.dumps(carrier, ensure_ascii=False)
    assert program_marker not in serialized
    assert sdk_marker not in serialized
    digest = carrier.get("result")
    assert isinstance(digest, dict)
    preview = digest.get("result_preview")
    assert isinstance(preview, dict)
    assert preview.get("value") == outer_value


def test_programmatic_carrier_redacts_sensitive_outer_value_fields() -> None:
    agent = Agently.create_agent()
    scope = {"kind": "action_run", "id": "sensitive-carrier-test"}
    with agent.action._artifact_manager.bind_artifact_scope(scope):
        finalized = agent.action._finalize_action_result(
            {
                "action_call_id": "outer-sensitive",
                "ok": True,
                "status": "success",
                "success": True,
                "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
                "kwargs": {
                    "program": "return {'token': 'secret'}",
                    "description": "sensitive carrier test",
                    "catalog_revision": "sha256:sensitive",
                },
                "data": {
                    "value": {
                        "user_id": "u1",
                        "access_token": "MUST_NOT_ENTER_MODEL_HOT_RESULT",
                    },
                    "logs": [],
                },
                "result": {},
                "executor_type": "programmatic_action",
            },
            artifact_scope=scope,
        )
    carrier = agent.action._to_action_flow_return_records([finalized])[0]

    serialized = json.dumps(carrier, ensure_ascii=False)
    assert "MUST_NOT_ENTER_MODEL_HOT_RESULT" not in serialized
    digest = carrier.get("result")
    assert isinstance(digest, dict)
    preview = digest.get("result_preview")
    assert isinstance(preview, dict)
    assert preview.get("value") == {
        "user_id": "u1",
        "access_token": "[REDACTED]",
    }


def test_programmatic_carrier_oversized_outer_value_uses_digest_fact() -> None:
    agent = Agently.create_agent()
    marker = "OVERSIZED_PROGRAM_VALUE_MUST_STAY_COLD"
    scope = {"kind": "action_run", "id": "oversized-carrier-test"}
    with agent.action._artifact_manager.bind_artifact_scope(scope):
        finalized = agent.action._finalize_action_result(
            {
                "action_call_id": "outer-oversized",
                "ok": True,
                "status": "success",
                "success": True,
                "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
                "kwargs": {
                    "program": "return {'body': 'large'}",
                    "description": "oversized carrier test",
                    "catalog_revision": "sha256:oversized",
                },
                "data": {
                    "value": {"body": marker + ("x" * 8_000)},
                    "logs": [],
                },
                "result": {},
                "executor_type": "programmatic_action",
            },
            artifact_scope=scope,
        )
    carrier = agent.action._to_action_flow_return_records([finalized])[0]

    serialized = json.dumps(carrier, ensure_ascii=False)
    assert marker not in serialized
    digest = carrier.get("result")
    assert isinstance(digest, dict)
    preview = digest.get("result_preview")
    assert isinstance(preview, dict)
    value_fact = preview.get("value")
    assert isinstance(value_fact, dict)
    assert value_fact.get("omitted") is True
    assert value_fact.get("reason") == ("programmatic_outer_value_exceeds_hot_limit")
    assert str(value_fact.get("sha256", "")).startswith("sha256:")


@pytest.mark.asyncio
async def test_default_programmatic_planner_builds_one_reserved_action_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_module = importlib.import_module("agently.builtins.plugins.ActionRuntime.AgentlyActionRuntime")

    agent = Agently.create_agent()

    @agent.action_func
    def lookup_record(record_id: str) -> dict[str, Any]:
        """Look up one test record."""

        return {"record_id": record_id}

    action_list = agent.action.get_action_list()
    captured: dict[str, Any] = {}

    class _Reader:
        async def async_get_data(self):
            return {
                "next_action": "execute",
                "description": "Look up the requested record",
                "program": "return await actions.lookup_record({'record_id': 'r1'})",
            }

    def fake_get_result(request, *, parent_run_context=None):
        _ = parent_run_context
        captured["request"] = request
        return SimpleNamespace(result=_Reader())

    monkeypatch.setattr(runtime_module, "_get_model_request_result", fake_get_result)
    decision = await agent.action.action_runtime._default_programmatic_planning_handler(
        {
            "prompt": agent.input("Look up r1").request.prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "done_plans": [],
            "last_round_records": [],
            "round_index": 0,
            "max_rounds": 2,
        },
        {"action_list": action_list, "planning_protocol": "programmatic"},
    )

    calls = decision.get("action_calls", [])
    assert len(calls) == 1
    assert calls[0]["action_id"] == PROGRAMMATIC_ACTION_TRANSPORT_ID
    assert calls[0]["source_protocol"] == "programmatic"
    assert calls[0]["action_input"]["catalog_revision"].startswith("sha256:")
    planning_observation = decision["planning_observation"]
    assert planning_observation["planning_protocol"] == "programmatic"
    assert planning_observation["sdk_renderer_version"].endswith(".v3")
    assert planning_observation["eligible_action_count"] == 1
    assert planning_observation["ineligible_action_count"] == 0
    assert planning_observation["sdk_bytes"] > planning_observation["contract_bytes"] > 0
    assert planning_observation["program_bytes"] == len(
        calls[0]["action_input"]["program"].encode("utf-8")
    )
    request = captured["request"]
    assert request.prompt.get("tools") is None
    prompt_text = request.prompt.to_text()
    assert "lookup_record" in prompt_text
    assert "local helper definitions are allowed" in prompt_text
    assert "concurrency_mode='parallel'" in prompt_text
    assert "direct return" in prompt_text
    assert request.settings.get("model_request.output_observation.sensitive_paths") == ["program"]
    assert agent.action.action_registry._is_reserved(PROGRAMMATIC_ACTION_TRANSPORT_ID)


@pytest.mark.asyncio
async def test_programmatic_activation_rejects_transport_collision_before_empty_catalog_return() -> None:
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id=PROGRAMMATIC_ACTION_TRANSPORT_ID,
        desc="User action colliding with the transport.",
        kwargs={},
        func=lambda: None,
        expose_to_model=True,
    )

    with pytest.raises(ValueError, match="reserved"):
        await agent.action.action_runtime._default_programmatic_planning_handler(
            {
                "prompt": agent.input("No eligible actions").request.prompt,
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


class _RecordingBoundResource:
    def __init__(self, *, raise_after_binding: bool = False) -> None:
        self.binding_count = 0
        self.timeout: int | None = None
        self.raise_after_binding = raise_after_binding

    async def async_execute_code(
        self,
        *,
        bundle,
        manifest,
        grant,
        timeout,
        bindings=(),
        binding_limits=None,
    ):
        _ = (manifest, grant, binding_limits)
        self.timeout = timeout
        self.binding_count = len(bindings)
        source = next(item.content for item in bundle.files if item.path == bundle.entrypoint)
        assert b"agently_code_bindings" in source
        binding = next(item for item in bindings if item.binding_key == "lookup_record")
        value = await binding.async_handler({"record_id": "r1", "admin": True})
        if self.raise_after_binding:
            raise RuntimeError("synthetic provider cleanup failure with private detail")
        return {
            "ok": True,
            "status": "success",
            "value": {"selected": value},
            "logs": ["selected one record"],
            "binding_summary": {"call_count": 1, "successful_calls": 1},
            "binding_calls": [{"sequence": 1, "binding_key": "lookup_record", "status": "success"}],
            "meta": {"mechanism": "synthetic-bound-resource"},
        }


@pytest.mark.asyncio
async def test_programmatic_executor_reenters_action_dispatcher_and_returns_outer_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agently.create_agent()
    agent.set_settings("action.programmatic.timeout", 2)
    received: list[dict[str, Any]] = []

    def lookup_record(record_id: str) -> dict[str, Any]:
        received.append({"record_id": record_id})
        return {"record_id": record_id, "secret_internal": "not-model-history"}

    agent.action.register_action(
        action_id="lookup_record",
        desc="Look up one record.",
        kwargs={"record_id": (str, "Record id")},
        func=lookup_record,
        returns={"record_id": (str, "Record id"), "secret_internal": (str, "Internal value")},
        side_effect_level="read",
        replay_safe=True,
        expose_to_model=True,
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("lookup_record")],  # type: ignore[list-item]
    )
    agent.action.action_runtime._retain_programmatic_catalog(dict(catalog))

    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="ptc-test")
    grant = workspace.issue_execution_access(
        action_call_id="outer-call",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    resource = _RecordingBoundResource()
    executor = ProgrammaticActionExecutor(action=agent.action, timeout=10)

    async def no_observation(_: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(agent.action, "_async_emit_action_flow_observation", no_observation)
    scope = {"kind": "action_run", "id": "ptc-test-run"}
    with agent.action._artifact_manager.bind_artifact_scope(scope):
        result = await executor.execute(
            spec={},
            action_call={
                "action_call_id": "outer-call",
                "action_input": {
                    "program": "return await actions.lookup_record({'record_id': 'r1'})",
                    "description": "lookup",
                    "catalog_revision": catalog["catalog_revision"],
                },
                "task_workspace": workspace,
                "task_workspace_access_grants": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: grant,
                },
                "execution_resource_resources": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: resource,
                },
                "execution_resource_handles": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: {
                        "provider_id": "synthetic",
                        "meta": {"provider_probes": []},
                    }
                },
            },
            policy={},
            settings=agent.settings,
        )

    assert received == [{"record_id": "r1"}]
    assert resource.binding_count == 1
    assert resource.timeout == 2
    assert result["ok"] is True
    assert result["data"]["value"]["selected"]["record_id"] == "r1"
    assert result["data"]["logs"] == ["selected one record"]
    assert result["data"]["subcall_evidence"][0]["action_id"] == "lookup_record"
    assert result["data"]["subcall_evidence"][0]["action_call_id"].startswith("act_call_")
    assert result["meta"]["programmatic_observation"] == {
        "sdk_renderer_version": catalog["renderer_version"],
        "eligible_action_count": 1,
        "ineligible_action_count": 0,
        "sdk_bytes": catalog["sdk_bytes"],
        "contract_bytes": catalog["contract_bytes"],
        "program_bytes": len("return await actions.lookup_record({'record_id': 'r1'})".encode("utf-8")),
        "wrapper_bytes": len(
            build_programmatic_python_source(
                "return await actions.lookup_record({'record_id': 'r1'})"
            ).encode("utf-8")
        ),
        "binding_call_count": 1,
        "successful_binding_calls": 1,
    }
    assert {item["artifact_type"] for item in result["artifacts"]} >= {
        "programmatic_action_sdk",
        "programmatic_action_catalog",
    }


@pytest.mark.asyncio
async def test_provider_exception_preserves_completed_subcall_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="lookup_record",
        desc="Look up one record.",
        kwargs={"record_id": (str, "Record id")},
        func=lambda record_id: {"record_id": record_id},
        returns={"record_id": (str, "Record id")},
        expose_to_model=True,
    )
    catalog = build_programmatic_action_catalog(
        [agent.action.action_registry.get_spec("lookup_record")]  # type: ignore[list-item]
    )
    agent.action.action_runtime._retain_programmatic_catalog(dict(catalog))
    workspace = TaskWorkspace(tmp_path / "provider-failure", execution_id="ptc-failure")
    grant = workspace.issue_execution_access(
        action_call_id="outer-failure",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    resource = _RecordingBoundResource(raise_after_binding=True)
    executor = ProgrammaticActionExecutor(action=agent.action, timeout=10)

    async def no_observation(_: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(agent.action, "_async_emit_action_flow_observation", no_observation)
    with agent.action._artifact_manager.bind_artifact_scope({"kind": "action_run", "id": "provider-failure"}):
        result = await executor.execute(
            spec={},
            action_call={
                "action_call_id": "outer-failure",
                "action_input": {
                    "program": "return await actions.lookup_record({'record_id': 'r1'})",
                    "description": "lookup",
                    "catalog_revision": catalog["catalog_revision"],
                },
                "task_workspace": workspace,
                "task_workspace_access_grants": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: grant,
                },
                "execution_resource_resources": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: resource,
                },
                "execution_resource_handles": {
                    PROGRAMMATIC_ACTION_TRANSPORT_ID: {
                        "provider_id": "synthetic",
                        "meta": {"provider_probes": []},
                    }
                },
            },
            policy={},
            settings=agent.settings,
        )

    assert result["status"] == "error"
    assert result["meta"]["program_status"] == "provider_exception"
    assert result["data"]["subcall_evidence"][0]["action_id"] == "lookup_record"
    assert "private detail" not in result["error"]


class _FlowBindingProvider:
    supported_kinds = ("code_execution",)
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.name = provider_id
        self.ensure_count = 0
        self.release_count = 0
        self.resource = _RecordingBoundResource()

    async def async_probe(self, *, requirement, policy):
        _ = (requirement, policy)
        return {
            "provider_id": self.provider_id,
            "available": True,
            "supported_kinds": ["code_execution"],
            "capabilities": {
                "languages": ["python"],
                "toolchains": {"python": {"version": "3.10.13"}},
                "workspace_access_modes": ["snapshot"],
                "isolation": {
                    **required_code_execution_isolation(),
                    "mechanism": "synthetic-flow-provider",
                },
                "host_async_bindings": True,
            },
            "reason": "synthetic flow provider",
        }

    async def async_ensure(self, *, requirement, policy, existing_handle=None):
        _ = (requirement, policy, existing_handle)
        self.ensure_count += 1
        return {
            "handle_id": f"{self.provider_id}:{self.ensure_count}",
            "resource": self.resource,
            "status": "ready",
        }

    async def async_health_check(self, handle):
        _ = handle
        return "ready"

    async def async_release(self, handle):
        _ = handle
        self.release_count += 1


@pytest.mark.asyncio
async def test_programmatic_reserved_action_runs_through_triggerflow_action_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_id = "ptc_flow_synthetic_provider"
    provider = _FlowBindingProvider(provider_id)
    Agently.execution_resource.register_provider(cast(Any, provider))
    agent = Agently.create_agent()
    agent.set_settings("code_execution.providers", [provider_id])
    agent.set_action_loop(planning_protocol="programmatic", max_rounds=2)
    observed: list[str] = []

    def lookup_record(record_id: str) -> dict[str, Any]:
        observed.append(record_id)
        return {"record_id": record_id}

    agent.action.register_action(
        action_id="lookup_record",
        desc="Look up one record.",
        kwargs={"record_id": (str, "Record id")},
        func=lookup_record,
        returns={"record_id": (str, "Record id")},
        tags=[f"agent-{agent.name}"],
        side_effect_level="read",
        replay_safe=True,
        expose_to_model=True,
    )
    action_list = agent.action.get_action_list(tags=[f"agent-{agent.name}"])
    catalog = build_programmatic_action_catalog(action_list)
    agent.action.action_runtime._retain_programmatic_catalog(dict(catalog))
    agent.action._ensure_programmatic_action_transport(settings=agent.settings)
    planning_calls = 0

    async def plan_handler(context, request):
        nonlocal planning_calls
        _ = request
        planning_calls += 1
        if not context.get("done_plans"):
            return {
                "next_action": "execute",
                "use_action": True,
                "action_calls": [
                    {
                        "purpose": "lookup",
                        "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
                        "action_input": {
                            "program": "return await actions.lookup_record({'record_id': 'r1'})",
                            "description": "lookup",
                            "catalog_revision": catalog["catalog_revision"],
                        },
                        "source_protocol": "programmatic",
                        "todo_suggestion": "respond",
                    }
                ],
            }
        return {"next_action": "response", "action_calls": []}

    records = await agent.action.async_plan_and_execute(
        prompt=agent.input("Look up r1").request.prompt,
        settings=agent.settings,
        action_list=action_list,
        agent_name=agent.name,
        planning_handler=plan_handler,
        max_rounds=2,
        planning_protocol="programmatic",
    )

    assert observed == ["r1"]
    assert planning_calls == 2
    assert provider.ensure_count == 1
    assert provider.release_count == 1
    assert len(records) == 1
    assert records[0].get("action_id") == PROGRAMMATIC_ACTION_TRANSPORT_ID
    assert all(record.get("action_id") != "lookup_record" for record in records)
    assert agent.action.action_runtime.resolve_programmatic_catalog(catalog["catalog_revision"]) is None
