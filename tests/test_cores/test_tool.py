import pytest

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from agently import Agently
from agently.core import Action, PluginManager
from agently.types.data import ActionCall, ActionDecision, ActionPolicy
from agently.utils import Settings


def test_tool():
    tool = Agently.tool

    tool.register(
        name="test",
        desc="test func",
        kwargs={},
        func=lambda: print("OK"),
    )

    @tool.tool_func
    async def add(a: int, b: int) -> int:
        """
        Get result of `a(int)` add `b(int)`
        """
        await asyncio.sleep(1)
        return a + b

    assert tool.get_tool_info() == {
        "add": {
            "name": "add",
            "desc": "Get result of `a(int)` add `b(int)`",
            "kwargs": {
                "a": (int, ""),
                "b": (int, ""),
            },
            "returns": int,
        },
        "test": {
            "desc": "test func",
            "kwargs": {},
            "name": "test",
        },
    }
    add_tool = tool.get_tool_func("add", shift="sync")
    if add_tool:
        result = add_tool(1, 2)
        assert result == 3


def test_action_alias_and_dispatcher_execute_result():
    action = Agently.action
    assert Agently.tool is action

    action_id = f"action_alias_test_{ uuid.uuid4().hex[:8] }"
    action.register_action(
        action_id=action_id,
        desc="Increment one integer.",
        kwargs={"value": (int, "")},
        func=lambda value: value + 1,
        expose_to_model=False,
    )

    executed = action.execute_action(action_id, {"value": 4})
    assert executed.get("status") == "success"
    assert executed.get("data") == 5
    assert action.call_action(action_id, {"value": 4}) == 5
    assert action.call_tool(action_id, {"value": 4}) == 5


def test_action_dispatcher_requires_approval():
    action = Agently.action
    action_id = f"approval_action_{ uuid.uuid4().hex[:8] }"
    action.register_action(
        action_id=action_id,
        desc="Approval gated action.",
        kwargs={},
        func=lambda: "ok",
        approval_required=True,
        expose_to_model=False,
    )

    result = action.execute_action(action_id, {})
    assert result.get("status") == "blocked"
    legacy = action.call_tool(action_id, {})
    assert legacy["status"] == "blocked"


def test_model_policy_override_cannot_grant_approval():
    """ISSUE-001: a model-planned command must not self-grant approval."""
    action = Agently.action
    action_id = f"approval_escalation_{ uuid.uuid4().hex[:8] }"
    action.register_action(
        action_id=action_id,
        desc="Approval gated action.",
        kwargs={},
        func=lambda: "ok",
        approval_required=True,
        expose_to_model=False,
    )

    # A structured_plan command (model output) cannot bypass the approval gate by
    # injecting host-only policy keys.
    blocked = action.execute_action(
        action_id,
        {},
        policy_override={"policy_approval_granted": True, "approval_mode": "auto"},
        source_protocol="structured_plan",
    )
    assert blocked.get("status") == "blocked"

    # Host code calling directly may still pre-grant approval.
    granted = action.execute_action(
        action_id,
        {},
        policy_override={"policy_approval_granted": True},
        source_protocol="direct",
    )
    assert granted.get("status") == "success"


def test_sanitize_policy_override_strips_host_only_keys_for_model_sources():
    from agently.core.operation.Action.ActionDispatcher import ActionDispatcher

    override: ActionPolicy = {
        "policy_approval_granted": True,
        "allowed_cmd_prefixes": ["rm"],
    }
    sanitized, stripped = ActionDispatcher._sanitize_policy_override(
        override, source_protocol="native_tool_calls"
    )
    assert "policy_approval_granted" not in sanitized
    assert "allowed_cmd_prefixes" not in sanitized
    assert sanitized == {}
    assert set(stripped) == {"policy_approval_granted", "allowed_cmd_prefixes"}

    kept, none_stripped = ActionDispatcher._sanitize_policy_override(
        override, source_protocol="direct"
    )
    assert kept == override
    assert none_stripped == []


