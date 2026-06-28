# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import pytest

from agently import Agently
from agently.builtins.plugins.Blocks.AgentlyBlocks import ExecutionBlockRegistry, PlanBlockRegistry
from agently.core.application.SkillsExecutor import DictSkillSource, SkillCapabilityAdapter
from agently.core import TaskDAGExecutor
from agently.types.data import BlockCompileRequest


def test_default_blocks_plugin_is_registered():
    assert "AgentlyBlocks" in Agently.plugin_manager.get_plugin_list("Blocks")
    summaries = Agently.blocks.list_plan_block_summaries()
    summary_ids = {summary.id for summary in summaries}
    assert {
        "model_request",
        "action_call",
        "skill_activation",
        "dag_segment",
        "approval_wait",
        "external_wait",
        "validation",
        "emit",
    }.issubset(summary_ids)


def test_blocks_compile_maps_plan_blocks_to_execution_block_graph():
    graph = Agently.blocks.compile(
        BlockCompileRequest.from_value(
            {
                "execution_id": "exec-1",
                "task_frame_id": "frame-1",
                "plan_id": "plan-1",
                "plan_blocks": [
                    {"id": "context", "plan_block_id": "skill_activation", "kind": "skill_activation"},
                    {"id": "validate", "plan_block_id": "validation", "kind": "validation"},
                ],
                "edges": [{"from": "context", "to": "validate"}],
            }
        )
    )

    assert graph.graph_id == "blocks:plan-1"
    assert [block.id for block in graph.execution_blocks] == [
        "context:skill_activation",
        "validate:validation",
    ]
    assert graph.edges[0].from_execution_block == "context:skill_activation"
    assert graph.edges[0].to_execution_block == "validate:validation"
    assert graph.start_blocks == ("context:skill_activation",)
    assert graph.terminal_blocks == ("validate:validation",)


def test_blocks_compile_rejects_denied_capability_before_runtime():
    with pytest.raises(PermissionError, match="requires denied capability"):
        Agently.blocks.compile(
            {
                "plan_id": "plan-denied",
                "capability_resolution": {"denied_capabilities": ["fs.write"]},
                "plan_blocks": [
                    {
                        "id": "write",
                        "plan_block_id": "workspace_operation",
                        "kind": "workspace_operation",
                        "capability_requirements": [{"need": "fs.write"}],
                    }
                ],
            }
        )


def test_empty_capability_resolution_does_not_activate_allowlist_gate():
    graph = Agently.blocks.compile(
        {
            "plan_id": "empty-resolution",
            "capability_resolution": {},
            "plan_blocks": [
                {
                    "id": "read",
                    "plan_block_id": "action_call",
                    "kind": "action_call",
                    "capability_requirements": [{"need": "workspace.read"}],
                }
            ],
        }
    )

    assert [block.kind for block in graph.execution_blocks] == ["action_call"]


def test_blocks_registries_validate_contract_metadata():
    with pytest.raises(ValueError, match="runtime binding option"):
        PlanBlockRegistry().register(
            {
                "id": "bad-plan",
                "kind": "action_call",
                "runtime_binding_options": [{"label": "missing trusted binding"}],
            }
        )
    with pytest.raises(ValueError, match="unknown block signal"):
        ExecutionBlockRegistry().register(
            {
                "id": "bad-block",
                "kind": "action_call",
                "signal_contract": {"emits": ["task.accepted"]},
            }
        )


