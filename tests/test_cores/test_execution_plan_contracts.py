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

"""Serialization tests for the Blocks-plugin complex-task lifecycle contracts."""

from __future__ import annotations

import pytest

from agently.types.data import (
    BLOCKS_SCHEMA_VERSION,
    EXECUTION_BLOCK_KINDS,
    EXECUTION_PLAN_SCHEMA_VERSION,
    PLAN_BLOCK_INSTANCE_KINDS,
    PLAN_BLOCK_KINDS,
    REPLAN_STATUSES,
    STANDARD_BLOCK_SIGNALS,
    BlockCompileRequest,
    BlockSignal,
    CapabilityResolution,
    EvidenceEnvelope,
    EvidenceMapper,
    ExecutionBlock,
    ExecutionBlockEdge,
    ExecutionBlockGraph,
    ExecutionPlan,
    ExecutionPlanEdge,
    PlanBlock,
    PlanBlockInstance,
    ResultAdapter,
    ReplanSignal,
    SkillActivation,
    TaskFrame,
)


# --- TaskFrame --------------------------------------------------------------


def test_task_frame_minimal_defaults():
    frame = TaskFrame.from_value({"id": "f1", "objective": "do thing"})
    assert frame.id == "f1"
    assert frame.objective == "do thing"
    assert frame.parent_frame_id is None
    assert frame.candidate_plan_block_ids == ()
    assert frame.candidate_skill_ids == ()
    assert frame.preferred_execution_shape is None
    assert frame.schema_version == EXECUTION_PLAN_SCHEMA_VERSION


def test_task_frame_normalizes_and_round_trips():
    frame = TaskFrame.from_value(
        {
            "id": "  f2  ",
            "objective": "ship",
            "parent_frame_id": "root",
            "inputs": {"topic": "x"},
            "candidate_plan_block_ids": "browser.action_call",
            "candidate_skill_ids": "webapp-testing",
            "capability_intents": {"need": "browser"},
            "success_evidence": ["screenshot-captured"],
            "dependency_refs": ["e1", "e2"],
            "budget": {"max_model_requests": 3},
            "preferred_execution_shape": "dag",
        }
    )
    assert frame.id == "f2"
    assert frame.candidate_plan_block_ids == ("browser.action_call",)
    assert frame.candidate_skill_ids == ("webapp-testing",)
    assert frame.capability_intents == ({"need": "browser"},)
    assert frame.success_evidence == ({"value": "screenshot-captured"},)
    assert frame.dependency_refs == ("e1", "e2")
    assert frame.preferred_execution_shape == "dag"
    assert TaskFrame.from_value(frame.to_dict()).to_dict() == frame.to_dict()


def test_task_frame_requires_id_and_objective():
    with pytest.raises(ValueError, match="non-empty 'id'"):
        TaskFrame.from_value({"objective": "x"})
    with pytest.raises(ValueError, match="non-empty 'objective'"):
        TaskFrame.from_value({"id": "f"})


# --- PlanBlockInstance / ExecutionPlanEdge ---------------------------------


def test_plan_block_instance_normalizes_contracts():
    block = PlanBlockInstance.from_value(
        {
            "id": "pb1",
            "plan_block_id": "browser.action_call",
            "kind": "action_call",
            "intent": "capture rendered page",
            "bound_inputs": {"url": "https://example.test"},
            "dependency_refs": "ctx",
            "capability_requirements": {"need": "web_browse"},
            "runtime_preferences": {"binding": "browser_open"},
        }
    )
    assert block.plan_block_id == "browser.action_call"
    assert block.kind == "action_call"
    assert block.dependency_refs == ("ctx",)
    assert block.capability_requirements == ({"need": "web_browse"},)
    assert PlanBlockInstance.from_value(block.to_dict()).to_dict() == block.to_dict()


def test_plan_block_instance_requires_id_and_plan_block_id():
    with pytest.raises(ValueError, match="non-empty 'id'"):
        PlanBlockInstance.from_value({"plan_block_id": "model_request"})
    with pytest.raises(ValueError, match="non-empty 'plan_block_id'"):
        PlanBlockInstance.from_value({"id": "pb"})


