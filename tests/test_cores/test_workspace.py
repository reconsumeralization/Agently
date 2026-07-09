import asyncio
import base64
import hashlib
from importlib import import_module
from numbers import Real
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import (
    LazyWorkspace,
    LocalVectorIndex,
    WorkspaceConfigurationError,
    WorkspaceManager,
    WorkspacePolicyError,
)
from agently.core.application import AgentTask
from agently.core.workspace._defaults import WORKSPACE_GUIDE_FILENAME, script_scope
from agently.core.orchestration.TriggerFlow import diagnose_runtime_event_records, project_runtime_event_record
from agently.types.data import RuntimeEvent, RunContext, WorkspaceContextPackage, WorkspaceContextPlan, WorkspaceRecordRef


def _retrieval_ref_id(item: Any) -> str:
    return cast(WorkspaceRecordRef, item.get("ref"))["id"]


def _retrieval_tags(item: Any) -> list[str]:
    return cast(list[str], item.get("tags"))


@pytest.mark.asyncio
async def test_agent_has_lazy_workspace_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = Agently.create_agent("lazy-workspace")
    workspace = agent.workspace

    assert isinstance(workspace, LazyWorkspace)
    assert workspace.is_materialized is False
    expected_root = tmp_path / ".agently" / "workspaces" / "scripts" / script_scope(agent.settings)
    assert workspace.root == expected_root.resolve()
    assert workspace.files_root == (
        expected_root / "files" / "lineage" / "agents" / "lazy-workspace" / "files"
    ).resolve()
    assert agent.settings.get("workspace.lazy") is True
    assert agent.settings.get("workspace.root") == str(workspace.root)
    assert not workspace.root.exists()

    ref = await workspace.put(
        {"status": "created"},
        collection="observations",
        kind="lazy_default_probe",
    )

    assert workspace.is_materialized is True
    assert ref["collection"] == "observations"
    assert workspace.root.exists()
    assert (workspace.root / "workspace.db").is_file()
    assert agent.settings.get("workspace.lazy") is False


@pytest.mark.asyncio
async def test_workspace_writes_layout_guides(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "guided")

    root_guide = workspace.root / WORKSPACE_GUIDE_FILENAME
    files_guide = workspace.files_root / WORKSPACE_GUIDE_FILENAME

    assert root_guide.is_file()
    assert files_guide.is_file()
    root_text = root_guide.read_text(encoding="utf-8")
    files_text = files_guide.read_text(encoding="utf-8")
    assert "workspace.db" in root_text
    assert "content/" in root_text
    assert "files/" in root_text
    assert "Standard file areas" in root_text
    assert "downloads/" in root_text
    assert "artifacts/" in root_text
    assert "reports/" in root_text
    assert "editable file working tree" in files_text
    assert "Standard file areas" in files_text
    assert "downloads/" in files_text
    assert "artifacts/" in files_text
    assert "reports/" in files_text
    assert "Workspace.open_scratch" in files_text
    assert str(workspace.files_root) in files_text

    child = workspace.with_scope_node("tasks", "task-one")
    child_guide = child.files_root / WORKSPACE_GUIDE_FILENAME
    assert child_guide.is_file()
    child_text = child_guide.read_text(encoding="utf-8")
    assert "tasks/task-one" in child_text
    assert "task_id" in child_text


def test_workspace_standard_file_area_paths_are_scoped(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "areas")

    assert set(workspace.standard_file_areas()) == {"downloads", "artifacts", "reports"}
    assert workspace.file_area_path("download", "remote.pdf") == workspace.files_root / "downloads" / "remote.pdf"
    reports_path = workspace.file_area_path("output", "daily", "brief.md", create=True)
    assert reports_path == workspace.files_root / "reports" / "daily" / "brief.md"
    assert reports_path.parent.is_dir()

    with pytest.raises(ValueError):
        workspace.file_area_path("unknown", "x.txt")
    with pytest.raises(ValueError):
        workspace.file_area_path("downloads", "../escape.txt")
    with pytest.raises(ValueError):
        workspace.file_area_path("downloads", tmp_path / "outside.txt")

    read_only = Agently.create_workspace(tmp_path / "areas", mode="read")
    with pytest.raises(PermissionError):
        read_only.file_area_path("reports", create=True)


@pytest.mark.asyncio
async def test_agent_default_workspace_rebinds_to_session_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = Agently.create_agent("session-worker")
    second = Agently.create_agent("session-worker")

    first.activate_session(session_id="issue-123")
    second.activate_session(session_id="issue-123")

    assert first.workspace.root == second.workspace.root
    assert first.workspace.root == (tmp_path / ".agently" / "workspaces" / "sessions" / "issue-123").resolve()
    assert first.workspace.files_root == (
        tmp_path / ".agently" / "workspaces" / "sessions" / "issue-123"
        / "files" / "lineage" / "agents" / "session-worker" / "files"
    ).resolve()

    await first.workspace.put("first", collection="observations", kind="probe")
    await second.workspace.put("second", collection="observations", kind="probe")

    assert (first.workspace.root / "workspace.db").is_file()
    assert first.workspace.root == second.workspace.root
    assert len(list((tmp_path / ".agently" / "workspaces" / "sessions").glob("**/workspace.db"))) == 1


@pytest.mark.asyncio
async def test_workspace_search_defaults_to_session_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = Agently.create_agent("scope-a").activate_session(session_id="scope-a")
    second = Agently.create_agent("scope-b").activate_session(session_id="scope-b")

    await first.workspace.put(
        content="visible only to session a",
        collection="observations",
        kind="scoped",
        summary="shared keyword",
    )
    await second.workspace.put(
        content="visible only to session b",
        collection="observations",
        kind="scoped",
        summary="shared keyword",
    )

    first_results = await first.workspace.search("shared keyword")
    second_results = await second.workspace.search("shared keyword")

    assert [item["scope"]["session_id"] for item in first_results] == ["scope-a"]
    assert [item["scope"]["session_id"] for item in second_results] == ["scope-b"]


@pytest.mark.asyncio
async def test_agent_tasks_share_script_workspace_db_and_isolate_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = Agently.create_agent("task-worker")

    first = AgentTask(
        agent,
        goal="Handle the first task.",
        success_criteria=["The first task has a durable observation."],
        task_id="task-one",
    )
    second = AgentTask(
        agent,
        goal="Handle the second task.",
        success_criteria=["The second task has a durable observation."],
        task_id="task-two",
    )

    assert first.workspace.root == second.workspace.root
    assert first.workspace.files_root == (
        first.workspace.root / "files" / "lineage" / "agents" / "task-worker" / "tasks" / "task-one" / "files"
    ).resolve()
    assert second.workspace.files_root == (
        second.workspace.root / "files" / "lineage" / "agents" / "task-worker" / "tasks" / "task-two" / "files"
    ).resolve()
    assert first.workspace.files_root != second.workspace.files_root

    await first.workspace.put("first task", collection="observations", kind="task_probe")
    await second.workspace.put("second task", collection="observations", kind="task_probe")

    assert len(list((tmp_path / ".agently" / "workspaces" / "scripts").glob("**/workspace.db"))) == 1


@pytest.mark.asyncio
async def test_workspace_local_put_search_link_and_get(tmp_path):
    agent = Agently.create_agent("workspace-test").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    ref = await workspace.put(
        content="pytest failed in route fallback test\nstack trace",
        collection="observations",
        kind="test_output",
        summary="pytest failed in route fallback test",
        scope={"task_id": "issue-123", "turn": 1},
        source={"type": "command", "name": "pytest"},
    )
    decision_ref = await workspace.put(
        "Use AgentRouteProvider as the route boundary.",
        collection="decisions",
        kind="architecture_decision",
        summary="Use AgentRouteProvider boundary",
        scope={"task_id": "issue-123", "turn": 1},
        source={"type": "human"},
        meta={"status": "accepted"},
    )
    link_ref = await workspace.link(decision_ref, ref, relation="responds_to")

    assert ref["id"].startswith("rec_")
    assert ref["collection"] == "observations"
    assert ref["sha256"]
    assert ref["size"] > 0
    assert link_ref["source_id"] == decision_ref["id"]

    content = await workspace.get(ref)
    assert "route fallback" in content

    results = await workspace.search(
        "route fallback",
        filters={"collection": "observations", "kind": "test_output"},
    )
    assert [item["id"] for item in results] == [ref["id"]]
    by_id = await workspace.search("route fallback", filters={"id": ref["id"]})
    by_path = await workspace.search("route fallback", filters={"path": ref["path"]})
    assert [item["id"] for item in by_id] == [ref["id"]]
    assert [item["id"] for item in by_path] == [ref["id"]]
    assert (tmp_path / "run" / "workspace.meta.json").is_file()
    assert (tmp_path / "run" / "workspace.db").is_file()
    assert (tmp_path / "run" / "files").is_dir()
    assert (tmp_path / "run" / "content" / "observations" / "_collection.meta.json").is_file()


@pytest.mark.asyncio
async def test_workspace_checkpoint_is_compact_and_searchable(tmp_path):
    agent = Agently.create_agent("workspace-checkpoint").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    ref = await workspace.checkpoint(
        "issue-123",
        {"phase": "debugging", "refs": ["rec_test"]},
        step_id="step-1",
    )

    assert ref["collection"] == "checkpoints"
    assert ref["scope"]["run_id"] == "issue-123"
    assert ref["scope"]["step_id"] == "step-1"
    data = await workspace.get(ref)
    assert "debugging" in data
    structured_data = await workspace.get_data(ref)
    assert structured_data == {"phase": "debugging", "refs": ["rec_test"]}

    latest = await workspace.latest_checkpoint("issue-123")
    assert latest is not None
    assert latest["id"] == ref["id"]
    history = await workspace.checkpoint_history("issue-123", step_id="step-1")
    assert [item["id"] for item in history] == [ref["id"]]


@pytest.mark.asyncio
async def test_workspace_checkpoint_cas_lease_and_artifact_refs(tmp_path):
    agent = Agently.create_agent("workspace-durable-provider").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    first = await workspace.put_checkpoint(
        "run-cas",
        {"state_version": 1, "value": "first"},
        step_id="first",
        expected_state_version=0,
    )
    latest = await workspace.get_checkpoint("run-cas")
    assert latest is not None
    assert latest["id"] == first["id"]

    with pytest.raises(RuntimeError, match="state version conflict"):
        await workspace.put_checkpoint(
            "run-cas",
            {"state_version": 2, "value": "stale"},
            step_id="stale",
            expected_state_version=0,
        )

    second = await workspace.put_checkpoint(
        "run-cas",
        {"state_version": 2, "value": "second"},
        step_id="second",
        expected_state_version=1,
    )
    artifact_ref = await workspace.put_artifact_ref(
        "run-cas",
        {"large": "payload"},
        metadata={"kind": "checkpoint_payload", "summary": "large Workspace record payload"},
    )
    lease = await workspace.claim_lease("run-cas", "worker-1", ttl=0.2, expected_state_version=2)

    assert second["id"] != first["id"]
    assert artifact_ref["collection"] == "artifacts"
    assert artifact_ref["scope"]["run_id"] == "run-cas"
    assert lease.get("owner_id") == "worker-1"
    assert lease.get("state_version") == 2
    with pytest.raises(RuntimeError, match="lease conflict"):
        await workspace.claim_lease("run-cas", "worker-2", ttl=0.2, expected_state_version=2)

    lease_token = lease.get("lease_token")
    lease_until = lease.get("lease_until")
    assert isinstance(lease_token, str)
    assert isinstance(lease_until, Real)
    refreshed = await workspace.heartbeat_lease("run-cas", "worker-1", lease_token)
    refreshed_until = refreshed.get("lease_until")
    refreshed_token = refreshed.get("lease_token")
    assert isinstance(refreshed_until, Real)
    assert isinstance(refreshed_token, str)
    assert refreshed_until >= lease_until
    released = await workspace.release_lease("run-cas", "worker-1", refreshed_token)
    assert released.get("released_at") is not None

    expired = await workspace.claim_lease("run-cas", "worker-1", ttl=0.01, expected_state_version=2)
    expired_token = expired.get("lease_token")
    assert isinstance(expired_token, str)
    await asyncio.sleep(0.02)
    stolen = await workspace.claim_lease("run-cas", "worker-2", ttl=0.2, expected_state_version=2)
    assert stolen.get("owner_id") == "worker-2"
    with pytest.raises(RuntimeError, match="lease conflict|expired"):
        await workspace.heartbeat_lease("run-cas", "worker-1", expired_token)