def test_blocks_compile_rejects_missing_edges_and_unresolved_capabilities():
    with pytest.raises(ValueError, match="missing to_plan_block"):
        Agently.blocks.compile(
            {
                "plan_id": "missing-edge",
                "plan_blocks": [{"id": "a", "plan_block_id": "model_request", "kind": "model_request"}],
                "edges": [{"from": "a", "to": "missing"}],
            }
        )
    with pytest.raises(PermissionError, match="pending approval"):
        Agently.blocks.compile(
            {
                "plan_id": "pending-capability",
                "capability_resolution": {
                    "pending_approvals": [{"capability_id": "deploy"}],
                },
                "plan_blocks": [
                    {
                        "id": "deploy",
                        "plan_block_id": "action_call",
                        "kind": "action_call",
                        "capability_requirements": [{"capability_id": "deploy"}],
                    }
                ],
            }
        )
    graph = Agently.blocks.compile(
        {
            "plan_id": "pending-with-approval-wait",
            "capability_resolution": {
                "pending_approvals": [{"capability_id": "deploy"}],
            },
            "plan_blocks": [
                {
                    "id": "approve",
                    "plan_block_id": "approval_wait",
                    "kind": "approval_wait",
                    "bound_inputs": {"request": {"capability_id": "deploy"}},
                },
                {
                    "id": "deploy",
                    "plan_block_id": "action_call",
                    "kind": "action_call",
                    "capability_requirements": [{"capability_id": "deploy"}],
                    "runtime_preferences": {"handler": "deploy"},
                },
            ],
            "edges": [{"from": "approve", "to": "deploy"}],
        }
    )
    assert [block.kind for block in graph.execution_blocks] == ["approval_wait", "action_call"]


@pytest.mark.asyncio
async def test_blocks_runtime_executes_on_triggerflow_with_handler_and_evidence():
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-runtime",
            "plan_blocks": [
                {
                    "id": "call",
                    "plan_block_id": "action_call",
                    "kind": "action_call",
                    "runtime_preferences": {"handler": "echo_action"},
                },
                {"id": "validate", "plan_block_id": "validation", "kind": "validation"},
            ],
            "edges": [{"from": "call", "to": "validate"}],
            "capability_resolution": {"allowed_capabilities": ["local.echo"]},
        }
    )
    flow = Agently.blocks.bind_runtime(graph)

    async def echo_action(context):
        return {
            "called": True,
            "input": context["input"],
            "block_id": context["block"].id,
        }

    execution = flow.create_execution(
        auto_close=False,
        workspace=False,
        runtime_resources={"blocks.handlers": {"echo_action": echo_action}},
    )
    await execution.async_start({"customer": "ACME"})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    result = Agently.blocks.map_result(graph, snapshot)
    assert [item["execution_block_id"] for item in evidence.execution_block_results] == [
        "call:action_call",
        "validate:validation",
    ]
    assert evidence.action_evidence[0]["execution_block_id"] == "call:action_call"
    assert evidence.validation_results[0]["execution_block_id"] == "validate:validation"
    assert result["semantic_outputs"]["validate:validation"]["ok"] is True


@pytest.mark.asyncio
async def test_blocks_replan_signal_cancels_only_affected_downstream_blocks():
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-replan-cancel",
            "plan_blocks": [
                {
                    "id": "root",
                    "plan_block_id": "model_request",
                    "kind": "model_request",
                    "runtime_preferences": {"handler": "root"},
                },
                {
                    "id": "affected",
                    "plan_block_id": "action_call",
                    "kind": "action_call",
                    "runtime_preferences": {"handler": "affected"},
                },
                {
                    "id": "unaffected",
                    "plan_block_id": "validation",
                    "kind": "validation",
                },
            ],
            "edges": [
                {"from": "root", "to": "affected"},
                {"from": "root", "to": "unaffected"},
            ],
        }
    )
    calls: list[str] = []

    async def root(_context):
        calls.append("root")
        return {
            "ok": False,
            "replan_signal": {
                "status": "replan_segment",
                "reason": "affected branch evidence is invalid",
                "affected_execution_block_ids": ["affected:action_call"],
            },
        }

    async def affected(_context):
        calls.append("affected")
        return {"should_not_run": True}

    execution = Agently.blocks.bind_runtime(graph).create_execution(
        auto_close=False,
        workspace=False,
        runtime_resources={"blocks.handlers": {"root": root, "affected": affected}},
    )
    await execution.async_start({"input": "x"})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    by_block = {item["execution_block_id"]: item for item in evidence.execution_block_results}
    assert calls == ["root"]
    assert by_block["affected:action_call"]["cancelled"] is True
    assert by_block["unaffected:validation"]["success"] is True
    assert evidence.diagnostics[0]["status"] == "replan_segment"


