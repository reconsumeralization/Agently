"""Observed Linux Landlock filesystem evidence."""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from agently.builtins.plugins.CodeRuntimeAdapter import PythonCodeRuntimeAdapter
from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
    LandlockExecutionResourceProvider,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessRequirement


@pytest.mark.asyncio
async def test_landlock_allows_granted_output_and_denies_ungranted_read(tmp_path: Path) -> None:
    if platform.system() != "Linux":
        pytest.skip("real Landlock evidence requires Linux")

    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="landlock-run")
    grant = workspace.issue_execution_access(
        action_call_id="run",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    source = (
        "import json\n"
        "from pathlib import Path\n"
        "denied = False\n"
        "try:\n"
        "    Path('/etc/shadow').read_text()\n"
        "except OSError:\n"
        "    denied = True\n"
        "Path('../output/result.json').write_text(json.dumps({'shadow_denied': denied}))\n"
    )
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="python",
            source_code=source,
            expected_outputs=["output/result.json"],
        ),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    provider = LandlockExecutionResourceProvider()
    handle = await provider.async_ensure(
        requirement={
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
            "task_workspace_access_grant": grant,
            "config": {},
        },
        policy={"timeout_seconds": 20, "max_output_bytes": 10000},
    )
    result = await handle["resource"].async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=20,
    )

    assert handle["meta"]["mechanism_verified"] is True
    assert result["ok"] is True, result
    observed = json.loads((Path(grant.execution_area) / "output" / "result.json").read_text())
    assert observed == {"shadow_denied": True}