@pytest.mark.asyncio
async def test_workspace_scoped_checkpoint_reads_do_not_cross_scope(tmp_path):
    workspace = Agently.create_workspace(
        tmp_path / "shared-checkpoint-scope",
        default_scope={"project_id": "shared-project"},
        default_search_scope={"project_id": "shared-project"},
    )
    first = workspace.with_scope_node(
        "sessions",
        "first-session",
        scope={"session_id": "first-session"},
        search_scope={"session_id": "first-session"},
    )
    second = workspace.with_scope_node(
        "sessions",
        "second-session",
        scope={"session_id": "second-session"},
        search_scope={"session_id": "second-session"},
    )

    first_checkpoint = await first.put_checkpoint(
        "shared-run",
        {"owner": "first", "state_version": 1},
        step_id="phase",
    )
    second_checkpoint = await second.put_checkpoint(
        "shared-run",
        {"owner": "second", "state_version": 1},
        step_id="phase",
    )

    first_latest = await first.latest_checkpoint("shared-run")
    first_get = await first.get_checkpoint("shared-run")
    second_latest = await second.latest_checkpoint("shared-run")
    assert first_latest is not None
    assert first_get is not None
    assert second_latest is not None
    assert first_latest["id"] == first_checkpoint["id"]
    assert first_get["id"] == first_checkpoint["id"]
    assert second_latest["id"] == second_checkpoint["id"]
    assert [item["id"] for item in await first.checkpoint_history("shared-run")] == [first_checkpoint["id"]]
    assert [item["id"] for item in await second.checkpoint_history("shared-run")] == [second_checkpoint["id"]]
    assert await first.get_snapshot("shared-run") == {"owner": "first", "state_version": 1}
    assert await second.get_snapshot("shared-run") == {"owner": "second", "state_version": 1}


@pytest.mark.asyncio
async def test_workspace_scoped_artifact_ref_inherits_workspace_scope(tmp_path):
    workspace = Agently.create_workspace(
        tmp_path / "shared-artifact-scope",
        default_scope={"project_id": "shared-project"},
        default_search_scope={"project_id": "shared-project"},
    )
    execution = workspace.with_scope_node(
        "executions",
        "exec-1",
        scope={"session_id": "scope-a"},
        search_scope={"session_id": "scope-a"},
    )

    artifact_ref = await execution.put_artifact_ref(
        "artifact-run",
        {"large": "payload"},
        metadata={
            "kind": "checkpoint_payload",
            "summary": "scoped artifact payload",
            "scope": {"custom": "kept"},
        },
    )

    assert artifact_ref["scope"]["project_id"] == "shared-project"
    assert artifact_ref["scope"]["session_id"] == "scope-a"
    assert artifact_ref["scope"]["execution_id"] == "exec-1"
    assert artifact_ref["scope"]["run_id"] == "artifact-run"
    assert artifact_ref["scope"]["custom"] == "kept"
    hits = await execution.search("payload", filters={"collection": "artifacts"})
    assert [item["id"] for item in hits] == [artifact_ref["id"]]


@pytest.mark.asyncio
async def test_workspace_rejects_path_traversal_and_read_only_writes(tmp_path):
    agent = Agently.create_agent("workspace-policy").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    with pytest.raises(WorkspacePolicyError):
        await workspace.get("../outside.txt")

    read_only_agent = Agently.create_agent("workspace-readonly").use_workspace(
        tmp_path / "run",
        create=False,
        mode="read_only",
    )
    read_only_workspace = read_only_agent.workspace
    assert read_only_workspace is not None
    with pytest.raises(WorkspacePolicyError):
        await read_only_workspace.put("nope", collection="observations")


def test_workspace_manager_registers_builtin_profiles():
    assert "fast" in Agently.workspace.list_profiles()
    assert "checkpoint" in Agently.workspace.list_profiles()
    assert "auto" in Agently.workspace.list_context_profiles()
    assert "text" in Agently.workspace.list_file_io_handlers()
    assert "pdf" in Agently.workspace.list_file_io_handlers()


@pytest.mark.asyncio
async def test_workspace_put_accepts_content_keyword_and_compat_profiles(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "put-profiles")

    record_ref = await workspace.put(
        content="profile-backed put record",
        collection="observations",
        kind="note",
        summary="profile-backed put",
        scope={"case_id": "put-profile"},
        profile="fast",
    )
    assert await workspace.get_data(record_ref) == "profile-backed put record"
    assert record_ref["scope"]["case_id"] == "put-profile"

    checkpoint_ref = await workspace.put(
        content={"status": "ready"},
        collection="checkpoints",
        kind="checkpoint",
        scope={"run_id": "put-profile-run", "step_id": "phase-1"},
        profile="checkpoint",
    )
    latest = await workspace.get_checkpoint("put-profile-run")
    assert latest is not None
    assert latest["id"] == checkpoint_ref["id"]

    alias_ref = await workspace.ingest(
        content="legacy alias record",
        collection="observations",
        kind="note",
        scope={"case_id": "ingest-alias"},
    )
    assert await workspace.get_data(alias_ref) == "legacy alias record"