@pytest.mark.asyncio
async def test_blocks_skill_activation_uses_adapter_and_records_context_evidence():
    adapter = SkillCapabilityAdapter(
        DictSkillSource(
            {
                "webapp-testing": {
                    "skill_id": "webapp-testing",
                    "card": {"name": "Web App Testing", "description": "Browser QA guidance"},
                    "guidance": {"body": "Use browser screenshots and write files only after approval."},
                    "resource_index": {
                        "references/readback.md": {"kind": "reference", "summary": "Readback checks", "size": 40}
                    },
                }
            }
        )
    )
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-skill-activation",
            "plan_blocks": [
                {
                    "id": "ctx",
                    "plan_block_id": "skill_activation",
                    "kind": "skill_activation",
                    "bound_inputs": {"skill_id": "webapp-testing", "task": "capture browser screenshot"},
                }
            ],
        }
    )
    execution = Agently.blocks.bind_runtime(graph).create_execution(
        auto_close=False,
        workspace=False,
        runtime_resources={"skills.capability_adapter": adapter},
    )

    await execution.async_start({"url": "https://example.test"})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    assert evidence.skill_evidence[0]["skill_id"] == "webapp-testing"
    assert evidence.skill_evidence[0]["evidence_kind"] == "skill_context"
    assert evidence.skill_evidence[0]["proves_side_effect"] is False
    assert evidence.skill_evidence[0]["execution_block_id"] == "ctx:skill_activation"
    output = evidence.execution_block_results[0]["output"]
    assert any(need["need"] == "web_browse" for need in output["capability_needs"])
    assert any(item["plan_block_id"] == "action_call" for item in output["plan_block_recommendations"])


@pytest.mark.asyncio
async def test_blocks_compile_skill_activation_before_model_action_dag_segment():
    adapter = SkillCapabilityAdapter(
        DictSkillSource(
            {
                "script-review": {
                    "skill_id": "script-review",
                    "card": {"name": "Script Review", "description": "Review scripts before execution"},
                    "guidance": {"body": "Load script review criteria before planning fixes."},
                    "resource_index": {},
                }
            }
        )
    )
    dag = {
        "graph_id": "script-review-dag",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {"id": "plan_fix", "kind": "model"},
            {"id": "run_script", "kind": "action", "depends_on": ["plan_fix"]},
        ],
        "semantic_outputs": {"script": "run_script"},
    }

    async def model_node(_context):
        return {"fix_plan": "normalize imports before running the script"}

    async def action_node(context):
        return {
            "status": "success",
            "dependency_results": dict(context["dependency_results"]),
            "action_evidence": [
                {
                    "action_id": "run_script",
                    "status": "success",
                    "proves_side_effect": True,
                }
            ],
        }

    executor = TaskDAGExecutor({"model": model_node, "action": action_node})
    validation = executor.validator.validate(dag, resolver=executor.resolver)
    graph = Agently.blocks.compile(
        {
            "plan_id": "skill-to-dag",
            "plan_blocks": [
                {
                    "id": "ctx",
                    "plan_block_id": "skill_activation",
                    "kind": "skill_activation",
                    "bound_inputs": {"skill_id": "script-review", "task": "repair the script"},
                },
                {
                    "id": "dag",
                    "plan_block_id": "dag_segment",
                    "kind": "dag_segment",
                    "bound_inputs": {
                        "task_dag": dag,
                        "task_dag_validation": validation,
                        "handler_prefix": "task_dag:script-review-dag",
                    },
                },
            ],
            "edges": [{"from": "ctx", "to": "dag"}],
        }
    )

    assert [block.kind for block in graph.execution_blocks] == [
        "skill_activation",
        "dag_node",
        "dag_node",
    ]
    assert [block.source_task_dag_node_id for block in graph.execution_blocks] == [
        None,
        "plan_fix",
        "run_script",
    ]
    assert ("ctx:skill_activation", "dag:dag_node:plan_fix") in {
        (edge.from_execution_block, edge.to_execution_block) for edge in graph.edges
    }
    assert ("dag:dag_node:plan_fix", "dag:dag_node:run_script") in {
        (edge.from_execution_block, edge.to_execution_block) for edge in graph.edges
    }

    execution = Agently.blocks.bind_runtime(graph).create_execution(
        auto_close=False,
        workspace=False,
        runtime_resources={
            "skills.capability_adapter": adapter,
            "blocks.handlers": {
                "task_dag:script-review-dag:plan_fix": model_node,
                "task_dag:script-review-dag:run_script": action_node,
            },
        },
    )
    await execution.async_start({"script": "legacy.py"})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    assert evidence.skill_evidence[0]["skill_id"] == "script-review"
    assert evidence.action_evidence[0]["action_id"] == "run_script"
    assert evidence.action_evidence[0]["source_task_dag_node_id"] == "run_script"