def test_execution_plan_edge_accepts_common_aliases():
    edge = ExecutionPlanEdge.from_value({"from": "a", "to": "b"})
    assert edge.from_plan_block == "a"
    assert edge.to_plan_block == "b"
    assert edge.kind == "sequence"
    assert ExecutionPlanEdge.from_value(edge.to_dict()).to_dict() == edge.to_dict()


def test_execution_plan_edge_requires_endpoints():
    with pytest.raises(ValueError, match="non-empty 'from_plan_block'"):
        ExecutionPlanEdge.from_value({"to_plan_block": "b"})
    with pytest.raises(ValueError, match="non-empty 'to_plan_block'"):
        ExecutionPlanEdge.from_value({"from_plan_block": "a"})


# --- ExecutionPlan ----------------------------------------------------------


def test_execution_plan_contains_plan_blocks_not_runtime_blocks():
    plan = ExecutionPlan.from_value(
        {
            "plan_id": "p1",
            "task_frame_id": "f1",
            "plan_blocks": [
                {"id": "ctx", "plan_block_id": "skills.webapp.skill_activation", "kind": "skill_activation"},
                {"id": "draft", "plan_block_id": "model.structured", "kind": "model_request"},
                {"id": "shot", "plan_block_id": "browser.action_call", "kind": "action_call"},
            ],
            "edges": [
                {"from_plan_block": "ctx", "to_plan_block": "draft"},
                {"from_plan_block": "draft", "to_plan_block": "shot", "kind": "data"},
            ],
            "evidence_requirements": [{"capability_id": "browser", "kind": "action_succeeded"}],
            "result_contracts": [{"semantic_output": "screenshot_ref"}],
            "diagnostics": "compiled from selected skill guidance",
        }
    )
    assert [block.id for block in plan.plan_blocks] == ["ctx", "draft", "shot"]
    assert all(isinstance(block, PlanBlockInstance) for block in plan.plan_blocks)
    assert all(isinstance(edge, ExecutionPlanEdge) for edge in plan.edges)
    assert "blocks" not in plan.to_dict()
    assert plan.diagnostics == ({"value": "compiled from selected skill guidance"},)
    assert ExecutionPlan.from_value(plan.to_dict()).to_dict() == plan.to_dict()


def test_execution_plan_rejects_non_sequence_plan_blocks():
    with pytest.raises(TypeError, match="'plan_blocks' must be a list/tuple"):
        ExecutionPlan.from_value({"plan_id": "p", "plan_blocks": {"id": "n"}})


def test_execution_plan_requires_plan_id():
    with pytest.raises(ValueError, match="non-empty 'plan_id'"):
        ExecutionPlan.from_value({"plan_blocks": []})


# --- CapabilityResolution / SkillActivation --------------------------------


def test_capability_resolution_round_trip():
    resolution = CapabilityResolution.from_value(
        {
            "allowed_capabilities": ["search"],
            "denied_capabilities": "fs.write",
            "pending_approvals": [{"capability_id": "deploy"}],
            "scoped_action_candidates": {"action_id": "open_browser"},
        }
    )
    assert resolution.allowed_capabilities == ("search",)
    assert resolution.denied_capabilities == ("fs.write",)
    assert resolution.scoped_action_candidates == ({"action_id": "open_browser"},)
    assert CapabilityResolution.from_value(resolution.to_dict()).to_dict() == resolution.to_dict()


def test_skill_activation_records_plan_block_recommendations_without_execution():
    activation = SkillActivation.from_value(
        {
            "skill_id": "webapp-testing",
            "loaded_guidance_refs": "webapp-testing:SKILL.md",
            "capability_needs": {"need": "web_browse"},
            "action_candidate_specs": {"capability": "web_browse", "grants": False},
            "plan_block_recommendations": {"plan_block_id": "browser.action_call"},
        }
    )
    assert activation.loaded_guidance_refs == ("webapp-testing:SKILL.md",)
    assert activation.plan_block_recommendations == ({"plan_block_id": "browser.action_call"},)
    assert activation.action_candidate_specs == ({"capability": "web_browse", "grants": False},)
    assert SkillActivation.from_value(activation.to_dict()).to_dict() == activation.to_dict()


