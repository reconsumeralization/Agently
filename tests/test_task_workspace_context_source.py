from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.TaskWorkspace import (
    TaskWorkspace,
    TaskWorkspaceContextSource,
    TaskWorkspacePolicyError,
)
from agently.core.application.SkillLibrary import SkillLibrary


@pytest.mark.asyncio
async def test_task_workspace_edits_caller_directory_with_direct_root(tmp_path: Path) -> None:
    (tmp_path / "draft.md").write_text("Draft one", encoding="utf-8")
    workspace = TaskWorkspace(tmp_path, mode="read_write", execution_id="task-1")

    edited = await workspace.edit_file("draft.md", "one", "two")
    created = await workspace.write_file("result.md", "Final result")
    readback = await workspace.read_file("result.md")

    assert workspace.root == tmp_path.resolve()
    assert edited.path == "draft.md"
    assert (tmp_path / "draft.md").read_text(encoding="utf-8") == "Draft two"
    assert created.path == "result.md"
    assert readback.content == "Final result"
    assert readback.truncated is False
    assert readback.sha256 == created.sha256


@pytest.mark.asyncio
async def test_terminal_target_write_guard_allows_only_digest_pinned_promotion(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_write", execution_id="task-guard")
    await workspace.write_file("final.md", "previous accepted body\n")
    staged = await workspace.write_file(
        "working/taskboard/finalize/terminal-candidates/final.md",
        "verified candidate body\n",
    )

    with workspace._protect_terminal_targets(["final.md"]):
        with pytest.raises(TaskWorkspacePolicyError, match="terminal target"):
            await workspace.write_file("final.md", "premature overwrite")
        with pytest.raises(TaskWorkspacePolicyError, match="terminal target"):
            await workspace.edit_file("final.md", "previous", "premature")
        with pytest.raises(TaskWorkspacePolicyError, match="terminal target"):
            await workspace.apply_patch(
                """diff --git a/final.md b/final.md
--- a/final.md
+++ b/final.md
@@ -1 +1 @@
-previous accepted body
+premature patch body
"""
            )
        promoted = await workspace.atomic_promote_file(
            staged.path,
            "final.md",
            expected_sha256=staged.sha256,
        )

    assert promoted["sha256"] == staged.sha256
    assert (tmp_path / "final.md").read_text(encoding="utf-8") == (
        "verified candidate body\n"
    )


@pytest.mark.asyncio
async def test_read_only_task_workspace_creates_task_artifact_in_private_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "existing.md").write_text("Do not mutate", encoding="utf-8")
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-42")

    created = await workspace.write_file("new/report.md", "Task artifact")
    logical_readback = await workspace.read_file("new/report.md")
    search_results = await workspace.search_files(
        "artifact",
        path="new",
        pattern="*.md",
    )

    assert created.requested_path == "new/report.md"
    assert created.path == ".agently/files/task-42/new/report.md"
    assert workspace.resolve_file_path("new/report.md") == (tmp_path / created.path).resolve()
    assert logical_readback.path == created.path
    assert logical_readback.content == "Task artifact"
    assert [item["path"] for item in search_results] == [created.path]
    assert (tmp_path / created.path).read_text(encoding="utf-8") == "Task artifact"
    with pytest.raises(TaskWorkspacePolicyError, match="write permission"):
        await workspace.write_file("existing.md", "mutated")


@pytest.mark.asyncio
async def test_context_source_exposes_execution_fallback_as_logical_file(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-context")
    created = await workspace.write_file(
        "notes/alpha.md",
        "release deadline is 2026-07-01\n",
    )
    source = TaskWorkspaceContextSource(workspace)

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=20,
    )

    assert created.path == ".agently/files/task-context/notes/alpha.md"
    assert [item.source_ref for item in page.descriptors] == ["notes/alpha.md"]
    assert page.descriptors[0].metadata["path"] == "notes/alpha.md"
    readback = await source.async_read_exact("notes/alpha.md", max_chars=200)
    assert readback.content == "release deadline is 2026-07-01\n"


@pytest.mark.asyncio
async def test_read_only_task_workspace_can_continue_only_its_own_fallback_carrier(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-42")
    created = await workspace.write_file("report.md", "first")

    appended = await workspace.write_file(created.path, " second", append=True)

    assert appended.path == created.path
    assert (tmp_path / appended.path).read_text(encoding="utf-8") == "first second"
    with pytest.raises(TaskWorkspacePolicyError, match="private state"):
        await workspace.write_file(".agently/files/another-task/report.md", "bad")


@pytest.mark.asyncio
async def test_task_workspace_enforces_path_containment(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_write")

    for unsafe in ("../outside.txt", "/tmp/outside.txt"):
        with pytest.raises(TaskWorkspacePolicyError, match="outside TaskWorkspace root"):
            await workspace.write_file(unsafe, "bad")


@pytest.mark.asyncio
async def test_task_workspace_context_source_enumerates_all_files_and_exact_reads(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.md").write_text(
        "Revenue increased by 12 percent.\nMargin was stable.",
        encoding="utf-8",
    )
    (tmp_path / "notes.md").write_text("Unrelated notes", encoding="utf-8")
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-1")
    source = TaskWorkspaceContextSource(workspace)

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=20,
    )
    descriptors = page.descriptors

    assert [item.source_ref for item in descriptors] == ["notes.md", "report.md"]
    report = next(item for item in descriptors if item.source_ref == "report.md")
    assert report.role == "information"
    block = await source.async_read_exact(report.source_ref, max_chars=200)
    assert block.content == "Revenue increased by 12 percent.\nMargin was stable."
    assert block.completeness == "complete"
    assert block.source_ref == "report.md"
    assert block.metadata["sha256"]


@pytest.mark.asyncio
async def test_task_workspace_context_source_pages_matching_files(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"report-{index}.md").write_text(
            f"shared marker {index}",
            encoding="utf-8",
        )
    source = TaskWorkspaceContextSource(
        TaskWorkspace(tmp_path, mode="read_only", execution_id="task-pages")
    )
    first = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=2,
    )
    second = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=first.next_cursor,
        limit=2,
    )

    assert [item.source_ref for item in first.descriptors] == [
        "report-0.md",
        "report-1.md",
    ]
    assert [item.source_ref for item in second.descriptors] == [
        "report-2.md",
        "report-3.md",
    ]
    assert first.next_cursor is not None
    assert set(item.source_ref for item in first.descriptors).isdisjoint(
        item.source_ref for item in second.descriptors
    )


