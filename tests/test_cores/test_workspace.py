import pytest

from agently import Agently
from agently.core import LazyWorkspace, WorkspaceConfigurationError, WorkspacePolicyError
from agently.types.data import RuntimeEvent, RunContext, WorkspaceContextPack, WorkspaceRecallPlan, WorkspaceRecordRef


@pytest.mark.asyncio
async def test_agent_has_lazy_workspace_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = Agently.create_agent("lazy-workspace")
    workspace = agent.workspace

    assert isinstance(workspace, LazyWorkspace)
    assert workspace.is_materialized is False
    assert workspace.root == (tmp_path / ".agently" / "workspaces" / f"lazy-workspace-{agent.id[:8]}").resolve()
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
    assert "auto" in Agently.workspace.list_recall_profiles()


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
        profile="software_dev",
    )

    assert context_pack["goal"] == "route fallback failure"
    assert context_pack["profile"] == "software_dev"
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
        profile="software_dev",
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

    checkpoint_ref = await workspace.checkpoint("exec-1", {"phase": "waiting"}, step_id="approval")
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
        idempotency_key="approval-request-1",
        checkpoint_ref=checkpoint_ref,
        artifact_refs=[artifact_ref],
        exchange_id="exchange-1",
    )
    duplicate = await workspace.append_runtime_event(
        "exec-1",
        event,
        idempotency_key="approval-request-1",
        checkpoint_ref=checkpoint_ref,
        artifact_refs=[artifact_ref],
        exchange_id="exchange-1",
    )
    second = await workspace.append_runtime_event(
        "exec-1",
        {"event_type": "triggerflow.execution_resumed", "event_id": "evt-resume", "meta": {"node_id": "resume-node"}},
    )

    assert duplicate["id"] == first["id"]
    assert first["sequence"] == 1
    assert first["parent_id"] == "evt-parent"
    assert first["causation_id"] == "evt-cause"
    assert first["node_id"] == "approval-node"
    assert first["aggregation_scope"] == "approval-scope"
    first_checkpoint_ref = first["checkpoint_ref"]
    assert first_checkpoint_ref is not None
    assert first_checkpoint_ref["record_id"] == checkpoint_ref["id"]
    assert first["artifact_refs"][0]["record_id"] == artifact_ref["id"]
    assert second["sequence"] == 2

    queried = await workspace.query_runtime_events("exec-1", sequence_from=2, limit=1)
    assert [item["event_id"] for item in queried] == ["evt-resume"]
    by_event_id = await workspace.query_runtime_events("exec-1", event_id=first["event_id"])
    assert [item["id"] for item in by_event_id] == [first["id"]]


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
async def test_workspace_registers_custom_recall_profile(tmp_path):
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
        ) -> WorkspaceRecallPlan:
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

        async def retrieve(self, *, workspace, plan: WorkspaceRecallPlan) -> list[WorkspaceRecordRef]:
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
        ) -> WorkspaceContextPack:
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

    Agently.workspace.register_recall_profile(
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
