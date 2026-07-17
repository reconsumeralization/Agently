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
They model the AgentTask-facing plan and evidence layer only:

    TaskFrame            -> one progressively resolved semantic unit
    PlanBlockInstance    -> one selected occurrence of a planner-facing PlanBlock
    ExecutionPlan        -> bounded plan data for one TaskFrame
    CapabilityResolution -> needs mapped to allow/deny/pending/scoped candidates
    EvidenceEnvelope     -> compact evidence for verifier and host guards
    ReplanSignal         -> structured early repair/replan signal

ExecutionBlock and ExecutionBlockGraph live in `blocks.py`. Keeping them out of
ExecutionPlan preserves the final architecture boundary: the Blocks plugin
lowers PlanBlock instances into TriggerFlow-backed runtime blocks, while
AgentTask remains the lifecycle owner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


EXECUTION_PLAN_SCHEMA_VERSION = "execution_plan/v1"

PlanBlockInstanceKind: TypeAlias = Literal[
    "model_request",
    "action_call",
    "mcp_tool_call",
    "script_action",
    "context_read",
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
        "context_read",
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


EvidenceItemStatus: TypeAlias = Literal["ok", "failed", "empty"]
EVIDENCE_ITEM_STATUSES: frozenset[str] = frozenset({"ok", "failed", "empty"})
EvidenceBodyState: TypeAlias = Literal["full", "bounded", "truncated", "ref_only"]
EVIDENCE_BODY_STATES: frozenset[str] = frozenset({"full", "bounded", "truncated", "ref_only"})


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


def _evidence_item_status(value: Any) -> EvidenceItemStatus:
    text = str(value or "").strip().lower()
    if text in {"ok", "success", "succeeded", "completed", "complete", "partial_success", "read"}:
        return "ok"
    if text in {"empty", "not_found", "no_results", "missing", "unavailable"}:
        return "empty"
    if text in {"failed", "failure", "error", "timed_out", "timeout", "blocked", "denied"}:
        return "failed"
    if isinstance(value, bool):
        return "ok" if value else "failed"
    return "ok"


def _evidence_body_state(value: Any) -> EvidenceBodyState:
    text = str(value or "").strip().lower()
    if text in {"full", "complete", "read"}:
        return "full"
    if text in {"bounded", "bounded_readback_available", "bounded_preview_available", "content_read"}:
        return "bounded"
    if text in {"truncated", "partial"}:
        return "truncated"
    return "ref_only"


def _evidence_item_id(prefix: str, index: int, *parts: Any) -> str:
    tokens = [str(part or "").strip() for part in parts if str(part or "").strip()]
    if not tokens:
        tokens = [str(index)]
    raw = ":".join([prefix, *tokens])
    return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in raw)[:240]


def _normalize_evidence_item(value: Any, *, index: int = 0, default_kind: str = "evidence") -> dict[str, Any]:
    item = dict(value) if isinstance(value, Mapping) else {"body": value}
    kind = str(item.get("kind") or item.get("block_kind") or item.get("role") or default_kind).strip() or default_kind
    output = item.get("output")
    operation = item.get("operation")
    if operation is None and isinstance(output, Mapping):
        operation = output.get("operation")
    if kind == "context_read" and operation not in (None, ""):
        kind = f"context_read.{ str(operation).strip() }"
    raw_status = item.get("raw_status", item.get("status", item.get("success")))
    if raw_status is None and any(token in kind for token in ("diagnostic", "error", "failure", "failed")):
        raw_status = "failed"
    status = _evidence_item_status(item.get("status", raw_status))
    body_state = _evidence_body_state(
        item.get("body_state")
        or item.get("content_state")
        or item.get("readback_state")
        or ("truncated" if item.get("truncated") is True else None)
        or ("bounded" if any(key in item for key in ("body", "content", "preview", "text", "snippet")) else "ref_only")
    )
    evidence_id = str(item.get("id") or item.get("evidence_id") or "").strip()
    if not evidence_id:
        evidence_id = _evidence_item_id(
            kind,
            index,
            item.get("execution_block_id"),
            item.get("source_plan_block_id"),
            item.get("action_call_id"),
            item.get("record_id"),
            item.get("path"),
            item.get("artifact_id"),
        )
    provenance = item.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {
            key: item.get(key)
            for key in (
                "execution_block_id",
                "source_plan_block_id",
                "source_task_dag_node_id",
                "action_call_id",
                "record_id",
                "path",
                "source",
            )
            if item.get(key) not in (None, "")
        }
    supports = item.get("supports")
    if not isinstance(supports, Mapping):
        supports = {
            "content": status == "ok" and body_state in {"full", "bounded", "truncated"},
            "unavailability": status in {"failed", "empty"},
            "ref_pointer": status == "ok" and body_state == "ref_only",
        }
    diagnostics = item.get("diagnostics")
    if diagnostics is None and item.get("error") is not None:
        diagnostics = ({"error": item.get("error")},)
    normalized = dict(item)
    normalized.update(
        {
            "id": evidence_id,
            "kind": kind,
            "status": status,
            "raw_status": raw_status if raw_status is not None else status,
            "body_state": body_state,
            "provenance": dict(provenance),
            "supports": dict(supports),
            "diagnostics": [dict(entry) if isinstance(entry, Mapping) else {"value": entry} for entry in _mapping_tuple(diagnostics)],
        }
    )
    return normalized


