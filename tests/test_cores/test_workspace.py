import asyncio
import base64
import hashlib
from importlib import import_module
from numbers import Real
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import LazyWorkspace, WorkspaceConfigurationError, WorkspaceManager, WorkspacePolicyError
from agently.core.application import AgentTask
from agently.core.workspace._defaults import script_scope
from agently.core.orchestration.TriggerFlow import diagnose_runtime_event_records, project_runtime_event_record
from agently.types.data import RuntimeEvent, RunContext, WorkspaceContextPackage, WorkspaceContextPlan, WorkspaceRecordRef


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

    await first.workspace.ingest(
        content="visible only to session a",
        collection="observations",
        kind="scoped",
        summary="shared keyword",
    )
    await second.workspace.ingest(
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
async def test_workspace_local_ingest_search_link_and_get(tmp_path):
    agent = Agently.create_agent("workspace-test").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None

    ref = await workspace.ingest(
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
    lease = await workspace.claim_lease("run-cas", "worker-1", ttl=0.02, expected_state_version=2)

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
    assert capabilities["components"]["content"] == "LocalContentStore"
    assert capabilities["components"]["vector_index"] == "NoopVectorIndex"
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
    assert capabilities["features"]["supports_remote_backend"] is False


@pytest.mark.asyncio
async def test_workspace_build_context_returns_refs_and_budget_diagnostics(tmp_path):
    agent = Agently.create_agent("workspace-recall").use_workspace(tmp_path / "run")
    workspace = agent.workspace
    assert workspace is not None
    failure_ref = await workspace.ingest(
        content="route fallback failed because provider returned no route candidate",
        collection="observations",
        kind="test_output",
        summary="route fallback pytest failure",
        scope={"task_id": "issue-123"},
        source={"type": "command", "name": "pytest"},
    )
    await workspace.ingest(
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
    version_ref = await workspace.ingest(
        content="Agently 4.1.3.4 task loop fix",
        collection="observations",
        kind="note",
        summary="Agently 4.1.3.4 task loop fix",
        scope={"task_id": "fts-safe"},
    )
    dotted_ref = await workspace.ingest(
        content="foo.bar import failed during verification",
        collection="observations",
        kind="note",
        summary="foo.bar import failed",
        scope={"task_id": "fts-safe"},
    )
    question_ref = await workspace.ingest(
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

    first_ref = await first.ingest(content="first execution", collection="observations", kind="partition")
    second_ref = await second.ingest(content="second execution", collection="observations", kind="partition")
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
