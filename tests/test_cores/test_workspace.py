import asyncio
from numbers import Real

import pytest

from agently import Agently
from agently.core import LazyWorkspace, WorkspaceConfigurationError, WorkspacePolicyError
from agently.core.orchestration.TriggerFlow import diagnose_runtime_event_records, project_runtime_event_record
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
