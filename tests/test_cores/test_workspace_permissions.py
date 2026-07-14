# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from pathlib import Path

import pytest

from agently import Agently
from agently.core.Workspace import Workspace
from agently.core.Workspace.Errors import WorkspacePolicyError


@pytest.mark.asyncio
async def test_read_only_new_product_uses_only_current_execution_fallback(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    result = await workspace.write_file("deliverables/report.md", "final product")

    ref = result["file_refs"][0]
    execution_id = ref.get("execution_id")
    workspace_id = ref.get("workspace_id")
    assert isinstance(execution_id, str)
    assert isinstance(workspace_id, str)
    expected_prefix = f".agently/files/{execution_id}/"
    assert result["path"] == f"{expected_prefix}deliverables/report.md"
    assert ref == {
        **ref,
        "type": "file",
        "path": result["path"],
        "workspace_id": workspace_id,
        "execution_id": execution_id,
        "size": len(b"final product"),
        "sha256": result["sha256"],
        "available": True,
    }
    assert (tmp_path / result["path"]).read_text(encoding="utf-8") == "final product"
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file()) == [
        result["path"]
    ]
    readback = await workspace.read_file(result["path"])
    assert readback["content"] == "final product"
    assert not (tmp_path / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_read_only_existing_external_file_cannot_be_remapped_as_success(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("before\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspacePolicyError):
        await workspace.write_file("src/app.py", "after\n")
    with pytest.raises(WorkspacePolicyError):
        await workspace.edit_file("src/app.py", "before", "after")

    assert source.read_text(encoding="utf-8") == "before\n"
    assert (tmp_path / ".agently").exists() is False


@pytest.mark.asyncio
async def test_explicit_external_write_creates_requested_subdirectory_without_private_state(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path, mode="read_write")

    result = await workspace.write_file("generated/report.md", "external product")

    assert result["path"] == "generated/report.md"
    assert (tmp_path / "generated" / "report.md").read_text(encoding="utf-8") == "external product"
    assert (tmp_path / ".agently").exists() is False


@pytest.mark.asyncio
async def test_other_execution_private_files_are_not_readable(tmp_path: Path) -> None:
    other = tmp_path / ".agently" / "files" / "other-execution" / "secret.txt"
    other.parent.mkdir(parents=True)
    other.write_text("secret", encoding="utf-8")
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspacePolicyError):
        await workspace.read_file(".agently/files/other-execution/secret.txt")


def test_agent_read_actions_bind_direct_root_without_materializing_private_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes" / "todo.txt"
    source.parent.mkdir()
    source.write_text("inspect only\n", encoding="utf-8")
    agent = Agently.create_agent("workspace-read-actions").use_workspace(tmp_path)

    agent.enable_workspace_file_actions(read=True, write=False)

    spec = agent.action.action_registry.get_spec("read_file")
    assert spec is not None
    assert spec.get("meta", {}).get("root") == str(tmp_path.resolve())
    result = agent.action.execute_action("read_file", {"path": "notes/todo.txt"})
    assert result.get("status") == "success"
    assert result.get("data", {}).get("content") == "inspect only\n"
    assert agent.action.action_registry.get_spec("write_file") is None
    assert not (tmp_path / ".agently").exists()


def test_enabling_coding_write_actions_is_an_explicit_external_write_grant(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('old')\n", encoding="utf-8")
    agent = Agently.create_agent("workspace-write-actions").use_workspace(
        tmp_path,
        mode="read_write",
    )
    assert agent.workspace.mode == "read_write"

    agent.enable_coding_agent_actions(write=True)
    read = agent.action.execute_action("read_file", {"path": "src/app.py"})
    assert read.get("status") == "success"
    edited = agent.action.execute_action(
        "edit_file",
        {
            "path": "src/app.py",
            "old_string": "old",
            "new_string": "new",
        },
    )

    assert edited.get("status") == "success"
    assert source.read_text(encoding="utf-8") == "print('new')\n"
    assert agent.workspace.mode == "read_write"
    assert not (tmp_path / ".agently").exists()


def test_shell_and_node_runtime_profiles_use_direct_workspace_root(tmp_path: Path) -> None:
    agent = Agently.create_agent("workspace-runtime-root").use_workspace(tmp_path)

    agent.enable_shell(commands=["pwd"], action_id="workspace_shell")
    agent.enable_nodejs(action_id="workspace_node")

    shell_spec = agent.action.action_registry.get_spec("workspace_shell")
    assert shell_spec is not None
    shell_requirements = shell_spec.get("execution_resources", [])
    assert shell_requirements
    shell_config = shell_requirements[0].get("config")
    assert shell_config is not None
    shell_profile = shell_config["runtime_profile"]
    assert shell_profile["allowed_workdir_roots"] == [str(tmp_path.resolve())]
    node_spec = agent.action.action_registry.get_spec("workspace_node")
    assert node_spec is not None
    node_requirements = node_spec.get("execution_resources", [])
    assert node_requirements
    node_config = node_requirements[0].get("config")
    assert node_config is not None
    node_profile = node_config["runtime_profile"]
    assert node_profile["cwd"] == str(tmp_path.resolve())
    assert not (tmp_path / ".agently").exists()