def test_task_workspace_source_revision_observes_external_file_change(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.write_text("First", encoding="utf-8")
    source = TaskWorkspaceContextSource(TaskWorkspace(tmp_path, mode="read_only"))

    first = source.source_revision
    target.write_text("Second", encoding="utf-8")
    second = source.source_revision

    assert first != second


@pytest.mark.asyncio
async def test_task_workspace_context_source_enumerates_without_query_and_reads_exact(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.md").write_text("Revenue increased", encoding="utf-8")
    source = TaskWorkspaceContextSource(
        TaskWorkspace(tmp_path, mode="read_only", execution_id="descriptor-source")
    )

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=10,
    )
    readback = await source.async_read_exact("report.md", max_chars=100)

    assert source.source_kind == "task_workspace"
    assert [item.source_ref for item in page.descriptors] == ["report.md"]
    assert page.descriptors[0].index_text
    assert readback.content == "Revenue increased"
    assert readback.source_revision == page.source_revision


@pytest.mark.asyncio
async def test_task_workspace_context_source_indexes_image_name_without_preparing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Minimal valid PNG signature is sufficient for deterministic media
    # classification; enumeration must not base64-prepare it.
    (tmp_path / "chart.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )
    workspace = TaskWorkspace(
        tmp_path,
        mode="read_only",
        execution_id="image-context-source",
    )
    source = TaskWorkspaceContextSource(workspace)

    async def fail_if_read(*_args, **_kwargs):
        raise AssertionError("image enumeration must not prepare attachment bytes")

    monkeypatch.setattr(workspace, "read_file", fail_if_read)
    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=10,
    )

    descriptor = page.descriptors[0]
    assert descriptor.source_ref == "chart.png"
    assert descriptor.summary == "chart.png"
    assert descriptor.index_text == "chart.png"
    assert descriptor.metadata["content_kind"] == "image"
    assert descriptor.metadata["media_type"] == "image/png"
    assert "base64" not in descriptor.index_text


@pytest.mark.asyncio
async def test_task_workspace_context_source_returns_image_attachment_only_on_exact_representation(
    tmp_path: Path,
) -> None:
    (tmp_path / "chart.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )
    source = TaskWorkspaceContextSource(
        TaskWorkspace(
            tmp_path,
            mode="read_only",
            execution_id="image-context-exact-read",
        )
    )

    readback = await source.async_read_exact(
        "chart.png",
        max_chars=1,
        representation="image_attachment",
    )

    assert readback.content[0]["type"] == "image_url"
    assert readback.content[0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert readback.metadata["context_representation"] == "image_attachment"


@pytest.mark.asyncio
async def test_task_workspace_context_source_parses_xlsx_for_context(
    tmp_path: Path,
) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "课程规划"
    sheet.append(["课程", "说明"])
    sheet.append(["AI 能力边界与银行责任", "只交付解析后的文本"])
    workbook.save(tmp_path / "curriculum.xlsx")
    workbook.close()
    source = TaskWorkspaceContextSource(
        TaskWorkspace(
            tmp_path,
            mode="read_only",
            execution_id="xlsx-context-source",
        )
    )

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=10,
    )
    descriptor = page.descriptors[0]
    readback = await source.async_read_exact(
        "curriculum.xlsx",
        max_chars=20_000,
    )

    assert descriptor.metadata["content_kind"] == "office"
    assert descriptor.metadata["context_representation"] == "parsed_text"
    assert readback.metadata["context_representation"] == "parsed_text"
    assert "AI 能力边界与银行责任" in readback.content
    assert not isinstance(readback.content, bytes)


def test_task_workspace_has_no_cross_source_context_builder(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path)

    assert not hasattr(workspace, "build_context")
    assert not hasattr(workspace, "context_builder")
    assert not hasattr(workspace, "semantic_selector")
    assert not hasattr(workspace, "request")


@pytest.mark.asyncio
async def test_skill_asset_copy_on_write_does_not_mutate_installed_revision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skill"
    (source / "assets").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: Template Skill\ndescription: Provides a template.\n---\nUse the template.",
        encoding="utf-8",
    )
    (source / "assets" / "template.txt").write_text("ORIGINAL", encoding="utf-8")
    library = SkillLibrary(tmp_path / "library")
    package = library.install(source, trust="trusted")
    workspace = TaskWorkspace(tmp_path / "work", mode="read_write")

    copied = await workspace.copy_from(
        Path(package.installed_path) / "assets" / "template.txt",
        "drafts/template.txt",
    )
    await workspace.edit_file(copied.path, "ORIGINAL", "CHANGED")

    assert (workspace.root / copied.path).read_text(encoding="utf-8") == "CHANGED"
    assert library.read_resource(package.revision_ref, "assets/template.txt").text == "ORIGINAL"