# --- Blocks contracts -------------------------------------------------------


def test_plan_block_round_trip_keeps_capability_source_projection():
    block = PlanBlock.from_value(
        {
            "id": "browser.action_call",
            "kind": "action_call",
            "planner_summary": "Open a browser or capture a screenshot.",
            "capability_requirements": {"need": "web_browse"},
            "runtime_binding_options": {"execution_block_id": "browser.open"},
            "source_refs": "action:browser_open",
        }
    )
    assert block.kind == "action_call"
    assert block.source_refs == ("action:browser_open",)
    assert block.capability_requirements == ({"need": "web_browse"},)
    assert PlanBlock.from_value(block.to_dict()).to_dict() == block.to_dict()


def test_execution_block_round_trip_separates_runtime_binding():
    block = ExecutionBlock.from_value(
        {
            "id": "browser.open",
            "kind": "action_call",
            "composition": "composite",
            "child_blocks": ["browser.invoke", "browser.readback"],
            "source_plan_block_id": "shot",
            "resource_requirements": {"resource": "browser"},
            "signal_contract": {"emits": ["block.started", "block.completed"]},
        }
    )
    assert block.composition == "composite"
    assert block.child_blocks == ("browser.invoke", "browser.readback")
    assert block.source_plan_block_id == "shot"
    assert ExecutionBlock.from_value(block.to_dict()).to_dict() == block.to_dict()


def test_execution_block_rejects_unknown_composition():
    with pytest.raises(ValueError, match="'composition' must be one of"):
        ExecutionBlock.from_value({"id": "b", "kind": "action_call", "composition": "loop"})


def test_execution_block_edge_accepts_aliases():
    edge = ExecutionBlockEdge.from_value({"from": "a", "to": "b", "kind": "data"})
    assert edge.from_execution_block == "a"
    assert edge.to_execution_block == "b"
    assert edge.kind == "data"
    assert ExecutionBlockEdge.from_value(edge.to_dict()).to_dict() == edge.to_dict()


def test_block_signal_requires_standard_signal():
    signal = BlockSignal.from_value(
        {
            "signal": "block.completed",
            "execution_id": "e1",
            "task_frame_id": "f1",
            "plan_id": "p1",
            "source_plan_block_id": "shot",
            "execution_block_id": "browser.open",
            "correlation_id": "c1",
        }
    )
    assert signal.signal == "block.completed"
    assert BlockSignal.from_value(signal.to_dict()).to_dict() == signal.to_dict()
    with pytest.raises(ValueError, match="'signal' must be one of"):
        BlockSignal.from_value({"signal": "task.accepted"})


def test_execution_block_graph_round_trip():
    graph = ExecutionBlockGraph.from_value(
        {
            "graph_id": "g1",
            "source_plan_id": "p1",
            "execution_blocks": [{"id": "browser.open", "kind": "action_call"}],
            "edges": [{"from": "browser.open", "to": "browser.readback"}],
            "signals": [{"signal": "block.completed", "execution_block_id": "browser.open"}],
            "start_blocks": "browser.open",
            "terminal_blocks": "browser.open",
            "evidence_mappers": [{"id": "map-browser", "source_block_ids": "browser.open"}],
            "result_adapters": [{"id": "result-browser", "source_block_ids": "browser.open"}],
        }
    )
    assert graph.schema_version == BLOCKS_SCHEMA_VERSION
    assert isinstance(graph.execution_blocks[0], ExecutionBlock)
    assert isinstance(graph.edges[0], ExecutionBlockEdge)
    assert isinstance(graph.signals[0], BlockSignal)
    assert isinstance(graph.evidence_mappers[0], EvidenceMapper)
    assert isinstance(graph.result_adapters[0], ResultAdapter)
    assert ExecutionBlockGraph.from_value(graph.to_dict()).to_dict() == graph.to_dict()


