from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently.core.Workspace import Workspace, WorkspaceManager
from agently.core.Workspace.Errors import WorkspacePolicyError
from agently.core.application.AgentTask import AgentTask
from agently.core.session import Session
from agently.utils import Settings


def _allocated_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(
        int(path.stat().st_blocks) * 512
        for path in (root, *root.rglob("*"))
        if not path.is_symlink()
    )


def _private_files(root: Path) -> list[str]:
    private_root = root / ".agently"
    if not private_root.exists():
        return []
    return sorted(
        str(path.relative_to(root))
        for path in private_root.rglob("*")
        if path.is_file()
    )


def _tables(database: Path) -> set[str]:
    if not database.exists():
        return set()
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
            if not str(row[0]).startswith("sqlite_")
        }


def _memory_record() -> dict[str, Any]:
    return {
        "memory_scope": "SESSION_MEMORY",
        "kind": "session_memory",
        "summary": "prefers compact evidence",
        "body": {"preference": "compact evidence"},
        "tags": ["preference"],
        "importance": 0.8,
        "provenance": {
            "plugin": "AgentlyMemory",
            "session_id": "workspace-acceptance",
            "turn_index": 1,
        },
    }


@pytest.mark.asyncio
async def test_acceptance_pure_project_read_uses_zero_additional_bytes(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('workspace acceptance')\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    before = _allocated_bytes(tmp_path)

    read = await workspace.read_file("src/app.py")
    globbed = await workspace.glob_files("*.py", path="src")
    grepped = await workspace.grep_files("workspace acceptance", path="src")

    assert read["content"] == "print('workspace acceptance')\n"
    assert globbed["matches"] == ["src/app.py"]
    assert [item["path"] for item in grepped] == ["src/app.py"]
    assert _allocated_bytes(tmp_path) == before
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_acceptance_read_only_product_creates_one_fallback_file_only(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    result = await workspace.write_file("outputs/report.md", "final")

    assert _private_files(tmp_path) == [result["path"]]
    assert result["path"].startswith(".agently/files/")
    assert not (tmp_path / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_acceptance_explicit_external_write_creates_no_private_state(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, mode="read_write")

    result = await workspace.write_file("outputs/report.md", "approved external product")

    assert result["path"] == "outputs/report.md"
    assert (tmp_path / result["path"]).read_text(encoding="utf-8") == "approved external product"
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_acceptance_denied_edit_keeps_external_file_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspacePolicyError):
        await workspace.edit_file("src/app.py", "before", "after")

    assert source.read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_acceptance_plain_record_creates_only_records_sqlite(tmp_path: Path) -> None:
    manager = WorkspaceManager()
    vector_calls = 0

    def unexpected_vector(**_options: Any) -> Any:
        nonlocal vector_calls
        vector_calls += 1
        raise AssertionError("plain records must not materialize vector storage")

    manager.register_vector_store_provider("probe", unexpected_vector)
    workspace = Workspace(tmp_path, manager, vector_store_provider="probe")

    await workspace.put("one durable fact", collection="facts", kind="plain")

    database = tmp_path / ".agently" / "workspace.db"
    assert vector_calls == 0
    assert _private_files(tmp_path) == [".agently/workspace.db"]
    assert _tables(database) == {"records"}
    assert not (tmp_path / ".agently" / "vectors").exists()


@pytest.mark.asyncio
async def test_acceptance_finite_triggerflow_has_zero_workspace_persistence(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    source.write_text("finite input", encoding="utf-8")
    workspace = Workspace(tmp_path)
    flow = TriggerFlow(name="workspace-acceptance-finite")

    async def read_input(data: TriggerFlowRuntimeData) -> None:
        bound = data.require_resource("workspace")
        read = await bound.read_file("input.txt")
        await data.async_set_state("content", read["content"])

    flow.to(read_input)
    execution = flow.create_execution(
        auto_close=True,
        auto_close_timeout=0,
        runtime_resources={"workspace": workspace},
    )

    result = await execution.async_start(None)

    assert result["content"] == "finite input"
    assert execution._snapshot_store is None
    assert execution._runtime_event_store is None
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_acceptance_triggerflow_pause_resume_uses_minimal_recovery_then_cleans(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    flow = TriggerFlow(name="workspace-acceptance-recovery")

    async def approval(data: TriggerFlowRuntimeData) -> Any:
        return await data.async_pause_for(
            type="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    async def finish(data: TriggerFlowRuntimeData) -> None:
        await data.async_set_state("result", {"approved": data.value["approved"]})

    flow.to(approval).to(finish)
    execution = flow.create_execution(
        auto_close=False,
        runtime_resources={"workspace": workspace},
    )

    await execution.async_start("draft")
    assert execution.get_status() == "waiting"
    assert _tables(tmp_path / ".agently" / "workspace.db") == {
        "checkpoints",
        "manifests",
        "records",
    }
    assert execution._runtime_event_store is None

    await execution.async_continue_with("approval", {"approved": True})
    close_snapshot = await execution.async_close()

    assert close_snapshot["result"] == {"approved": True}
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_acceptance_session_memory_disabled_and_record_only_are_vector_free(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager()
    calls = {"embedding": 0, "vector": 0}

    def unexpected_embedding(**_options: Any) -> Any:
        calls["embedding"] += 1
        raise AssertionError("record-only memory must not start embedding")

    def unexpected_vector(**_options: Any) -> Any:
        calls["vector"] += 1
        raise AssertionError("record-only memory must not start vector storage")

    manager.register_embedding_provider("probe", unexpected_embedding)
    manager.register_vector_store_provider("probe", unexpected_vector)
    disabled_root = tmp_path / "disabled"
    disabled_workspace = Workspace(disabled_root, manager)
    Session(
        id="memory-disabled",
        plugin_manager=Agently.plugin_manager,
        settings=Agently.settings,
        workspace=disabled_workspace,
    )
    assert not disabled_root.exists()

    record_root = tmp_path / "record-only"
    record_workspace = Workspace(
        record_root,
        manager,
        embedding_provider="probe",
        vector_store_provider="probe",
    )
    settings = Settings(name="WorkspaceAcceptanceMemory", parent=Agently.settings)
    session = Session(
        id="workspace-acceptance",
        plugin_manager=Agently.plugin_manager,
        settings=settings,
        workspace=record_workspace,
    )
    session.use_memory(mode="AgentlyMemory")

    await cast(Any, session.memory)._store_memory(
        record_workspace,
        _memory_record(),
        session=session,
    )

    assert calls == {"embedding": 0, "vector": 0}
    assert record_workspace.capabilities()["materialized_components"] == ["records"]
    assert _private_files(record_root) == [".agently/workspace.db"]


@pytest.mark.asyncio
async def test_acceptance_taskboard_terminal_closure_keeps_only_selected_product(
    tmp_path: Path,
) -> None:
    root = tmp_path / "taskboard"
    agent = Agently.create_agent("workspace-acceptance-taskboard").use_workspace(root)
    task = AgentTask(
        agent,
        task_id="workspace-acceptance-taskboard",
        goal="Produce one final report.",
        success_criteria=["The selected final report is retained."],
        execution="taskboard",
    )
    draft = await task.workspace.write_file("reports/draft.md", "draft")
    final = await task.workspace.write_file("reports/final.md", "final")
    final_ref = cast(dict[str, Any], final["file_refs"][0])

    retained = await task._register_terminal_deliverables([cast(Any, final_ref)])
    task.result = {"status": "completed", "artifact_refs": retained}
    closure = await task._apply_terminal_workspace_retention(status="completed")

    assert closure and closure["status"] == "applied"
    assert not (root / cast(str, draft["path"])).exists()
    assert (root / cast(str, final["path"])).read_text(encoding="utf-8") == "final"
    assert _private_files(root) == [str(final["path"])]


@pytest.mark.asyncio
async def test_acceptance_empty_transient_recovery_database_is_reclaimed(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    await workspace.put_snapshot("transient-run", {"status": "waiting"}, step_id="wait")
    assert (tmp_path / ".agently" / "workspace.db").is_file()

    result = await workspace.delete_snapshot("transient-run")

    assert result["database_removed"] is True
    assert not (tmp_path / ".agently").exists()
    assert workspace.capabilities()["materialized_components"] == []