@pytest.mark.asyncio
async def test_workspace_file_io_text_read_write_binary_and_policy(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "file-io")

    written = await workspace.write_file("notes/todo.txt", "hello world")
    assert written["ok"] is True
    assert written["path"] == "notes/todo.txt"
    assert written["bytes"] == len("hello world".encode("utf-8"))
    assert written["sha256"] == hashlib.sha256(b"hello world").hexdigest()

    read = await workspace.read_file("notes/todo.txt", max_bytes=5, offset=6)
    assert read["ok"] is True
    assert read["readable"] is True
    assert read["content"] == "world"
    assert read["offset"] == 6
    assert read["read_bytes"] == 5
    assert read["truncated"] is False
    assert read["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert read["file_refs"][0]["path"] == "notes/todo.txt"

    materialized = await workspace.materialize_file(
        "downloads/remote.txt",
        b"downloaded official syllabus",
        source={"kind": "test_download", "url": "https://example.com/syllabus.txt"},
        media_type="text/plain",
    )
    assert materialized["ok"] is True
    assert materialized["path"] == "downloads/remote.txt"
    assert materialized["bytes"] == len(b"downloaded official syllabus")
    assert materialized["sha256"] == hashlib.sha256(b"downloaded official syllabus").hexdigest()
    assert materialized["file_refs"][0]["role"] == "download"
    materialized_read = await workspace.read_file("downloads/remote.txt")
    assert materialized_read["ok"] is True
    assert materialized_read["content"] == "downloaded official syllabus"

    (workspace.files_root / "payload.bin").write_bytes(b"\x00\xffbinary")
    binary = await workspace.read_file("payload.bin")
    assert binary["ok"] is False
    assert binary["readable"] is False
    assert binary["diagnostics"][0]["code"] == "workspace.file.no_read_handler"

    with pytest.raises(ValueError, match="outside workspace file root"):
        await workspace.read_file("../outside.txt")

    read_only = WorkspaceManager().create(tmp_path / "readonly", mode="read_only")
    with pytest.raises(PermissionError, match="read-only"):
        await read_only.write_file("blocked.txt", "nope")
    with pytest.raises(PermissionError, match="read-only"):
        await read_only.materialize_file("blocked.bin", b"nope")


@pytest.mark.asyncio
async def test_workspace_file_io_handler_registry_and_dispatch(tmp_path):
    manager = WorkspaceManager()
    workspace = manager.create(tmp_path / "dispatch")
    await workspace.write_file("note.upper", "mixed case")
    events: list[str] = []

    class UpperHandler:
        name = "upper"
        priority = 10
        DEFAULT_SETTINGS: dict[str, Any] = {}

        @staticmethod
        def _on_register():
            events.append("upper.register")

        @staticmethod
        def _on_unregister():
            events.append("upper.unregister")

        def supports(self, *, operation, file_info, export_kind=None):
            _ = export_kind
            return operation == "read" and file_info.get("extension") == ".upper"

        async def read(self, *, path, file_info, max_bytes=20000, offset=0, options=None):
            _ = (max_bytes, offset, options)
            return {
                "ok": True,
                "readable": True,
                "path": file_info["path"],
                "content": path.read_text(encoding="utf-8").upper(),
                "truncated": False,
                "bytes": file_info["bytes"],
                "offset": 0,
                "read_bytes": file_info["bytes"],
                "sha256": file_info["sha256"],
                "media_type": file_info.get("media_type"),
                "content_kind": file_info["content_kind"],
                "encoding": "utf-8",
                "handler_id": self.name,
                "extraction_method": "test.upper",
                "diagnostics": [],
                "file_refs": [],
            }

        async def write(self, *, path, file_info, content, append=False, options=None):
            _ = (path, file_info, content, append, options)
            raise AssertionError("UpperHandler is read-only in this test.")

        async def export(self, *, source_path, output_path, source_info, output_info, export_kind, options=None):
            _ = (source_path, output_path, source_info, output_info, export_kind, options)
            raise AssertionError("UpperHandler does not export in this test.")

    manager.register_file_io_handler(cast(Any, UpperHandler()))
    assert events == ["upper.register"]
    assert "upper" in manager.list_file_io_handlers()
    with pytest.raises(WorkspaceConfigurationError, match="already registered"):
        manager.register_file_io_handler(cast(Any, UpperHandler()))
    assert events == ["upper.register"]

    result = await workspace.read_file("note.upper")
    assert result["handler_id"] == "upper"
    assert result["content"] == "MIXED CASE"

    class ReplacementUpperHandler(UpperHandler):
        @staticmethod
        def _on_register():
            events.append("replacement.register")

        @staticmethod
        def _on_unregister():
            events.append("replacement.unregister")

        async def read(self, *, path, file_info, max_bytes=20000, offset=0, options=None):
            result = await super().read(
                path=path,
                file_info=file_info,
                max_bytes=max_bytes,
                offset=offset,
                options=options,
            )
            result["content"] = str(result["content"]).lower()
            return result

    manager.register_file_io_handler(cast(Any, ReplacementUpperHandler()), replace=True)
    assert events == ["upper.register", "replacement.register", "upper.unregister"]
    replaced = await workspace.read_file("note.upper")
    assert replaced["handler_id"] == "upper"
    assert replaced["content"] == "mixed case"

    manager.unregister_file_io_handler("upper")
    assert events == ["upper.register", "replacement.register", "upper.unregister", "replacement.unregister"]
    assert "upper" not in manager.list_file_io_handlers()


@pytest.mark.asyncio
async def test_workspace_file_io_optional_dependencies_fail_closed(tmp_path, monkeypatch):
    workspace = Agently.create_workspace(tmp_path / "optional-deps")

    def missing_dependency(name: str, *args: Any, **kwargs: Any):
        _ = (args, kwargs)
        raise ImportError(name)

    monkeypatch.setattr("agently.core.workspace.FileIO.LazyImport.import_package", missing_dependency)

    (workspace.files_root / "scan.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    pdf = await workspace.read_file("scan.pdf")
    assert pdf["ok"] is False
    assert pdf["readable"] is False
    assert pdf["diagnostics"][0]["code"] == "workspace.file.pdf_dependency_missing"

    (workspace.files_root / "sheet.xlsx").write_bytes(b"PK\x03\x04placeholder")
    office = await workspace.read_file("sheet.xlsx")
    assert office["ok"] is False
    assert office["diagnostics"][0]["code"] == "workspace.file.xlsx_dependency_missing"

    await workspace.write_file("page.html", "<h1>Hello</h1>")
    exported = await workspace.export_file("page.html", "page.pdf", export_kind="html_pdf")
    assert exported["ok"] is False
    assert exported["exported"] is False
    assert exported["diagnostics"][0]["code"] == "workspace.file.export_dependency_missing"


@pytest.mark.asyncio
async def test_workspace_file_io_image_prepares_model_attachment(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "image-vlm")
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    (workspace.files_root / "pixel.png").write_bytes(png_bytes)

    result = await workspace.read_file("pixel.png")

    assert result["ok"] is True
    assert result["readable"] is False
    assert result["content"] == ""
    assert result["handler_id"] == "image_vlm"
    assert result["extraction_method"] == "model.image_attachment.prepare"
    attachments = result.get("attachments", [])
    assert attachments[0]["type"] == "image_url"
    assert attachments[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_workspace_file_io_optional_handlers_success_when_dependencies_available(tmp_path):
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    pytest.importorskip("docx")
    pytest.importorskip("openpyxl")
    pytest.importorskip("pptx")
    pytest.importorskip("markdown")
    pytest.importorskip("playwright.async_api")

    workspace = Agently.create_workspace(tmp_path / "optional-success")
    root = workspace.files_root

    canvas_module = import_module("reportlab.pdfgen.canvas")
    canvas = canvas_module.Canvas(str(root / "sample.pdf"))
    canvas.drawString(72, 720, "workspace pdf success")
    canvas.save()

    docx_module = import_module("docx")
    document = docx_module.Document()
    document.add_paragraph("workspace docx success")
    document.save(str(root / "sample.docx"))

    openpyxl = import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["kind", "status"])
    sheet.append(["workspace xlsx", "success"])
    workbook.save(str(root / "sample.xlsx"))

    pptx_module = import_module("pptx")
    presentation = pptx_module.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    textbox = slide.shapes.add_textbox(1000000, 1000000, 6000000, 1000000)
    textbox.text = "workspace pptx success"
    presentation.save(str(root / "sample.pptx"))

    await workspace.write_file("sample.html", "<html><body><h1>workspace html export success</h1></body></html>")
    await workspace.write_file("sample.md", "# workspace markdown export success\n")

    pdf = await workspace.read_file("sample.pdf")
    docx = await workspace.read_file("sample.docx")
    xlsx = await workspace.read_file("sample.xlsx")
    pptx_result = await workspace.read_file("sample.pptx")

    assert pdf["ok"] is True
    assert pdf["extraction_method"] == "pypdf.extract_text"
    assert "workspace pdf success" in pdf["content"]
    assert docx["ok"] is True
    assert docx["extraction_method"] == "python-docx"
    assert "workspace docx success" in docx["content"]
    assert xlsx["ok"] is True
    assert xlsx["extraction_method"] == "openpyxl"
    assert "workspace xlsx\tsuccess" in xlsx["content"]
    assert pptx_result["ok"] is True
    assert pptx_result["extraction_method"] == "python-pptx"
    assert "workspace pptx success" in pptx_result["content"]

    async def assert_exported(source: str, output: str, export_kind: str):
        result = await workspace.export_file(source, output, export_kind=export_kind)
        diagnostics = result["diagnostics"]
        if diagnostics and diagnostics[0]["code"] == "workspace.file.export_failed":
            message = diagnostics[0]["message"]
            if "Executable doesn't exist" in message or "playwright install" in message:
                pytest.skip("Playwright browser runtime is not installed.")
        assert result["ok"] is True
        assert result["exported"] is True
        assert result["bytes"] > 0
        assert result["file_refs"][1]["role"] == "output"
        assert (workspace.files_root / output).is_file()

    await assert_exported("sample.html", "sample.html.pdf", "html_pdf")
    await assert_exported("sample.md", "sample.md.pdf", "markdown_pdf")
    await assert_exported("sample.html", "sample.png", "html_screenshot")


@pytest.mark.asyncio
async def test_create_workspace_public_factory_can_be_shared_with_agent(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "shared")
    agent = Agently.create_agent("shared-workspace").use_workspace(workspace)

    ref = await agent.workspace.put(
        {"status": "shared"},
        collection="observations",
        kind="shared_workspace_probe",
    )

    assert ref["collection"] == "observations"
    assert await workspace.get_data(ref) == {"status": "shared"}


def test_workspace_backend_provider_missing_registration_fails_fast():
    with pytest.raises(WorkspaceConfigurationError, match="Workspace backend provider is not registered"):
        Agently.workspace.create(provider="missing-provider")


def test_workspace_component_provider_registry_and_conflicts(tmp_path):
    manager = WorkspaceManager()

    assert "sqlite" in manager.list_db_store_providers()
    assert {"agent", "callable"}.issubset(set(manager.list_embedding_providers()))
    assert {"chroma", "sqlite"}.issubset(set(manager.list_vector_store_providers()))

    with pytest.raises(WorkspaceConfigurationError, match="cannot be combined"):
        manager.create(
            tmp_path / "provider-conflict",
            provider="missing-provider",
            embedding_provider=lambda texts: [[1.0] for _ in texts],
        )

    manager.unregister_vector_store_provider("chroma")
    fallback = manager.create(tmp_path / "auto-fallback", vector_store_provider="auto")
    assert getattr(fallback.backend, "vector_store_provider_name") == "sqlite"
    assert str(getattr(fallback.backend, "vector_store_fallback_reason")).startswith("chroma_unavailable")

    seen_db_store_options: dict[str, Any] = {}

    def sqlite_alias_provider(**options: Any) -> str:
        seen_db_store_options.update(options)
        return "sqlite"

    manager.register_db_store_provider("sqlite-alias", sqlite_alias_provider)
    aliased = manager.create(
        tmp_path / "sqlite-alias",
        db_store_provider="sqlite-alias",
        db_store_options={"label": "db-store-options"},
        vector_store_provider="sqlite",
    )
    assert seen_db_store_options["label"] == "db-store-options"
    assert seen_db_store_options["root"] == tmp_path / "sqlite-alias"
    assert aliased.capabilities()["components"]["db_store_provider"] == "sqlite-alias"

    with pytest.raises(WorkspaceConfigurationError, match="Workspace vector store provider is not registered"):
        manager.create(tmp_path / "explicit-chroma-missing", vector_store_provider="chroma")


@pytest.mark.asyncio
async def test_workspace_db_store_provider_delegates_record_behavior(tmp_path):
    class CustomDBStoreProvider:
        name = "custom-db"

        def __init__(self):
            self.records: list[WorkspaceRecordRef] = []
            self.data: dict[str, Any] = {}

        async def put(
            self,
            content: Any,
            *,
            collection: str,
            kind: str | None = None,
            summary: str | None = None,
            scope: dict[str, Any] | None = None,
            source: dict[str, Any] | None = None,
            meta: dict[str, Any] | None = None,
        ) -> WorkspaceRecordRef:
            record_id = f"custom-{len(self.records) + 1}"
            ref: WorkspaceRecordRef = {
                "id": record_id,
                "collection": collection,
                "kind": kind,
                "path": f"{collection}/{record_id}.json",
                "sha256": "custom",
                "size": 0,
                "summary": summary or str(content),
                "scope": scope or {},
                "source": source or {},
                "meta": meta or {},
                "created_at": "2026-07-09T00:00:00Z",
            }
            self.records.append(ref)
            self.data[record_id] = content
            return ref

        async def put_record(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef:
            self.records.append(ref)
            return ref

        async def get_record(self, record_id: str) -> WorkspaceRecordRef | None:
            return next((ref for ref in self.records if ref["id"] == record_id), None)

        async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
            _ = ref, content

        async def get(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
            return await self.get_data(ref_or_path)

        async def get_data(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
            record_id = ref_or_path["id"] if isinstance(ref_or_path, dict) else str(ref_or_path)
            return self.data[record_id]

        async def ref_envelope(self, ref_or_id: WorkspaceRecordRef | str) -> dict[str, Any]:
            record_id = ref_or_id["id"] if isinstance(ref_or_id, dict) else str(ref_or_id)
            ref = await self.get_record(record_id)
            assert ref is not None
            return {"workspace_id": "custom", "kind": "record", "record_id": ref["id"], **ref}

        async def search(
            self,
            query: str | None = None,
            filters: dict[str, Any] | None = None,
        ) -> list[WorkspaceRecordRef]:
            _ = query
            filters = filters or {}
            results = list(self.records)
            for key, value in filters.items():
                if key == "collection":
                    results = [ref for ref in results if ref["collection"] == value]
                elif key == "kind":
                    results = [ref for ref in results if ref["kind"] == value]
            return results

        async def link(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            _ = args, kwargs
            return {"id": "link-empty"}

        async def link_evidence(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return await self.link(*args, **kwargs)

        async def links(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            _ = args, kwargs
            return []

        async def checkpoint(self, run_id: str, state: dict[str, Any], **kwargs: Any) -> WorkspaceRecordRef:
            return await self.put(state, collection="checkpoints", kind="checkpoint", scope={"run_id": run_id})

        async def put_checkpoint(self, run_id: str, state: dict[str, Any], **kwargs: Any) -> WorkspaceRecordRef:
            return await self.checkpoint(run_id, state, **kwargs)

        async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
            _ = run_id
            return None

        async def put_artifact_ref(self, run_id: str, artifact: Any, **kwargs: Any) -> WorkspaceRecordRef:
            return await self.put(
                {"artifact": artifact, "metadata": kwargs.get("metadata") or {}},
                collection="artifacts",
                kind="artifact_ref",
                scope={"run_id": run_id},
            )

        async def claim_lease(self, run_id: str, owner_id: str, **kwargs: Any) -> dict[str, Any]:
            return {"run_id": run_id, "owner_id": owner_id, "lease_token": "empty", **kwargs}

        async def heartbeat_lease(self, run_id: str, owner_id: str, lease_token: str) -> dict[str, Any]:
            return {"run_id": run_id, "owner_id": owner_id, "lease_token": lease_token}

        async def release_lease(self, run_id: str, owner_id: str, lease_token: str) -> dict[str, Any]:
            return {"run_id": run_id, "owner_id": owner_id, "lease_token": lease_token}

        async def put_snapshot(self, run_id: str, state: dict[str, Any], **kwargs: Any) -> WorkspaceRecordRef:
            return await self.checkpoint(run_id, state, **kwargs)

        async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
            _ = run_id
            return None

        async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None:
            _ = run_id
            return None

        async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
            _ = run_id
            return None

        async def checkpoint_history(self, run_id: str, **kwargs: Any) -> list[WorkspaceRecordRef]:
            _ = run_id, kwargs
            return []

        async def append_runtime_event(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            _ = args, kwargs
            return {"id": "event-empty", "execution_id": "custom", "sequence": 1}

        async def query_runtime_events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            _ = args, kwargs
            return []

        async def record_file_policy(self, metadata: dict[str, Any]) -> dict[str, Any]:
            return metadata

        async def get_file_policy(self) -> dict[str, Any]:
            return {}

        async def add_retention_anchor(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            _ = args, kwargs
            return {"id": "anchor-empty"}

        async def retention_anchors(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            _ = args, kwargs
            return []

        async def prune_scope(self, scope: dict[str, Any], *, remove_files: bool = True) -> dict[str, Any]:
            _ = remove_files
            return {"scope": scope, "records_deleted": 0}

        async def register_scratch_lease(self, lease: dict[str, Any]) -> dict[str, Any]:
            return lease

        async def get_scratch_lease(self, lease_id: str) -> None:
            _ = lease_id
            return None

        async def list_scratch_leases(self, **kwargs: Any) -> list[dict[str, Any]]:
            _ = kwargs
            return []

        async def close_scratch_lease(self, lease_id: str, **kwargs: Any) -> None:
            _ = lease_id, kwargs
            return None

    manager = WorkspaceManager()
    provider = CustomDBStoreProvider()
    manager.register_db_store_provider("custom-db", lambda **options: provider)
    workspace = manager.create(
        tmp_path / "custom-db-provider",
        db_store_provider="custom-db",
        vector_store_provider="sqlite",
    )

    ref = await workspace.put(
        {"memory": "custom db record"},
        collection="memory",
        kind="db_provider_probe",
    )
    hits = await workspace.search(None, filters={"collection": "memory", "kind": "db_provider_probe"})

    assert ref["id"].startswith("rec_")
    assert [stored["id"] for stored in provider.records] == [ref["id"]]
    assert await workspace.get_data(ref) == {"memory": "custom db record"}
    assert [hit["id"] for hit in hits] == [ref["id"]]
    assert workspace.capabilities()["components"]["db_store_provider"] == "custom-db"


@pytest.mark.asyncio
async def test_workspace_embedding_and_vector_store_provider_factories_receive_options(tmp_path):
    manager = WorkspaceManager()
    seen_embedding_options: dict[str, Any] = {}
    seen_vector_options: dict[str, Any] = {}
    indexed: list[WorkspaceRecordRef] = []

    class CustomEmbeddingProvider:
        name = "custom-embedding"

        def __init__(self, *, marker: str):
            self.marker = marker

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            seen_embedding_options["used_marker"] = self.marker
            return [[1.0, 0.0] if "alpha" in text else [0.0, 1.0] for text in texts]

    class CustomVectorStoreProvider:
        name = "custom-vector"

        def __init__(self, *, bucket: str):
            self.bucket = bucket

        async def index_record(self, ref: WorkspaceRecordRef, embedding: list[float]) -> None:
            seen_vector_options["used_bucket"] = self.bucket
            indexed.append(ref)

        async def search_by_embedding(
            self,
            embedding: list[float],
            *,
            filters: dict[str, Any] | None = None,
            limit: int | None = None,
        ) -> list[WorkspaceRecordRef]:
            _ = embedding, filters
            return indexed[:limit]

    def build_embedding_provider(**options: Any) -> CustomEmbeddingProvider:
        seen_embedding_options.update(options)
        return CustomEmbeddingProvider(marker=str(options["marker"]))

    def build_vector_store_provider(**options: Any) -> CustomVectorStoreProvider:
        seen_vector_options.update(options)
        return CustomVectorStoreProvider(bucket=str(options["bucket"]))

    manager.register_embedding_provider("custom-embedding", build_embedding_provider)
    manager.register_vector_store_provider("custom-vector", build_vector_store_provider)

    workspace = manager.create(
        tmp_path / "custom-component-providers",
        embedding_provider="custom-embedding",
        embedding_options={"marker": "embed-options"},
        vector_store_provider="custom-vector",
        vector_store_options={"bucket": "vector-options"},
    )
    expected = await workspace.put(
        {"memory": "alpha provider probe"},
        collection="memory",
        kind="provider_factory_probe",
        summary="alpha provider probe",
    )
    package = await workspace.retrieve(
        "alpha",
        filters={"collection": "memory", "kind": "provider_factory_probe"},
        method="vector",
        rerank=False,
    )

    assert seen_embedding_options["marker"] == "embed-options"
    assert seen_embedding_options["used_marker"] == "embed-options"
    assert seen_vector_options["bucket"] == "vector-options"
    assert seen_vector_options["used_bucket"] == "vector-options"
    assert workspace.capabilities()["components"]["embedding_provider"] == "custom-embedding"
    assert workspace.capabilities()["components"]["vector_store_provider"] == "custom-vector"
    assert _retrieval_ref_id(package["items"][0]) == expected["id"]


@pytest.mark.asyncio
async def test_workspace_structured_data_links_and_capabilities(tmp_path):
    agent = Agently.create_agent("workspace-foundation-components").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    observation = {
        "attempt": 2,
        "result": {"status": "failed", "reason": "missing route candidate"},
        "evidence": ["pytest::test_route_fallback"],
    }
    observation_ref = await workspace.put(
        observation,
        collection="observations",
        kind="execution_observation",
        summary="route fallback failed",
        scope={"task_id": "issue-123"},
    )
    decision_ref = await workspace.put(
        {"decision": "patch route provider fallback"},
        collection="decisions",
        kind="loop_decision",
        summary="patch route provider fallback",
        scope={"task_id": "issue-123"},
    )

    link_ref = await workspace.link(decision_ref, observation_ref, relation="responds_to")

    assert await workspace.get_data(observation_ref) == observation
    assert [item["id"] for item in await workspace.links(decision_ref)] == [link_ref["id"]]
    assert [item["id"] for item in await workspace.links(source=decision_ref)] == [link_ref["id"]]
    assert [item["id"] for item in await workspace.links(target=observation_ref, relation="responds_to")] == [
        link_ref["id"]
    ]
    capabilities = workspace.capabilities()
    assert capabilities["backend"] == "local"
    assert capabilities["files_root"] == str(workspace.files_root)
    assert capabilities["components"]["db_store_provider"] == "sqlite"
    assert capabilities["components"]["content"] == "LocalContentStore"
    assert capabilities["components"]["embedding_provider"] is None
    assert capabilities["components"]["vector_store_provider"] in {"chroma", "sqlite"}
    assert capabilities["components"]["vector_index"] == "VectorIndexPipeline"
    assert capabilities["features"]["structured_get_data"] is True
    assert capabilities["features"]["links_query"] is True
    assert capabilities["features"]["checkpoint_lookup"] is True
    assert capabilities["features"]["workspace_reference_envelopes"] is True
    assert capabilities["features"]["supports_range_read"] is True
    assert capabilities["features"]["supports_stream_read"] is True
    assert capabilities["features"]["supports_cas"] is True
    assert capabilities["features"]["supports_lease"] is True
    assert capabilities["features"]["supports_artifact_refs"] is True
    assert capabilities["features"]["supports_event_sequence"] is True
    assert capabilities["features"]["vector_search"] is False
    assert capabilities["features"]["supports_remote_backend"] is False


@pytest.mark.asyncio
async def test_workspace_build_context_returns_refs_and_budget_diagnostics(tmp_path):
    agent = Agently.create_agent("workspace-recall").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    failure_ref = await workspace.put(
        content="route fallback failed because provider returned no route candidate",
        collection="observations",
        kind="test_output",
        summary="route fallback pytest failure",
        scope={"task_id": "issue-123"},
        source={"type": "command", "name": "pytest"},
    )
    await workspace.put(
        content="unrelated release note draft",
        collection="artifacts",
        kind="note",
        summary="release note draft",
        scope={"task_id": "issue-999"},
        source={"type": "human"},
    )

    context_pack = await workspace.build_context(
        goal="route fallback failure",
        scope={"task_id": "issue-123"},
        budget={"chars": 600},
        profile="auto",
    )

    assert context_pack["goal"] == "route fallback failure"
    assert context_pack["profile"] == "auto"
    assert [item["ref"]["id"] for item in context_pack["items"]] == [failure_ref["id"]]
    content = context_pack["items"][0]["content"]
    assert isinstance(content, str)
    assert "route fallback" in content
    assert context_pack["diagnostics"]["planner"] == "rule"


@pytest.mark.asyncio
async def test_workspace_search_sanitizes_fts_queries_for_natural_task_text(tmp_path):
    agent = Agently.create_agent("workspace-fts-safe-query").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    version_ref = await workspace.put(
        content="Agently 4.1.3.4 task loop fix",
        collection="observations",
        kind="note",
        summary="Agently 4.1.3.4 task loop fix",
        scope={"task_id": "fts-safe"},
    )
    dotted_ref = await workspace.put(
        content="foo.bar import failed during verification",
        collection="observations",
        kind="note",
        summary="foo.bar import failed",
        scope={"task_id": "fts-safe"},
    )
    question_ref = await workspace.put(
        content="interview question preparation",
        collection="observations",
        kind="note",
        summary="interview question preparation",
        scope={"task_id": "fts-safe"},
    )

    version_results = await workspace.search("4.1.3.4", filters={"scope.task_id": "fts-safe"})
    dotted_results = await workspace.search("foo.bar", filters={"scope.task_id": "fts-safe"})
    question_results = await workspace.search("question", filters={"scope.task_id": "fts-safe"})
    context_pack = await workspace.build_context(
        goal="fix 4.1.3.4 foo.bar question",
        scope={"task_id": "fts-safe"},
        budget={"chars": 1000},
        profile="auto",
    )

    assert version_ref["id"] in [item["id"] for item in version_results]
    assert dotted_ref["id"] in [item["id"] for item in dotted_results]
    assert question_ref["id"] in [item["id"] for item in question_results]
    assert context_pack["items"]
    assert "fallback_reason" not in context_pack["diagnostics"]


@pytest.mark.asyncio
async def test_workspace_search_preserves_deterministic_shape_for_small_candidate_pool(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-grep-aliases")
    await workspace.put(
        content="release deadline is Monday",
        collection="observations",
        kind="note",
        summary="deadline note",
        scope={"case_id": "grep-alias"},
    )
    await workspace.write_file("notes/todo.txt", "release deadline is 2026-07-01\n")

    search_refs = await workspace.search("deadline", filters={"scope.case_id": "grep-alias"})
    grep_refs = await workspace.grep("deadline", filters={"scope.case_id": "grep-alias"})
    assert [ref["id"] for ref in grep_refs] == [ref["id"] for ref in search_refs]

    search_hits = await workspace.search_files("deadline", path="notes", pattern="*.txt")
    grep_hits = await workspace.grep_files("deadline", path="notes", pattern="*.txt")
    assert [item["path"] for item in grep_hits] == [item["path"] for item in search_hits]
    assert grep_hits[0]["role"] == "evidence_snippet"


@pytest.mark.asyncio
async def test_workspace_search_auto_uses_retrieve_for_large_record_candidate_pool(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-search-auto-records")
    refs = []
    for index in range(12):
        refs.append(
            await workspace.put(
                {
                    "body": f"needle candidate {index}",
                    "padding": "x" * 1800,
                },
                collection="observations",
                kind="auto_search_probe",
                summary=f"needle candidate {index}",
                scope={"case_id": "search-auto"},
            )
        )

    grep_refs = await workspace.grep("needle", filters={"scope.case_id": "search-auto"})
    search_refs = await workspace.search("needle", filters={"scope.case_id": "search-auto"})

    assert len(grep_refs) == len(refs)
    assert 0 < len(search_refs) < len(grep_refs)
    assert {ref["id"] for ref in search_refs}.issubset({ref["id"] for ref in grep_refs})
    assert all("id" in ref and "collection" in ref and "path" in ref for ref in search_refs)


@pytest.mark.asyncio
async def test_workspace_search_files_auto_uses_retrieve_for_large_file_candidate_pool(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-search-auto-files")
    for index in range(12):
        await workspace.write_file(
            f"auto-files/file-{index}.txt",
            f"needle candidate {index}\n" + ("x" * 1800),
        )

    grep_hits = await workspace.grep_files("needle", path=".", pattern="**/*.txt", max_results=20, context_lines=1)
    search_hits = await workspace.search_files("needle", path=".", pattern="**/*.txt", max_results=20, context_lines=1)

    assert len(grep_hits) == 12
    assert 0 < len(search_hits) < len(grep_hits)
    assert {item["path"] for item in search_hits}.issubset({item["path"] for item in grep_hits})
    assert all(item["role"] == "evidence_snippet" for item in search_hits)


@pytest.mark.asyncio
async def test_workspace_retrieve_keyword_tag_candidates(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-tags")
    expected = await workspace.put(
        {"memory": "User prefers concise project updates."},
        collection="memory",
        kind="session_memory",
        summary="concise project update preference",
        scope={"memory_scope": "SESSION_MEMORY"},
        meta={"tags": ["preference", "project"]},
    )
    await workspace.put(
        {"memory": "Unrelated billing note."},
        collection="memory",
        kind="session_memory",
        summary="billing note",
        scope={"memory_scope": "SESSION_MEMORY"},
        meta={"tags": ["billing"]},
    )

    package = await workspace.retrieve(
        "project updates",
        tags=["preference"],
        filters={"collection": "memory", "kind": "session_memory"},
        scope={"memory_scope": "SESSION_MEMORY"},
        budget={"chars": 2000},
        rerank=False,
    )

    assert [_retrieval_ref_id(item) for item in package["items"]] == [expected["id"]]
    assert _retrieval_tags(package["items"][0]) == ["preference", "project"]
    assert package["diagnostics"]["deterministic_record_candidates"] == 1


@pytest.mark.asyncio
async def test_workspace_retrieve_projects_structured_records_for_model_hot_view(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-projected-record")
    ref = await workspace.put(
        {
            "source_system": "crm_activity_export",
            "subject": "CivicPay credit guidance",
            "attributes": {
                "labels": ["civicpay", "credit"],
                "raw_lines": [
                    "Merchant: CivicPay",
                    "Credit policy: service credits may not exceed 15 percent.",
                    "Approval rule: Risk lead approval is required above USD 2,500.",
                ],
            },
            "audit": {"schema": "crm.varying.v3", "export_batch": "nightly-private"},
        },
        collection="support-intel",
        kind="credit_policy",
        scope={"case_id": "projection"},
        meta={"tags": ["civicpay", "credit"]},
    )

    package = await workspace.retrieve(
        "CivicPay credit",
        filters={"collection": "support-intel"},
        scope={"case_id": "projection"},
        budget={"chars": 2000, "record_representation": "projected"},
        rerank=False,
    )

    item = cast(Any, package["items"][0])
    content = str(item["content"])
    assert _retrieval_ref_id(item) == ref["id"]
    assert item["content_state"] == "projected_from_raw_record"
    assert item["body_state"] == "bounded"
    assert item["original_ref"]["record_id"] == ref["id"]
    assert item["original_ref"]["content_state"] == "raw_readback_available"
    assert item["projection"]["strategy"] == "deterministic_structured_projection"
    assert item["projection"]["chosen_representation"] == "projected"
    assert item["projection"]["raw_chars"] > item["projection"]["projected_chars"]
    assert "Credit policy: service credits may not exceed 15 percent." in content
    assert "Risk lead approval is required above USD 2,500." in content
    assert "source_system" not in content
    assert "nightly-private" not in content
    assert "audit" in item["projection"]["omitted_keys"]


@pytest.mark.asyncio
async def test_workspace_retrieve_auto_preserves_short_structured_records(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-auto-short-record")
    ref = await workspace.put(
        {
            "account": "Vega Clinical",
            "region": "EU",
            "launch_gate": {
                "blocked_item": "VP-17",
                "deadline": "2026-08-05",
                "required_signoffs": [
                    {"role": "QA lead", "owner": "Noam Fischer"},
                    {"role": "Regulatory owner", "owner": "Lena Ortiz"},
                ],
            },
            "source_system": "clinical_ops_export",
            "audit": {"export_batch": "nightly-private"},
            "noise": {"superseded_gate": "APAC trial shipment is not current launch evidence"},
        },
        collection="launch-intel",
        kind="gate_review",
        summary="Vega Clinical EU launch gate",
        scope={"case_id": "auto-short-structure"},
    )

    package = await workspace.retrieve(
        "Vega VP-17 signoffs",
        filters={"collection": "launch-intel"},
        scope={"case_id": "auto-short-structure"},
        budget={"chars": 2000},
        rerank=False,
    )

    item = cast(Any, package["items"][0])
    content = str(item["content"])
    assert _retrieval_ref_id(item) == ref["id"]
    assert item["content_state"] == "compact_structured_record"
    assert item["projection"]["strategy"] == "compact_structured_record"
    assert item["projection"]["chosen_representation"] == "compact_structured"
    assert item["projection"]["reason"] == "short_structured_record_preserved"
    assert package["diagnostics"]["representation"]["record_counts"] == {"compact_structured": 1}
    assert "blocked_item" in content
    assert "required_signoffs" in content
    assert "Noam Fischer" in content
    assert "Lena Ortiz" in content
    assert "nightly-private" not in content
    assert "APAC trial shipment" not in content
    assert "audit" in item["projection"]["omitted_keys"]
    assert "noise" in item["projection"]["omitted_keys"]


@pytest.mark.asyncio
async def test_workspace_retrieve_can_disable_record_projection(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-raw-record")
    ref = await workspace.put(
        {
            "source_system": "crm_activity_export",
            "subject": "CivicPay credit guidance",
            "attributes": {"raw_lines": ["Merchant: CivicPay"]},
        },
        collection="support-intel",
        kind="credit_policy",
        scope={"case_id": "raw-projection"},
    )

    package = await workspace.retrieve(
        "CivicPay",
        filters={"collection": "support-intel"},
        scope={"case_id": "raw-projection"},
        budget={"chars": 2000, "record_projection": False},
        rerank=False,
    )

    item = cast(Any, package["items"][0])
    assert _retrieval_ref_id(item) == ref["id"]
    assert item["content_state"] == "bounded_readback_available"
    assert item["projection"]["strategy"] == "raw_excerpt"
    assert "source_system" in str(item["content"])


@pytest.mark.asyncio
async def test_workspace_retrieve_default_rerank_gate_skips_focused_pool(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-rerank-gate-skip")
    for index in range(2):
        await workspace.put(
            {"memory": f"focused candidate {index}"},
            collection="memory",
            kind="session_memory",
            summary=f"focused candidate {index}",
            scope={"memory_scope": "SESSION_MEMORY"},
            meta={"tags": ["focused"]},
        )
    calls = 0

    async def rerank_handler(*, query, candidates):
        nonlocal calls
        _ = (query, candidates)
        calls += 1
        return {"decisions": []}

    package = await workspace.retrieve(
        "focused candidate",
        tags=["focused"],
        filters={"collection": "memory", "kind": "session_memory"},
        scope={"memory_scope": "SESSION_MEMORY"},
        selection="top_n",
        top_n=5,
        rerank_handler=rerank_handler,
    )

    assert calls == 0
    assert len(package["items"]) == 2
    assert package["diagnostics"]["rerank"]["enabled"] is False
    assert package["diagnostics"]["rerank"]["reason"] == "candidate_count_within_selection"
    assert package["diagnostics"]["rerank_gate"]["enabled"] is False


@pytest.mark.asyncio
async def test_workspace_retrieve_default_rerank_gate_uses_structure_signals(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-rerank-gate-use")
    refs = []
    for index in range(6):
        refs.append(
            await workspace.put(
                {"memory": f"candidate {index}"},
                collection="memory",
                kind=f"kind_{index}",
                summary=f"candidate {index}",
            )
        )
    calls = 0

    async def rerank_handler(*, query, candidates):
        nonlocal calls
        _ = query
        calls += 1
        return {
            "decisions": [
                {"id": candidates[0]["id"], "useful": False, "score": 0.0, "reason": "first distractor"},
                {"id": candidates[1]["id"], "useful": True, "score": 0.9, "reason": "best"},
            ]
        }

    package = await workspace.retrieve(
        "candidate",
        selection="top_n",
        top_n=2,
        rerank_handler=rerank_handler,
    )

    assert calls == 1
    assert _retrieval_ref_id(package["items"][0]) == refs[1]["id"]
    assert package["diagnostics"]["rerank_gate"]["enabled"] is True
    assert "many_record_kinds" in package["diagnostics"]["rerank_gate"]["reasons"]
    assert package["omitted"][0] == {"reason": "rerank_drop", "count": 1}


@pytest.mark.asyncio
async def test_workspace_retrieve_model_rerank_drops_and_refills(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-rerank")
    refs = []
    for index in range(3):
        refs.append(
            await workspace.put(
                {"memory": f"candidate {index}"},
                collection="memory",
                kind="global_memory",
                summary=f"candidate {index}",
                scope={"memory_scope": "GLOBAL_MEMORY"},
                meta={"tags": ["topic"]},
            )
        )

    async def rerank_handler(*, query, candidates):
        _ = query
        seen_candidates[:] = list(candidates)
        return {
            "decisions": [
                {"id": candidates[0]["id"], "useful": False, "score": 0.0, "reason": "irrelevant"},
                {"id": candidates[1]["id"], "useful": True, "score": 0.9, "reason": "best"},
            ]
        }

    seen_candidates: list[dict[str, Any]] = []
    package = await workspace.retrieve(
        None,
        tags=["topic"],
        filters={"collection": "memory", "kind": "global_memory"},
        scope={"memory_scope": "GLOBAL_MEMORY"},
        budget={"chars": 2000, "rerank_candidates": 2},
        selection="top_n",
        top_n=2,
        rerank=True,
        rerank_handler=rerank_handler,
    )

    selected_ids = [_retrieval_ref_id(item) for item in package["items"]]
    reranked_ids = [str(candidate["id"]).split("record:", 1)[-1] for candidate in seen_candidates]
    dropped_id, kept_id = reranked_ids[:2]
    refill_ids = {ref["id"] for ref in refs} - set(reranked_ids)
    assert len(selected_ids) == 2
    assert dropped_id not in selected_ids
    assert kept_id in selected_ids
    assert any(refill_id in selected_ids for refill_id in refill_ids)
    assert package["omitted"][0] == {"reason": "rerank_drop", "count": 1}
    assert package["diagnostics"]["rerank"]["degraded"] is False


@pytest.mark.asyncio
async def test_workspace_retrieve_applies_unprefixed_rerank_record_ids(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-rerank-unprefixed-id")
    refs = []
    for index in range(3):
        refs.append(
            await workspace.put(
                {"memory": f"candidate {index}"},
                collection="memory",
                kind="global_memory",
                summary=f"candidate {index}",
                scope={"memory_scope": "GLOBAL_MEMORY"},
                meta={"tags": ["topic"]},
            )
        )

    async def rerank_handler(*, query, candidates):
        _ = (query, candidates)
        return {
            "decisions": [
                {"id": refs[0]["id"], "useful": False, "score": 0.0, "reason": "irrelevant"},
                {"id": refs[1]["id"], "useful": True, "score": 0.9, "reason": "best"},
            ]
        }

    package = await workspace.retrieve(
        None,
        tags=["topic"],
        filters={"collection": "memory", "kind": "global_memory"},
        scope={"memory_scope": "GLOBAL_MEMORY"},
        budget={"chars": 2000, "rerank_candidates": 2},
        selection="top_n",
        top_n=2,
        rerank=True,
        rerank_handler=rerank_handler,
    )

    selected_ids = [_retrieval_ref_id(item) for item in package["items"]]
    assert selected_ids == [refs[1]["id"], refs[2]["id"]]
    assert package["omitted"][0] == {"reason": "rerank_drop", "count": 1}


@pytest.mark.asyncio
async def test_workspace_retrieve_default_rerank_window_covers_broad_pool(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-rerank-window")
    for index in range(18):
        await workspace.put(
            {"memory": f"candidate {index}"},
            collection="memory",
            kind=f"kind_{index}",
            summary=f"candidate {index}",
            scope={"memory_scope": "GLOBAL_MEMORY"},
            meta={"tags": ["topic"]},
        )
    seen_count = 0

    async def rerank_handler(*, query, candidates):
        nonlocal seen_count
        _ = query
        seen_count = len(candidates)
        return {
            "decisions": [
                {"id": candidate["id"], "useful": True, "score": 1.0, "reason": "candidate"}
                for candidate in candidates
            ]
        }

    await workspace.retrieve(
        "candidate",
        filters={"collection": "memory"},
        selection="top_n",
        top_n=6,
        rerank_handler=rerank_handler,
    )

    assert seen_count == 18


@pytest.mark.asyncio
async def test_workspace_retrieve_rerank_retry_failure_degrades(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-rerank-degrade")
    ref = await workspace.put(
        {"memory": "deterministic fallback"},
        collection="memory",
        kind="global_memory",
        summary="deterministic fallback",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )
    calls = 0

    async def failing_rerank(*, query, candidates):
        nonlocal calls
        _ = (query, candidates)
        calls += 1
        raise RuntimeError("model unavailable")

    package = await workspace.retrieve(
        None,
        filters={"collection": "memory", "kind": "global_memory"},
        scope={"memory_scope": "GLOBAL_MEMORY"},
        rerank=True,
        rerank_handler=failing_rerank,
        max_rerank_retries=1,
    )

    assert calls == 2
    assert [_retrieval_ref_id(item) for item in package["items"]] == [ref["id"]]
    assert package["diagnostics"]["rerank"]["degraded"] is True
    assert package["diagnostics"]["rerank"]["reason"] == "rerank_failed"


@pytest.mark.asyncio
async def test_workspace_retrieve_vector_mode_uses_index_or_degrades(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-vector")
    first = await workspace.put(
        {"memory": "keyword candidate"},
        collection="memory",
        kind="global_memory",
        summary="keyword candidate",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )
    vector_only = await workspace.put(
        {"memory": "vector candidate"},
        collection="memory",
        kind="global_memory",
        summary="vector candidate",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )

    degraded = await workspace.retrieve(
        "candidate",
        filters={"collection": "memory", "kind": "global_memory"},
        scope={"memory_scope": "GLOBAL_MEMORY"},
        method="vector",
        rerank=False,
    )
    assert degraded["diagnostics"]["vector"]["used"] is False
    assert degraded["diagnostics"]["vector"]["reason"] == "embedding_provider_unavailable"
    assert first["id"] in [_retrieval_ref_id(item) for item in degraded["items"]]

    class FakeVectorIndex:
        name = "fake"

        async def index_record(self, ref, content):
            _ = (ref, content)

        async def search(self, query, *, filters=None, limit=None):
            _ = (query, filters, limit)
            return [vector_only]

    cast(Any, workspace.backend).vector_index = FakeVectorIndex()
    used = await workspace.retrieve(
        "semantic query",
        filters={"collection": "memory", "kind": "global_memory"},
        scope={"memory_scope": "GLOBAL_MEMORY"},
        method="vector",
        rerank=False,
    )

    assert used["diagnostics"]["vector"]["used"] is True
    assert _retrieval_ref_id(used["items"][0]) == vector_only["id"]


@pytest.mark.asyncio
async def test_workspace_sqlite_vector_store_uses_pluggable_embedding_provider(tmp_path):
    def embed_texts(texts: str | list[str]) -> list[list[float]]:
        values = [texts] if isinstance(texts, str) else texts
        vectors: list[list[float]] = []
        for text in values:
            normalized = text.lower()
            if "alpha" in normalized:
                vectors.append([1.0, 0.0])
            elif "beta" in normalized:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([1.0, 0.0])
        return vectors

    root = tmp_path / "workspace-sqlite-vector-store"
    workspace = Agently.create_workspace(
        root,
        embedding_provider=embed_texts,
        vector_store_provider="sqlite",
    )
    alpha = await workspace.put(
        {"memory": "alpha vector candidate"},
        collection="memory",
        kind="global_memory",
        summary="alpha vector candidate",
    )
    await workspace.put(
        {"memory": "beta vector candidate"},
        collection="memory",
        kind="global_memory",
        summary="beta vector candidate",
    )

    package = await workspace.retrieve(
        "alpha query",
        filters={"collection": "memory", "kind": "global_memory"},
        method="vector",
        rerank=False,
    )

    assert package["diagnostics"]["vector"]["used"] is True
    assert package["diagnostics"]["vector"]["vector_store_provider"] == "sqlite"
    assert _retrieval_ref_id(package["items"][0]) == alpha["id"]

    reopened = Agently.create_workspace(
        root,
        create=False,
        embedding_provider=embed_texts,
        vector_store_provider="sqlite",
    )
    reopened_package = await reopened.retrieve(
        "alpha query",
        filters={"collection": "memory", "kind": "global_memory"},
        method="vector",
        rerank=False,
    )
    assert _retrieval_ref_id(reopened_package["items"][0]) == alpha["id"]


@pytest.mark.asyncio
async def test_workspace_local_vector_index_defaults_to_cosine_similarity(tmp_path):
    def embed_texts(texts: str | list[str]) -> list[list[float]]:
        values = [texts] if isinstance(texts, str) else texts
        vectors: list[list[float]] = []
        for text in values:
            normalized = text.lower()
            if "alpha" in normalized:
                vectors.append([1.0, 0.0])
            elif "beta" in normalized:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.7, 0.3])
        return vectors

    workspace = Agently.create_workspace(tmp_path / "workspace-local-vector-index")
    cast(Any, workspace.backend).vector_index = LocalVectorIndex(embed_texts)
    alpha = await workspace.put(
        {"memory": "alpha vector candidate"},
        collection="memory",
        kind="global_memory",
        summary="alpha vector candidate",
    )
    await workspace.put(
        {"memory": "beta vector candidate"},
        collection="memory",
        kind="global_memory",
        summary="beta vector candidate",
    )

    package = await workspace.retrieve(
        "alpha query",
        filters={"collection": "memory", "kind": "global_memory"},
        method="vector",
        rerank=False,
    )

    assert cast(Any, workspace.backend).vector_index.similarity == "cosine"
    assert package["diagnostics"]["vector"]["similarity"] == "cosine"
    assert _retrieval_ref_id(package["items"][0]) == alpha["id"]


@pytest.mark.asyncio
async def test_workspace_local_vector_index_similarity_modes_change_ranking(tmp_path):
    def embed_texts(texts: str | list[str]) -> list[list[float]]:
        values = [texts] if isinstance(texts, str) else texts
        vectors: list[list[float]] = []
        for text in values:
            normalized = text.lower()
            if "exact-direction" in normalized:
                vectors.append([1.0, 0.0])
            elif "large-magnitude" in normalized:
                vectors.append([10.0, 1.0])
            elif "nearby-offset" in normalized:
                vectors.append([0.9, 0.1])
            else:
                vectors.append([1.0, 0.0])
        return vectors

    async def run_similarity(similarity: str) -> tuple[list[str], str]:
        workspace = Agently.create_workspace(tmp_path / f"workspace-vector-{similarity}")
        cast(Any, workspace.backend).vector_index = LocalVectorIndex(
            embed_texts,
            similarity=cast(Any, similarity),
        )
        exact = await workspace.put(
            {"memory": "exact-direction candidate"},
            collection="memory",
            kind="global_memory",
            summary="exact-direction candidate",
        )
        large = await workspace.put(
            {"memory": "large-magnitude candidate"},
            collection="memory",
            kind="global_memory",
            summary="large-magnitude candidate",
        )
        nearby = await workspace.put(
            {"memory": "nearby-offset candidate"},
            collection="memory",
            kind="global_memory",
            summary="nearby-offset candidate",
        )
        package = await workspace.retrieve(
            "query vector",
            filters={"collection": "memory", "kind": "global_memory"},
            method="vector",
            rerank=False,
            selection="top_n",
            top_n=3,
        )
        names_by_id = {
            exact["id"]: "exact",
            large["id"]: "large",
            nearby["id"]: "nearby",
        }
        return [names_by_id[_retrieval_ref_id(item)] for item in package["items"]], cast(
            str,
            package["diagnostics"]["vector"]["similarity"],
        )

    cosine_order, cosine_similarity = await run_similarity("cosine")
    dot_order, dot_similarity = await run_similarity("dot")
    l2_order, l2_similarity = await run_similarity("l2")

    assert cosine_similarity == "cosine"
    assert dot_similarity == "dot"
    assert l2_similarity == "l2"
    assert cosine_order == ["exact", "large", "nearby"]
    assert dot_order == ["large", "exact", "nearby"]
    assert l2_order == ["exact", "nearby", "large"]


@pytest.mark.asyncio
async def test_workspace_retrieve_auto_method_keeps_keyword_without_embedding_policy(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-auto-keyword")
    keyword_ref = await workspace.put(
        {"memory": "alpha keyword candidate"},
        collection="memory",
        kind="global_memory",
        summary="alpha keyword candidate",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )
    vector_only = await workspace.put(
        {"memory": "semantic vector-only candidate"},
        collection="memory",
        kind="global_memory",
        summary="semantic vector-only candidate",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )

    class FakeVectorIndex:
        name = "fake"
        used = False

        async def index_record(self, ref, content):
            _ = (ref, content)

        async def search(self, query, *, filters=None, limit=None):
            self.used = True
            _ = (query, filters, limit)
            return [vector_only]

    fake_index = FakeVectorIndex()
    cast(Any, workspace.backend).vector_index = fake_index

    package = await workspace.retrieve(
        "alpha",
        filters={"collection": "memory", "kind": "global_memory"},
        scope={"memory_scope": "GLOBAL_MEMORY"},
        method="auto",
        rerank=False,
        settings={},
    )

    assert package["diagnostics"]["method"] == "auto"
    assert package["diagnostics"]["effective_method"] == "keyword"
    assert package["diagnostics"]["method_resolution"]["reason"] == "embedding_policy_not_configured"
    assert "vector" not in package["diagnostics"]
    assert fake_index.used is False
    assert _retrieval_ref_id(package["items"][0]) == keyword_ref["id"]


@pytest.mark.asyncio
async def test_workspace_retrieve_auto_method_uses_hybrid_when_embedding_policy_configured(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-retrieve-auto-hybrid")
    await workspace.put(
        {"memory": "keyword fallback candidate"},
        collection="memory",
        kind="global_memory",
        summary="keyword fallback candidate",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )
    vector_only = await workspace.put(
        {"memory": "semantic vector-only candidate"},
        collection="memory",
        kind="global_memory",
        summary="semantic vector-only candidate",
        scope={"memory_scope": "GLOBAL_MEMORY"},
    )

    class FakeVectorIndex:
        name = "fake"

        async def index_record(self, ref, content):
            _ = (ref, content)

        async def search(self, query, *, filters=None, limit=None):
            _ = (query, filters, limit)
            return [vector_only]

    cast(Any, workspace.backend).vector_index = FakeVectorIndex()

    package = await workspace.retrieve(
        "semantic query",
        filters={"collection": "memory", "kind": "global_memory"},
        scope={"memory_scope": "GLOBAL_MEMORY"},
        method="auto",
        rerank=False,
        settings={"workspace": {"retrieval": {"embedding_model": "test-embedding"}}},
    )

    assert package["diagnostics"]["method"] == "auto"
    assert package["diagnostics"]["effective_method"] == "hybrid"
    assert package["diagnostics"]["method_resolution"]["reason"] == "embedding_model"
    assert package["diagnostics"]["vector"]["used"] is True
    assert _retrieval_ref_id(package["items"][0]) == vector_only["id"]


@pytest.mark.asyncio
async def test_workspace_search_files_returns_bounded_retrieval_roles(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-search-files")
    await workspace.write_file("notes/todo.txt", "alpha\nrelease deadline is 2026-07-01\n")
    await workspace.write_file("notes/skip.txt", f"{'x' * 100}\ndeadline appears in a skipped large file\n")

    results = await workspace.search_files(
        "deadline",
        path="notes",
        pattern="*.txt",
        max_results=5,
        max_file_bytes=64,
    )

    assert [item["path"] for item in results] == ["notes/todo.txt"]
    assert results[0]["line"] == 2
    assert results[0]["text"] == "release deadline is 2026-07-01"
    assert results[0]["role"] == "evidence_snippet"
    assert results[0]["content_state"] == "bounded_readback_available"
    assert results[0]["snippet_bytes"] == len(results[0]["text"].encode("utf-8"))
    assert results[0]["locator_ref"]["role"] == "locator_ref"
    assert results[0]["locator_ref"]["content_state"] == "ref_only"
    assert results[0]["locator_ref"]["path"] == "notes/todo.txt"
    assert results[0]["search_engine"] in {"workspace_file_grep", "workspace_file_scan"}
    assert results[0]["scope"]["search_engine"] == results[0]["search_engine"]
    assert results[0]["truncated"] is False
    assert not {"useful", "accepted", "semantically_relevant"}.intersection(results[0])


@pytest.mark.asyncio
async def test_workspace_search_files_marks_truncated_snippets(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-search-files-truncated")
    await workspace.write_file("notes/todo.txt", "alpha deadline " + ("details " * 20) + "\n")

    results = await workspace.search_files(
        "deadline",
        path="notes",
        pattern="*.txt",
        max_results=5,
        max_snippet_bytes=24,
    )

    assert results[0]["path"] == "notes/todo.txt"
    assert results[0]["snippet_bytes"] <= 24
    assert results[0]["truncated"] is True
    assert results[0]["locator_ref"]["content_state"] == "ref_only"


@pytest.mark.asyncio
async def test_workspace_search_files_treats_double_star_as_recursive_files(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-search-files-recursive")
    await workspace.write_file("retained/nested/ops-note.md", "Project Atlas evidence code ATLAS-RENEWAL-77\n")

    results = await workspace.search_files(
        "Project Atlas",
        path="retained",
        pattern="**",
        max_results=5,
    )

    assert [item["path"] for item in results] == ["retained/nested/ops-note.md"]
    assert results[0]["scope"]["pattern"] == "**"
    assert results[0]["scope"]["effective_pattern"] == "**/*"
    assert results[0]["locator_ref"]["path"] == "retained/nested/ops-note.md"


@pytest.mark.asyncio
async def test_workspace_coding_file_operations_edit_glob_grep_and_patch(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-coding-files")
    await workspace.write_file("src/app.py", "print('old')\nprint('old')\n")
    await workspace.write_file("src/readme.md", "Project Atlas\n")

    globbed = await workspace.glob_files("*.py", path="src")
    assert globbed["matches"] == ["src/app.py"]

    grep = await workspace.grep_files(r"Project\s+Atlas", path="src", glob="*.md", regex=True)
    assert grep["matches"][0]["path"] == "src/readme.md"
    assert grep["matches"][0]["line"] == 1

    with pytest.raises(ValueError, match="multiple"):
        await workspace.edit_file("src/app.py", "print('old')", "print('new')")

    edited = await workspace.edit_file("src/app.py", "print('old')", "print('new')", replace_all=True)
    assert edited.get("replacements") == 2
    readback = await workspace.read_file("src/app.py")
    assert readback["content"] == "print('new')\nprint('new')\n"

    patch = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
-print('new')
+print('patched')
 print('new')
"""
    applied = await workspace.apply_patch(patch, expected_files=["src/app.py"])
    assert applied["paths"] == ["src/app.py"]
    readback = await workspace.read_file("src/app.py")
    assert "print('patched')" in readback["content"]

    with pytest.raises(ValueError, match="expected_files"):
        await workspace.apply_patch(patch, expected_files=["src/other.py"])


@pytest.mark.asyncio
async def test_workspace_search_files_action_returns_retrieval_roles(tmp_path):
    agent = Agently.create_agent("workspace-search-files-roles").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    await workspace.write_file("notes/todo.txt", "alpha\nrelease deadline is 2026-07-01\n")
    agent.enable_workspace_file_actions(read=True, write=False, search=True)

    result = await agent.action.async_execute_action(
        "search_files",
        {
            "query": "deadline",
            "path": "notes",
            "pattern": "*.txt",
            "max_results": 5,
        },
    )

    assert result.get("status") == "success"
    data = result.get("data")
    assert isinstance(data, list)
    assert data[0]["path"] == "notes/todo.txt"
    assert data[0]["line"] == 2
    assert data[0]["text"] == "release deadline is 2026-07-01"
    assert data[0]["role"] == "evidence_snippet"
    assert data[0]["content_state"] == "bounded_readback_available"
    assert data[0]["query"] == "deadline"
    assert data[0]["scope"]["path"] == "notes"
    assert data[0]["snippet_chars"] == len(data[0]["text"])
    assert data[0]["locator_ref"]["role"] == "locator_ref"
    assert data[0]["locator_ref"]["content_state"] == "ref_only"
    assert data[0]["locator_ref"]["path"] == "notes/todo.txt"
    assert not {"useful", "accepted", "semantically_relevant"}.intersection(data[0])


@pytest.mark.asyncio
async def test_workspace_backend_component_protocols_are_wired(tmp_path):
    agent = Agently.create_agent("workspace-components").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    ref: WorkspaceRecordRef = {
        "id": "rec_component_protocol",
        "collection": "observations",
        "kind": "note",
        "path": None,
        "sha256": None,
        "size": 0,
        "summary": "component protocol record",
        "scope": {"task_id": "issue-789"},
        "source": {"type": "test"},
        "created_at": "2026-05-29T00:00:00Z",
        "meta": {},
    }

    await workspace.backend.metadata.put_record(ref)
    await workspace.backend.text_index.index_record(ref, "component protocol indexed content")

    records = await workspace.search("indexed", filters={"collection": "observations"})

    assert [record["id"] for record in records] == [ref["id"]]
    assert workspace.backend.vector_index is not None
    assert await workspace.backend.vector_index.search("indexed") == []


@pytest.mark.asyncio
async def test_workspace_reference_envelopes_bounded_reads_and_checkpoint_history_limit(tmp_path):
    agent = Agently.create_agent("workspace-provider-refs").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    ref = await workspace.put(
        "abcdefghijklmnopqrstuvwxyz",
        collection="artifacts",
        kind="text_artifact",
        summary="alphabet artifact",
        meta={"policy_labels": ["audit"]},
    )

    envelope = await workspace.ref_envelope(ref)
    assert envelope["workspace_id"].startswith("ws_")
    assert envelope["record_id"] == ref["id"]
    assert envelope["content_ref"] == ref["path"]
    assert envelope["digest"] == ref["sha256"]
    assert envelope["policy_labels"] == ["audit"]
    assert envelope["backend_capabilities"]["supports_range_read"] is True

    segment = await workspace.read_bounded(ref, offset=2, limit=4)
    assert segment["content"] == "cdef"
    assert segment["offset"] == 2
    assert segment["size"] == 4
    assert segment["total_size"] == 26
    assert segment["eof"] is False
    assert segment["ref"]["record_id"] == ref["id"]

    chunks = [chunk async for chunk in workspace.stream_read(ref, limit=7, chunk_size=3)]
    assert [chunk["content"] for chunk in chunks] == ["abc", "def", "g"]
    assert chunks[-1]["eof"] is False

    first_checkpoint = await workspace.checkpoint("run-1", {"step": 1}, step_id="a")
    second_checkpoint = await workspace.checkpoint("run-1", {"step": 2}, step_id="b")
    history = await workspace.checkpoint_history("run-1", limit=1)
    assert [item["id"] for item in history] == [second_checkpoint["id"]]
    assert first_checkpoint["id"] != second_checkpoint["id"]


@pytest.mark.asyncio
async def test_workspace_runtime_event_store_is_idempotent_and_bounded(tmp_path):
    agent = Agently.create_agent("workspace-runtime-events").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    snapshot_ref = await workspace.checkpoint("exec-1", {"phase": "waiting"}, step_id="approval")
    artifact_ref = await workspace.put(
        {"decision": "needs approval"},
        collection="artifacts",
        kind="approval_payload",
        summary="approval payload",
    )
    run = RunContext.create(run_kind="workflow_execution", execution_id="exec-1")
    event = RuntimeEvent(
        event_type="triggerflow.interrupt_raised",
        source="TriggerFlow",
        run=run,
        message="approval requested",
        payload={"interrupt_id": "approval-1"},
        meta={
            "parent_event_id": "evt-parent",
            "causation_id": "evt-cause",
            "node_id": "approval-node",
            "aggregation_scope": "approval-scope",
        },
    )

    first = await workspace.append_runtime_event(
        "exec-1",
        event,
        expected_sequence=1,
        idempotency_key="approval-request-1",
        snapshot_ref=snapshot_ref,
        artifact_refs=[artifact_ref],
        exchange_id="exchange-1",
        state_version=7,
        parent_signal_id="signal-parent",
        operator_id="approval-operator",
        interrupt_id="approval-1",
        resume_request_id="resume-1",
        actor_id="reviewer",
        lease_owner_id="worker-1",
    )
    duplicate = await workspace.append_runtime_event(
        "exec-1",
        event,
        expected_sequence=1,
        idempotency_key="approval-request-1",
        snapshot_ref=snapshot_ref,
        artifact_refs=[artifact_ref],
        exchange_id="exchange-1",
    )
    second = await workspace.append_runtime_event(
        "exec-1",
        {"event_type": "triggerflow.execution_resumed", "event_id": "evt-resume", "meta": {"node_id": "resume-node"}},
        expected_sequence=2,
    )
    with pytest.raises(RuntimeError, match="sequence conflict"):
        await workspace.append_runtime_event(
            "exec-1",
            {"event_type": "triggerflow.execution_closed", "event_id": "evt-closed"},
            expected_sequence=2,
        )
    with pytest.raises(RuntimeError, match="sequence conflict"):
        await workspace.append_runtime_event(
            "exec-1",
            {"event_type": "triggerflow.future_event", "event_id": "evt-future"},
            expected_sequence=4,
        )

    assert duplicate["id"] == first["id"]
    assert first["sequence"] == 1
    assert first["state_version"] == 7
    assert first["parent_id"] == "evt-parent"
    assert first["causation_id"] == "evt-cause"
    assert first["parent_signal_id"] == "signal-parent"
    assert first["node_id"] == "approval-node"
    assert first["operator_id"] == "approval-operator"
    assert first["interrupt_id"] == "approval-1"
    assert first["resume_request_id"] == "resume-1"
    assert first["actor_id"] == "reviewer"
    assert first["lease_owner_id"] == "worker-1"
    assert first["aggregation_scope"] == "approval-scope"
    assert first["persisted_at"]
    first_snapshot_ref = first["snapshot_ref"]
    assert first_snapshot_ref is not None
    assert first_snapshot_ref["record_id"] == snapshot_ref["id"]
    assert first["artifact_refs"][0]["record_id"] == artifact_ref["id"]
    assert second["sequence"] == 2

    queried = await workspace.query_runtime_events("exec-1", sequence_from=2, limit=1)
    assert [item["event_id"] for item in queried] == ["evt-resume"]
    by_event_id = await workspace.query_runtime_events("exec-1", event_id=first["event_id"])
    assert [item["id"] for item in by_event_id] == [first["id"]]


@pytest.mark.asyncio
async def test_workspace_prune_scope_removes_only_matching_lineage_subtree(tmp_path):
    workspace = Agently.create_workspace(
        tmp_path / "shared",
        default_scope={"session_id": "issue-123"},
        default_search_scope={"session_id": "issue-123"},
    )
    # Lineage-aware execution partitions, contained under files/lineage so a
    # scoped prune removes only the matching subtree (spec sections 8.2 / 9).
    first = workspace.with_scope_node("executions", "exec-1")
    second = workspace.with_scope_node("executions", "exec-2")
    (first.files_root / "notes").mkdir(parents=True)
    (first.files_root / "notes" / "result.txt").write_text("temporary execution file", encoding="utf-8")
    (second.files_root / "keep").mkdir(parents=True)
    (second.files_root / "keep" / "result.txt").write_text("durable execution file", encoding="utf-8")

    first_ref = await first.put(content="first execution", collection="observations", kind="partition")
    second_ref = await second.put(content="second execution", collection="observations", kind="partition")
    await first.put_checkpoint("exec-1", {"status": "first"}, step_id="phase-1")
    second_checkpoint = await second.put_checkpoint("exec-2", {"status": "second"}, step_id="phase-1")
    await first.append_runtime_event("exec-1", {"event_type": "first"})
    await second.append_runtime_event("exec-2", {"event_type": "second"})

    result = await first.prune_scope({"execution_id": "exec-1"})

    assert result["records_deleted"] == 2
    assert result["runtime_events_deleted"] == 1
    assert result["removed_files"] is True
    # Only the matching execution subtree is removed; the sibling is preserved.
    assert not first.files_root.exists()
    assert second.files_root.exists()
    assert (second.files_root / "keep" / "result.txt").read_text(encoding="utf-8") == "durable execution file"
    backend = cast(Any, workspace.backend)
    assert await backend.get_record(second_ref["id"]) == second_ref
    assert await backend.get_record(first_ref["id"]) is None
    assert await workspace.latest_checkpoint("exec-1") is None
    assert await workspace.latest_checkpoint("exec-2") == second_checkpoint
    assert await workspace.query_runtime_events("exec-1") == []
    assert [event["event_type"] for event in await workspace.query_runtime_events("exec-2")] == ["second"]


@pytest.mark.asyncio
async def test_workspace_scratch_lease_is_durable_and_recoverable(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "scratch-ws")
    execution = workspace.with_scope_node("executions", "exec-1")

    lease = await execution.open_scratch(purpose="unzip", ttl_seconds=-1)
    lease_id = lease.get("lease_id")
    local_path_value = lease.get("local_path")
    assert isinstance(lease_id, str)
    assert isinstance(local_path_value, str)
    local_path = Path(local_path_value)
    assert local_path.exists()
    assert lease.get("closed_at") is None
    assert lease.get("cleanup_policy") == "on_close"
    # The lease is a durable Workspace fact, not just an in-memory handle.
    backend = cast(Any, workspace.backend)
    stored = await backend.get_scratch_lease(lease_id)
    assert stored is not None
    assert stored.get("local_path") == local_path_value

    (local_path / "tmp.txt").write_text("scratch", encoding="utf-8")

    # Crash recovery: TTL/startup cleanup uses the durable lease record, not mtime.
    result = await execution.cleanup_scratch_leases()
    assert lease_id in result["recovered_leases"]
    assert not local_path.exists()
    closed = await backend.get_scratch_lease(lease_id)
    assert closed is not None
    assert closed.get("closed_at") is not None


@pytest.mark.asyncio
async def test_workspace_scratch_lease_removed_with_scope_prune(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "scratch-prune-ws")
    execution = workspace.with_scope_node("executions", "exec-1")

    lease = await execution.open_scratch(purpose="hold", cleanup_policy="on_scope_prune")
    lease_id = lease.get("lease_id")
    local_path_value = lease.get("local_path")
    assert isinstance(lease_id, str)
    assert isinstance(local_path_value, str)
    local_path = Path(local_path_value)
    assert local_path.exists()

    await execution.prune_scope({"execution_id": "exec-1"})

    assert not local_path.exists()
    backend = cast(Any, workspace.backend)
    # Pruning the owning scope also removes the durable lease record.
    assert await backend.get_scratch_lease(lease_id) is None


def test_trigger_flow_runtime_event_recovery_diagnostics_and_projection_shape():
    records = [
        {
            "execution_id": "exec-cycle",
            "sequence": 1,
            "event_id": "evt-a",
            "event_type": "triggerflow.signal",
            "state_version": 1,
            "parent_id": None,
            "causation_id": None,
            "parent_signal_id": "sig-b",
            "aggregation_scope": "scope-1",
            "operator_id": "operator-a",
            "interrupt_id": "approval",
            "resume_request_id": "resume-1",
            "actor_id": "reviewer",
            "lease_owner_id": "worker-1",
            "snapshot_ref": None,
            "artifact_refs": [],
            "event": {"payload": {"SIGNAL_ID": "sig-a"}},
        },
        {
            "execution_id": "exec-cycle",
            "sequence": 3,
            "event_id": "evt-b",
            "event_type": "triggerflow.signal",
            "parent_signal_id": "sig-a",
            "event": {"payload": {"SIGNAL_ID": "sig-b"}},
        },
    ]

    diagnostics = diagnose_runtime_event_records(records)
    codes = {diagnostic["code"] for diagnostic in diagnostics}
    projection = project_runtime_event_record(records[0])

    assert "triggerflow.runtime_event.missing_sequence" in codes
    assert "triggerflow.runtime_event.parent_signal_cycle" in codes
    assert projection["parent_signal_id"] == "sig-b"
    assert projection["aggregation_scope"] == "scope-1"
    assert projection["operator_id"] == "operator-a"
    assert projection["interrupt_id"] == "approval"
    assert projection["resume_request_id"] == "resume-1"
    assert projection["actor_id"] == "reviewer"
    assert projection["lease_owner_id"] == "worker-1"


@pytest.mark.asyncio
async def test_workspace_runtime_event_store_sanitizes_runtime_objects(tmp_path):
    class RuntimeOnlyObject:
        pass

    workspace = Agently.create_workspace(tmp_path / "runtime-event-sanitize")
    runtime_object = RuntimeOnlyObject()
    event = RuntimeEvent(
        event_type="triggerflow.signal",
        payload={"value": runtime_object},
        meta={"runtime_object": runtime_object},
    )

    record = await workspace.append_runtime_event("exec-sanitize", event)
    queried = await workspace.query_runtime_events("exec-sanitize")

    assert "RuntimeOnlyObject" in record["event"]["payload"]["value"]
    assert "RuntimeOnlyObject" in record["event"]["meta"]["runtime_object"]
    assert queried[0]["event"] == record["event"]


@pytest.mark.asyncio
async def test_workspace_file_policy_evidence_links_retention_and_capabilities(tmp_path):
    agent = Agently.create_agent("workspace-provider-policy").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    file_policy = await workspace.record_file_policy(
        action_file_root=str(tmp_path / "isolated-actions"),
        allowed_roots=[str(workspace.files_root), str(tmp_path / "isolated-actions")],
        root_source="explicit",
        policy_labels=["customer-data"],
        links={"execution_id": "exec-2", "operation_id": "file-read-1"},
    )
    assert file_policy == await workspace.get_file_policy()
    assert file_policy["content_root"] == str(workspace.content_root)
    assert file_policy["files_root"] == str(workspace.files_root)
    assert file_policy["root_source"] == "explicit"
    assert file_policy["policy_labels"] == ["customer-data"]

    request_ref = await workspace.put(
        {"prompt": "read file"},
        collection="observations",
        kind="operation_request",
        summary="file operation request",
    )
    result_ref = await workspace.put(
        {"status": "success"},
        collection="observations",
        kind="operation_result",
        summary="file operation result",
    )
    artifact_ref = await workspace.put(
        "file contents",
        collection="artifacts",
        kind="file_snapshot",
        summary="file snapshot",
    )
    event_record = await workspace.append_runtime_event(
        "exec-2",
        {"event_type": "action.completed", "event_id": "evt-action-done"},
    )
    link = await workspace.link_evidence(
        request_ref,
        result_ref,
        relation="resulted_in",
        execution_id="exec-2",
        operation_id="file-read-1",
        runtime_event_id=event_record["event_id"],
        checkpoint_id="checkpoint-1",
        exchange_id="exchange-2",
        artifact_refs=[artifact_ref],
    )
    assert link["meta"]["evidence"]["execution_id"] == "exec-2"
    assert link["meta"]["evidence"]["artifact_refs"][0]["record_id"] == artifact_ref["id"]

    summary_ref = await workspace.put(
        "compacted summary",
        collection="artifacts",
        kind="compaction_summary",
        summary="compaction summary",
    )
    anchor = await workspace.add_retention_anchor(
        "exec-2",
        anchor_type="compaction",
        sequence=event_record["sequence"],
        record_ref=artifact_ref,
        summary_ref=summary_ref,
        preserved_event_ids=[event_record["event_id"]],
        meta={"reason": "bounded restore"},
    )
    anchors = await workspace.retention_anchors("exec-2", anchor_type="compaction")
    assert [item["id"] for item in anchors] == [anchor["id"]]
    anchor_record_ref = anchors[0]["record_ref"]
    anchor_summary_ref = anchors[0]["summary_ref"]
    assert anchor_record_ref is not None
    assert anchor_summary_ref is not None
    assert anchor_record_ref["record_id"] == artifact_ref["id"]
    assert anchor_summary_ref["record_id"] == summary_ref["id"]
    assert anchors[0]["preserved_event_ids"] == [event_record["event_id"]]

    capabilities = workspace.capabilities()
    assert capabilities["components"]["runtime_event_store"] == "LocalWorkspaceBackend"
    assert capabilities["components"]["retention_policy"] == "LocalWorkspaceBackend"
    assert capabilities["features"]["file_policy_metadata"] is True
    assert capabilities["features"]["evidence_links"] is True
    assert capabilities["features"]["supports_compaction_anchor"] is True


@pytest.mark.asyncio
async def test_workspace_registers_custom_context_profile(tmp_path):
    class StaticPlanner:
        name = "static"

        async def plan(
            self,
            *,
            workspace,
            goal: str,
            scope: dict,
            budget: dict,
            profile: str,
        ) -> WorkspaceContextPlan:
            _ = (workspace, budget)
            return {
                "goal": goal,
                "profile": profile,
                "queries": [],
                "filters": {f"scope.{key}": value for key, value in scope.items()},
                "scope": scope,
                "budget": budget,
                "diagnostics": {"planner": self.name},
            }

    class StaticRetriever:
        name = "static"

        async def retrieve(self, *, workspace, plan: WorkspaceContextPlan) -> list[WorkspaceRecordRef]:
            return await workspace.search(None, filters=plan["filters"])

    class StaticBuilder:
        name = "static"

        async def build(
            self,
            *,
            workspace,
            goal: str,
            profile: str,
            records: list[WorkspaceRecordRef],
            budget: dict,
            diagnostics: dict,
        ) -> WorkspaceContextPackage:
            _ = (workspace, budget)
            return {
                "goal": goal,
                "profile": profile,
                "items": [
                    {
                        "ref": record,
                        "kind": record["kind"],
                        "summary": record["summary"],
                        "content": None,
                        "use": "custom",
                    }
                    for record in records
                ],
                "omitted": [],
                "diagnostics": diagnostics,
            }

    Agently.workspace.register_context_profile(
        "custom-test",
        planner=StaticPlanner(),
        retriever=StaticRetriever(),
        context_builder=StaticBuilder(),
    )
    agent = Agently.create_agent("workspace-custom-recall").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    ref = await workspace.put(
        "custom recall record",
        collection="decisions",
        kind="decision",
        summary="custom recall decision",
        scope={"task_id": "issue-456"},
    )

    context_pack = await workspace.build_context(
        goal="anything",
        scope={"task_id": "issue-456"},
        profile="custom-test",
    )

    assert context_pack["items"][0]["ref"]["id"] == ref["id"]
    assert context_pack["items"][0]["use"] == "custom"
