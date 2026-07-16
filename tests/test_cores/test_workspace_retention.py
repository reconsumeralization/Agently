from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.Workspace import Workspace
from agently.core.Workspace.Identity import WorkspaceIdentityCatalog


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


@pytest.mark.asyncio
async def test_identity_retention_collects_only_unreachable_reference_closure(
    tmp_path: Path,
) -> None:
    system_root = tmp_path / ".agently"
    catalog = WorkspaceIdentityCatalog(system_root, workspace_id="workspace-alpha")
    blob_root = system_root / "identity" / "blobs"
    blob_root.mkdir(parents=True)
    old_blob = blob_root / "old.bin"
    shared_blob = blob_root / "shared.bin"
    old_blob.write_bytes(b"old")
    shared_blob.write_bytes(b"shared")

    old = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="report.md",
        digest="a" * 64,
        size=3,
        payload_pointer={"type": "managed_blob", "path": "identity/blobs/old.bin", "managed": True},
    )
    old_segment = await catalog.add_segment(
        content_version_id=old.content_version_id,
        ordinal=0,
        offset=0,
        length=3,
        digest="a" * 64,
        payload_pointer={"type": "managed_blob", "path": "identity/blobs/old.bin", "managed": True},
    )
    old_link = await catalog.add_link(
        source_id=old.content_version_id,
        target_id=old_segment.entity_id,
        relation="contains",
        role="source_segment",
    )
    current = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="report.md",
        digest="b" * 64,
        size=6,
        payload_pointer={"type": "managed_blob", "path": "identity/blobs/shared.bin", "managed": True},
    )
    current_segment = await catalog.add_segment(
        content_version_id=current.content_version_id,
        ordinal=0,
        offset=0,
        length=6,
        digest="b" * 64,
        payload_pointer={"type": "managed_blob", "path": "identity/blobs/shared.bin", "managed": True},
    )
    current_link = await catalog.add_link(
        source_id=current.content_version_id,
        target_id=current_segment.entity_id,
        relation="contains",
        role="source_segment",
    )
    mirrored = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="mirror/report.md",
        digest="b" * 64,
        size=6,
        payload_pointer={"type": "managed_blob", "path": "identity/blobs/shared.bin", "managed": True},
    )
    await catalog.retain_task_manifest(
        "agent_task_one",
        root_ids=[current.content_version_id],
        state="accepted",
    )
    await catalog.pin(mirrored.content_version_id, reason="application")

    report = await catalog.collect_unreachable()

    assert set(report.deleted_entity_ids) == {
        old.content_version_id,
        old_segment.entity_id,
        old_link.entity_id,
    }
    assert {
        current.locator_id,
        current.content_version_id,
        current_segment.entity_id,
        current_link.entity_id,
        mirrored.locator_id,
        mirrored.content_version_id,
    } <= set(report.retained_entity_ids)
    assert not old_blob.exists()
    assert shared_blob.read_bytes() == b"shared"

    await catalog.unpin(mirrored.content_version_id)
    second_report = await catalog.collect_unreachable()
    assert mirrored.locator_id in second_report.deleted_entity_ids
    assert mirrored.content_version_id in second_report.deleted_entity_ids
    assert shared_blob.exists()

    before = int(report.high_water)
    next_identity = await catalog.allocate("record")
    assert next_identity.sequence > before


@pytest.mark.asyncio
async def test_released_task_manifest_drops_roots_without_leaving_identity_debris(
    tmp_path: Path,
) -> None:
    catalog = WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    observed = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="ephemeral.md",
        digest="a" * 64,
        size=1,
        payload_pointer={"type": "workspace_file", "path": "ephemeral.md"},
    )
    await catalog.retain_task_manifest(
        "agent_task_ephemeral",
        root_ids=[observed.content_version_id],
        state="accepted",
    )
    first = await catalog.collect_unreachable()
    assert observed.content_version_id in first.retained_entity_ids

    await catalog.retain_task_manifest(
        "agent_task_ephemeral",
        root_ids=[],
        state="released",
    )
    second = await catalog.collect_unreachable()

    assert observed.locator_id in second.deleted_entity_ids
    assert observed.content_version_id in second.deleted_entity_ids
    assert list((tmp_path / ".agently" / "identity" / "tasks").rglob("manifest.json")) == []
    assert not (tmp_path / ".agently" / "identity" / "tombstones").exists()


@pytest.mark.asyncio
async def test_only_explicit_audit_deletion_keeps_a_compact_tombstone(tmp_path: Path) -> None:
    catalog = WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    observed = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="audit.md",
        digest="a" * 64,
        size=1,
        payload_pointer={"type": "workspace_file", "path": "audit.md"},
    )

    report = await catalog.collect_unreachable(audit_retained_ids=[observed.content_version_id])

    assert observed.locator_id in report.deleted_entity_ids
    assert observed.content_version_id in report.deleted_entity_ids
    tombstones = list((tmp_path / ".agently" / "identity" / "tombstones").rglob("*.json"))
    assert len(tombstones) == 1
    assert observed.content_version_id in tombstones[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_collected_version_is_pruned_from_locator_index_and_never_resurrected(
    tmp_path: Path,
) -> None:
    catalog = WorkspaceIdentityCatalog(
        tmp_path / ".agently",
        workspace_id="workspace-alpha",
    )
    old = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="changing.md",
        digest="a" * 64,
        size=1,
        payload_pointer={"type": "workspace_file", "path": "changing.md"},
    )
    current = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="changing.md",
        digest="b" * 64,
        size=2,
        payload_pointer={"type": "workspace_file", "path": "changing.md"},
    )

    collected = await catalog.collect_unreachable(strong_roots=[current.content_version_id])
    assert old.content_version_id in collected.deleted_entity_ids

    observed_again = await catalog.observe_content(
        locator_kind="path",
        normalized_locator="changing.md",
        digest="a" * 64,
        size=1,
        payload_pointer={"type": "workspace_file", "path": "changing.md"},
    )

    assert observed_again.created is True
    assert observed_again.locator_id == old.locator_id
    assert observed_again.content_version_id != old.content_version_id
    assert (await catalog.resolve(observed_again.content_version_id))["digest"] == "a" * 64