def test_model_sourced_action_input_strips_undeclared_kwargs():
    action = Agently.create_agent().action
    action_id = f"input_safety_{ uuid.uuid4().hex[:8] }"
    received: list[dict[str, Any]] = []

    def capture(**kwargs):
        received.append(dict(kwargs))
        return dict(kwargs)

    action.register_action(
        action_id=action_id,
        desc="Capture received kwargs.",
        kwargs={"value": (int, "")},
        func=capture,
        expose_to_model=False,
    )

    model_result = action.execute_action(
        action_id,
        {"value": 3, "admin": True, "policy": {"approval": "self_grant"}},
        source_protocol="structured_plan",
    )

    assert model_result.get("status") == "success"
    assert model_result.get("data") == {"value": 3}
    assert received[-1] == {"value": 3}
    assert model_result.get("kwargs") == {"value": 3}
    diagnostics = model_result.get("diagnostics")
    assert isinstance(diagnostics, list)
    strip_diagnostic = next(item for item in diagnostics if item.get("code") == "action.input.unexpected_keys_stripped")
    strip_meta = strip_diagnostic.get("meta", {})
    assert strip_meta["source_protocol"] == "structured_plan"
    assert set(strip_meta["stripped_keys"]) == {"admin", "policy"}

    direct_result = action.execute_action(
        action_id,
        {"value": 4, "admin": True},
        source_protocol="direct",
    )
    assert direct_result.get("status") == "success"
    assert received[-1] == {"value": 4, "admin": True}
    assert not any(
        item.get("code") == "action.input.unexpected_keys_stripped"
        for item in direct_result.get("diagnostics", [])
        if isinstance(item, dict)
    )


def test_model_sourced_action_input_strips_spec_host_only_kwargs():
    action = Agently.create_agent().action
    action_id = f"input_host_only_{ uuid.uuid4().hex[:8] }"
    received: list[dict[str, Any]] = []

    def capture(**kwargs):
        received.append(dict(kwargs))
        return dict(kwargs)

    action.register_action(
        action_id=action_id,
        desc="Capture received kwargs.",
        kwargs={"value": (int, ""), "privileged": (bool, "")},
        func=capture,
        expose_to_model=False,
        meta={"host_only_input_keys": ["privileged"]},
    )

    model_result = action.execute_action(
        action_id,
        {"value": 3, "privileged": True},
        source_protocol="structured_plan",
    )

    assert model_result.get("status") == "success"
    assert model_result.get("data") == {"value": 3}
    assert received[-1] == {"value": 3}
    diagnostics = model_result.get("diagnostics")
    assert isinstance(diagnostics, list)
    strip_diagnostic = next(item for item in diagnostics if item.get("code") == "action.input.unexpected_keys_stripped")
    assert strip_diagnostic.get("meta", {}).get("stripped_keys") == ["privileged"]

    direct_result = action.execute_action(
        action_id,
        {"value": 4, "privileged": True},
        source_protocol="direct",
    )
    assert direct_result.get("status") == "success"
    assert received[-1] == {"value": 4, "privileged": True}


def test_model_sourced_bash_action_input_strips_allow_unsafe(tmp_path):
    agent = Agently.create_agent()
    action_id = f"bash_input_safety_{ uuid.uuid4().hex[:8] }"
    target = tmp_path / "unsafe_probe.txt"
    command = "python -c \"open('unsafe_probe.txt','w').write('bypassed')\""

    agent.enable_shell(root=tmp_path, commands=["pwd"], action_id=action_id)
    spec = agent.action.action_registry.get_spec(action_id)
    assert spec is not None
    kwargs = spec.get("kwargs")
    assert isinstance(kwargs, dict)
    assert "allow_unsafe" not in kwargs

    model_result = agent.action.execute_action(
        action_id,
        {"cmd": command, "workdir": str(tmp_path), "allow_unsafe": True},
        source_protocol="structured_plan",
    )

    assert model_result.get("status") == "blocked"
    assert not target.exists()
    assert model_result.get("kwargs") == {"cmd": command, "workdir": str(tmp_path)}
    diagnostics = model_result.get("diagnostics")
    assert isinstance(diagnostics, list)
    strip_diagnostic = next(item for item in diagnostics if item.get("code") == "action.input.unexpected_keys_stripped")
    strip_meta = strip_diagnostic.get("meta", {})
    assert strip_meta["source_protocol"] == "structured_plan"
    assert strip_meta["stripped_keys"] == ["allow_unsafe"]

    direct_result = agent.action.execute_action(
        action_id,
        {"cmd": command, "workdir": str(tmp_path), "allow_unsafe": True},
        source_protocol="direct",
    )
    assert direct_result.get("status") == "success"
    assert target.read_text(encoding="utf-8") == "bypassed"


