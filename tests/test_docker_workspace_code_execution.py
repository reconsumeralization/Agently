from __future__ import annotations

import asyncio
import importlib
import time
from pathlib import Path

import pytest

from agently.builtins.plugins.CodeRuntimeAdapter import PythonCodeRuntimeAdapter
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
    DockerExecutionResourceProvider,
)
from agently.builtins.plugins.ExecutionResourceProvider._bounded_process import (
    BoundedProcessResult,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessRequirement


@pytest.mark.asyncio
async def test_docker_provider_exposes_code_execution_capabilities() -> None:
    provider = DockerExecutionResourceProvider()
    probe = await provider.async_probe(
        requirement={
            "kind": "code_execution",
            "required_capabilities": {
                "language": "python",
                "toolchains": {"python": {"minimum_version": "3.10"}},
            },
            "config": {"runtime_profile": {"image_pull_policy": "never"}},
        },
        policy={},
    )

    assert provider.provider_id == "docker"
    assert "code_execution" in provider.supported_kinds
    capabilities = probe.get("capabilities")
    assert isinstance(capabilities, dict)
    assert capabilities["isolation"]["process_contained"] is True
    assert capabilities["isolation"]["host_filesystem_restricted"] is True
    assert "python" in capabilities["languages"]
    assert capabilities["toolchains"]["python"]["available"] is True
    assert capabilities["toolchains"]["python"]["version"].startswith("3.")


@pytest.mark.asyncio
async def test_docker_executes_materialized_workspace_bundle(tmp_path: Path) -> None:
    availability = DockerExecutionResource().inspect_availability()
    if not availability["available"]:
        pytest.skip(f"Docker is unavailable: {availability}")

    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="run",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="python",
            source_code=(
                "from pathlib import Path\n"
                "Path('../output/result.txt').write_text('docker-ok')\n"
                "print('docker-stdout')\n"
            ),
            expected_outputs=["output/result.txt"],
        ),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    provider = DockerExecutionResourceProvider()
    handle = await provider.async_ensure(
        requirement={
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
            "task_workspace_access_grant": grant,
            "config": {
                "runtime_profile": {
                    "image": "python:3.12-slim",
                    "image_pull_policy": "never",
                    "network_mode": "disabled",
                }
            },
        },
        policy={"timeout_seconds": 20, "max_output_bytes": 10000},
    )

    resource = handle.get("resource")
    assert resource is not None
    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=20,
    )

    assert result["ok"] is True
    assert result["stdout"] == "docker-stdout\n"
    assert result["outputs"] == ["output/result.txt"]
    assert (Path(grant.execution_area) / "output" / "result.txt").read_text() == "docker-ok"


@pytest.mark.asyncio
async def test_docker_async_container_execution_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = DockerExecutionResource()
    monkeypatch.setattr(resource, "is_binary_available", lambda: True)
    monkeypatch.setattr(resource, "ensure_image_ready", lambda *_args, **_kwargs: {})

    async def slow_run(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return BoundedProcessResult(
            returncode=0,
            stdout=b"ok\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )
    monkeypatch.setattr(docker_module, "run_bounded_process", slow_run)
    started = time.monotonic()
    ticked_at: list[float] = []

    async def ticker() -> None:
        await asyncio.sleep(0.01)
        ticked_at.append(time.monotonic())

    await asyncio.gather(
        resource._run_container(image="test-image", cmd=["true"]),
        ticker(),
    )

    assert ticked_at[0] - started < 0.1


@pytest.mark.asyncio
async def test_docker_failed_container_cleanup_remains_visible_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedCleanupProcess:
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 1
            return 1

        def kill(self) -> None:
            self.returncode = -9

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FailedCleanupProcess()

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    resource = DockerExecutionResource()
    resource._active_containers.add("agently-code-cleanup-failed")

    with pytest.raises(RuntimeError, match="container_cleanup_failed"):
        await resource._remove_container("agently-code-cleanup-failed")

    assert "agently-code-cleanup-failed" in resource._active_containers