def test_block_compile_request_round_trip():
    request = BlockCompileRequest.from_value(
        {
            "execution_id": "e1",
            "task_frame_id": "f1",
            "plan_id": "p1",
            "plan_blocks": [{"id": "shot", "plan_block_id": "browser.action_call"}],
            "edges": [{"from": "ctx", "to": "shot"}],
            "capability_resolution": {"allowed_capabilities": ["web_browse"]},
            "evidence_requirements": {"kind": "action_succeeded"},
            "result_contracts": {"semantic_output": "screenshot_ref"},
        }
    )
    assert isinstance(request.plan_blocks[0], PlanBlockInstance)
    assert isinstance(request.edges[0], ExecutionPlanEdge)
    assert request.capability_resolution is not None
    assert request.capability_resolution.allowed_capabilities == ("web_browse",)
    assert BlockCompileRequest.from_value(request.to_dict()).to_dict() == request.to_dict()


# --- EvidenceEnvelope / ReplanSignal ---------------------------------------


def test_evidence_envelope_separates_runtime_plan_action_and_skill_evidence():
    envelope = EvidenceEnvelope.from_value(
        {
            "task_frame_id": "f1",
            "plan_id": "p1",
            "execution_block_results": [{"execution_block_id": "browser.open", "success": True}],
            "plan_block_results": [{"plan_block_id": "shot", "output_ref": "ws://out/1"}],
            "action_evidence": [{"action_call_id": "ac-1", "success": True}],
            "skill_evidence": [{"skill_id": "webapp-testing", "loaded_guidance": True}],
            "artifact_refs": "ws://artifact/1",
        }
    )
    assert envelope.execution_block_results == ({"execution_block_id": "browser.open", "success": True},)
    assert envelope.plan_block_results == ({"plan_block_id": "shot", "output_ref": "ws://out/1"},)
    assert envelope.action_evidence == ({"action_call_id": "ac-1", "success": True},)
    assert envelope.skill_evidence == ({"skill_id": "webapp-testing", "loaded_guidance": True},)
    assert envelope.artifact_refs == ("ws://artifact/1",)
    assert {item["kind"] for item in envelope.evidence_items}.issuperset(
        {"execution_block", "plan_block", "action", "skill_context", "artifact_ref"}
    )
    assert all(item["id"] for item in envelope.evidence_items)
    assert all(item["status"] in {"ok", "failed", "empty"} for item in envelope.evidence_items)
    assert all(item["body_state"] in {"full", "bounded", "truncated", "ref_only"} for item in envelope.evidence_items)
    assert EvidenceEnvelope.from_value(envelope.to_dict()).to_dict() == envelope.to_dict()


def test_evidence_envelope_derives_legacy_buckets_from_canonical_items():
    envelope = EvidenceEnvelope.from_value(
        {
            "evidence_items": [
                {
                    "id": "quote.failed",
                    "kind": "action_evidence",
                    "status": "failed",
                    "body_state": "bounded",
                    "action_id": "quote",
                    "diagnostics": [{"message": "provider unavailable"}],
                },
                {
                    "id": "repo.path",
                    "kind": "locator_ref",
                    "status": "ok",
                    "body_state": "ref_only",
                    "path": "src/app.py",
                },
            ]
        }
    )

    assert envelope.action_evidence[0]["id"] == "quote.failed"
    assert envelope.action_evidence[0]["status"] == "failed"
    assert envelope.workspace_refs == ("src/app.py",)
    assert EvidenceEnvelope.from_value({"diagnostics": [{"message": "boom"}]}).evidence_items[0]["status"] == "failed"


@pytest.mark.parametrize("status", sorted(REPLAN_STATUSES))
def test_replan_signal_accepts_every_known_status(status):
    signal = ReplanSignal.from_value({"status": status, "reason": "because"})
    assert signal.status == status
    assert ReplanSignal.from_value(signal.to_dict()).to_dict() == signal.to_dict()


def test_replan_signal_names_affected_plan_and_execution_blocks():
    signal = ReplanSignal.from_value(
        {
            "status": "replan_segment",
            "affected_plan_block_ids": ["pb2", "pb3"],
            "affected_execution_block_ids": ["eb2", "eb3"],
            "reusable_output_refs": "ws://out/1",
            "missing_capabilities": ["fs.write"],
        }
    )
    assert signal.affected_plan_block_ids == ("pb2", "pb3")
    assert signal.affected_execution_block_ids == ("eb2", "eb3")
    assert signal.reusable_output_refs == ("ws://out/1",)
    assert signal.missing_capabilities == ("fs.write",)