def test_action_dispatcher_parameter_error_has_structured_diagnostic():
    action = Agently.create_agent().action
    action_id = f"input_type_error_{ uuid.uuid4().hex[:8] }"

    def requires_value(value: int):
        return value

    action.register_action(
        action_id=action_id,
        desc="Require a value.",
        kwargs={"value": (int, "")},
        func=requires_value,
        expose_to_model=False,
    )

    result = action.execute_action(action_id, {}, source_protocol="structured_plan")

    assert result.get("status") == "error"
    diagnostics = result.get("diagnostics")
    assert isinstance(diagnostics, list)
    error_diagnostic = next(item for item in diagnostics if item.get("code") == "action.input.type_error")
    error_meta = error_diagnostic.get("meta", {})
    assert error_meta["exception_type"] == "TypeError"


def test_action_dispatcher_timeout_has_structured_diagnostic():
    action = Agently.create_agent().action
    action_id = f"timeout_diagnostic_{ uuid.uuid4().hex[:8] }"

    async def slow_action():
        await asyncio.sleep(0.05)
        return "done"

    action.register_action(
        action_id=action_id,
        desc="Sleep briefly.",
        kwargs={},
        func=slow_action,
        default_policy={"timeout_seconds": 0.001},
        expose_to_model=False,
    )

    result = action.execute_action(
        action_id,
        {},
        source_protocol="structured_plan",
    )

    assert result.get("status") == "error"
    assert result.get("meta", {}).get("timeout_seconds") == 0.001
    diagnostics = result.get("diagnostics")
    assert isinstance(diagnostics, list)
    timeout_diagnostic = next(item for item in diagnostics if item.get("code") == "action.execution.timeout")
    timeout_meta = timeout_diagnostic.get("meta", {})
    assert timeout_meta["timeout_seconds"] == 0.001


def test_large_action_output_uses_digest_and_artifact_ref():
    action = Agently.create_agent().action
    action_id = f"large_output_{ uuid.uuid4().hex[:8] }"
    stdout = "x" * 12000
    stderr = "y" * 9000

    action.register_action(
        action_id=action_id,
        desc="Return large command-like output.",
        kwargs={},
        func=lambda: {"stdout": stdout, "stderr": stderr, "exit_code": 0},
        expose_to_model=False,
    )

    record = action.execute_action(action_id, {})

    assert record.get("status") == "success"
    assert record.get("data", {}).get("stdout") == stdout
    artifact_refs = record.get("artifact_refs")
    assert isinstance(artifact_refs, list)
    output_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_output")
    assert output_ref.get("role") == "output"
    assert output_ref.get("truncated") is True
    assert output_ref.get("bytes", 0) > output_ref.get("preview_size", 0)
    assert isinstance(output_ref.get("sha256"), str) and len(str(output_ref.get("sha256"))) == 64

    digest = record.get("model_digest")
    assert isinstance(digest, dict)
    preview_meta = digest.get("result_preview_meta")
    assert isinstance(preview_meta, dict)
    assert preview_meta["truncated"] is True
    truncated_paths = preview_meta["truncated_paths"]
    assert any(item["path"] == "stdout" for item in truncated_paths)
    assert any(item["path"] == "stderr" for item in truncated_paths)

    visible = Action.to_action_results([record])
    visible_digest = next(iter(visible.values()))
    assert visible_digest["result_preview_meta"]["truncated"] is True
    assert "artifact_refs" in visible_digest

    recalled = action.read_action_artifact(
        artifact_id=str(output_ref.get("artifact_id", "")),
        action_call_id=str(output_ref.get("action_call_id", "")),
    )
    assert recalled["ok"] is True
    assert recalled["value"]["stdout"] == stdout
    assert recalled["value"]["stderr"] == stderr

    dispatched_recall = action.execute_action(
        "read_action_artifact",
        {
            "artifact_id": str(output_ref.get("artifact_id", "")),
            "action_call_id": str(output_ref.get("action_call_id", "")),
        },
        source_protocol="structured_plan",
    )
    assert dispatched_recall.get("status") == "success"
    assert dispatched_recall.get("data", {}).get("stdout") == stdout
    assert dispatched_recall.get("result", {}).get("stderr") == stderr


