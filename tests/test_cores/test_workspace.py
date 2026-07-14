from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from agently import Agently
from agently.core.Workspace import Workspace, WorkspaceManager
from agently.core.Workspace.Errors import WorkspacePolicyError


@pytest.mark.asyncio
async def test_workspace_record_round_trip_search_and_link(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    observation = await workspace.put(
        "pytest failed in route fallback",
        collection="observations",
        kind="test_output",
        summary="route fallback failure",
        scope={"task_id": "issue-123"},
    )
    decision = await workspace.put(
        {"decision": "use the provider boundary"},
        collection="decisions",
        kind="architecture",
    )
    linked = await workspace.link(decision, observation, "responds_to")

    assert await workspace.get(observation) == "pytest failed in route fallback"
    assert await workspace.get_data(decision) == {"decision": "use the provider boundary"}
    assert [item["id"] for item in await workspace.search("route fallback")] == [observation["id"]]
    assert (await workspace.links(observation))[0]["id"] == linked["id"]
    assert (tmp_path / ".agently" / "workspace.db").is_file()


@pytest.mark.asyncio
async def test_workspace_checkpoint_snapshot_cas_and_lease(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    first = await workspace.put_snapshot(
        "run-1",
        {"state_version": 1, "phase": "waiting"},
        step_id="approval",
        expected_state_version=0,
    )

    assert await workspace.get_snapshot("run-1") == {"state_version": 1, "phase": "waiting"}
    assert (await workspace.latest_snapshot("run-1"))["id"] == first["id"]  # type: ignore[index]
    with pytest.raises(RuntimeError, match="state version conflict"):
        await workspace.put_snapshot(
            "run-1",
            {"state_version": 2},
            expected_state_version=0,
        )

    lease = await workspace.claim_lease("run-1", "worker-a", ttl=0.05, expected_state_version=1)
    with pytest.raises(RuntimeError, match="lease conflict"):
        await workspace.claim_lease("run-1", "worker-b", ttl=0.05)
    token = str(lease["lease_token"])
    refreshed = await workspace.heartbeat_lease("run-1", "worker-a", token)
    released = await workspace.release_lease("run-1", "worker-a", str(refreshed["lease_token"]))
    assert released["released_at"] is not None
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_workspace_runtime_events_are_explicit_and_sequenced(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    first = await workspace.append_runtime_event(
        "execution-1",
        {"event_id": "event-1", "event_type": "explicit.audit"},
        expected_sequence=1,
        idempotency_key="audit-1",
    )
    duplicate = await workspace.append_runtime_event(
        "execution-1",
        {"event_id": "event-1", "event_type": "explicit.audit"},
        expected_sequence=1,
        idempotency_key="audit-1",
    )
    assert duplicate["id"] == first["id"]
    with pytest.raises(RuntimeError, match="sequence conflict"):
        await workspace.append_runtime_event(
            "execution-1",
            {"event_type": "out-of-order"},
            expected_sequence=3,
        )
    assert [item["event_id"] for item in await workspace.query_runtime_events("execution-1")] == ["event-1"]


@pytest.mark.asyncio
async def test_workspace_direct_file_io_and_private_boundary(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('old')\n", encoding="utf-8")
    workspace = Workspace(tmp_path, mode="read_write")

    read = await workspace.read_file("src/app.py")
    edited = await workspace.edit_file("src/app.py", "old", "new")
    created = await workspace.write_file("generated/result.txt", "done")

    assert read["content"] == "print('old')\n"
    assert edited["sha256"] == hashlib.sha256(b"print('new')\n").hexdigest()
    assert created["path"] == "generated/result.txt"
    assert not (tmp_path / ".agently").exists()
    with pytest.raises((ValueError, WorkspacePolicyError)):
        await workspace.read_file("../outside.txt")


@pytest.mark.asyncio
async def test_workspace_read_only_materialization_uses_execution_fallback(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)._bind_execution("download-run")
    result = await workspace.materialize_file(
        "remote/manual.pdf",
        b"%PDF-1.4\n%%EOF",
        media_type="application/pdf",
    )

    assert result["path"] == ".agently/files/download-run/remote/manual.pdf"
    assert (tmp_path / result["path"]).read_bytes() == b"%PDF-1.4\n%%EOF"
    assert not (tmp_path / ".agently" / "workspace.db").exists()


def test_workspace_manager_and_agently_factory_remain_pure(tmp_path: Path) -> None:
    manager = WorkspaceManager()
    root = tmp_path / "missing-project"
    first = manager.create(root)
    second = Agently.create_workspace(root)

    assert first.root == root.resolve()
    assert second.root == root.resolve()
    assert first.mode == second.mode == "read_only"
    assert not root.exists()
