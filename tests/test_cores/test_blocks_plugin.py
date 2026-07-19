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

from typing import Any

import pytest

from agently import Agently
from agently.builtins.plugins.Blocks.AgentlyBlocks import ExecutionBlockRegistry, PlanBlockRegistry
from agently.core import TaskDAGExecutor
from agently.core.context import TaskContext
from agently.types.data import BlockCompileRequest, ContextBudget, ContextConsumer


def test_default_blocks_plugin_is_registered():
    assert "AgentlyBlocks" in Agently.plugin_manager.get_plugin_list("Blocks")
    summaries = Agently.blocks.list_plan_block_summaries()
    summary_ids = {summary.id for summary in summaries}
    assert {
        "model_request",
        "action_call",
        "context_read",
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
                    {"id": "context", "plan_block_id": "context_read", "kind": "context_read"},
                    {"id": "validate", "plan_block_id": "validation", "kind": "validation"},
                ],
                "edges": [{"from": "context", "to": "validate"}],
            }
        )
    )

    assert graph.graph_id == "blocks:plan-1"
    assert [block.id for block in graph.execution_blocks] == [
        "context:context_read",
        "validate:validation",
    ]
    assert graph.edges[0].from_execution_block == "context:context_read"
    assert graph.edges[0].to_execution_block == "validate:validation"
    assert graph.start_blocks == ("context:context_read",)
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
                        "plan_block_id": "context_read",
                        "kind": "context_read",
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
        record_store=False,
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
        record_store=False,
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




def _context_read_graph(*, operation: str = "read"):
    return Agently.blocks.compile(
        {
            "plan_id": f"plan-context-{operation}",
            "plan_blocks": [
                {
                    "id": "context",
                    "plan_block_id": "context_read",
                    "kind": "context_read",
                    "intent": "Find the task deadline.",
                    "bound_inputs": {
                        "operation": operation,
                        "query": "task deadline",
                    },
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_blocks_context_read_uses_bound_context_reader():
    task_context = TaskContext("task-blocks")
    task_context.put(
        role="information",
        content="The task deadline is 2026-07-01.",
        source_ref="task/deadline",
        required=True,
    )
    reader = task_context.reader(
        consumer=ContextConsumer("blocks:execution"),
        phase="execution",
        budget=ContextBudget(max_chars=2000, max_blocks=8, max_block_chars=1000),
    )
    graph = _context_read_graph()
    execution = Agently.blocks.bind_runtime(graph).create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"context_reader": reader},
    )

    await execution.async_start({"ignored": True})
    snapshot = await execution.async_close(timeout=5)

    evidence = Agently.blocks.map_evidence(graph, snapshot)
    output = evidence.execution_block_results[0]["output"]
    assert output["operation"] == "read"
    assert output["query"] == "task deadline"
    assert output["context_package"]["task_context_id"] == task_context.context_id
    assert output["context_package"]["blocks"][0]["content"] == "The task deadline is 2026-07-01."
    assert output["locator_refs"][0]["source_ref"] == "task/deadline"
    assert output["evidence_snippets"][0]["content"] == "The task deadline is 2026-07-01."
    assert output["bounded"]["source_coverage"] == {}
    assert output["bounded"]["continuation_available"] is False


@pytest.mark.asyncio
async def test_blocks_context_read_requires_context_reader():
    execution = Agently.blocks.bind_runtime(_context_read_graph()).create_execution(
        auto_close=False,
        record_store=False,
    )
    with pytest.raises(RuntimeError, match="context_reader"):
        await execution.async_start({"ignored": True})


@pytest.mark.asyncio
async def test_blocks_context_read_rejects_side_effect_operations():
    task_context = TaskContext("task-blocks")
    reader = task_context.reader(
        consumer=ContextConsumer("blocks:execution"),
        phase="execution",
    )
    graph = _context_read_graph(operation="write")
    execution = Agently.blocks.bind_runtime(graph).create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"context_reader": reader},
    )
    with pytest.raises(ValueError, match="read-only"):
        await execution.async_start({"ignored": True})


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
        execution = Agently.blocks.bind_runtime(graph).create_execution(auto_close=False, record_store=False)

        await execution.async_start({"draft": True})
        snapshot = await execution.async_close(timeout=5)

        evidence = Agently.blocks.map_evidence(graph, snapshot)
        output = evidence.execution_block_results[0]["output"]
        assert output["approved"] is True
        assert output["decision"]["status"] == "approved"
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")