def test_max_output_bytes_preserves_full_output_in_artifact():
    action = Agently.create_agent().action
    action_id = f"max_output_preserve_{ uuid.uuid4().hex[:8] }"
    output = "z" * 2000

    action.register_action(
        action_id=action_id,
        desc="Return output larger than policy preview.",
        kwargs={},
        func=lambda: output,
        default_policy={"max_output_bytes": 20},
        expose_to_model=False,
    )

    record = action.execute_action(action_id, {})

    assert record.get("data") == output
    assert record.get("meta", {}).get("max_output_bytes_exceeded") is True
    diagnostics = record.get("diagnostics")
    assert isinstance(diagnostics, list)
    assert any(item.get("code") == "action.output.max_output_bytes_exceeded" for item in diagnostics)
    artifact_refs = record.get("artifact_refs")
    assert isinstance(artifact_refs, list)
    output_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_output")
    recalled = action.read_action_artifact(
        artifact_id=str(output_ref.get("artifact_id", "")),
        action_call_id=str(output_ref.get("action_call_id", "")),
    )
    assert recalled["value"] == output


def test_explicit_action_artifacts_are_preserved_without_large_output():
    action = Agently.create_agent().action
    action_id = f"explicit_artifact_{ uuid.uuid4().hex[:8] }"

    action.register_action(
        action_id=action_id,
        desc="Return an explicit artifact reference.",
        kwargs={},
        func=lambda: {
            "status": "success",
            "data": {"summary": "created"},
            "artifacts": [
                {
                    "artifact_type": "mcp_resource_link",
                    "label": "report.md",
                    "path": "artifacts/report.md",
                    "media_type": "text/markdown",
                    "meta": {"source": "mcp"},
                }
            ],
        },
        expose_to_model=False,
    )

    record = action.execute_action(action_id, {})

    assert record.get("status") == "success"
    artifact_refs = record.get("artifact_refs")
    assert isinstance(artifact_refs, list)
    assert len(artifact_refs) == 1
    artifact_ref = artifact_refs[0]
    assert artifact_ref.get("artifact_type") == "mcp_resource_link"
    assert artifact_ref.get("path") == "artifacts/report.md"
    assert artifact_ref.get("media_type") == "text/markdown"
    meta = artifact_ref.get("meta")
    assert isinstance(meta, dict)
    assert meta.get("source") == "mcp"
    assert record.get("artifacts") == artifact_refs


def test_action_execution_record_dedupes_same_action_call_id():
    action = Agently.create_agent().action
    records = [
        {
            "action_call_id": "act_call_same",
            "status": "success",
            "success": True,
            "action_id": "echo",
            "purpose": "Echo",
            "data": "first",
        },
        {
            "action_call_id": "act_call_same",
            "status": "success",
            "success": True,
            "action_id": "echo",
            "purpose": "Echo",
            "data": "duplicate",
        },
    ]

    normalized = action._artifact_manager.normalize_execution_records(records, [])

    assert len(normalized) == 1
    assert normalized[0].get("data") == "first"


def test_action_dispatcher_fail_closed_handler_returns_approval_required():
    action = Agently.action
    action_id = f"approval_pending_action_{ uuid.uuid4().hex[:8] }"
    action.register_action(
        action_id=action_id,
        desc="Approval gated action.",
        kwargs={},
        func=lambda: "ok",
        approval_required=True,
        expose_to_model=False,
    )

    Agently.configure_policy_approval(handler="fail_closed")
    try:
        legacy = action.call_tool(action_id, {})
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")
    assert legacy["status"] == "approval_required"


