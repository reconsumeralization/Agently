from __future__ import annotations

from pathlib import Path

import pytest

from agently.builtins.plugins.ActionExecutor.CodeExecutionActionExecutor import (
    CodeExecutionActionExecutor,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import TaskWorkspaceAccessRequirement


@pytest.mark.asyncio
async def test_action_materializes_bundle_before_provider_receives_execution_plan(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="run_python",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    received: dict[str, object] = {}

    class Resource:
        async def async_execute_code(self, **kwargs):
            received.update(kwargs)
            manifest = kwargs["manifest"]
            assert Path(manifest.files[0].host_path).read_text() == "print('ok')\n"
            output = Path(kwargs["grant"].execution_area) / "output" / "result.txt"
            output.write_text("done\n", encoding="utf-8")
            return {
                "ok": True,
                "status": "success",
                "stdout": "ok\n",
                "stderr": "",
                "outputs": ["output/result.txt"],
            }

    executor = CodeExecutionActionExecutor(language="python")
    result = await executor.execute(
        spec={"action_id": "run_python", "meta": {}},
        action_call={
            "action_input": {
                "source_code": "print('ok')\n",
                "args": [],
                "expected_outputs": ["output/result.txt"],
            },
            "task_workspace": workspace,
            "task_workspace_access_grants": {"run_python": grant},
            "execution_resource_resources": {"run_python": Resource()},
            "execution_resource_handles": {
                "run_python": {
                    "provider_id": "synthetic-isolator",
                    "meta": {
                        "provider_probes": [
                            {
                                "provider_id": "synthetic-isolator",
                                "available": True,
                                "capabilities": {
                                    "isolation": {
                                        "process_contained": True,
                                        "host_filesystem_restricted": True,
                                        "privilege_escalation_blocked": True,
                                        "syscalls_restricted": True,
                                        "mechanism": "synthetic-test-provider",
                                    },
                                    "safety_class": "isolated",
                                    "toolchains": {
                                        "python": {"version": "3.10.13"}
                                    },
                                },
                                "reason": "synthetic fixture",
                            }
                        ]
                    },
                }
            },
        },
        policy={"timeout_seconds": 10},
        settings=None,
    )

    assert result["ok"] is True
    assert set(received) == {"bundle", "manifest", "grant", "timeout"}
    assert "source_code" not in received
    assert result["artifacts"][0]["path"].endswith("output/result.txt")
    assert result["artifacts"][0]["media_type"] == "text/plain"
    assert result["meta"]["bundle_digest"].startswith("sha256:")
    assert result["meta"]["provider_id"] == "synthetic-isolator"
    assert result["meta"]["provider_capabilities"]["safety_class"] == "isolated"
    assert result["meta"]["provider_capabilities"]["toolchains"]["python"]["version"] == "3.10.13"


@pytest.mark.asyncio
async def test_action_uses_host_registration_dependency_policy_for_adapter_plan(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="run_python",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )

    class Resource:
        async def async_execute_code(self, *, bundle, manifest, grant, timeout):
            _ = manifest, grant, timeout
            assert bundle.build_steps[0].argv[:4] == (
                "python",
                "-m",
                "pip",
                "install",
            )
            return {"ok": True, "status": "success", "outputs": []}

    executor = CodeExecutionActionExecutor(language="python")
    result = await executor.execute(
        spec={
            "action_id": "run_python",
            "meta": {},
            "execution_resources": [
                {
                    "kind": "code_execution",
                    "config": {"dependency_policy": {"mode": "install"}},
                }
            ],
        },
        action_call={
            "action_input": {
                "source_code": "print('ok')\n",
                "files": {"requirements.txt": ""},
                "args": [],
            },
            "task_workspace": workspace,
            "task_workspace_access_grants": {"run_python": grant},
            "execution_resource_resources": {"run_python": Resource()},
        },
        policy={"dependency_install": "deny"},
        settings=None,
    )

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_action_fails_when_a_declared_expected_output_is_missing(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="act_call_missing_output",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )

    class Resource:
        async def async_execute_code(self, *, bundle, manifest, grant, timeout):
            _ = bundle, manifest, grant, timeout
            return {"ok": True, "status": "success", "outputs": []}

    executor = CodeExecutionActionExecutor(language="python")
    result = await executor.execute(
        spec={"action_id": "run_python", "meta": {}},
        action_call={
            "action_call_id": "act_call_missing_output",
            "action_input": {
                "source_code": "print('done')\n",
                "expected_outputs": ["output/result.txt"],
            },
            "task_workspace": workspace,
            "task_workspace_access_grants": {"run_python": grant},
            "execution_resource_resources": {"run_python": Resource()},
        },
        policy={"timeout_seconds": 10},
        settings=None,
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"] == "Declared code execution outputs were not produced."
    assert result["data"]["missing_expected_outputs"] == ["output/result.txt"]