@pytest.mark.asyncio
async def test_blocks_workspace_operation_ingests_through_workspace_resource(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "blocks-workspace")
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-workspace",
            "plan_blocks": [
                {
                    "id": "store",
                    "plan_block_id": "workspace_operation",
                    "kind": "workspace_operation",
                    "bound_inputs": {
                        "operation": "ingest",
                        "content": {"answer": "ok"},
                        "collection": "observations",
                        "kind": "blocks_example_observation",
                    },
                }
            ],
        }
    )
    execution = Agently.blocks.bind_runtime(graph).create_execution(auto_close=False, workspace=workspace)

    await execution.async_start({"ignored": True})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    block_output = evidence.execution_block_results[0]["output"]
    ref = block_output["ref"]
    assert block_output["operation"] == "ingest"
    assert evidence.workspace_refs == (ref["id"],)
    assert await workspace.get_data(ref) == {"answer": "ok"}


@pytest.mark.asyncio
async def test_blocks_workspace_operation_search_returns_scoped_retrieval_roles(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "blocks-workspace-search")
    expected_ref = await workspace.ingest(
        content="Alpha deadline is 2026-07-01. Keep this scoped evidence short.",
        collection="observations",
        kind="note",
        summary="alpha deadline note",
        scope={"task_id": "alpha"},
    )
    await workspace.ingest(
        content="Beta deadline is unrelated and must stay outside the scoped result.",
        collection="observations",
        kind="note",
        summary="beta deadline note",
        scope={"task_id": "beta"},
    )
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-workspace-search",
            "plan_blocks": [
                {
                    "id": "search",
                    "plan_block_id": "workspace_operation",
                    "kind": "workspace_operation",
                    "bound_inputs": {
                        "operation": "search",
                        "query": "deadline",
                        "filters": {"scope.task_id": "alpha"},
                        "max_results": 4,
                        "include_snippets": True,
                        "snippet_limit": 24,
                    },
                }
            ],
        }
    )

    execution = Agently.blocks.bind_runtime(graph).create_execution(auto_close=False, workspace=workspace)
    await execution.async_start({"ignored": True})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    output = evidence.execution_block_results[0]["output"]
    assert output["operation"] == "search"
    assert output["query"] == "deadline"
    assert output["filters"] == {"scope.task_id": "alpha"}
    assert output["bounded"]["max_results"] == 4
    assert output["bounded"]["snippet_limit"] == 24
    assert [item["ref"]["id"] for item in output["locator_refs"]] == [expected_ref["id"]]
    assert output["locator_refs"][0]["role"] == "locator_ref"
    assert output["locator_refs"][0]["content_state"] == "ref_only"
    assert output["evidence_snippets"][0]["role"] == "evidence_snippet"
    assert output["evidence_snippets"][0]["content_state"] == "bounded_readback_available"
    assert output["evidence_snippets"][0]["locator_ref"]["ref"]["id"] == expected_ref["id"]
    assert output["evidence_snippets"][0]["snippet_chars"] <= 24
    assert evidence.workspace_refs == (expected_ref["id"],)
    assert not {"useful", "accepted", "semantically_relevant"}.intersection(output)