def test_action_sandbox_executors(tmp_path):
    action = Agently.action

    python_action_id = f"python_sandbox_{ uuid.uuid4().hex[:8] }"
    bash_action_id = f"bash_sandbox_{ uuid.uuid4().hex[:8] }"

    action.register_python_sandbox_action(action_id=python_action_id)
    python_result = action.execute_action(python_action_id, {"python_code": "result = 1 + 2"})
    assert python_result.get("status") == "success"
    python_data = cast(dict[str, Any], python_result.get("data"))
    assert python_data["result"] == 3

    action.register_bash_sandbox_action(
        action_id=bash_action_id,
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(tmp_path)],
    )
    bash_result = action.execute_action(
        bash_action_id,
        {"cmd": "pwd", "workdir": str(tmp_path)},
    )
    assert bash_result.get("status") == "success"
    bash_data = cast(dict[str, Any], bash_result.get("data"))
    assert bash_data["ok"] is True
    assert str(tmp_path) in bash_data["stdout"]


def test_custom_action_executor_plugin_registration():
    class EchoActionExecutor:
        name = "EchoActionExecutor"
        DEFAULT_SETTINGS = {}
        kind = "custom_echo"
        sandboxed = False

        def __init__(self, *, prefix: str):
            self.prefix = prefix

        @staticmethod
        def _on_register():
            pass

        @staticmethod
        def _on_unregister():
            pass

        async def execute(self, *, spec, action_call, policy, settings):
            _ = (spec, policy, settings)
            action_input = action_call.get("action_input", {})
            if not isinstance(action_input, dict):
                action_input = {}
            value = str(action_input.get("value", ""))
            return f"{ self.prefix }:{ value }"

    plugin_registered = False
    try:
        Agently.plugin_manager.register("ActionExecutor", EchoActionExecutor, activate=False)
        plugin_registered = True

        action = Agently.create_agent().action
        action_id = f"echo_custom_{ uuid.uuid4().hex[:8] }"
        action.register_action(
            action_id=action_id,
            desc="Echo a value through a custom action executor plugin.",
            kwargs={"value": (str, "")},
            executor=action.create_action_executor("EchoActionExecutor", prefix="custom"),
            expose_to_model=False,
        )

        executed = action.execute_action(action_id, {"value": "ok"})
        assert executed.get("status") == "success"
        assert executed.get("data") == "custom:ok"
    finally:
        if plugin_registered:
            Agently.plugin_manager.unregister("ActionExecutor", "EchoActionExecutor")


def test_custom_action_executor_plugin_registration_with_child_plugin_manager():
    class PrefixActionExecutor:
        name = "PrefixActionExecutor"
        DEFAULT_SETTINGS = {}
        kind = "child_prefix"
        sandboxed = False

        def __init__(self, *, prefix: str):
            self.prefix = prefix

        @staticmethod
        def _on_register():
            pass

        @staticmethod
        def _on_unregister():
            pass

        async def execute(self, *, spec, action_call, policy, settings):
            _ = (spec, policy, settings)
            action_input = action_call.get("action_input", {})
            if not isinstance(action_input, dict):
                action_input = {}
            return f"{ self.prefix }:{ action_input.get('value', '') }"

    child_settings = Settings(name="ChildExecutorSettings", parent=Agently.settings)
    child_plugin_manager = PluginManager(
        child_settings,
        parent=Agently.plugin_manager,
        name="ChildExecutorPluginManager",
    )
    child_plugin_manager.register("ActionExecutor", PrefixActionExecutor, activate=False)

    action = Action(child_plugin_manager, child_settings)
    action_id = f"child_executor_{ uuid.uuid4().hex[:8] }"
    action.register_action(
        action_id=action_id,
        desc="Use a child plugin manager action executor.",
        kwargs={"value": (str, "")},
        executor=action.create_action_executor("PrefixActionExecutor", prefix="child"),
        expose_to_model=False,
    )

    result = action.execute_action(action_id, {"value": "ok"})
    assert result.get("status") == "success"
    assert result.get("data") == "child:ok"


