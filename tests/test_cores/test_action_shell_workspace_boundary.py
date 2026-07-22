# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
)


def test_read_only_task_workspace_rejects_unenforceable_trusted_local_shell(tmp_path: Path) -> None:
    agent = Agently.create_agent("read-only-local-shell").use_task_workspace(
        tmp_path,
        mode="read_only",
    )

    with pytest.raises(ValueError, match="read-only TaskWorkspace"):
        agent.enable_shell(sandbox="trusted_local")

    assert not (tmp_path / ".agently").exists()


def test_read_only_task_workspace_shell_declares_ro_project_and_rw_execution_mounts(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("read-only-docker-shell").use_task_workspace(
        tmp_path,
        mode="read_only",
    )
    workspace = agent.task_workspace

    agent.enable_shell(sandbox="docker", action_id="task_workspace_shell")

    spec = agent.action.action_registry.get_spec("task_workspace_shell")
    assert spec is not None
    requirements = spec.get("execution_resources")
    assert requirements
    config = requirements[0].get("config")
    assert config is not None
    profile = config["runtime_profile"]
    fallback = tmp_path / ".agently" / "files" / workspace.execution_id
    assert profile["task_workspace_mounts"] == [
        {
            "host_path": str(tmp_path.resolve()),
                "container_path": "/task_workspace",
            "mode": "ro",
        },
        {
            "host_path": str(fallback.resolve()),
                "container_path": f"/task_workspace/.agently/files/{workspace.execution_id}",
            "mode": "rw",
        },
    ]
    assert profile["output_artifact_dir"] == str(fallback / "shell-output")
    assert not (tmp_path / ".agently").exists()


def test_explicit_read_write_task_workspace_allows_trusted_local_shell(tmp_path: Path) -> None:
    agent = Agently.create_agent("writable-local-shell").use_task_workspace(
        tmp_path,
        mode="read_write",
    )

    agent.enable_shell(sandbox="trusted_local", action_id="task_workspace_shell")

    spec = agent.action.action_registry.get_spec("task_workspace_shell")
    assert spec is not None
    requirements = spec.get("execution_resources")
    assert requirements
    requirement = requirements[0]
    assert requirement.get("kind") == "bash"
    config = requirement.get("config")
    assert config is not None
    assert config["allowed_workdir_roots"] == [str(tmp_path.resolve())]
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_docker_shell_uses_declared_mount_modes(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    fallback = root / ".agently" / "files" / "execution-1"
    captured: dict[str, Any] = {}
    resource = DockerExecutionResource(
        runtime_profile={
            "language": "shell",
            "image": "bash:5",
            "allowed_cmd_prefixes": ["pwd"],
            "allowed_workdir_roots": [str(root)],
            "task_workspace_mounts": [
                {"host_path": str(root), "container_path": "/workspace", "mode": "ro"},
                {
                    "host_path": str(fallback),
                    "container_path": "/workspace/.agently/files/execution-1",
                    "mode": "rw",
                },
            ],
        }
    )

    async def capture_run_container(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "stdout": ""}

    resource._run_container = capture_run_container  # type: ignore[method-assign]
    result = await resource.run_shell_command(cmd="pwd")

    assert result["ok"] is True
    assert captured["extra_mounts"] == [
        f"{root}:/workspace:ro",
        f"{fallback}:/workspace/.agently/files/execution-1:rw",
    ]


@pytest.mark.asyncio
async def test_task_workspace_shell_executes_in_its_declared_container_mount(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("task-workspace-docker-workdir").use_task_workspace(
        tmp_path,
        mode="read_write",
    )
    agent.enable_shell(
        sandbox="docker",
        action_id="task_workspace_shell",
        commands=["pwd"],
    )
    spec = agent.action.action_registry.get_spec("task_workspace_shell")
    assert spec is not None
    profile = spec["execution_resources"][0]["config"]["runtime_profile"]
    captured: dict[str, Any] = {}
    resource = DockerExecutionResource(runtime_profile=profile)

    async def capture_run_container(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "stdout": ""}

    resource._run_container = capture_run_container  # type: ignore[method-assign]

    result = await resource.run_shell_command(cmd="pwd", workdir=tmp_path)

    assert result["ok"] is True
    assert captured["workdir"] == "/task_workspace"
    assert captured["extra_mounts"] == [
        f"{tmp_path.resolve()}:/task_workspace:rw",
    ]


@pytest.mark.asyncio
async def test_task_workspace_shell_resolves_relative_workdir_from_authorized_root(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    project_root = workspace_root / "project"
    project_root.mkdir(parents=True)
    resource = DockerExecutionResource(
        runtime_profile={
            "language": "shell",
            "image": "python:3.12-slim",
            "allowed_cmd_prefixes": ["pwd"],
            "allowed_workdir_roots": [str(workspace_root)],
            "task_workspace_mounts": [
                {
                    "host_path": str(workspace_root),
                    "container_path": "/task_workspace",
                    "mode": "rw",
                }
            ],
        }
    )
    captured: list[dict[str, Any]] = []

    async def capture_run_container(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"ok": True, "stdout": ""}

    resource._run_container = capture_run_container  # type: ignore[method-assign]

    root_result = await resource.run_shell_command(cmd="pwd", workdir=".")
    child_result = await resource.run_shell_command(cmd="pwd", workdir="project")

    assert root_result["ok"] is True
    assert child_result["ok"] is True
    assert [item["workdir"] for item in captured] == [
        "/task_workspace",
        "/task_workspace/project",
    ]
