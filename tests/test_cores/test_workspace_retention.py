from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.Workspace import Workspace


@pytest.mark.asyncio
async def test_execution_cleanup_reclaims_only_unretained_fallback_files(tmp_path: Path) -> None:
    external = tmp_path / "project.py"
    external.write_text("caller-owned", encoding="utf-8")
    workspace = Workspace(tmp_path)._bind_execution("run-1")
    draft = await workspace.write_file("working/draft.md", "discard")
    final = await workspace.write_file("deliverables/final.md", "retain")

    result = await workspace._close_execution_files(
        retained_refs=[final["file_refs"][0]],
        status="completed",
    )

    assert result["status"] == "applied"
    assert result["deleted_bytes"] == len(b"discard")
    assert result["retained_bytes"] == len(b"retain")
    assert not (tmp_path / draft["path"]).exists()
    assert (tmp_path / final["path"]).read_text(encoding="utf-8") == "retain"
    assert external.read_text(encoding="utf-8") == "caller-owned"


@pytest.mark.asyncio
async def test_cleanup_preserves_execution_files_when_integrity_cannot_be_verified(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)._bind_execution("run-2")
    draft = await workspace.write_file("working/draft.md", "draft")
    final = await workspace.write_file("deliverables/final.md", "retain safely")
    damaged_ref = dict(final["file_refs"][0])
    damaged_ref["sha256"] = "0" * 64

    result = await workspace._close_execution_files(
        retained_refs=[damaged_ref],
        status="completed",
    )

    assert result["status"] == "deferred"
    assert result["diagnostics"][0]["code"] == "workspace.file_ref.digest_mismatch"
    assert (tmp_path / draft["path"]).read_text(encoding="utf-8") == "draft"
    assert (tmp_path / final["path"]).read_text(encoding="utf-8") == "retain safely"
    assert result["retained_refs"] == []
    assert result["retained_bytes"] == 0
    assert result["deleted_bytes"] == 0


@pytest.mark.asyncio
async def test_cleanup_never_deletes_or_defers_a_verified_external_product(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path, mode="read_write")._bind_execution("run-3")
    external = await workspace.write_file("generated/report.md", "external")
    await workspace.write_file(".agently/files/run-3/working/tmp.txt", "temporary")

    result = await workspace._close_execution_files(
        retained_refs=[external["file_refs"][0]],
        status="completed",
    )

    assert result["status"] == "applied"
    assert result["diagnostics"] == []
    assert (tmp_path / "generated" / "report.md").read_text(encoding="utf-8") == "external"
    assert not (tmp_path / ".agently").exists()


def test_workspace_keeps_terminal_file_cleanup_internal(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    assert callable(workspace._close_execution_files)
    for removed in (
        "close_execution_files",
        "inspect_retention",
        "apply_retention",
        "add_retention_anchor",
        "retention_anchors",
        "get_retention_lifecycle",
        "prune_scope",
    ):
        assert not hasattr(workspace, removed)