def test_custom_action_flow_plugin_registration():
    class StubActionFlow:
        name = "StubActionFlow"
        DEFAULT_SETTINGS = {}

        def __init__(self, *, plugin_manager, settings):
            self.plugin_manager = plugin_manager
            self.settings = settings

        @staticmethod
        def _on_register():
            pass

        @staticmethod
        def _on_unregister():
            pass

        async def async_run(
            self,
            *,
            action,
            prompt,
            settings,
            action_list,
            agent_name="Manual",
            parent_run_context=None,
            planning_handler=None,
            execution_handler=None,
            max_rounds=None,
            concurrency=None,
            timeout=None,
            planning_protocol=None,
        ):
            _ = (
                prompt,
                settings,
                parent_run_context,
                planning_handler,
                execution_handler,
                max_rounds,
                concurrency,
                timeout,
                planning_protocol,
            )
            return [
                {
                    "ok": True,
                    "status": "success",
                    "purpose": f"stub:{ agent_name }",
                    "action_id": "stub_flow",
                    "tool_name": "stub_flow",
                    "kwargs": {"action_count": len(action_list)},
                    "success": True,
                    "result": f"{ getattr(action.action_flow, 'name', '') }:{ len(action_list) }",
                    "data": f"{ getattr(action.action_flow, 'name', '') }:{ len(action_list) }",
                    "error": "",
                }
            ]

    plugin_registered = False
    plugin_manager: PluginManager | None = None
    try:
        settings = Settings(name="StubActionFlowSettings", parent=Agently.settings)
        plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="StubActionFlowPluginManager")
        plugin_manager.register("ActionFlow", StubActionFlow)
        plugin_registered = True

        action = Action(plugin_manager, settings)
        action_id = f"flow_action_{ uuid.uuid4().hex[:8] }"
        tag = f"stub-flow-tag-{ uuid.uuid4().hex[:8] }"
        action.register_action(
            action_id=action_id,
            desc="Flow plugin smoke test.",
            kwargs={},
            func=lambda: "ok",
            tags=[tag],
        )
        prompt = Agently.create_prompt()
        prompt.set("input", "run stub action flow")

        records = action.plan_and_execute(
            prompt=prompt,
            settings=settings,
            action_list=action.get_action_list(tags=[tag]),
            agent_name="stub-flow-agent",
        )

        assert len(records) == 1
        assert records[0].get("result") == "StubActionFlow:1"
        assert getattr(action.action_flow, "name", "") == "StubActionFlow"
    finally:
        if plugin_registered and plugin_manager is not None:
            plugin_manager.unregister("ActionFlow", "StubActionFlow")


def test_use_mcp():
    tool = Agently.tool

    server_script = Path(__file__).with_name("cal_mcp_server.py")
    tool.use_mcp(str(server_script))

    result = tool.call_tool("add", kwargs={"first_number": 1, "second_number": 2})
    assert result["result"] == 3

    result = tool.call_tool("add", kwargs={"a": 1, "b": 2})
    assert "validation error" in result["error"].lower()
    assert "first_number" in result["error"]
    assert "second_number" in result["error"]