@pytest.mark.asyncio
async def test_blocks_approval_wait_global_access_control_auto_allow():
    Agently.configure_policy_approval(handler="fail_closed")
    Agently.set_settings("access_control_policy.auto_allow", True)
    try:
        graph = Agently.blocks.compile(
            {
                "plan_id": "plan-approval-auto-allow",
                "plan_blocks": [
                    {
                        "id": "approve",
                        "plan_block_id": "approval_wait",
                        "kind": "approval_wait",
                        "bound_inputs": {
                            "request": {
                                "request_id": "blocks-approval-auto-allow",
                                "capability": "write_file",
                                "subject": "write report",
                            }
                        },
                    }
                ],
            }
        )
        execution = Agently.blocks.bind_runtime(graph).create_execution(auto_close=False, record_store=False)

        await execution.async_start({"draft": True})
        snapshot = await execution.async_close(timeout=5)

        evidence = Agently.blocks.map_evidence(graph, snapshot)
        output = evidence.execution_block_results[0]["output"]
        assert execution.get_pending_interrupts() == {}
        assert output["approved"] is True
        assert output["decision"]["status"] == "approved"
        assert output["decision"]["handler"] == "access_control_policy.auto_allow"
    finally:
        Agently.set_settings("access_control_policy.auto_allow", False)
        Agently.configure_policy_approval(handler="input_timeout_fail")


@pytest.mark.asyncio
async def test_blocks_approval_wait_uses_triggerflow_pause_and_resume(tmp_path):
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
            record_store=tmp_path / "approval-resume",
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
async def test_blocks_external_wait_uses_triggerflow_pause_and_resume(tmp_path):
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
        record_store=tmp_path / "external-wait",
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
    execution = flow.create_execution(auto_close=False, record_store=False)
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


@pytest.mark.asyncio
async def test_blocks_external_wait_forwards_exchange_metadata_to_envelope(tmp_path):
    graph = Agently.blocks.compile(
        {
            "plan_id": "plan-external-wait-metadata",
            "plan_blocks": [
                {
                    "id": "callback",
                    "plan_block_id": "external_wait",
                    "kind": "external_wait",
                    "bound_inputs": {
                        "type": "webhook",
                        "exchange_kind": "clarification",
                        "interrupt_id": "external-callback",
                        "payload": {"ticket_id": "INC-42"},
                        "channel_id": "blocks-channel",
                        "provider_id": "blocks-provider",
                        "wait_mode": "connected_then_disconnected",
                        "hot_wait_timeout": 7.5,
                        "response_payload_schema": {"type": "object", "required": ["status"]},
                        "audit_metadata": {"case": "blocks-passthrough"},
                    },
                }
            ],
        }
    )
    execution = await Agently.blocks.bind_runtime(graph).async_start_execution(
        None,
        wait_for_result=False,
        record_store=tmp_path / "external-wait-metadata",
    )
    pending = execution.get_pending_interrupts()
    envelope = pending["external-callback"]["external_wait_request"]

    assert envelope["channel_id"] == "blocks-channel"
    assert envelope["provider_id"] == "blocks-provider"
    assert envelope["wait_mode"] == "connected_then_disconnected"
    assert envelope["hot_wait_timeout"] == 7.5
    assert envelope["response_payload_schema"] == {"type": "object", "required": ["status"]}
    assert envelope["audit_metadata"]["case"] == "blocks-passthrough"
    assert envelope["exchange_kind"] == "clarification"

    await execution.async_continue_with("external-callback", {"status": "ready"})
    await execution.async_close(timeout=5)