def _legacy_evidence_items(value: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    for key, default_kind in (
        ("execution_block_results", "execution_block"),
        ("plan_block_results", "plan_block"),
        ("action_evidence", "action"),
        ("skill_evidence", "skill_context"),
        ("capability_evidence", "capability"),
        ("validation_results", "validation"),
        ("diagnostics", "diagnostic"),
    ):
        for entry in _mapping_tuple(value.get(key)):
            items.append(_normalize_evidence_item(entry, index=len(items), default_kind=default_kind))
    for key, default_kind in (("context_refs", "context_ref"), ("artifact_refs", "artifact_ref"), ("runtime_event_refs", "runtime_event_ref")):
        raw_refs = value.get(key)
        if raw_refs is None:
            continue
        refs = raw_refs if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str | bytes | bytearray) else (raw_refs,)
        for ref in refs:
            if isinstance(ref, Mapping):
                entry = dict(ref)
            else:
                entry = {"ref": ref}
            entry.setdefault("body_state", "ref_only")
            items.append(_normalize_evidence_item(entry, index=len(items), default_kind=default_kind))
    return tuple(items)


def _derive_legacy_buckets_from_evidence_items(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, Any] = {
        "execution_block_results": [],
        "plan_block_results": [],
        "action_evidence": [],
        "skill_evidence": [],
        "capability_evidence": [],
        "context_refs": [],
        "artifact_refs": [],
        "runtime_event_refs": [],
        "validation_results": [],
        "diagnostics": [],
    }
    for item in items:
        kind = str(item.get("kind") or "").strip()
        if "plan_block" in kind:
            buckets["plan_block_results"].append(dict(item))
        elif "action" in kind:
            buckets["action_evidence"].append(dict(item))
        elif "skill" in kind:
            buckets["skill_evidence"].append(dict(item))
        elif "capability" in kind:
            buckets["capability_evidence"].append(dict(item))
        elif "validation" in kind:
            buckets["validation_results"].append(dict(item))
        elif "artifact" in kind:
            buckets["artifact_refs"].append(_legacy_ref_from_evidence_item(item))
        elif "context" in kind or "locator" in kind or "readback" in kind or "source_ref" in kind:
            buckets["context_refs"].append(_legacy_ref_from_evidence_item(item))
        elif "runtime_event" in kind:
            buckets["runtime_event_refs"].append(_legacy_ref_from_evidence_item(item))
        elif any(token in kind for token in ("diagnostic", "error", "failure", "failed")):
            buckets["diagnostics"].append(dict(item))
        else:
            buckets["execution_block_results"].append(dict(item))
    return buckets


