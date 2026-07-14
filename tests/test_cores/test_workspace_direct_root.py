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
import sys

import pytest

from agently import Agently
from agently.core.Workspace import Workspace
from agently.core.Workspace.Errors import WorkspacePolicyError
from agently.core.Workspace._defaults import default_workspace_root


def _allocated_bytes(root: Path) -> int:
    return sum(
        int(path.stat().st_blocks) * 512
        for path in (root, *root.rglob("*"))
        if not path.is_symlink()
    )


def _relative_tree(root: Path) -> tuple[str, ...]:
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))


def test_explicit_workspace_root_is_direct_read_only_and_binding_is_pure(tmp_path: Path) -> None:
    project_file = tmp_path / "README.md"
    project_file.write_text("existing project", encoding="utf-8")
    before_tree = _relative_tree(tmp_path)
    before_allocated = _allocated_bytes(tmp_path)

    workspace = Workspace(tmp_path)

    assert workspace.root == tmp_path.resolve()
    assert workspace.mode == "read_only"
    assert not hasattr(workspace, "content_root")
    assert not hasattr(workspace, "files_root")
    assert _relative_tree(tmp_path) == before_tree
    assert _allocated_bytes(tmp_path) == before_allocated
    assert (tmp_path / ".agently").exists() is False


def test_capability_inspection_does_not_materialize_private_state(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    capabilities = workspace.capabilities()

    assert capabilities == {
        "root": str(tmp_path.resolve()),
        "mode": "read_only",
        "external_read": True,
        "external_write": False,
        "private_write": True,
        "materialized_components": [],
    }
    assert list(tmp_path.iterdir()) == []
    assert (tmp_path / ".agently").exists() is False


@pytest.mark.asyncio
async def test_reading_existing_project_files_uses_direct_root_without_private_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('direct root')\n", encoding="utf-8")
    before_tree = _relative_tree(tmp_path)
    before_allocated = _allocated_bytes(tmp_path)
    workspace = Workspace(tmp_path)

    result = await workspace.read_file("src/app.py")

    assert result["content"] == "print('direct root')\n"
    assert result["path"] == "src/app.py"
    assert _relative_tree(tmp_path) == before_tree
    assert _allocated_bytes(tmp_path) == before_allocated
    assert (tmp_path / ".agently").exists() is False


def test_public_workspace_factory_is_pure_and_read_only_by_default(tmp_path: Path) -> None:
    workspace = Agently.create_workspace(tmp_path)

    assert workspace.root == tmp_path.resolve()
    assert workspace.mode == "read_only"
    assert workspace.capabilities()["materialized_components"] == []
    assert list(tmp_path.iterdir()) == []


def test_default_root_uses_entry_script_directory_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_dir = tmp_path / "application"
    entry_file = entry_dir / "main.py"
    main_module = sys.modules["__main__"]
    monkeypatch.setattr(main_module, "__file__", str(entry_file), raising=False)

    assert default_workspace_root() == entry_dir.resolve()
    assert entry_dir.exists() is False


def test_default_root_uses_cwd_for_python_installation_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    launcher = (
        Path(sys.prefix)
        / "lib"
        / "python3.10"
        / "site-packages"
        / "runner"
        / "__main__.py"
    )
    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(launcher), raising=False)

    assert default_workspace_root() == tmp_path.resolve()


def test_agent_default_workspace_is_direct_read_only_and_pure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_dir = tmp_path / "agent-app"
    entry_file = entry_dir / "run.py"
    main_module = sys.modules["__main__"]
    monkeypatch.setattr(main_module, "__file__", str(entry_file), raising=False)

    agent = Agently.create_agent("direct-root-agent")

    assert isinstance(agent.workspace, Workspace)
    assert agent.workspace.root == entry_dir.resolve()
    assert agent.workspace.mode == "read_only"
    assert agent.settings.get("workspace.root") == str(entry_dir.resolve())
    assert agent.settings.get("workspace.content_root", None) is None
    assert entry_dir.exists() is False


@pytest.mark.asyncio
async def test_generic_file_traversal_never_exposes_agently_private_state(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("public needle", encoding="utf-8")
    private_file = tmp_path / ".agently" / "workspace.db"
    private_file.parent.mkdir()
    private_file.write_text("private needle", encoding="utf-8")
    workspace = Workspace(tmp_path)

    globbed = await workspace.glob_files("**", include_hidden=True)
    grepped = await workspace.grep_files("needle", path=".")

    assert globbed["matches"] == ["visible.txt"]
    assert [item["path"] for item in grepped] == ["visible.txt"]
    with pytest.raises(WorkspacePolicyError):
        await workspace.read_file(".agently/workspace.db")