@pytest.mark.asyncio
async def test_blocks_workspace_operation_search_can_use_workspace_files_surface(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "blocks-workspace-file-search")
    await workspace.write_file("notes/todo.md", "alpha\nrelease deadline is 2026-07-01\n")
    await workspace.ingest(
        content="Indexed record is unrelated to the file-only query.",
        collection="observations",
        kind="note",
        summary="unrelated index record",
    )
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-workspace-file-search",
            "plan_blocks": [
                {
                    "id": "search-files",
                    "plan_block_id": "workspace_operation",
                    "kind": "workspace_operation",
                    "bound_inputs": {
                        "operation": "search",
                        "query": "deadline",
                        "search_surface": "workspace_files",
                        "path": "notes",
                        "pattern": "*.md",
                        "max_results": 3,
                        "max_file_bytes": 1024,
                    },
                }
            ],
        }
    )

    execution = Agently.blocks.bind_runtime(graph).create_execution(auto_close=False, workspace=workspace)
    await execution.async_start({"ignored": True})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    output = evidence.execution_block_results[0]["output"]
    assert output["operation"] == "search"
    assert output["bounded"]["search_surface"] == "workspace_files"
    assert output["bounded"]["search_engines"] == ["workspace_file_scan"]
    assert output["bounded"]["index_total_matches"] == 0
    assert output["bounded"]["file_returned_results"] == 1
    assert output["bounded"]["returned_results"] == 1
    assert output["bounded"]["candidate_bytes"] >= output["bounded"]["returned_snippet_bytes"]
    assert output["locator_refs"][0]["role"] == "locator_ref"
    assert output["locator_refs"][0]["content_state"] == "ref_only"
    assert output["locator_refs"][0]["path"] == "notes/todo.md"
    assert output["evidence_snippets"][0]["role"] == "evidence_snippet"
    assert output["evidence_snippets"][0]["content"] == "release deadline is 2026-07-01"
    assert output["evidence_snippets"][0]["locator_ref"]["path"] == "notes/todo.md"
    assert not {"useful", "accepted", "semantically_relevant"}.intersection(output)


@pytest.mark.asyncio
async def test_blocks_workspace_operation_read_bounded_returns_evidence_snippet(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "blocks-workspace-read-bounded")
    ref = await workspace.put(
        "abcdefghijklmnopqrstuvwxyz",
        collection="artifacts",
        kind="text_artifact",
        summary="alphabet artifact",
    )
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-workspace-read-bounded",
            "plan_blocks": [
                {
                    "id": "read",
                    "plan_block_id": "workspace_operation",
                    "kind": "workspace_operation",
                    "bound_inputs": {
                        "operation": "read_bounded",
                        "ref": ref,
                        "offset": 4,
                        "limit": 6,
                    },
                }
            ],
        }
    )

    execution = Agently.blocks.bind_runtime(graph).create_execution(auto_close=False, workspace=workspace)
    await execution.async_start({"ignored": True})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    output = evidence.execution_block_results[0]["output"]
    snippet = output["evidence_snippet"]
    assert output["operation"] == "read_bounded"
    assert snippet["role"] == "evidence_snippet"
    assert snippet["content"] == "efghij"
    assert snippet["offset"] == 4
    assert snippet["size"] == 6
    assert snippet["total_size"] == 26
    assert snippet["locator_ref"]["ref"]["id"] == ref["id"]
    assert evidence.workspace_refs == (ref["id"],)


