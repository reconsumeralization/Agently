import pytest

from agently import Agently
from agently.core import WorkspacePolicyError
from agently.types.data import WorkspaceContextPack, WorkspaceRecallPlan, WorkspaceRecordRef


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
    assert context_pack["items"][0]["content"] is not None
    assert "route fallback" in context_pack["items"][0]["content"]
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
