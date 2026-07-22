from __future__ import annotations

from pathlib import Path

import pytest

from agently.builtins.plugins.ActionExecutor.CodeExecutionActionExecutor import (
    CodeExecutionActionExecutor,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.application.SkillLibrary import SkillLibrary
from agently.types.data import TaskWorkspaceAccessRequirement


def _skill(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: Bridge E2E\ndescription: Exercise the code bridge.\n---\n\nRun it.",
        encoding="utf-8",
    )
    (root / "scripts" / "check.py").write_text("print('bridge-ok')\n", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_skill_script_bytes_land_in_workspace_before_provider_execution(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_skill(tmp_path / "skill"), trust="trusted")
    workspace = TaskWorkspace(tmp_path / "workspace", mode="read_only", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="call-1",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    calls: list[tuple[str, object]] = []

    class Resource:
        async def async_execute_code(self, *, bundle, manifest, grant, timeout):
            calls.append(("execute", manifest))
            assert Path(manifest.files[0].host_path).is_file()
            assert package.installed_path not in repr(manifest)
            return {"ok": True, "status": "success", "stdout": "bridge-ok\n", "outputs": []}

    executor = CodeExecutionActionExecutor(language="python")
    result = await executor.execute(
        spec={
            "action_id": "skill_bridge_check",
            "meta": {
                "skill_revision_ref": package.revision_ref,
                "skill_resource_path": "scripts/check.py",
                "skill_resource_sha256": package.resource("scripts/check.py").sha256,
            },
        },
        action_call={
            "action_input": {"args": []},
            "task_workspace": workspace,
            "task_workspace_access_grants": {"skill_bridge_check": grant},
            "execution_resource_resources": {"skill_bridge_check": Resource()},
            "skill_library": library,
        },
        policy={"timeout_seconds": 10},
        settings=None,
    )

    assert result["ok"] is True
    assert calls and calls[0][0] == "execute"
    assert not (Path(package.installed_path) / "output").exists()