@pytest.mark.asyncio
async def test_tool_plan_execute_loop_with_trigger_flow():
    tool = Agently.tool
    tag = f"tool-loop-test-{ uuid.uuid4().hex }"

    async def add_for_loop_test(a: int, b: int):
        await asyncio.sleep(0.01)
        return a + b

    tool.register(
        name=f"add_for_loop_test_{ uuid.uuid4().hex[:8] }",
        desc="Add two integers for tool loop test.",
        kwargs={"a": (int, ""), "b": (int, "")},
        func=add_for_loop_test,
        tags=[tag],
    )
    tool_name = tool.get_tool_list(tags=[tag])[0]["name"]
    prompt = Agently.create_prompt()
    prompt.set("input", "calculate two additions")

    plan_rounds: list[dict] = []

    async def plan_handler(
        context,
        request,
    ):
        _ = request
        done_plans = context.get("done_plans", [])
        last_round_records = context.get("last_round_records", [])
        round_index = context.get("round_index", 0)
        plan_rounds.append(
            {
                "round_index": round_index,
                "done_count": len(done_plans),
                "last_count": len(last_round_records),
            }
        )
        if len(done_plans) == 0:
            return cast(ActionDecision, {
                "next_action": "execute",
                "tool_commands": [
                    {
                        "purpose": "calc_1",
                        "tool_name": tool_name,
                        "tool_kwargs": {"a": 1, "b": 2},
                        "next": "continue",
                    },
                    {
                        "purpose": "calc_2",
                        "tool_name": tool_name,
                        "tool_kwargs": {"a": 3, "b": 4},
                        "next": "continue",
                    },
                ],
            })
        return cast(ActionDecision, {
            "next_action": "response",
            "next": "enough information",
            "tool_commands": [],
        })

    async def execution_handler(
        context,
        request,
    ):
        _ = context
        tool_commands = request.get("action_calls", [])
        async_call_tool = request["async_call_action"]
        concurrency = request.get("concurrency")
        semaphore = asyncio.Semaphore(concurrency or len(tool_commands))

        async def run(command):
            async with semaphore:
                result = await async_call_tool(command["tool_name"], command.get("tool_kwargs", {}))
                return {
                    "purpose": command["purpose"],
                    "tool_name": command["tool_name"],
                    "kwargs": command.get("tool_kwargs", {}),
                    "next": command.get("next", ""),
                    "success": True,
                    "result": result,
                    "error": "",
                }

        return await asyncio.gather(*[run(command) for command in tool_commands])

    records = await tool.async_plan_and_execute(
        prompt=prompt,
        settings=Agently.settings,
        tool_list=tool.get_tool_list(tags=[tag]),
        agent_name="tool-loop-test",
        plan_analysis_handler=plan_handler,  # type: ignore
        tool_execution_handler=execution_handler,  # type: ignore
        max_rounds=3,
        concurrency=2,
        timeout=5,
    )

    assert len(records) == 2
    assert {record["result"] if "result" in record else None for record in records} == {3, 7}
    assert plan_rounds[0]["done_count"] == 0
    assert plan_rounds[1]["done_count"] == 2
    assert plan_rounds[1]["last_count"] == 2


@pytest.mark.asyncio
async def test_action_loop_stops_after_repeated_failed_action_rounds():
    action = Agently.action
    tag = f"failed-action-convergence-{ uuid.uuid4().hex }"
    action_id = f"unstable_search_{ uuid.uuid4().hex[:8] }"
    action.register_action(
        action_id=action_id,
        desc="Unstable search backend.",
        kwargs={"query": (str, "Search query.")},
        func=lambda query: query,
        tags=[tag],
    )
    prompt = Agently.create_prompt()
    prompt.set("input", "find official source")
    plan_rounds: list[int] = []

    async def plan_handler(context, request):
        _ = request
        round_index = int(context["round_index"])
        plan_rounds.append(round_index)
        return cast(ActionDecision, {
            "next_action": "execute",
            "use_action": True,
            "action_calls": [
                {
                    "purpose": "try unstable search",
                    "action_id": action_id,
                    "action_input": {"query": f"query {round_index}"},
                    "todo_suggestion": "continue",
                }
            ],
        })

    async def execution_handler(context, request):
        _ = context
        return [
            {
                "purpose": "try unstable search",
                "action_id": command.get("action_id"),
                "tool_name": command.get("action_id"),
                "kwargs": command.get("action_input", {}),
                "success": False,
                "status": "error",
                "result": None,
                "data": None,
                "error": "backend unavailable",
            }
            for command in request.get("action_calls", [])
        ]

    try:
        records = await action.async_plan_and_execute(
            prompt=prompt,
            settings=Agently.settings,
            action_list=action.get_action_list(tags=[tag]),
            agent_name="failed-action-convergence-test",
            planning_handler=plan_handler,  # type: ignore[arg-type]
            action_execution_handler=execution_handler,  # type: ignore[arg-type]
            max_rounds=5,
            timeout=5,
        )
    finally:
        action.unregister_action(action_id)

    assert plan_rounds == [0, 1]
    assert len(records) == 2
    assert all(record.get("action_id") == action_id for record in records)
    assert all(record.get("status") == "error" for record in records)