def _legacy_ref_from_evidence_item(item: Mapping[str, Any]) -> str:
    for key in ("ref", "path", "record_id", "artifact_id", "url", "href", "id"):
        value = item.get(key)
        if value not in (None, "", [], {}):
            if isinstance(value, Mapping):
                for nested_key in ("id", "path", "url", "href"):
                    nested = value.get(nested_key)
                    if nested not in (None, "", [], {}):
                        return str(nested)
            return str(value)
    return str(item)


def _value_or_derived(value: Mapping[str, Any], key: str, derived: Mapping[str, Any]) -> Any:
    if key in value:
        return value.get(key)
    return derived.get(key)


@dataclass(frozen=True)
class TaskFrame:
    """One progressively resolved semantic unit before plan compilation.

    A TaskFrame is not an execution engine. It may compile to one PlanBlock
    instance, a DAG-shaped segment, a TriggerFlow wait segment, or a bounded
    child Agent step. It must not start an unrestricted nested AgentTask.
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
class CapabilityResolution:
    """Capability needs mapped to one policy result envelope."""

    allowed_capabilities: tuple[str, ...] = field(default_factory=tuple)
    denied_capabilities: tuple[str, ...] = field(default_factory=tuple)
    pending_approvals: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    scoped_action_candidates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    execution_resource_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    task_workspace_boundaries: Mapping[str, Any] = field(default_factory=dict)
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
            task_workspace_boundaries=_mapping(value.get("task_workspace_boundaries")),
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
            "task_workspace_boundaries": dict(self.task_workspace_boundaries),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Compact evidence package for observation, verifier, host guards, and resume."""

    task_frame_id: str | None = None
    plan_id: str | None = None
    evidence_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    execution_block_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    plan_block_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    semantic_outputs: Mapping[str, Any] = field(default_factory=dict)
    action_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    skill_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    capability_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    context_refs: tuple[str, ...] = field(default_factory=tuple)
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
        evidence_items = _mapping_tuple(value.get("evidence_items"))
        if not evidence_items:
            evidence_items = _legacy_evidence_items(value)
        normalized_evidence_items = tuple(
            _normalize_evidence_item(item, index=index)
            for index, item in enumerate(evidence_items)
        )
        derived = _derive_legacy_buckets_from_evidence_items(normalized_evidence_items)
        return cls(
            task_frame_id=_optional_str(value.get("task_frame_id")),
            plan_id=_optional_str(value.get("plan_id")),
            evidence_items=normalized_evidence_items,
            execution_block_results=_mapping_tuple(
                value.get(
                    "execution_block_results",
                    value.get("block_results", derived.get("execution_block_results")),
                )
            ),
            plan_block_results=_mapping_tuple(_value_or_derived(value, "plan_block_results", derived)),
            semantic_outputs=_mapping(value.get("semantic_outputs")),
            action_evidence=_mapping_tuple(_value_or_derived(value, "action_evidence", derived)),
            skill_evidence=_mapping_tuple(_value_or_derived(value, "skill_evidence", derived)),
            capability_evidence=_mapping_tuple(_value_or_derived(value, "capability_evidence", derived)),
            context_refs=_str_tuple(_value_or_derived(value, "context_refs", derived)),
            artifact_refs=_str_tuple(_value_or_derived(value, "artifact_refs", derived)),
            runtime_event_refs=_str_tuple(_value_or_derived(value, "runtime_event_refs", derived)),
            validation_results=_mapping_tuple(_value_or_derived(value, "validation_results", derived)),
            diagnostics=_mapping_tuple(_value_or_derived(value, "diagnostics", derived)),
            schema_version=str(value.get("schema_version") or EXECUTION_PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_frame_id": self.task_frame_id,
            "plan_id": self.plan_id,
            "evidence_items": [dict(item) for item in self.evidence_items],
            "execution_block_results": [dict(item) for item in self.execution_block_results],
            "plan_block_results": [dict(item) for item in self.plan_block_results],
            "semantic_outputs": dict(self.semantic_outputs),
            "action_evidence": [dict(item) for item in self.action_evidence],
            "skill_evidence": [dict(item) for item in self.skill_evidence],
            "capability_evidence": [dict(item) for item in self.capability_evidence],
            "context_refs": list(self.context_refs),
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
    produce this signal. AgentTask owns the decision to continue, repair,
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
