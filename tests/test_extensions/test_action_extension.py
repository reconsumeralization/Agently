import pytest
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

import json
import os
import asyncio
import time
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from agently import Agently
from agently.core import PluginManager, RuntimeStageStallError
from agently.types.data import AgentlyRequestData
from agently.types.data import StreamingData
from agently.utils import Settings


class MockActionExtensionRequester:
    name = "MockActionExtensionRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        prompt_object = self.prompt.to_prompt_object()
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "messages": self.prompt.to_messages(),
                "prompt_text": self.prompt.to_text(),
                "output_format": prompt_object.output_format,
                "action_results": self.prompt.get("action_results"),
            },
            request_options={"stream": True},
            request_url="mock://tool-extension",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        action_results = request_data.data.get("action_results", {})
        if isinstance(action_results, dict):
            result_value = action_results.get("Use add")
            if result_value is None:
                result_value = action_results.get("Use add (2)")
        else:
            result_value = None
        yield "message", json.dumps({"result": result_value}, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {"provider": "mock-tool-extension"}


def _create_test_agent():
    settings = Settings(name="ActionExtensionTestSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="ActionExtensionTestPluginManager")
    plugin_manager.register("ModelRequester", MockActionExtensionRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="tool-extension-agent",
    )


def test_action_extension():
    agent = _create_test_agent()

    @agent.tool_func
    async def add(a: int, b: int) -> int:
        """
        Get result of `a(int)` add `b(int)`
        """
        await asyncio.sleep(1)
        assert a == 34643523
        return a + b

    async def fake_plan_handler(
        context,
        request,
    ):
        _ = request
        done_plans = context.get("done_plans", [])
        if len(done_plans) == 0:
            return {
                "next_action": "execute",
                "execution_commands": [
                    {
                        "purpose": "Use add",
                        "tool_name": "add",
                        "tool_kwargs": {"a": 34643523, "b": 52131231},
                        "todo_suggestion": "respond",
                    }
                ],
            }
        return {
            "next_action": "response",
            "execution_commands": [],
        }

    agent.register_tool_plan_analysis_handler(fake_plan_handler)

    result = (
        agent.input("34643523+52131231=? Use tool to calculate!")
        .use_tool(add)
        .output(
            {
                "result": (int,),
            }
        )
        .start()
    )
    assert result["result"] == 86774754


def test_action_extension_set_tool_loop_config():
    agent = Agently.create_agent()
    assert agent.action is agent.tool
    assert callable(agent.use_actions)
    assert callable(agent.enable_workspace_file_actions)
    assert callable(agent.enable_coding_agent_actions)
    assert callable(agent.use_workspace_file_actions)
    assert callable(agent.action_func)
    agent.set_tool_loop(
        enabled=True,
        max_rounds=3,
        concurrency=2,
        timeout=6.5,
    )
    assert agent.settings.get("tool.loop.enabled") is True
    assert agent.settings.get("tool.loop.max_rounds") == 3
    assert agent.settings.get("tool.loop.concurrency") == 2
    assert agent.settings.get("tool.loop.timeout") == 6.5


def test_action_extension_default_loop_has_no_round_cap():
    agent = Agently.create_agent()

    assert agent.settings.get("action.loop.max_rounds") is None
    assert agent.settings.get("tool.loop.max_rounds") is None
    assert agent.action.action_settings.get("loop.max_rounds") is None
    assert agent.action.tool_settings.get("loop.max_rounds") is None


@pytest.mark.asyncio
async def test_action_flow_max_rounds_returns_diagnostic_without_executing():
    agent = Agently.create_agent()
    agent.input("Keep using actions.")
    action_list = [{"action_id": "dummy_action", "name": "dummy_action", "desc": "Dummy action.", "kwargs": {}}]
    executed = False

    async def fake_plan_handler(context, request):
        _ = (context, request)
        return {
            "next_action": "execute",
            "use_action": True,
            "action_calls": [
                {
                    "purpose": "keep going",
                    "action_id": "dummy_action",
                    "action_input": {},
                    "policy_override": {},
                    "todo_suggestion": "Run another action round.",
                }
            ],
        }

    async def fake_execution_handler(context, request):
        nonlocal executed
        _ = (context, request)
        executed = True
        return []

    records = await agent.action.async_plan_and_execute(
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=action_list,
        planning_handler=fake_plan_handler,
        action_execution_handler=fake_execution_handler,
        max_rounds=0,
    )

    assert executed is False
    assert len(records) == 1
    assert records[0].get("status") == "blocked"
    assert records[0].get("action_id") == "action_loop"
    diagnostics = records[0].get("diagnostics", [])
    assert isinstance(diagnostics, list)
    assert diagnostics[0].get("code") == "action_loop.max_rounds_reached"


@pytest.mark.asyncio
async def test_action_flow_max_rounds_stops_before_extra_planning_round():
    agent = Agently.create_agent()
    agent.input("Use one action round, then stop.")
    action_list = [{"action_id": "dummy_action", "name": "dummy_action", "desc": "Dummy action.", "kwargs": {}}]
    planning_calls = 0
    execution_calls = 0

    async def fake_plan_handler(context, request):
        nonlocal planning_calls
        _ = (context, request)
        planning_calls += 1
        return {
            "next_action": "execute",
            "use_action": True,
            "action_calls": [
                {
                    "purpose": "first action",
                    "action_id": "dummy_action",
                    "action_input": {},
                    "policy_override": {},
                    "todo_suggestion": "Run another action round.",
                }
            ],
        }

    async def fake_execution_handler(context, request):
        nonlocal execution_calls
        _ = (context, request)
        execution_calls += 1
        return [
            {
                "ok": True,
                "status": "success",
                "success": True,
                "purpose": "first action",
                "action_id": "dummy_action",
                "kwargs": {},
                "result": "ok",
                "data": "ok",
                "error": "",
            }
        ]

    records = await agent.action.async_plan_and_execute(
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=action_list,
        planning_handler=fake_plan_handler,
        action_execution_handler=fake_execution_handler,
        max_rounds=1,
    )

    assert planning_calls == 1
    assert execution_calls == 1
    assert [record.get("action_id") for record in records] == ["dummy_action", "action_loop"]
    assert records[-1].get("status") == "blocked"
    diagnostics = records[-1].get("diagnostics", [])
    assert isinstance(diagnostics, list)
    assert diagnostics[0].get("code") == "action_loop.max_rounds_reached"


def test_action_extension_use_sandbox_registers_agent_scoped_bash_action(tmp_path):
    agent = Agently.create_agent()
    action_id = f"agent_bash_sandbox_{ agent.name }"
    agent.use_sandbox(
        "bash",
        action_id=action_id,
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(tmp_path)],
        sandbox_mode="trusted_local",
    )

    action_list = agent.action.get_action_list(tags=[f"agent-{ agent.name }"])
    assert any(action.get("action_id") == action_id for action in action_list)

    result = agent.action.execute_action(
        action_id,
        {"cmd": "pwd", "workdir": str(tmp_path)},
    )
    assert result.get("status") == "success"
    assert str(tmp_path) in str(result.get("data"))


def test_action_extension_enable_python_registers_run_python_action():
    agent = Agently.create_agent()
    agent.enable_python(
        action_id="test_run_python",
        desc="Use this only for arithmetic tests.",
        sandbox="trusted_local",
    )

    action_list = agent.action.get_action_list(tags=[f"agent-{ agent.name }"])
    assert any(action.get("action_id") == "test_run_python" for action in action_list)
    spec = agent.action.action_registry.get_spec("test_run_python")
    assert spec is not None
    spec_desc = str(spec.get("desc", ""))
    assert "explicitly trusted local in-process execution resource" in spec_desc
    assert "Use this only for arithmetic tests." in spec_desc

    result = agent.action.execute_action(
        "test_run_python",
        {"python_code": ["numbers = [1, 2, 3]", "result = sum(numbers)"]},
    )
    assert result.get("status") == "success"
    assert result.get("data", {}).get("result") == 6
    assert Agently.execution_resource.list(scope="action_call") == []


def test_action_extension_enable_python_defaults_to_docker_profile():
    agent = Agently.create_agent()
    agent.enable_python(action_id="docker_default_python", desc="Use this only for arithmetic tests.")

    spec = agent.action.action_registry.get_spec("docker_default_python")
    assert spec is not None
    assert set(spec.get("kwargs", {}).keys()) == {"python_code"}
    assert "packages" not in spec.get("kwargs", {})
    spec_desc = str(spec.get("desc", ""))
    assert "Docker-backed" in spec_desc
    assert "Dependency installation" in spec_desc

    requirements = spec.get("execution_resources", [])
    assert len(requirements) == 1
    requirement = cast(dict[str, Any], requirements[0])
    assert requirement["kind"] == "docker"
    assert requirement["resource_key"] == "docker_default_python"
    profile = requirement["config"]["runtime_profile"]
    assert profile["language"] == "python"
    assert profile["image"] == "python:3.12-slim"
    assert profile["dependency_policy"] == {"mode": "deny"}
    assert requirement["policy"]["network_mode"] == "disabled"


def test_action_extension_use_action_sandbox_defaults_to_docker_profile():
    agent = Agently.create_agent()
    agent.use_action_sandbox("python", action_id="alias_default_python")

    spec = agent.action.action_registry.get_spec("alias_default_python")
    assert spec is not None
    requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
    assert requirement["kind"] == "docker"
    assert requirement["config"]["runtime_profile"]["language"] == "python"


def test_action_extension_enable_python_dependency_request_requires_resource_approval():
    agent = Agently.create_agent()
    agent.enable_python(
        action_id="dependency_request_python",
        dependency_policy="request",
    )

    spec = agent.action.action_registry.get_spec("dependency_request_python")
    assert spec is not None
    assert set(spec.get("kwargs", {}).keys()) == {"python_code"}
    requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
    assert requirement["kind"] == "docker"
    assert requirement["approval_required"] is True
    assert requirement["config"]["runtime_profile"]["dependency_policy"] == {"mode": "request"}


def test_action_extension_enable_python_trusted_local_keeps_legacy_resource():
    agent = Agently.create_agent()
    agent.enable_python(action_id="trusted_local_python", sandbox="trusted_local")

    spec = agent.action.action_registry.get_spec("trusted_local_python")
    assert spec is not None
    requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
    assert requirement["kind"] == "python"

    result = agent.action.execute_action(
        "trusted_local_python",
        {"python_code": ["numbers = [1, 2, 3]", "result = sum(numbers)"]},
    )
    assert result.get("status") == "success"
    assert result.get("data", {}).get("result") == 6


def test_action_extension_default_introspection_includes_agent_scoped_actions():
    agent = Agently.create_agent()
    action_id = f"agent_visible_probe_{ agent.name }"
    agent.register_action(
        name=action_id,
        desc="Agent-scoped action visible through default introspection.",
        kwargs={"value": (int, "value to echo")},
        func=lambda value: value,
    )

    action_info = agent.action.get_action_info()
    tool_info = agent.action.get_tool_info()

    assert action_id in action_info
    assert action_info[action_id]["kwargs"] == {"value": (int, "value to echo")}
    assert action_id in tool_info
    assert tool_info[action_id]["kwargs"] == {"value": (int, "value to echo")}


def test_action_extension_enable_shell_registers_run_bash_action(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(
        root=tmp_path,
        commands=["pwd"],
        action_id="test_run_bash",
        desc="Only inspect the cwd.",
        sandbox="trusted_local",
    )

    spec = agent.action.action_registry.get_spec("test_run_bash")
    assert spec is not None
    spec_desc = str(spec.get("desc", ""))
    assert "allowlisted shell command" in spec_desc
    assert "Only inspect the cwd." in spec_desc
    assert "Allowed command prefixes: pwd." in spec_desc
    assert f"Allowed working directory roots: {tmp_path}" in spec_desc
    assert "Timeout: 20 seconds." in spec_desc
    assert "Output preview limit: 20000 characters per stream." in spec_desc
    assert "Prefer dedicated Workspace actions" in spec_desc

    result = agent.action.execute_action(
        "test_run_bash",
        {"cmd": "pwd", "workdir": str(tmp_path)},
    )
    assert result.get("status") == "success"
    assert str(tmp_path) in str(result.get("data"))
    assert Agently.execution_resource.list(scope="action_call") == []


def test_action_extension_enable_shell_defaults_to_docker_profile(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(root=tmp_path, commands=["pwd"], action_id="docker_default_bash")

    spec = agent.action.action_registry.get_spec("docker_default_bash")
    assert spec is not None
    assert set(spec.get("kwargs", {}).keys()) == {"cmd", "workdir"}
    spec_desc = str(spec.get("desc", ""))
    assert "Docker-backed" in spec_desc
    assert "Allowed command prefixes: pwd." in spec_desc

    requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
    assert requirement["kind"] == "docker"
    assert requirement["resource_key"] == "docker_default_bash"
    profile = requirement["config"]["runtime_profile"]
    assert profile["language"] == "shell"
    assert profile["allowed_cmd_prefixes"] == ["pwd"]
    assert profile["allowed_workdir_roots"] == [str(tmp_path.resolve())]
    assert requirement["policy"]["network_mode"] == "disabled"


def test_action_extension_enable_shell_defaults_to_safe_profile(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(root=tmp_path, action_id="default_safe_bash", sandbox="trusted_local")

    allowed = agent.action.execute_action(
        "default_safe_bash",
        {"cmd": "pwd"},
    )
    blocked = agent.action.execute_action(
        "default_safe_bash",
        {"cmd": "python -c 'print(1)'"},
    )

    assert allowed.get("status") == "success"
    assert str(tmp_path) in str(allowed.get("data", {}).get("stdout", ""))
    assert blocked.get("status") == "blocked"
    assert blocked.get("reason") == "cmd_not_allowed"
    assert blocked.get("diagnostics", [{}])[0].get("code") == "shell.cmd_not_allowed"


def test_action_extension_enable_shell_redacts_env_in_action_info(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(
        root=tmp_path,
        commands=["pwd"],
        action_id="redacted_env_bash",
        sandbox="trusted_local",
        env={
            "PUBLIC_FLAG": "1",
            "SECRET_TOKEN": "should-not-be-model-visible",
        },
    )

    raw_spec = agent.action.action_registry.get_spec("redacted_env_bash")
    assert raw_spec is not None
    raw_environments = raw_spec.get("execution_resources", [])
    assert isinstance(raw_environments, list)
    raw_config = raw_environments[0].get("config", {})
    assert isinstance(raw_config, dict)
    raw_env = raw_config.get("env", {})
    assert isinstance(raw_env, dict)
    assert raw_env["SECRET_TOKEN"] == "should-not-be-model-visible"

    action_info = agent.action.get_action_info()["redacted_env_bash"]
    visible_environments = action_info.get("execution_resources", [])
    assert isinstance(visible_environments, list)
    visible_config = visible_environments[0].get("config", {})
    assert isinstance(visible_config, dict)
    visible_env = visible_config.get("env", {})
    assert visible_env == {
        "PUBLIC_FLAG": "[REDACTED]",
        "SECRET_TOKEN": "[REDACTED]",
    }
    assert "should-not-be-model-visible" not in str(action_info)


def test_action_extension_enable_shell_supports_multi_token_command_prefixes(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(
        root=tmp_path,
        commands=["echo allowed"],
        action_id="test_prefix_bash",
        sandbox="trusted_local",
    )

    allowed = agent.action.execute_action(
        "test_prefix_bash",
        {"cmd": "echo allowed value", "workdir": str(tmp_path)},
    )
    blocked = agent.action.execute_action(
        "test_prefix_bash",
        {"cmd": "echo denied value", "workdir": str(tmp_path)},
    )

    assert allowed.get("status") == "success"
    assert "allowed value" in str(allowed.get("data", {}).get("stdout", ""))
    assert blocked.get("status") == "blocked"
    assert blocked.get("reason") == "cmd_not_allowed"


def test_action_extension_enable_shell_uses_root_as_default_workdir(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(
        root=tmp_path,
        commands=["pwd"],
        action_id="default_workdir_bash",
        sandbox="trusted_local",
    )

    result = agent.action.execute_action(
        "default_workdir_bash",
        {"cmd": ["pwd"]},
    )

    assert result.get("status") == "success"
    assert str(tmp_path) in str(result.get("data", {}).get("stdout", ""))


def test_action_extension_enable_shell_persists_large_output_artifacts(tmp_path):
    source_path = tmp_path / "big.txt"
    source_text = "x" * 80
    source_path.write_text(source_text, encoding="utf-8")
    agent = Agently.create_agent().use_workspace(tmp_path, mode="read_write")
    agent.enable_shell(
        root=tmp_path,
        commands=["cat"],
        action_id="bounded_output_bash",
        max_output_chars=12,
        sandbox="trusted_local",
    )

    result = agent.action.execute_action(
        "bounded_output_bash",
        {"cmd": "cat big.txt"},
    )

    data = result.get("data", {})
    assert result.get("status") == "success"
    assert data.get("stdout") == source_text[:12]
    assert data.get("stdout_truncated") is True
    artifacts = data.get("output_artifacts", [])
    assert len(artifacts) == 1
    artifact_path = artifacts[0]["path"]
    assert artifacts[0]["stream"] == "stdout"
    assert artifacts[0]["relative_path"].startswith(
        f".agently/files/{agent.workspace.execution_id}/shell-output/"
    )
    assert os.path.exists(artifact_path)
    with open(artifact_path, encoding="utf-8") as handle:
        assert handle.read() == source_text


def test_action_extension_enable_shell_reports_timeout(tmp_path):
    python_prefix = f"{Path(sys.executable).name} -c"
    agent = Agently.create_agent()
    agent.enable_shell(
        root=tmp_path,
        commands=[python_prefix],
        action_id="timeout_bash",
        timeout=1,
        sandbox="trusted_local",
    )

    result = agent.action.execute_action(
        "timeout_bash",
        {"cmd": [sys.executable, "-c", "import time; time.sleep(2)"]},
    )

    data = result.get("data", {})
    assert result.get("status") == "error"
    assert data.get("status") == "timed_out"
    assert data.get("reason") == "command_timeout"
    assert data.get("timeout_seconds") == 1
    assert data.get("diagnostics", [{}])[0].get("code") == "shell.command_timeout"


def test_action_extension_enable_helper_desc_modes():
    agent = Agently.create_agent()

    agent.enable_python(action_id="append_python", desc="Only use for sums.")
    append_spec = agent.action.action_registry.get_spec("append_python")
    assert append_spec is not None
    append_desc = str(append_spec.get("desc", ""))
    assert "Docker-backed sandbox" in append_desc
    assert "Only use for sums." in append_desc

    agent.enable_python(action_id="override_python", desc="Custom calculator only.", desc_mode="override")
    override_spec = agent.action.action_registry.get_spec("override_python")
    assert override_spec is not None
    override_desc = str(override_spec.get("desc", ""))
    assert override_desc == "Custom calculator only."

    agent.enable_python(action_id="default_python", desc="Ignored guidance.", desc_mode="default")
    default_spec = agent.action.action_registry.get_spec("default_python")
    assert default_spec is not None
    default_desc = str(default_spec.get("desc", ""))
    assert "Docker-backed sandbox" in default_desc
    assert "Ignored guidance." not in default_desc

    bad_mode: Any = "replace"
    with pytest.raises(ValueError, match="desc_mode"):
        agent.enable_python(action_id="bad_desc_mode", desc="x", desc_mode=bad_mode)


def test_action_extension_enable_nodejs_defaults_to_docker_profile():
    agent = Agently.create_agent()
    agent.enable_nodejs(action_id="docker_default_node")

    spec = agent.action.action_registry.get_spec("docker_default_node")
    assert spec is not None
    assert set(spec.get("kwargs", {}).keys()) == {"js_code", "args"}
    assert "packages" not in spec.get("kwargs", {})
    spec_desc = str(spec.get("desc", ""))
    assert "Docker-backed" in spec_desc

    requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
    assert requirement["kind"] == "docker"
    assert requirement["resource_key"] == "docker_default_node"
    profile = requirement["config"]["runtime_profile"]
    assert profile["language"] == "nodejs"
    assert profile["image"] == "node:22-slim"
    assert profile["dependency_policy"] == {"mode": "deny"}
    assert requirement["policy"]["network_mode"] == "disabled"


def test_action_extension_enable_code_runtime_go_uses_developer_docker_profile():
    agent = Agently.create_agent()
    agent.enable_code_runtime(
        language="go",
        action_id="run_go_code",
        provisioning_profile="developer",
    )

    spec = agent.action.action_registry.get_spec("run_go_code")
    assert spec is not None
    assert set(spec.get("kwargs", {}).keys()) == {"source_code", "files", "args"}
    assert "build_cmd" not in spec.get("kwargs", {})
    spec_desc = str(spec.get("desc", ""))
    assert "Docker-backed" in spec_desc
    assert "Go" in spec_desc

    requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
    assert requirement["kind"] == "docker"
    assert requirement["resource_key"] == "run_go_code"
    profile = requirement["config"]["runtime_profile"]
    assert profile["language"] == "go"
    assert profile["image"] == "golang:1"
    assert profile["provisioning_profile"] == "developer"
    assert profile["image_pull_policy"] == "if_missing"
    assert profile["dependency_policy"] == {"mode": "install"}


def test_action_extension_enable_code_runtime_common_language_catalog():
    expected = {
        "python": ("python", "python:3.12-slim", "main.py"),
        "javascript": ("nodejs", "node:22-slim", "main.js"),
        "typescript": ("typescript", "denoland/deno:alpine", "main.ts"),
        "c": ("c", "gcc:14", "main.c"),
        "cpp": ("cpp", "gcc:14", "main.cpp"),
        "go": ("go", "golang:1", "main.go"),
        "rust": ("rust", "rust:1", "main.rs"),
        "java": ("java", "maven:3-eclipse-temurin-21", "Main.java"),
        "csharp": ("csharp", "mcr.microsoft.com/dotnet/sdk:8.0", "Program.cs"),
        "php": ("php", "php:8.3-cli", "main.php"),
        "ruby": ("ruby", "ruby:3.3", "main.rb"),
        "perl": ("perl", "perl:5.40", "main.pl"),
        "r": ("r", "r-base:4.4", "main.R"),
        "lua": ("lua", "nickblah/lua:5.4", "main.lua"),
        "bash": ("bash", "bash:5", "main.sh"),
    }
    agent = Agently.create_agent()

    for requested, (canonical, image, source_file) in expected.items():
        action_id = f"run_{ canonical }_catalog"
        agent.enable_code_runtime(language=requested, action_id=action_id)
        spec = agent.action.action_registry.get_spec(action_id)
        assert spec is not None
        requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
        profile = requirement["config"]["runtime_profile"]
        assert profile["language"] == canonical
        assert profile["image"] == image
        assert profile["source_file"] == source_file
        assert "entrypoint" in profile


def test_action_extension_enable_nodejs_trusted_local_keeps_legacy_resource(tmp_path):
    agent = Agently.create_agent()
    agent.enable_nodejs(action_id="trusted_local_node", sandbox="trusted_local", cwd=str(tmp_path))

    spec = agent.action.action_registry.get_spec("trusted_local_node")
    assert spec is not None
    requirement = cast(dict[str, Any], spec.get("execution_resources", [])[0])
    assert requirement["kind"] == "node"
    assert requirement["config"]["cwd"] == str(tmp_path)


def test_action_extension_enable_workspace_file_actions_registers_file_actions(tmp_path):
    agent = Agently.create_agent()
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "todo.txt").write_text("fix runtime docs\nship examples\n", encoding="utf-8")

    agent.enable_workspace_file_actions(root=tmp_path, write=True, desc="Project notes workspace.")

    spec = agent.action.action_registry.get_spec("read_file")
    assert spec is not None
    spec_desc = str(spec.get("desc", ""))
    assert "registered Workspace file IO handlers" in spec_desc
    assert "Project notes workspace." in spec_desc
    assert agent.action.action_registry.get_spec("export_file") is None

    listed = agent.action.execute_action("list_files", {"path": "notes"})
    assert listed.get("status") == "success"
    assert listed.get("data") == ["notes/todo.txt"]

    searched = agent.action.execute_action("search_files", {"query": "runtime", "path": "notes"})
    assert searched.get("status") == "success"
    assert searched.get("data", [])[0]["line"] == 1

    read = agent.action.execute_action("read_file", {"path": "notes/todo.txt"})
    assert read.get("status") == "success"
    assert "ship examples" in read.get("data", {}).get("content", "")
    assert read.get("data", {}).get("sha256")

    (tmp_path / "notes" / "payload.bin").write_bytes(b"\x00\xffbinary")
    binary_read = agent.action.execute_action("read_file", {"path": "notes/payload.bin"})
    assert binary_read.get("status") == "success"
    assert binary_read.get("data", {}).get("readable") is False
    assert binary_read.get("data", {}).get("diagnostics", [])[0]["code"] == "workspace.file.no_read_handler"

    written = agent.action.execute_action("write_file", {"path": "notes/out.txt", "content": "ok"})
    assert written.get("status") == "success"
    assert (tmp_path / "notes" / "out.txt").read_text(encoding="utf-8") == "ok"

    outside = agent.action.execute_action("read_file", {"path": "../outside.txt"})
    assert outside.get("status") == "error"


def test_action_extension_workspace_file_actions_export_flag_and_idempotent_user_action(tmp_path):
    agent = Agently.create_agent()
    (tmp_path / "input.md").write_text("# Hello\n", encoding="utf-8")
    agent.register_action(
        name="read_file",
        desc="User-owned read file action.",
        kwargs={"path": (str, "path")},
        func=lambda path: {"user_action": path},
    )

    agent.enable_workspace_file_actions(root=tmp_path, write=True, export=True)

    assert agent.action.execute_action("read_file", {"path": "input.md"}).get("data") == {"user_action": "input.md"}
    assert agent.action.action_registry.get_spec("export_file") is not None
    export_result = agent.action.execute_action(
        "export_file",
        {
            "source_path": "input.md",
            "output_path": "out.pdf",
            "export_kind": "unknown_export",
        },
    )
    assert export_result.get("status") == "success"
    assert export_result.get("data", {}).get("exported") is False
    assert export_result.get("data", {}).get("diagnostics", [])[0]["code"] == "workspace.file.no_export_handler"


def test_action_extension_enable_coding_agent_actions_registers_guarded_file_tools(tmp_path):
    agent = Agently.create_agent("coding-agent-actions").use_workspace(
        tmp_path / "run",
        mode="read_write",
    )
    workspace = agent.workspace
    assert workspace is not None
    (workspace.root / "src").mkdir(parents=True)
    (workspace.root / "src" / "app.py").write_text("print('old')\n", encoding="utf-8")
    (workspace.root / "src" / "notes.md").write_text("Project Atlas\n", encoding="utf-8")

    agent.enable_coding_agent_actions()

    for action_id in ("read_file", "write_file", "edit_file", "apply_patch", "glob_files", "grep_files"):
        spec = agent.action.action_registry.get_spec(action_id)
        assert spec is not None
        assert spec.get("meta", {}).get("coding_agent") is True or action_id in {"read_file", "write_file"}

    globbed = agent.action.execute_action("glob_files", {"pattern": "*.py", "path": "src"})
    assert globbed.get("status") == "success"
    assert globbed.get("data", {}).get("matches") == ["src/app.py"]

    grepped = agent.action.execute_action("grep_files", {"pattern": "Project\\s+Atlas", "path": "src", "glob": "*.md"})
    assert grepped.get("status") == "success"
    assert grepped.get("data", {}).get("matches", [])[0]["path"] == "src/notes.md"

    stale_write = agent.action.execute_action("write_file", {"path": "src/app.py", "content": "blocked\n"})
    assert stale_write.get("status") == "error"

    read = agent.action.execute_action("read_file", {"path": "src/app.py"})
    assert read.get("status") == "success"
    original_sha = read.get("data", {}).get("sha256")

    edited = agent.action.execute_action(
        "edit_file",
        {
            "path": "src/app.py",
            "old_string": "print('old')",
            "new_string": "print('new')",
        },
    )
    assert edited.get("status") == "success"
    assert "print('new')" in (workspace.root / "src" / "app.py").read_text(encoding="utf-8")

    (workspace.root / "src" / "app.py").write_text("print('user change')\n", encoding="utf-8")
    stale_edit = agent.action.execute_action(
        "edit_file",
        {
            "path": "src/app.py",
            "old_string": "user",
            "new_string": "agent",
            "expected_sha256": original_sha,
        },
    )
    assert stale_edit.get("status") == "error"

    reread = agent.action.execute_action("read_file", {"path": "src/app.py"})
    current_sha = reread.get("data", {}).get("sha256")
    written = agent.action.execute_action(
        "write_file",
        {"path": "src/app.py", "content": "print('ready')\n", "expected_sha256": current_sha},
    )
    assert written.get("status") == "success"

    patch = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-print('ready')
+print('patched')
"""
    agent.action.execute_action("read_file", {"path": "src/app.py"})
    patched = agent.action.execute_action(
        "apply_patch",
        {"patch": patch, "expected_files": ["src/app.py"]},
    )
    assert patched.get("status") == "success"
    assert "print('patched')" in (workspace.root / "src" / "app.py").read_text(encoding="utf-8")

    outside = agent.action.execute_action("edit_file", {"path": "../outside.py", "old_string": "", "new_string": "x"})
    assert outside.get("status") == "error"


def test_action_extension_enable_workspace_file_actions_inherits_foundation_workspace(tmp_path):
    agent = Agently.create_agent().use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    (workspace.root / "notes").mkdir(parents=True)
    (workspace.root / "notes" / "todo.txt").write_text("use foundation workspace\n", encoding="utf-8")

    agent.enable_workspace_file_actions()

    spec = agent.action.action_registry.get_spec("read_file")
    assert spec is not None
    assert spec.get("meta", {}).get("root") == str(workspace.root)

    listed = agent.action.execute_action("list_files", {"path": "notes"})
    assert listed.get("status") == "success"
    assert listed.get("data") == ["notes/todo.txt"]

    read = agent.action.execute_action("read_file", {"path": "notes/todo.txt"})
    assert read.get("status") == "success"
    assert read.get("data", {}).get("path") == "notes/todo.txt"


def test_action_extension_enable_workspace_file_actions_uses_default_workspace_without_private_state(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    agent = Agently.create_agent()
    workspace = agent.workspace

    agent.enable_workspace_file_actions()

    spec = agent.action.action_registry.get_spec("read_file")
    assert spec is not None
    assert spec.get("meta", {}).get("root") == str(workspace.root)
    assert workspace.root == tmp_path.resolve()
    assert not (workspace.root / ".agently").exists()


def test_action_extension_enable_workspace_compat_alias_warns(tmp_path):
    agent = Agently.create_agent().use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    with pytest.warns(DeprecationWarning, match="enable_workspace_file_actions"):
        agent.enable_workspace()

    spec = agent.action.action_registry.get_spec("read_file")
    assert spec is not None
    assert spec.get("meta", {}).get("root") == str(workspace.root)


def test_action_extension_shell_and_nodejs_inherit_foundation_workspace(tmp_path):
    agent = Agently.create_agent().use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    agent.enable_shell(commands=["pwd"], action_id="workspace_shell")
    agent.enable_nodejs(action_id="workspace_node")

    shell_spec = agent.action.action_registry.get_spec("workspace_shell")
    assert shell_spec is not None
    shell_req = shell_spec.get("execution_resources", [])[0]
    shell_profile = shell_req.get("config", {}).get("runtime_profile", {})
    assert shell_profile.get("allowed_workdir_roots") == [str(workspace.root)]

    node_spec = agent.action.action_registry.get_spec("workspace_node")
    assert node_spec is not None
    node_req = node_spec.get("execution_resources", [])[0]
    node_profile = node_req.get("config", {}).get("runtime_profile", {})
    assert node_profile.get("cwd") == str(workspace.root)


@pytest.mark.asyncio
async def test_action_extension_request_prefix_injects_action_results(monkeypatch):
    agent = Agently.create_agent()
    request = agent.create_request()
    prompt = request.prompt
    prompt.set("input", "test tool loop")

    monkeypatch.setattr(
        agent.action,
        "get_action_list",
        lambda tags=None: [
            {"name": "dummy_tool", "desc": "dummy", "kwargs": {}},
        ],
    )

    async def fake_loop(**kwargs):
        _ = kwargs
        return [
            {
                "purpose": "fetch_dummy",
                "tool_name": "dummy_tool",
                "kwargs": {},
                "next": "respond",
                "success": True,
                "result": {"ok": 1},
                "error": "",
            }
        ]

    monkeypatch.setattr(agent.tool, "async_plan_and_execute", fake_loop)

    await agent._ActionExtension__request_prefix(prompt, None)  # type: ignore

    action_results = prompt.get("action_results")
    assert isinstance(action_results, dict)
    assert action_results.get("fetch_dummy") == {"ok": 1}
    assert "extra_instruction" in prompt


@pytest.mark.asyncio
async def test_action_extension_broadcast_prefix_keeps_action_and_tool_logs():
    agent = Agently.create_agent()
    full_result_data: dict[str, object] = {}
    agent._ActionExtension__action_logs = [  # type: ignore[attr-defined]
        {
            "purpose": "visible action",
            "action_id": "visible_action",
            "tool_name": "visible_action",
            "kwargs": {},
            "success": True,
            "result": {"ok": 1},
            "status": "success",
            "expose_to_model": True,
        },
        {
            "purpose": "hidden action",
            "action_id": "hidden_action",
            "tool_name": "hidden_action",
            "kwargs": {},
            "success": True,
            "result": {"ok": 2},
            "status": "success",
            "expose_to_model": False,
        },
    ]

    events = [event async for event in agent._ActionExtension__broadcast_prefix(full_result_data, None)]  # type: ignore[attr-defined]
    assert events[0][0] == "action"
    assert events[1][0] == "action"
    assert events[2][0] == "tool"
    assert full_result_data["extra"]["action_logs"][0]["action_id"] == "visible_action"  # type: ignore[index]
    assert len(full_result_data["extra"]["action_logs"]) == 2  # type: ignore[index]
    assert len(full_result_data["extra"]["tool_logs"]) == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_action_extension_plan_handler_instant_response_short_circuit(monkeypatch):
    agent = Agently.create_agent()
    request = agent.create_request()
    prompt = request.prompt
    prompt.set("input", "hello")
    prompt.set("instruct", "just answer directly")

    closed = False

    async def fake_close():
        nonlocal closed
        closed = True

    async def fake_async_get_data():
        raise AssertionError("async_get_data should not be called when next_action is response")

    class FakeResponse:
        def __init__(self):
            self.result = SimpleNamespace(
                async_get_data=fake_async_get_data,
                _response_parser=SimpleNamespace(
                    _response_consumer=SimpleNamespace(
                        close=fake_close,
                    )
                ),
            )

        def get_async_generator(self, type=None, **kwargs):
            _ = kwargs
            assert type == "instant"

            async def gen():
                yield StreamingData(
                    path="$.next_action",
                    value="response",
                    is_complete=True,
                )

            return gen()

    class FakeModelRequest:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)

        def input(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def info(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def instruct(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def output(self, *args, **kwargs):
            _ = (args, kwargs)
            return self

        def get_response(self, *, parent_run_context=None):
            _ = parent_run_context
            return FakeResponse()

    import agently.core as core_module

    monkeypatch.setattr(core_module, "ModelRequest", FakeModelRequest)

    decision = await agent.tool._default_plan_analysis_handler(  # type: ignore[attr-defined]
        {
            "prompt": prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "round_index": 0,
            "max_rounds": 3,
            "done_plans": [],
            "last_round_records": [],
            "action": agent.tool,
            "runtime": agent.tool.action_runtime,
        },
        {
            "action_list": [{"name": "dummy_tool", "desc": "dummy", "kwargs": {}}],
            "planning_protocol": "structured_plan",
        },
    )

    assert decision.get("next_action") == "response"
    assert decision.get("execution_commands") == []
    assert closed is True


@pytest.mark.asyncio
async def test_action_extension_generate_tool_command_only(monkeypatch):
    agent = Agently.create_agent()
    agent.input("find docs")

    monkeypatch.setattr(
        agent.tool,
        "get_tool_list",
        lambda tags=None: [{"name": "search", "desc": "search", "kwargs": {"query": ("str", "")}}],
    )

    async def fake_plan_handler(
        context,
        request,
    ):
        _ = (context, request)
        return {
            "next_action": "execute",
            "execution_commands": [
                {
                    "purpose": "search docs",
                    "tool_name": "search",
                    "tool_kwargs": {"query": "Agently TriggerFlow"},
                    "todo_suggestion": "browse best result",
                }
            ],
        }

    agent.register_tool_plan_analysis_handler(fake_plan_handler)

    called = False

    async def fake_async_call_tool(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Tool should not be called in generate_tool_command")

    monkeypatch.setattr(agent.tool, "async_call_tool", fake_async_call_tool)

    commands = await agent.async_generate_tool_command()
    assert called is False
    assert len(commands) == 1
    assert commands[0].get("tool_name") == "search"
    assert commands[0].get("tool_kwargs") == {"query": "Agently TriggerFlow"}


@pytest.mark.asyncio
async def test_action_extension_get_action_result_runs_action_loop_without_reply(monkeypatch):
    agent = Agently.create_agent()
    agent.input("normalize this title")

    action_list = [
        {
            "action_id": "normalize_title",
            "name": "normalize_title",
            "desc": "Normalize title text",
            "kwargs": {"text": ("str", "raw title")},
        }
    ]
    monkeypatch.setattr(agent.action, "get_action_list", lambda tags=None: action_list)

    async def fake_plan_and_execute(**kwargs):
        assert kwargs["prompt"] is agent.request.prompt
        assert kwargs["settings"] is agent.settings
        assert kwargs["action_list"] == action_list
        assert kwargs["agent_name"] == agent.name
        assert kwargs["max_rounds"] == 2
        assert kwargs["concurrency"] == 1
        assert kwargs["timeout"] == 3.0
        assert kwargs["planning_protocol"] == "structured_plan"
        return [
            {
                "ok": True,
                "status": "success",
                "purpose": "normalize",
                "action_id": "normalize_title",
                "kwargs": {"text": "  Hello  "},
                "result": "hello",
                "data": "hello",
                "success": True,
                "error": "",
            }
        ]

    monkeypatch.setattr(agent.action, "async_plan_and_execute", fake_plan_and_execute)

    records = await agent.async_get_action_result(
        max_rounds=2,
        concurrency=1,
        timeout=3.0,
        planning_protocol="structured_plan",
    )

    assert len(records) == 1
    assert records[0].get("action_id") == "normalize_title"
    assert records[0].get("result") == "hello"
    assert agent.request.prompt.get("action_results") == {"normalize": "hello"}


@pytest.mark.asyncio
async def test_action_extension_get_action_result_can_skip_reply_storage(monkeypatch):
    agent = Agently.create_agent()
    agent.input("normalize this title")

    monkeypatch.setattr(
        agent.action,
        "get_action_list",
        lambda tags=None: [{"action_id": "normalize_title", "desc": "Normalize title text", "kwargs": {}}],
    )

    async def fake_plan_and_execute(**kwargs):
        _ = kwargs
        return [
            {
                "ok": True,
                "status": "success",
                "purpose": "normalize",
                "action_id": "normalize_title",
                "kwargs": {},
                "result": "hello",
                "data": "hello",
                "success": True,
                "error": "",
            }
        ]

    monkeypatch.setattr(agent.action, "async_plan_and_execute", fake_plan_and_execute)

    records = await agent.async_get_action_result(store_for_reply=False)

    assert records[0].get("result") == "hello"
    assert agent.request.prompt.get("action_results") is None


@pytest.mark.asyncio
async def test_action_extension_get_action_result_stores_empty_reply_sentinel(monkeypatch):
    agent = Agently.create_agent()

    def noop_action():
        return "ok"

    agent.use_actions(noop_action, always=True)
    prompt = agent.input("probe reentry").prompt
    calls = 0

    async def fake_plan_and_execute(**kwargs):
        nonlocal calls
        _ = kwargs
        calls += 1
        return []

    monkeypatch.setattr(agent.action, "async_plan_and_execute", fake_plan_and_execute)

    records = await agent.async_get_action_result(prompt=prompt, store_for_reply=True)
    await agent._ActionExtension__request_prefix(prompt, None)  # type: ignore[attr-defined]

    assert records == []
    assert calls == 1
    assert prompt.get("action_results") == {}


@pytest.mark.asyncio
async def test_action_extension_request_prefix_reuses_stored_action_result(monkeypatch):
    agent = Agently.create_agent()
    request = agent.create_request()
    prompt = request.prompt
    prompt.set("input", "use stored result")
    prompt.set("action_results", {"normalize": "hello"})
    prompt.set("extra_instruction", agent.action.ACTION_RESULT_QUOTE_NOTICE)
    agent._ActionExtension__action_logs = [  # type: ignore[attr-defined]
        {
            "ok": True,
            "status": "success",
            "purpose": "normalize",
            "action_id": "normalize_title",
            "kwargs": {},
            "result": "hello",
            "data": "hello",
            "success": True,
            "error": "",
            "expose_to_model": True,
        }
    ]
    agent._ActionExtension__prepared_action_results = {"normalize": "hello"}  # type: ignore[attr-defined]

    async def fake_plan_and_execute(**kwargs):
        _ = kwargs
        raise AssertionError("Stored action_results should skip action loop execution")

    monkeypatch.setattr(agent.action, "async_plan_and_execute", fake_plan_and_execute)

    await agent._ActionExtension__request_prefix(prompt, None)  # type: ignore[attr-defined]

    full_result_data: dict[str, object] = {}
    events = [event async for event in agent._ActionExtension__broadcast_prefix(full_result_data, None)]  # type: ignore[attr-defined]
    assert events[0][0] == "action"
    assert full_result_data["extra"]["action_logs"][0]["result"] == "hello"  # type: ignore[index]


@pytest.mark.asyncio
async def test_action_extension_get_action_result_timeout_bounds_planning_handler():
    agent = Agently.create_agent()

    def slow_probe_action():
        return "ok"

    agent.use_actions(slow_probe_action, always=True)

    async def slow_planner(context, request):
        _ = (context, request)
        await asyncio.sleep(5)
        return {"next_action": "response", "execution_commands": []}

    agent.register_action_planning_handler(slow_planner)
    agent.input("probe timeout")

    started_at = time.monotonic()
    with pytest.raises(RuntimeStageStallError) as raised:
        await agent.async_get_action_result(timeout=0.1, planning_protocol="structured_plan")

    assert time.monotonic() - started_at < 2
    assert raised.value.stage == "action_loop_close"
    assert raised.value.timeout_seconds == 0.1


def test_action_extension_summarize_records_latest_validation_wins():
    agent = Agently.create_agent()
    records = [
        {
            "action_id": "run_bash",
            "status": "success",
            "success": True,
            "kwargs": {"cmd": ["python", "-m", "pytest", "tests/test_app.py", "-q"]},
            "result": {"returncode": 0},
        },
        {
            "action_id": "run_bash",
            "status": "error",
            "success": False,
            "kwargs": {"cmd": ["python", "-m", "pytest", "-q"]},
            "result": {"returncode": 1, "stdout": "1 failed"},
            "error": "validation failed",
        },
    ]

    summary = agent.action.summarize_records(records, validation_command_markers=["pytest"])

    assert summary["actions_attempted"] == 2
    assert summary["commands_run"] == ["python -m pytest tests/test_app.py -q"]
    assert summary["commands_attempted"] == [
        "python -m pytest tests/test_app.py -q",
        "python -m pytest -q",
    ]
    assert summary["latest_validation"] == {
        "index": 1,
        "action_id": "run_bash",
        "command": "python -m pytest -q",
        "status": "failed",
        "returncode": 1,
    }
    assert summary["validation_passed"] is False


def test_action_extension_must_call_soft_compatible(monkeypatch):
    agent = Agently.create_agent()
    agent.input("find docs")

    monkeypatch.setattr(
        agent.tool,
        "get_tool_list",
        lambda tags=None: [{"name": "search", "desc": "search", "kwargs": {"query": ("str", "")}}],
    )

    async def fake_plan_handler(
        context,
        request,
    ):
        _ = (context, request)
        return {
            "next_action": "execute",
            "execution_commands": [
                {
                    "purpose": "search docs",
                    "tool_name": "search",
                    "tool_kwargs": {"query": "Agently TriggerFlow"},
                    "todo_suggestion": "browse best result",
                }
            ],
        }

    agent.register_tool_plan_analysis_handler(fake_plan_handler)

    with pytest.warns(DeprecationWarning):
        commands = agent.must_call()
    assert len(commands) == 1
    assert commands[0].get("tool_name") == "search"
