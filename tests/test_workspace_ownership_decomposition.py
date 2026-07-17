from __future__ import annotations

import importlib.util
from pathlib import Path

import agently
from agently import Agent
from agently.core import TaskWorkspace
from agently.core.storage import RecordStore


def test_legacy_combined_workspace_aggregate_is_removed() -> None:
    assert importlib.util.find_spec("agently.core.Workspace") is None
    assert not hasattr(agently, "create_workspace")
    assert not hasattr(agently, "task_workspace")


def test_agent_exposes_task_workspace_as_the_only_file_boundary(tmp_path) -> None:
    agent = Agent().use_task_workspace(tmp_path, mode="read_write")

    assert isinstance(agent.task_workspace, TaskWorkspace)
    assert not hasattr(agent, "workspace")
    assert not hasattr(agent.task_workspace, "build_context")
    assert not hasattr(agent.task_workspace, "put_snapshot")


def test_default_task_workspace_is_an_isolated_hidden_task_directory() -> None:
    agent = Agent()

    assert agent.task_workspace.root.name == agent.id
    assert agent.task_workspace.root.parent.name == "task_workspaces"
    assert agent.task_workspace.root.parent.parent.name == ".agently"


def test_agent_binds_task_workspace_and_record_store_independently(tmp_path) -> None:
    agent = Agent()
    original_record_store = agent.record_store

    agent.use_task_workspace(tmp_path / "files", mode="read_write")
    assert agent.record_store is original_record_store

    agent.use_record_store(tmp_path / "records", mode="read_write")
    assert isinstance(agent.record_store, RecordStore)
    assert agent.record_store.root == (tmp_path / "records").resolve()
    assert agent.task_workspace.root == (tmp_path / "files").resolve()


def test_context_builder_protocol_is_removed() -> None:
    assert importlib.util.find_spec("agently.types.plugins.ContextBuilder") is None


def test_record_store_implementation_does_not_retain_workspace_ownership_names() -> None:
    storage_root = Path(__file__).resolve().parents[1] / "agently" / "core" / "storage"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in storage_root.rglob("*.py")
    ).lower()

    assert "task_workspace" not in source
    assert "workspaceretrieval" not in source
