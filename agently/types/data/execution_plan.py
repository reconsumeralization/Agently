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

"""Internal complex-task execution plan contracts.

These contracts implement Slice 1 from
`spec/implemented/architecture/COMPLEX_TASK_EXECUTION_LIFECYCLE_BLOCKS_PLUGIN_SPEC.md`.
They model the AgentTaskLoop-facing plan and evidence layer only:

    TaskFrame            -> one progressively resolved semantic unit
    PlanBlockInstance    -> one selected occurrence of a planner-facing PlanBlock
    ExecutionPlan        -> bounded plan data for one TaskFrame
    CapabilityResolution -> needs mapped to allow/deny/pending/scoped candidates
    SkillActivation      -> progressive Skill context and capability needs
    EvidenceEnvelope     -> compact evidence for verifier and host guards
    ReplanSignal         -> structured early repair/replan signal

ExecutionBlock and ExecutionBlockGraph live in `blocks.py`. Keeping them out of
ExecutionPlan preserves the final architecture boundary: the Blocks plugin
lowers PlanBlock instances into TriggerFlow-backed runtime blocks, while
AgentTaskLoop remains the lifecycle owner.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


EXECUTION_PLAN_SCHEMA_VERSION = "execution_plan/v1"

PlanBlockInstanceKind: TypeAlias = Literal[
    "model_request",
    "action_call",
    "mcp_tool_call",
    "script_action",
    "workspace_operation",
    "skill_activation",
    "approval_wait",
    "external_wait",
    "validation",
    "observation",
    "dag_segment",
    "flow_segment",
    "emit",
    "agent_step",
]

PLAN_BLOCK_INSTANCE_KINDS: frozenset[str] = frozenset(
    {
        "model_request",
        "action_call",
        "mcp_tool_call",
        "script_action",
        "workspace_operation",
        "skill_activation",
        "approval_wait",
        "external_wait",
        "validation",
        "observation",
        "dag_segment",
        "flow_segment",
        "emit",
        "agent_step",
    }
)

PreferredExecutionShape: TypeAlias = Literal["direct", "dag", "triggerflow", "agent_step"]
PREFERRED_EXECUTION_SHAPES: frozenset[str] = frozenset({"direct", "dag", "triggerflow", "agent_step"})

ReplanStatus: TypeAlias = Literal[
    "continue",
    "repair",
    "replan_segment",
    "replan_goal",
    "blocked",
    "clarify",
]
REPLAN_STATUSES: frozenset[str] = frozenset(
    {"continue", "repair", "replan_segment", "replan_goal", "blocked", "clarify"}
)


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, Mapping):
        raise TypeError(f"Expected a string or sequence of strings, got mapping: { value }.")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Expected a mapping, got: { type(value) }.")


def _mapping_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        value = (value,)
    return tuple(dict(item) if isinstance(item, Mapping) else {"value": item} for item in value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


@dataclass(frozen=True)
class TaskFrame:
    """One progressively resolved semantic unit before plan compilation.

    A TaskFrame is not an execution engine. It may compile to one PlanBlock
    instance, a DAG-shaped segment, a TriggerFlow wait segment, or a bounded
    child Agent step. It must not start an unrestricted nested AgentTaskLoop.
    """

    id: str
    objective: str
    parent_frame_id: str | None = None
    inputs: Any = field(default_factory=dict)
    expected_output_schema: Any = None
    success_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    candidate_plan_block_ids: tuple[str, ...] = field(default_factory=tuple)
    candidate_skill_ids: tuple[str, ...] = field(default_factory=tuple)
    capability_intents: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    dependency_refs: tuple[str, ...] = field(default_factory=tuple)
    budget: Mapping[str, Any] = field(default_factory=dict)
    risk_profile: Mapping[str, Any] = field(default_factory=dict)
    preferred_execution_shape: PreferredExecutionShape | None = None
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "TaskFrame | Mapping[str, Any]") -> "TaskFrame":
        if isinstance(value, TaskFrame):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"TaskFrame must be a mapping or TaskFrame, got: { type(value) }.")
        frame_id = value.get("id")
        if frame_id is None or not str(frame_id).strip():
            raise ValueError("TaskFrame requires non-empty 'id'.")
        objective = value.get("objective")
        if objective is None or not str(objective).strip():
            raise ValueError("TaskFrame requires non-empty 'objective'.")
        return cls(
            id=str(frame_id).strip(),
            objective=str(objective).strip(),
            parent_frame_id=_optional_str(value.get("parent_frame_id")),
            inputs=value.get("inputs", {}),
            expected_output_schema=value.get("expected_output_schema"),
            success_evidence=_mapping_tuple(value.get("success_evidence")),
            candidate_plan_block_ids=_str_tuple(value.get("candidate_plan_block_ids")),
            candidate_skill_ids=_str_tuple(value.get("candidate_skill_ids")),
            capability_intents=_mapping_tuple(value.get("capability_intents")),
            dependency_refs=_str_tuple(value.get("dependency_refs")),
            budget=_mapping(value.get("budget")),
            risk_profile=_mapping(value.get("risk_profile")),
            preferred_execution_shape=_optional_str(value.get("preferred_execution_shape")),  # type: ignore[arg-type]
            schema_version=str(value.get("schema_version") or EXECUTION_PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "parent_frame_id": self.parent_frame_id,
            "inputs": self.inputs,
            "expected_output_schema": self.expected_output_schema,
            "success_evidence": [dict(item) for item in self.success_evidence],
            "candidate_plan_block_ids": list(self.candidate_plan_block_ids),
            "candidate_skill_ids": list(self.candidate_skill_ids),
            "capability_intents": [dict(item) for item in self.capability_intents],
            "dependency_refs": list(self.dependency_refs),
            "budget": dict(self.budget),
            "risk_profile": dict(self.risk_profile),
            "preferred_execution_shape": self.preferred_execution_shape,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PlanBlockInstance:
    """One selected occurrence of a PlanBlock inside an ExecutionPlan.

    This is planner data only. It references a planner-facing PlanBlock and
    records bound inputs, dependencies, evidence requirements, output contract,
    runtime preferences, and budget. It does not execute and cannot grant
    capability or accept task completion.
    """

    id: str
    plan_block_id: str
    kind: PlanBlockInstanceKind | str | None = None
    intent: str | None = None
    bound_inputs: Any = field(default_factory=dict)
    dependency_refs: tuple[str, ...] = field(default_factory=tuple)
    capability_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    output_contract: Mapping[str, Any] = field(default_factory=dict)
    evidence_contract: Mapping[str, Any] = field(default_factory=dict)
    runtime_preferences: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: "PlanBlockInstance | Mapping[str, Any]") -> "PlanBlockInstance":
        if isinstance(value, PlanBlockInstance):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"PlanBlockInstance must be a mapping or PlanBlockInstance, got: { type(value) }."
            )
        instance_id = value.get("id")
        if instance_id is None or not str(instance_id).strip():
            raise ValueError("PlanBlockInstance requires non-empty 'id'.")
        plan_block_id = value.get("plan_block_id", value.get("block_id"))
        if plan_block_id is None or not str(plan_block_id).strip():
            raise ValueError("PlanBlockInstance requires non-empty 'plan_block_id'.")
        return cls(
            id=str(instance_id).strip(),
            plan_block_id=str(plan_block_id).strip(),
            kind=_optional_str(value.get("kind")),
            intent=_optional_str(value.get("intent")),
            bound_inputs=value.get("bound_inputs", value.get("inputs", {})),
            dependency_refs=_str_tuple(value.get("dependency_refs")),
            capability_requirements=_mapping_tuple(value.get("capability_requirements")),
            output_contract=_mapping(value.get("output_contract")),
            evidence_contract=_mapping(value.get("evidence_contract")),
            runtime_preferences=_mapping(value.get("runtime_preferences")),
            budget=_mapping(value.get("budget")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_block_id": self.plan_block_id,
            "kind": self.kind,
            "intent": self.intent,
            "bound_inputs": self.bound_inputs,
            "dependency_refs": list(self.dependency_refs),
            "capability_requirements": [dict(item) for item in self.capability_requirements],
            "output_contract": dict(self.output_contract),
            "evidence_contract": dict(self.evidence_contract),
            "runtime_preferences": dict(self.runtime_preferences),
            "budget": dict(self.budget),
        }


@dataclass(frozen=True)
class ExecutionPlanEdge:
    """Dependency/data wiring between PlanBlockInstance objects."""

    from_plan_block: str
    to_plan_block: str
    kind: str = "sequence"
    condition: Any = None
    binding: Any = None

    @classmethod
    def from_value(cls, value: "ExecutionPlanEdge | Mapping[str, Any]") -> "ExecutionPlanEdge":
        if isinstance(value, ExecutionPlanEdge):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"ExecutionPlanEdge must be a mapping or ExecutionPlanEdge, got: { type(value) }."
            )
        from_block = value.get("from_plan_block", value.get("from_block", value.get("from")))
        to_block = value.get("to_plan_block", value.get("to_block", value.get("to")))
        if from_block is None or not str(from_block).strip():
            raise ValueError("ExecutionPlanEdge requires non-empty 'from_plan_block'.")
        if to_block is None or not str(to_block).strip():
            raise ValueError("ExecutionPlanEdge requires non-empty 'to_plan_block'.")
        return cls(
            from_plan_block=str(from_block).strip(),
            to_plan_block=str(to_block).strip(),
            kind=str(value.get("kind", "sequence")).strip() or "sequence",
            condition=value.get("condition"),
            binding=value.get("binding"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_plan_block": self.from_plan_block,
            "to_plan_block": self.to_plan_block,
            "kind": self.kind,
            "condition": self.condition,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    """Normalized internal plan for one TaskFrame.

    ExecutionPlan contains selected PlanBlock instances, dependencies, semantic
    outputs, evidence requirements, result contracts, and policies. It is not a
    TaskDAG replacement, a TriggerFlow public syntax, or a runtime block graph.
    """

    plan_id: str
    task_frame_id: str | None = None
    plan_blocks: tuple[PlanBlockInstance, ...] = field(default_factory=tuple)
    edges: tuple[ExecutionPlanEdge, ...] = field(default_factory=tuple)
    semantic_outputs: Mapping[str, Any] = field(default_factory=dict)
    evidence_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    result_contracts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    failure_policy: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_policy: Mapping[str, Any] = field(default_factory=dict)
    replan_policy: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "ExecutionPlan | Mapping[str, Any]") -> "ExecutionPlan":
        if isinstance(value, ExecutionPlan):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"ExecutionPlan must be a mapping or ExecutionPlan, got: { type(value) }.")
        plan_id = value.get("plan_id")
        if plan_id is None or not str(plan_id).strip():
            raise ValueError("ExecutionPlan requires non-empty 'plan_id'.")
        raw_plan_blocks = value.get("plan_blocks", value.get("blocks", ()))
        if raw_plan_blocks is None:
            raw_plan_blocks = ()
        if not isinstance(raw_plan_blocks, list | tuple):
            raise TypeError(
                f"ExecutionPlan 'plan_blocks' must be a list/tuple, got: { type(raw_plan_blocks) }."
            )
        raw_edges = value.get("edges", ())
        if raw_edges is None:
            raw_edges = ()
        if not isinstance(raw_edges, list | tuple):
            raise TypeError(f"ExecutionPlan 'edges' must be a list/tuple, got: { type(raw_edges) }.")
        return cls(
            plan_id=str(plan_id).strip(),
            task_frame_id=_optional_str(value.get("task_frame_id")),
            plan_blocks=tuple(PlanBlockInstance.from_value(block) for block in raw_plan_blocks),
            edges=tuple(ExecutionPlanEdge.from_value(edge) for edge in raw_edges),
            semantic_outputs=_mapping(value.get("semantic_outputs")),
            evidence_requirements=_mapping_tuple(value.get("evidence_requirements")),
            result_contracts=_mapping_tuple(value.get("result_contracts")),
            failure_policy=_mapping(value.get("failure_policy")),
            checkpoint_policy=_mapping(value.get("checkpoint_policy")),
            replan_policy=_mapping(value.get("replan_policy")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
            schema_version=str(value.get("schema_version") or EXECUTION_PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "task_frame_id": self.task_frame_id,
            "plan_blocks": [block.to_dict() for block in self.plan_blocks],
            "edges": [edge.to_dict() for edge in self.edges],
            "semantic_outputs": dict(self.semantic_outputs),
            "evidence_requirements": [dict(item) for item in self.evidence_requirements],
            "result_contracts": [dict(item) for item in self.result_contracts],
            "failure_policy": dict(self.failure_policy),
            "checkpoint_policy": dict(self.checkpoint_policy),
            "replan_policy": dict(self.replan_policy),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SkillActivation:
    """Result of loading selected Skill guidance before or during planning."""

    skill_id: str
    source: Mapping[str, Any] = field(default_factory=dict)
    loaded_guidance_refs: tuple[str, ...] = field(default_factory=tuple)
    selected_resource_refs: tuple[str, ...] = field(default_factory=tuple)
    capability_needs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    action_candidate_specs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    plan_block_recommendations: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    citations: tuple[str, ...] = field(default_factory=tuple)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "SkillActivation | Mapping[str, Any]") -> "SkillActivation":
        if isinstance(value, SkillActivation):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"SkillActivation must be a mapping or SkillActivation, got: { type(value) }.")
        skill_id = value.get("skill_id")
        if skill_id is None or not str(skill_id).strip():
            raise ValueError("SkillActivation requires non-empty 'skill_id'.")
        return cls(
            skill_id=str(skill_id).strip(),
            source=_mapping(value.get("source")),
            loaded_guidance_refs=_str_tuple(value.get("loaded_guidance_refs")),
            selected_resource_refs=_str_tuple(value.get("selected_resource_refs")),
            capability_needs=_mapping_tuple(value.get("capability_needs")),
            action_candidate_specs=_mapping_tuple(value.get("action_candidate_specs")),
            plan_block_recommendations=_mapping_tuple(value.get("plan_block_recommendations")),
            citations=_str_tuple(value.get("citations")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
            schema_version=str(value.get("schema_version") or EXECUTION_PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "source": dict(self.source),
            "loaded_guidance_refs": list(self.loaded_guidance_refs),
            "selected_resource_refs": list(self.selected_resource_refs),
            "capability_needs": [dict(item) for item in self.capability_needs],
            "action_candidate_specs": [dict(item) for item in self.action_candidate_specs],
            "plan_block_recommendations": [dict(item) for item in self.plan_block_recommendations],
            "citations": list(self.citations),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CapabilityResolution:
    """Capability needs mapped to one policy result envelope."""

    allowed_capabilities: tuple[str, ...] = field(default_factory=tuple)
    denied_capabilities: tuple[str, ...] = field(default_factory=tuple)
    pending_approvals: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    scoped_action_candidates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    execution_resource_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    workspace_boundaries: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "CapabilityResolution | Mapping[str, Any]") -> "CapabilityResolution":
        if isinstance(value, CapabilityResolution):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"CapabilityResolution must be a mapping or CapabilityResolution, got: { type(value) }."
            )
        return cls(
            allowed_capabilities=_str_tuple(value.get("allowed_capabilities")),
            denied_capabilities=_str_tuple(value.get("denied_capabilities")),
            pending_approvals=_mapping_tuple(value.get("pending_approvals")),
            scoped_action_candidates=_mapping_tuple(value.get("scoped_action_candidates")),
            execution_resource_requirements=_mapping_tuple(value.get("execution_resource_requirements")),
            workspace_boundaries=_mapping(value.get("workspace_boundaries")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
            schema_version=str(value.get("schema_version") or EXECUTION_PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_capabilities": list(self.allowed_capabilities),
            "denied_capabilities": list(self.denied_capabilities),
            "pending_approvals": [dict(item) for item in self.pending_approvals],
            "scoped_action_candidates": [dict(item) for item in self.scoped_action_candidates],
            "execution_resource_requirements": [dict(item) for item in self.execution_resource_requirements],
            "workspace_boundaries": dict(self.workspace_boundaries),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Compact evidence package for observation, verifier, host guards, and resume."""

    task_frame_id: str | None = None
    plan_id: str | None = None
    execution_block_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    plan_block_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    semantic_outputs: Mapping[str, Any] = field(default_factory=dict)
    action_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    skill_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    capability_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    workspace_refs: tuple[str, ...] = field(default_factory=tuple)
    artifact_refs: tuple[str, ...] = field(default_factory=tuple)
    runtime_event_refs: tuple[str, ...] = field(default_factory=tuple)
    validation_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "EvidenceEnvelope | Mapping[str, Any]") -> "EvidenceEnvelope":
        if isinstance(value, EvidenceEnvelope):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"EvidenceEnvelope must be a mapping or EvidenceEnvelope, got: { type(value) }.")
        return cls(
            task_frame_id=_optional_str(value.get("task_frame_id")),
            plan_id=_optional_str(value.get("plan_id")),
            execution_block_results=_mapping_tuple(
                value.get("execution_block_results", value.get("block_results"))
            ),
            plan_block_results=_mapping_tuple(value.get("plan_block_results")),
            semantic_outputs=_mapping(value.get("semantic_outputs")),
            action_evidence=_mapping_tuple(value.get("action_evidence")),
            skill_evidence=_mapping_tuple(value.get("skill_evidence")),
            capability_evidence=_mapping_tuple(value.get("capability_evidence")),
            workspace_refs=_str_tuple(value.get("workspace_refs")),
            artifact_refs=_str_tuple(value.get("artifact_refs")),
            runtime_event_refs=_str_tuple(value.get("runtime_event_refs")),
            validation_results=_mapping_tuple(value.get("validation_results")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
            schema_version=str(value.get("schema_version") or EXECUTION_PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_frame_id": self.task_frame_id,
            "plan_id": self.plan_id,
            "execution_block_results": [dict(item) for item in self.execution_block_results],
            "plan_block_results": [dict(item) for item in self.plan_block_results],
            "semantic_outputs": dict(self.semantic_outputs),
            "action_evidence": [dict(item) for item in self.action_evidence],
            "skill_evidence": [dict(item) for item in self.skill_evidence],
            "capability_evidence": [dict(item) for item in self.capability_evidence],
            "workspace_refs": list(self.workspace_refs),
            "artifact_refs": list(self.artifact_refs),
            "runtime_event_refs": list(self.runtime_event_refs),
            "validation_results": [dict(item) for item in self.validation_results],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ReplanSignal:
    """Structured early repair/replan control signal.

    Blocks, TriggerFlow diagnostics, validators, and verifier checkpoints can
    produce this signal. AgentTaskLoop owns the decision to continue, repair,
    replace bindings, regenerate a segment, re-enter goal planning, block, or
    clarify.
    """

    status: ReplanStatus
    reason: str | None = None
    affected_plan_block_ids: tuple[str, ...] = field(default_factory=tuple)
    affected_execution_block_ids: tuple[str, ...] = field(default_factory=tuple)
    reusable_output_refs: tuple[str, ...] = field(default_factory=tuple)
    invalidated_output_refs: tuple[str, ...] = field(default_factory=tuple)
    missing_capabilities: tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    budget_impact: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = EXECUTION_PLAN_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "ReplanSignal | Mapping[str, Any]") -> "ReplanSignal":
        if isinstance(value, ReplanSignal):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"ReplanSignal must be a mapping or ReplanSignal, got: { type(value) }.")
        status = value.get("status")
        if status is None or not str(status).strip():
            raise ValueError("ReplanSignal requires non-empty 'status'.")
        if str(status).strip() not in REPLAN_STATUSES:
            raise ValueError(
                f"ReplanSignal 'status' must be one of { sorted(REPLAN_STATUSES) }, got: { status }."
            )
        return cls(
            status=str(status).strip(),  # type: ignore[arg-type]
            reason=str(value["reason"]) if value.get("reason") is not None else None,
            affected_plan_block_ids=_str_tuple(value.get("affected_plan_block_ids")),
            affected_execution_block_ids=_str_tuple(
                value.get("affected_execution_block_ids", value.get("affected_block_ids"))
            ),
            reusable_output_refs=_str_tuple(value.get("reusable_output_refs")),
            invalidated_output_refs=_str_tuple(value.get("invalidated_output_refs")),
            missing_capabilities=_str_tuple(value.get("missing_capabilities")),
            evidence_refs=_str_tuple(value.get("evidence_refs")),
            budget_impact=_mapping(value.get("budget_impact")),
            schema_version=str(value.get("schema_version") or EXECUTION_PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "affected_plan_block_ids": list(self.affected_plan_block_ids),
            "affected_execution_block_ids": list(self.affected_execution_block_ids),
            "reusable_output_refs": list(self.reusable_output_refs),
            "invalidated_output_refs": list(self.invalidated_output_refs),
            "missing_capabilities": list(self.missing_capabilities),
            "evidence_refs": list(self.evidence_refs),
            "budget_impact": dict(self.budget_impact),
            "schema_version": self.schema_version,
        }