@pytest.mark.asyncio
async def test_blocks_approval_wait_uses_policy_approval_gate():
    Agently.configure_policy_approval(handler="auto_approve")
    try:
        graph = Agently.blocks.compile(
            {
                "plan_id": "plan-approval",
                "plan_blocks": [
                    {
                        "id": "approve",
                        "plan_block_id": "approval_wait",
                        "kind": "approval_wait",
                        "bound_inputs": {
                            "request": {
                                "request_id": "blocks-approval-test",
                                "capability": "write_file",
                                "subject": "write report",
                            }
                        },
                    }
                ],
            }
        )
        execution = Agently.blocks.bind_runtime(graph).create_execution(auto_close=False, workspace=False)

        await execution.async_start({"draft": True})
        snapshot = await execution.async_close(timeout=5)

        evidence = Agently.blocks.map_evidence(graph, snapshot)
        output = evidence.execution_block_results[0]["output"]
        assert output["approved"] is True
        assert output["decision"]["status"] == "approved"
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")


@pytest.mark.asyncio
async def test_blocks_approval_wait_uses_triggerflow_pause_and_resume():
    Agently.configure_policy_approval(handler="fail_closed")
    try:
        graph = Agently.blocks.compile(
            {
                "plan_id": "plan-approval-resume",
                "plan_blocks": [
                    {
                        "id": "approve",
                        "plan_block_id": "approval_wait",
                        "kind": "approval_wait",
                        "bound_inputs": {
                            "request": {
                                "request_id": "blocks-approval-resume",
                                "capability": "write_file",
                                "subject": "write report",
                            }
                        },
                    }
                ],
            }
        )
        execution = await Agently.blocks.bind_runtime(graph).async_start_execution(
            {"draft": True},
            wait_for_result=False,
            workspace=False,
        )
        pending = execution.get_pending_interrupts()

        assert execution.get_status() == "waiting"
        assert "policy:blocks-approval-resume" in pending

        resumed = await execution.async_continue_with(
            "policy:blocks-approval-resume",
            {"status": "approved", "reason": "ok"},
        )
        snapshot = await execution.async_close(timeout=5)
        evidence = Agently.blocks.map_evidence(graph, snapshot)
        output = evidence.execution_block_results[0]["output"]
        assert evidence.execution_block_results[0]["waiting"] is True
        assert output["status"] == "waiting"
        assert output["type"] == "policy_approval"
        assert resumed["status"] == "resumed"
        assert resumed["response"] == {"status": "approved", "reason": "ok"}
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")


@pytest.mark.asyncio
async def test_blocks_external_wait_uses_triggerflow_pause_and_resume():
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-external-wait",
            "plan_blocks": [
                {
                    "id": "callback",
                    "plan_block_id": "external_wait",
                    "kind": "external_wait",
                    "bound_inputs": {
                        "type": "webhook",
                        "exchange_kind": "callback",
                        "interrupt_id": "external-callback",
                        "payload": {"ticket_id": "INC-42"},
                    },
                }
            ],
        }
    )
    execution = await Agently.blocks.bind_runtime(graph).async_start_execution(
        None,
        wait_for_result=False,
        workspace=False,
    )
    pending = execution.get_pending_interrupts()

    assert execution.get_status() == "waiting"
    assert pending["external-callback"]["type"] == "webhook"

    resumed = await execution.async_continue_with("external-callback", {"status": "ready"})
    snapshot = await execution.async_close(timeout=5)
    evidence = Agently.blocks.map_evidence(graph, snapshot)
    output = evidence.execution_block_results[0]["output"]
    assert evidence.execution_block_results[0]["waiting"] is True
    assert output["status"] == "waiting"
    assert output["type"] == "webhook"
    assert resumed["status"] == "resumed"
    assert resumed["response"] == {"status": "ready"}


