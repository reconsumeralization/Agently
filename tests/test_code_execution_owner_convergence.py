from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from agently import Agently
from agently.types.data.code_execution import required_code_execution_isolation


ROOT = Path(__file__).resolve().parents[1]


def _requirement(agent: Any, action_id: str) -> dict[str, Any]:
    spec = agent.action.action_registry.get_spec(action_id)
    assert spec is not None
    assert set(spec.get("kwargs", {})) == {
        "source_code",
        "files",
        "entrypoint",
        "args",
        "expected_outputs",
    }
    requirements = spec.get("execution_resources", [])
    assert len(requirements) == 1
    requirement = cast(dict[str, Any], requirements[0])
    assert requirement["kind"] == "code_execution"
    assert requirement["workspace_access"] == {"mode": "snapshot"}
    return requirement


def test_python_and_node_helpers_reuse_the_workspace_code_execution_owner() -> None:
    agent = Agently.create_agent("canonical-language-helpers")

    agent.enable_python(
        action_id="canonical_python",
        sandbox="trusted_local",
    )
    python_requirement = _requirement(agent, "canonical_python")
    assert python_requirement["required_capabilities"]["language"] == "python"
    assert python_requirement["required_capabilities"]["toolchains"] == {
        "python": {"minimum_version": "3.10"}
    }
    assert "isolation" not in python_requirement["required_capabilities"]
    assert python_requirement["provider_candidates"] == [
        {
            "provider_id": "trusted_local",
            "config": {"allow_unsafe_local": True},
        }
    ]

    agent.enable_nodejs(
        action_id="canonical_node",
        sandbox="docker",
    )
    node_requirement = _requirement(agent, "canonical_node")
    assert node_requirement["required_capabilities"] == {
        "language": "nodejs",
        "toolchains": {"node": {"minimum_version": "18"}},
        "workspace_access_mode": "snapshot",
        "isolation": required_code_execution_isolation(),
    }
    assert node_requirement["provider_candidates"][0]["provider_id"] == "docker"


def test_raw_language_execution_owners_are_removed() -> None:
    removed_files = [
        ROOT / "agently/builtins/plugins/ActionExecutor/PythonSandboxActionExecutor.py",
        ROOT / "agently/builtins/plugins/ActionExecutor/NodeJSActionExecutor.py",
        ROOT / "agently/builtins/plugins/ActionExecutor/CodeRuntimeActionExecutor.py",
        ROOT
        / "agently/builtins/plugins/ExecutionResourceProvider/PythonExecutionResourceProvider.py",
        ROOT
        / "agently/builtins/plugins/ExecutionResourceProvider/NodeExecutionResourceProvider.py",
    ]
    assert all(not path.exists() for path in removed_files)

    owner_sources = [
        ROOT / "agently/_default_init.py",
        ROOT / "agently/builtins/plugins/__init__.py",
        ROOT / "agently/builtins/plugins/ActionExecutor/__init__.py",
        ROOT / "agently/builtins/plugins/ExecutionResourceProvider/__init__.py",
        ROOT / "agently/builtins/plugins/ToolManager/AgentlyToolManager.py",
    ]
    obsolete_owner_names = (
        "PythonSandboxActionExecutor",
        "NodeJSActionExecutor",
        "CodeRuntimeActionExecutor",
        "PythonExecutionResourceProvider",
        "NodeExecutionResourceProvider",
    )
    for source_path in owner_sources:
        source = source_path.read_text(encoding="utf-8")
        assert not any(name in source for name in obsolete_owner_names)

    docker_source = (
        ROOT
        / "agently/builtins/plugins/ExecutionResourceProvider/DockerExecutionResourceProvider.py"
    ).read_text(encoding="utf-8")
    for obsolete_symbol in (
        "CODE_RUNTIME_PROFILES",
        "get_code_runtime_profile",
        "run_python_code",
        "run_nodejs_code",
        "_code_runtime_script",
        "_code_runtime_files",
        "async def run_code",
    ):
        assert obsolete_symbol not in docker_source


def test_language_helpers_do_not_reach_through_action_private_owners() -> None:
    extension_source = (
        ROOT / "agently/builtins/agent_extensions/ActionExtension.py"
    ).read_text(encoding="utf-8")
    assert "._resource_registrar" not in extension_source
