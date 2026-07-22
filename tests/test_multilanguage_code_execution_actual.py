from __future__ import annotations

from pathlib import Path

import pytest

from agently import Agently
from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter
from agently.builtins.plugins.ExecutionResourceProvider.TrustedLocalExecutionResourceProvider import (
    TrustedLocalExecutionResourceProvider,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core import ExecutionResourceManager
from agently.core.runtime import bind_runtime_context
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessRequirement
from agently.utils import Settings


CASES = {
    "python": (
        "from pathlib import Path\n"
        "Path('../output/result.txt').write_text('python-ok')\n"
        "print('python-stdout')\n"
    ),
    "nodejs": (
        "const fs = require('fs');\n"
        "fs.writeFileSync('../output/result.txt', 'nodejs-ok');\n"
        "console.log('nodejs-stdout');\n"
    ),
    "go": (
        "package main\n"
        "import (\"fmt\"; \"os\")\n"
        "func main() { _ = os.WriteFile(\"../output/result.txt\", []byte(\"go-ok\"), 0644); fmt.Println(\"go-stdout\") }\n"
    ),
    "cpp": (
        "#include <fstream>\n"
        "#include <iostream>\n"
        "int main() { std::ofstream(\"../output/result.txt\") << \"cpp-ok\"; std::cout << \"cpp-stdout\\n\"; }\n"
    ),
}


@pytest.mark.parametrize("language", ["python", "nodejs", "go", "cpp"])
@pytest.mark.asyncio
async def test_locally_installed_mainstream_toolchain_executes_workspace_bundle(
    tmp_path: Path,
    language: str,
) -> None:
    provider = TrustedLocalExecutionResourceProvider()
    probe = await provider.async_probe(requirement={"kind": "code_execution"}, policy={})
    if language not in probe["capabilities"]["languages"]:
        pytest.skip(f"{language} toolchain unavailable: {probe['meta']['toolchains'][language]}")
    workspace = TaskWorkspace(
        tmp_path / language,
        execution_id=f"actual-{language}",
    )
    grant = workspace.issue_execution_access(
        action_call_id=f"run-{language}",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    adapter = get_code_runtime_adapter(language)
    bundle = adapter.prepare(
        CodeExecutionRequest.create(
            language=language,
            source_code=CASES[language],
            expected_outputs=["output/result.txt"],
        ),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    toolchains = {
        item.tool: {
            **(
                {"minimum_version": item.minimum_version}
                if item.minimum_version is not None
                else {}
            ),
            **(
                {"exact_version": item.exact_version}
                if item.exact_version is not None
                else {}
            ),
            **(
                {"required": True}
                if item.minimum_version is None and item.exact_version is None
                else {}
            ),
        }
        for item in bundle.toolchains
    }
    manager = ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=Settings(name=f"actual-{language}", parent=Agently.settings),
        event_center=Agently.event_center,
    )
    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_id": "trusted_local",
            "required_capabilities": {
                "language": language,
                "toolchains": toolchains,
                "workspace_access_mode": "snapshot",
            },
            "config": {"allow_unsafe_local": True},
            "policy": {"max_output_bytes": 10_000},
            "task_workspace_access_grant": grant,
        }
    )

    resource = handle.get("resource")
    assert resource is not None
    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=30,
    )

    assert result["ok"] is True, result
    assert result["stdout"] == f"{language}-stdout\n"
    assert result["outputs"] == ["output/result.txt"]
    assert (Path(grant.execution_area) / "output" / "result.txt").read_text() == f"{language}-ok"
    await manager.async_release(handle)


@pytest.mark.asyncio
async def test_action_runtime_runs_workspace_bound_provider_chain_with_explicit_unsafe_fallback(
    tmp_path: Path,
) -> None:
    provider = TrustedLocalExecutionResourceProvider()
    probe = await provider.async_probe(requirement={"kind": "code_execution"}, policy={})
    if "python" not in probe["capabilities"]["languages"]:
        pytest.skip("Python toolchain is unavailable")
    workspace = TaskWorkspace(tmp_path / "action-workspace", execution_id="actual-action")
    agent = Agently.create_agent("actual-code-execution")
    agent.enable_code_runtime(
        language="python",
        action_id="actual_python",
        providers=["missing-provider"],
        unsafe_fallback=True,
        isolation="preferred",
    )

    with bind_runtime_context(
        agent_execution_context=type("Context", (), {"task_workspace": workspace})()
    ):
        result = await agent.action.async_execute_action(
            "actual_python",
            {
                "source_code": "print('workspace-action-ok')\n",
                "files": None,
                "args": [],
            },
        )

    assert result.get("status") == "success", result
    result_data = result.get("data")
    assert isinstance(result_data, dict)
    assert result_data["stdout"] == "workspace-action-ok\n"
    assert result_data["unsafe"] is True
    assert result_data["meta"]["provider_contract"] == "workspace_code_execution_v1"