@pytest.mark.asyncio
async def test_blocks_dag_segment_reuses_task_dag_validation_and_runtime_signals():
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-dag",
            "plan_blocks": [
                {
                    "id": "dag",
                    "plan_block_id": "dag_segment",
                    "kind": "dag_segment",
                    "bound_inputs": {
                        "task_dag": {
                            "graph_id": "review",
                            "task_schema_version": "task_dag/v1",
                            "tasks": [
                                {"id": "extract", "kind": "validate"},
                                {"id": "final", "kind": "emit", "depends_on": ["extract"]},
                            ],
                            "semantic_outputs": {"final": "final"},
                        }
                    },
                }
            ],
        }
    )

    assert [block.source_task_dag_node_id for block in graph.execution_blocks] == ["extract", "final"]
    assert graph.edges[0].from_execution_block == "dag:dag_node:extract"
    assert graph.edges[0].to_execution_block == "dag:dag_node:final"

    flow = Agently.blocks.bind_runtime(graph)
    execution = flow.create_execution(auto_close=False, workspace=False)
    await execution.async_start({"doc": "policy"})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    assert [item["source_task_dag_node_id"] for item in evidence.execution_block_results] == [
        "extract",
        "final",
    ]


def test_task_dag_executor_can_compile_validated_dag_through_blocks():
    executor = TaskDAGExecutor()
    graph = executor.compile_blocks(
        {
            "graph_id": "review",
            "task_schema_version": "task_dag/v1",
            "tasks": [
                {"id": "extract", "kind": "validate"},
                {"id": "final", "kind": "emit", "depends_on": ["extract"]},
            ],
            "semantic_outputs": {"final": "final"},
        },
        blocks=Agently.blocks,
    )

    assert graph.source_plan_id == "task_dag:review"
    assert [block.kind for block in graph.execution_blocks] == ["dag_node", "dag_node"]
    assert [block.source_task_dag_node_id for block in graph.execution_blocks] == ["extract", "final"]


@pytest.mark.asyncio
async def test_task_dag_executor_blocks_path_runs_local_dag_with_join_and_semantic_outputs():
    calls: list[str] = []

    async def local_handler(context):
        calls.append(context.task.id)
        if context.dependency_results:
            deps = ",".join(
                f"{ task_id }={ result }"
                for task_id, result in sorted(context.dependency_results.items())
            )
            return f"{ context.task.id }({ deps })"
        return f"{ context.task.id }:{ context.graph_input['doc'] }"

    graph = {
        "graph_id": "blocks-local-review",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {"id": "extract_terms", "kind": "local", "binding": "local_handler"},
            {"id": "extract_dates", "kind": "local", "binding": "local_handler"},
            {
                "id": "final_review",
                "kind": "local",
                "binding": "local_handler",
                "depends_on": ["extract_terms", "extract_dates"],
            },
        ],
        "semantic_outputs": {"final": "final_review"},
    }

    result = await TaskDAGExecutor({"local_handler": local_handler}).async_run_blocks(
        graph,
        graph_input={"doc": "policy"},
        timeout=1,
    )

    assert calls.count("final_review") == 1
    assert result["evidence"]["execution_block_results"][-1]["source_task_dag_node_id"] == "final_review"
    assert result["result"]["semantic_outputs"]["final"] == {
        "task_id": "final_review",
        "result": (
            "final_review(extract_dates=extract_dates:policy,"
            "extract_terms=extract_terms:policy)"
        ),
    }


def test_task_dag_executor_blocks_path_still_rejects_invalid_dag():
    executor = TaskDAGExecutor()
    with pytest.raises(ValueError, match="depends on missing task"):
        executor.compile_blocks(
            {
                "graph_id": "bad",
                "tasks": [{"id": "final", "kind": "emit", "depends_on": ["missing"]}],
            },
            blocks=Agently.blocks,
        )