def test_replan_signal_rejects_unknown_status():
    with pytest.raises(ValueError, match="must be one of"):
        ReplanSignal.from_value({"status": "abort"})
    with pytest.raises(ValueError, match="non-empty 'status'"):
        ReplanSignal.from_value({})


# --- Cross-contract schema and normalization guarantees ---------------------


@pytest.mark.parametrize(
    ("contract", "payload", "schema_version"),
    [
        (TaskFrame, {"id": "f", "objective": "o"}, EXECUTION_PLAN_SCHEMA_VERSION),
        (ExecutionPlan, {"plan_id": "p"}, EXECUTION_PLAN_SCHEMA_VERSION),
        (CapabilityResolution, {}, EXECUTION_PLAN_SCHEMA_VERSION),
        (SkillActivation, {"skill_id": "s"}, EXECUTION_PLAN_SCHEMA_VERSION),
        (EvidenceEnvelope, {}, EXECUTION_PLAN_SCHEMA_VERSION),
        (ReplanSignal, {"status": "continue"}, EXECUTION_PLAN_SCHEMA_VERSION),
        (PlanBlock, {"id": "pb", "kind": "model_request"}, BLOCKS_SCHEMA_VERSION),
        (ExecutionBlock, {"id": "eb", "kind": "model_request"}, BLOCKS_SCHEMA_VERSION),
        (ExecutionBlockGraph, {"graph_id": "g"}, BLOCKS_SCHEMA_VERSION),
        (BlockCompileRequest, {}, BLOCKS_SCHEMA_VERSION),
    ],
)
def test_schema_version_defaults_and_survives_round_trip(contract, payload, schema_version):
    instance = contract.from_value(payload)
    assert instance.schema_version == schema_version
    bumped = contract.from_value({**payload, "schema_version": "future/v9"})
    assert bumped.schema_version == "future/v9"


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (TaskFrame, {"id": "f", "objective": "o"}),
        (PlanBlockInstance, {"id": "n", "plan_block_id": "model_request"}),
        (ExecutionPlanEdge, {"from": "a", "to": "b"}),
        (ExecutionPlan, {"plan_id": "p"}),
        (CapabilityResolution, {}),
        (SkillActivation, {"skill_id": "s"}),
        (EvidenceEnvelope, {}),
        (ReplanSignal, {"status": "continue"}),
        (PlanBlock, {"id": "pb", "kind": "model_request"}),
        (ExecutionBlock, {"id": "eb", "kind": "model_request"}),
        (ExecutionBlockEdge, {"from": "a", "to": "b"}),
        (BlockSignal, {"signal": "block.completed"}),
        (ResultAdapter, {"id": "ra"}),
        (EvidenceMapper, {"id": "em"}),
        (ExecutionBlockGraph, {"graph_id": "g"}),
        (BlockCompileRequest, {}),
    ],
)
def test_from_value_rejects_non_mapping(contract, payload):
    with pytest.raises(TypeError):
        contract.from_value(["not", "a", "mapping"])
    instance = contract.from_value(payload)
    assert contract.from_value(instance) is instance


def test_kind_and_signal_constants_are_complete():
    assert PLAN_BLOCK_INSTANCE_KINDS == PLAN_BLOCK_KINDS
    assert "skill_activation" in PLAN_BLOCK_KINDS
    assert "dag_segment" in PLAN_BLOCK_KINDS
    assert "agent_step" in PLAN_BLOCK_KINDS
    assert len(PLAN_BLOCK_KINDS) == 14
    assert "skill_activation" in EXECUTION_BLOCK_KINDS
    assert "dag_node" in EXECUTION_BLOCK_KINDS
    assert "snapshot" in EXECUTION_BLOCK_KINDS
    assert "agent_step" in EXECUTION_BLOCK_KINDS
    assert len(EXECUTION_BLOCK_KINDS) == 14
    assert "block.replan_requested" in STANDARD_BLOCK_SIGNALS
    assert "block.completed" in STANDARD_BLOCK_SIGNALS
    assert len(STANDARD_BLOCK_SIGNALS) == 10