@pytest.mark.asyncio
async def test_action_generate_native_tool_calls_matches_structured(monkeypatch):
    action = Agently.action
    tag = f"native-tool-call-{ uuid.uuid4().hex }"
    action_id = f"search_docs_{ uuid.uuid4().hex[:8] }"

    action.register_action(
        action_id=action_id,
        desc="Search docs.",
        kwargs={"query": (str, "")},
        func=lambda query: query,
        tags=[tag],
    )

    prompt = Agently.create_prompt()
    prompt.set("input", "find Agently TriggerFlow docs")
    action_list = action.get_action_list(tags=[tag])

    async def structured_handler(
        context,
        request,
    ):
        _ = (context, request)
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "purpose": "search docs",
                    "action_id": action_id,
                    "action_input": {"query": "Agently TriggerFlow"},
                    "todo_suggestion": "respond",
                }
            ],
        }

    structured = await action.async_generate_action_call(
        prompt=prompt,
        settings=Agently.settings,
        action_list=action_list,
        agent_name="native-tool-call-test",
        planning_handler=structured_handler,
    )

    class FakeResponse:
        def get_async_generator(self, type=None, specific=None, **kwargs):
            _ = kwargs
            assert type == "specific"
            assert specific == ["tool_calls", "delta", "done"]

            async def gen():
                yield (
                    "tool_calls",
                    [
                        {
                            "index": 0,
                            "type": "function",
                            "function": {
                                "name": action_id,
                                "arguments": '{"query": "Agently TriggerFlow"}',
                            },
                        }
                    ],
                )
                yield ("done", "")

            return gen()

    class FakeModelRequest:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)
            self.prompt = SimpleNamespace(set=lambda *a, **k: None)

        def input(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def info(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def instruct(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def get_response(self, *, parent_run_context=None):
            _ = parent_run_context
            return FakeResponse()

    import agently.core as core_module

    monkeypatch.setattr(core_module, "ModelRequest", FakeModelRequest)

    native = await action.async_generate_action_call(
        prompt=prompt,
        settings=Agently.settings,
        action_list=action_list,
        agent_name="native-tool-call-test",
        planning_protocol="native_tool_calls",
    )

    assert len(native) == 1
    assert len(structured) == 1
    native_first = cast(dict[str, Any], native[0])
    structured_first = cast(dict[str, Any], structured[0])
    assert native_first["action_id"] == structured_first["action_id"] == action_id
    assert native_first["action_input"] == structured_first["action_input"] == {"query": "Agently TriggerFlow"}


@pytest.mark.asyncio
async def test_native_tool_calls_empty_result_surfaces_planning_diagnostic(monkeypatch):
    action = Agently.action
    tag = f"native-tool-empty-{ uuid.uuid4().hex }"
    action_id = f"search_docs_{ uuid.uuid4().hex[:8] }"

    action.register_action(
        action_id=action_id,
        desc="Search docs.",
        kwargs={"query": (str, "")},
        func=lambda query: query,
        tags=[tag],
    )

    prompt = Agently.create_prompt()
    prompt.set("input", "inspect the repository")
    action_list = action.get_action_list(tags=[tag])

    class FakeResponse:
        def get_async_generator(self, type=None, specific=None, **kwargs):
            _ = kwargs
            assert type == "specific"
            assert specific == ["tool_calls", "delta", "done"]

            async def gen():
                yield ("delta", "<bash><command>pwd</command></bash>")
                yield ("done", "")

            return gen()

    class FakeModelRequest:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)
            self.prompt = SimpleNamespace(set=lambda *a, **k: None)

        def input(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def info(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def instruct(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def get_response(self, *, parent_run_context=None):
            _ = parent_run_context
            return FakeResponse()

    import agently.core as core_module

    monkeypatch.setattr(core_module, "ModelRequest", FakeModelRequest)

    records = await action.async_plan_and_execute(
        prompt=prompt,
        settings=Agently.settings,
        action_list=action_list,
        agent_name="native-tool-empty-test",
        max_rounds=1,
        timeout=2,
        planning_protocol="native_tool_calls",
    )

    assert len(records) == 1
    diagnostic_record = records[0]
    assert diagnostic_record.get("status") == "skipped"
    assert diagnostic_record.get("action_id") == "action_planning"
    diagnostics = diagnostic_record.get("diagnostics", [])
    assert isinstance(diagnostics, list)
    first_diagnostic = diagnostics[0]
    assert first_diagnostic.get("code") == "action_runtime.native_tool_calls.empty"
    meta = first_diagnostic.get("meta", {})
    assert isinstance(meta, dict)
    assert meta.get("textual_tool_markup_detected") is True
