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

"""Blocks plugin data contracts.

The Blocks plugin bridges planner-facing PlanBlocks to TriggerFlow-backed
ExecutionBlocks. These contracts are inert data shapes; they do not grant
capability, run chunks, validate TaskDAG data, dispatch TriggerFlow, or accept
task completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from .execution_plan import (
    CapabilityResolution,
    ExecutionPlanEdge,
    PlanBlockInstance,
    _mapping,
    _mapping_tuple,
    _optional_str,
    _str_tuple,
)


BLOCKS_SCHEMA_VERSION = "blocks/v1"

PlanBlockKind: TypeAlias = Literal[
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
PLAN_BLOCK_KINDS: frozenset[str] = frozenset(
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

ExecutionBlockKind: TypeAlias = Literal[
    "model_request",
    "action_call",
    "context_read",
    "validation",
    "approval_wait",
    "external_wait",
    "fan_out",
    "fan_in",
    "dag_node",
    "emit",
    "snapshot",
    "flow_segment",
    "agent_step",
]
EXECUTION_BLOCK_KINDS: frozenset[str] = frozenset(
    {
        "model_request",
        "action_call",
        "context_read",
        "validation",
        "approval_wait",
        "external_wait",
        "fan_out",
        "fan_in",
        "dag_node",
        "emit",
        "snapshot",
        "flow_segment",
        "agent_step",
    }
)

BlockComposition: TypeAlias = Literal["atomic", "composite"]
BLOCK_COMPOSITIONS: frozenset[str] = frozenset({"atomic", "composite"})

StandardBlockSignal: TypeAlias = Literal[
    "block.started",
    "block.output",
    "block.evidence",
    "block.failed",
    "block.repair_requested",
    "block.replan_requested",
    "block.waiting",
    "block.resumed",
    "block.cancelled",
    "block.completed",
]
STANDARD_BLOCK_SIGNALS: frozenset[str] = frozenset(
    {
        "block.started",
        "block.output",
        "block.evidence",
        "block.failed",
        "block.repair_requested",
        "block.replan_requested",
        "block.waiting",
        "block.resumed",
        "block.cancelled",
        "block.completed",
    }
)


@dataclass(frozen=True)
class PlanBlock:
    """Planner-visible capability specification.

    A PlanBlock is a typed projection over self-describing capability sources
    such as Action schemas, Skill metadata, dynamic-graph candidates, and trusted
    route capabilities. Capability facts stay with their source; the PlanBlock
    adds planner summary, evidence contract, and runtime binding options.
    """

    id: str
    kind: PlanBlockKind | str
    name: str | None = None
    description: str | None = None
    planner_summary: str | None = None
    input_schema: Any = None
    output_contract: Mapping[str, Any] = field(default_factory=dict)
    evidence_contract: Mapping[str, Any] = field(default_factory=dict)
    capability_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    resource_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    runtime_binding_options: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    risk_profile: Mapping[str, Any] = field(default_factory=dict)
    diagnostics_schema: Any = None
    source_refs: tuple[str, ...] = field(default_factory=tuple)
    disabled_reason: str | None = None
    schema_version: str = BLOCKS_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "PlanBlock | Mapping[str, Any]") -> "PlanBlock":
        if isinstance(value, PlanBlock):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"PlanBlock must be a mapping or PlanBlock, got: { type(value) }.")
        block_id = value.get("id")
        if block_id is None or not str(block_id).strip():
            raise ValueError("PlanBlock requires non-empty 'id'.")
        kind = value.get("kind")
        if kind is None or not str(kind).strip():
            raise ValueError("PlanBlock requires non-empty 'kind'.")
        return cls(
            id=str(block_id).strip(),
            kind=str(kind).strip(),
            name=_optional_str(value.get("name")),
            description=_optional_str(value.get("description")),
            planner_summary=_optional_str(value.get("planner_summary")),
            input_schema=value.get("input_schema"),
            output_contract=_mapping(value.get("output_contract")),
            evidence_contract=_mapping(value.get("evidence_contract")),
            capability_requirements=_mapping_tuple(value.get("capability_requirements")),
            resource_requirements=_mapping_tuple(value.get("resource_requirements")),
            runtime_binding_options=_mapping_tuple(value.get("runtime_binding_options")),
            risk_profile=_mapping(value.get("risk_profile")),
            diagnostics_schema=value.get("diagnostics_schema"),
            source_refs=_str_tuple(value.get("source_refs")),
            disabled_reason=_optional_str(value.get("disabled_reason")),
            schema_version=str(value.get("schema_version") or BLOCKS_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "planner_summary": self.planner_summary,
            "input_schema": self.input_schema,
            "output_contract": dict(self.output_contract),
            "evidence_contract": dict(self.evidence_contract),
            "capability_requirements": [dict(item) for item in self.capability_requirements],
            "resource_requirements": [dict(item) for item in self.resource_requirements],
            "runtime_binding_options": [dict(item) for item in self.runtime_binding_options],
            "risk_profile": dict(self.risk_profile),
            "diagnostics_schema": self.diagnostics_schema,
            "source_refs": list(self.source_refs),
            "disabled_reason": self.disabled_reason,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ExecutionBlock:
    """TriggerFlow-backed executable block definition.

    Atomic blocks lower to one trusted TriggerFlow chunk. Composite blocks lower
    to a fixed chunk/signal group or a fixed group of child ExecutionBlocks.
    ExecutionBlocks cannot grant capability or accept terminal task completion.
    """

    id: str
    kind: ExecutionBlockKind | str
    composition: BlockComposition | str = "atomic"
    chunk_binding: str | None = None
    child_blocks: tuple[str, ...] = field(default_factory=tuple)
    signal_contract: Mapping[str, Any] = field(default_factory=dict)
    input_binding_schema: Any = None
    input_bindings: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Any = None
    evidence_mapping_contract: Mapping[str, Any] = field(default_factory=dict)
    result_adapter_contract: Mapping[str, Any] = field(default_factory=dict)
    resource_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_policy: Mapping[str, Any] = field(default_factory=dict)
    stream_projection_contract: Mapping[str, Any] = field(default_factory=dict)
    source_plan_block_id: str | None = None
    source_task_dag_node_id: str | None = None
    runtime_limits: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = BLOCKS_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "ExecutionBlock | Mapping[str, Any]") -> "ExecutionBlock":
        if isinstance(value, ExecutionBlock):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"ExecutionBlock must be a mapping or ExecutionBlock, got: { type(value) }.")
        block_id = value.get("id")
        if block_id is None or not str(block_id).strip():
            raise ValueError("ExecutionBlock requires non-empty 'id'.")
        kind = value.get("kind")
        if kind is None or not str(kind).strip():
            raise ValueError("ExecutionBlock requires non-empty 'kind'.")
        composition = str(value.get("composition", "atomic")).strip() or "atomic"
        if composition not in BLOCK_COMPOSITIONS:
            raise ValueError(
                f"ExecutionBlock 'composition' must be one of { sorted(BLOCK_COMPOSITIONS) }, got: { composition }."
            )
        return cls(
            id=str(block_id).strip(),
            kind=str(kind).strip(),
            composition=composition,
            chunk_binding=_optional_str(value.get("chunk_binding")),
            child_blocks=_str_tuple(value.get("child_blocks")),
            signal_contract=_mapping(value.get("signal_contract")),
            input_binding_schema=value.get("input_binding_schema"),
            input_bindings=_mapping(value.get("input_bindings")),
            output_schema=value.get("output_schema"),
            evidence_mapping_contract=_mapping(value.get("evidence_mapping_contract")),
            result_adapter_contract=_mapping(value.get("result_adapter_contract")),
            resource_requirements=_mapping_tuple(value.get("resource_requirements")),
            retry_policy=_mapping(value.get("retry_policy")),
            checkpoint_policy=_mapping(value.get("checkpoint_policy")),
            stream_projection_contract=_mapping(value.get("stream_projection_contract")),
            source_plan_block_id=_optional_str(value.get("source_plan_block_id")),
            source_task_dag_node_id=_optional_str(value.get("source_task_dag_node_id")),
            runtime_limits=_mapping(value.get("runtime_limits")),
            schema_version=str(value.get("schema_version") or BLOCKS_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "composition": self.composition,
            "chunk_binding": self.chunk_binding,
            "child_blocks": list(self.child_blocks),
            "signal_contract": dict(self.signal_contract),
            "input_binding_schema": self.input_binding_schema,
            "input_bindings": dict(self.input_bindings),
            "output_schema": self.output_schema,
            "evidence_mapping_contract": dict(self.evidence_mapping_contract),
            "result_adapter_contract": dict(self.result_adapter_contract),
            "resource_requirements": [dict(item) for item in self.resource_requirements],
            "retry_policy": dict(self.retry_policy),
            "checkpoint_policy": dict(self.checkpoint_policy),
            "stream_projection_contract": dict(self.stream_projection_contract),
            "source_plan_block_id": self.source_plan_block_id,
            "source_task_dag_node_id": self.source_task_dag_node_id,
            "runtime_limits": dict(self.runtime_limits),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class BlockSignal:
    """Standard block signal contract carried over TriggerFlow signals."""

    signal: StandardBlockSignal | str
    execution_id: str | None = None
    task_frame_id: str | None = None
    plan_id: str | None = None
    source_plan_block_id: str | None = None
    execution_block_id: str | None = None
    correlation_id: str | None = None
    payload_schema: Any = None
    refs: tuple[str, ...] = field(default_factory=tuple)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_value(cls, value: "BlockSignal | Mapping[str, Any]") -> "BlockSignal":
        if isinstance(value, BlockSignal):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"BlockSignal must be a mapping or BlockSignal, got: { type(value) }.")
        signal = value.get("signal")
        if signal is None or not str(signal).strip():
            raise ValueError("BlockSignal requires non-empty 'signal'.")
        if str(signal).strip() not in STANDARD_BLOCK_SIGNALS:
            raise ValueError(
                f"BlockSignal 'signal' must be one of { sorted(STANDARD_BLOCK_SIGNALS) }, got: { signal }."
            )
        return cls(
            signal=str(signal).strip(),
            execution_id=_optional_str(value.get("execution_id")),
            task_frame_id=_optional_str(value.get("task_frame_id")),
            plan_id=_optional_str(value.get("plan_id")),
            source_plan_block_id=_optional_str(value.get("source_plan_block_id")),
            execution_block_id=_optional_str(value.get("execution_block_id")),
            correlation_id=_optional_str(value.get("correlation_id")),
            payload_schema=value.get("payload_schema"),
            refs=_str_tuple(value.get("refs")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "execution_id": self.execution_id,
            "task_frame_id": self.task_frame_id,
            "plan_id": self.plan_id,
            "source_plan_block_id": self.source_plan_block_id,
            "execution_block_id": self.execution_block_id,
            "correlation_id": self.correlation_id,
            "payload_schema": self.payload_schema,
            "refs": list(self.refs),
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


@dataclass(frozen=True)
class ExecutionBlockEdge:
    """Dependency/data wiring between ExecutionBlocks inside one graph."""

    from_execution_block: str
    to_execution_block: str
    kind: str = "sequence"
    condition: Any = None
    binding: Any = None

    @classmethod
    def from_value(cls, value: "ExecutionBlockEdge | Mapping[str, Any]") -> "ExecutionBlockEdge":
        if isinstance(value, ExecutionBlockEdge):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"ExecutionBlockEdge must be a mapping or ExecutionBlockEdge, got: { type(value) }."
            )
        from_block = value.get("from_execution_block", value.get("from_block", value.get("from")))
        to_block = value.get("to_execution_block", value.get("to_block", value.get("to")))
        if from_block is None or not str(from_block).strip():
            raise ValueError("ExecutionBlockEdge requires non-empty 'from_execution_block'.")
        if to_block is None or not str(to_block).strip():
            raise ValueError("ExecutionBlockEdge requires non-empty 'to_execution_block'.")
        return cls(
            from_execution_block=str(from_block).strip(),
            to_execution_block=str(to_block).strip(),
            kind=str(value.get("kind", "sequence")).strip() or "sequence",
            condition=value.get("condition"),
            binding=value.get("binding"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_execution_block": self.from_execution_block,
            "to_execution_block": self.to_execution_block,
            "kind": self.kind,
            "condition": self.condition,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class ResultAdapter:
    """Maps runtime outputs into semantic outputs and result views."""

    id: str
    source_block_ids: tuple[str, ...] = field(default_factory=tuple)
    semantic_output_map: Mapping[str, Any] = field(default_factory=dict)
    result_view: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_value(cls, value: "ResultAdapter | Mapping[str, Any]") -> "ResultAdapter":
        if isinstance(value, ResultAdapter):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"ResultAdapter must be a mapping or ResultAdapter, got: { type(value) }.")
        adapter_id = value.get("id")
        if adapter_id is None or not str(adapter_id).strip():
            raise ValueError("ResultAdapter requires non-empty 'id'.")
        return cls(
            id=str(adapter_id).strip(),
            source_block_ids=_str_tuple(value.get("source_block_ids")),
            semantic_output_map=_mapping(value.get("semantic_output_map")),
            result_view=_mapping(value.get("result_view")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_block_ids": list(self.source_block_ids),
            "semantic_output_map": dict(self.semantic_output_map),
            "result_view": dict(self.result_view),
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


@dataclass(frozen=True)
class EvidenceMapper:
    """Maps runtime block output into evidence refs and diagnostic facts."""

    id: str
    source_block_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_kinds: tuple[str, ...] = field(default_factory=tuple)
    required_refs: tuple[str, ...] = field(default_factory=tuple)
    mapping_contract: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_value(cls, value: "EvidenceMapper | Mapping[str, Any]") -> "EvidenceMapper":
        if isinstance(value, EvidenceMapper):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"EvidenceMapper must be a mapping or EvidenceMapper, got: { type(value) }.")
        mapper_id = value.get("id")
        if mapper_id is None or not str(mapper_id).strip():
            raise ValueError("EvidenceMapper requires non-empty 'id'.")
        return cls(
            id=str(mapper_id).strip(),
            source_block_ids=_str_tuple(value.get("source_block_ids")),
            evidence_kinds=_str_tuple(value.get("evidence_kinds")),
            required_refs=_str_tuple(value.get("required_refs")),
            mapping_contract=_mapping(value.get("mapping_contract")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_block_ids": list(self.source_block_ids),
            "evidence_kinds": list(self.evidence_kinds),
            "required_refs": list(self.required_refs),
            "mapping_contract": dict(self.mapping_contract),
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


@dataclass(frozen=True)
class ExecutionBlockGraph:
    """Blocks compiler output for one bounded plan or TaskDAG segment.

    This is a lowering artifact, not a separate runtime engine. TriggerFlow owns
    dispatch, concurrency, waits, pause/resume, runtime stream, and close
    snapshots.
    """

    graph_id: str
    source_plan_id: str | None = None
    execution_blocks: tuple[ExecutionBlock, ...] = field(default_factory=tuple)
    edges: tuple[ExecutionBlockEdge, ...] = field(default_factory=tuple)
    signals: tuple[BlockSignal, ...] = field(default_factory=tuple)
    start_blocks: tuple[str, ...] = field(default_factory=tuple)
    terminal_blocks: tuple[str, ...] = field(default_factory=tuple)
    evidence_mappers: tuple[EvidenceMapper, ...] = field(default_factory=tuple)
    result_adapters: tuple[ResultAdapter, ...] = field(default_factory=tuple)
    resource_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    checkpoint_policy: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = BLOCKS_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "ExecutionBlockGraph | Mapping[str, Any]") -> "ExecutionBlockGraph":
        if isinstance(value, ExecutionBlockGraph):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"ExecutionBlockGraph must be a mapping or ExecutionBlockGraph, got: { type(value) }."
            )
        graph_id = value.get("graph_id")
        if graph_id is None or not str(graph_id).strip():
            raise ValueError("ExecutionBlockGraph requires non-empty 'graph_id'.")
        raw_blocks = value.get("execution_blocks", ())
        raw_edges = value.get("edges", ())
        raw_signals = value.get("signals", ())
        raw_mappers = value.get("evidence_mappers", ())
        raw_adapters = value.get("result_adapters", ())
        for field_name, raw_value in (
            ("execution_blocks", raw_blocks),
            ("edges", raw_edges),
            ("signals", raw_signals),
            ("evidence_mappers", raw_mappers),
            ("result_adapters", raw_adapters),
        ):
            if raw_value is not None and not isinstance(raw_value, list | tuple):
                raise TypeError(f"ExecutionBlockGraph '{ field_name }' must be a list/tuple.")
        return cls(
            graph_id=str(graph_id).strip(),
            source_plan_id=_optional_str(value.get("source_plan_id")),
            execution_blocks=tuple(ExecutionBlock.from_value(block) for block in (raw_blocks or ())),
            edges=tuple(ExecutionBlockEdge.from_value(edge) for edge in (raw_edges or ())),
            signals=tuple(BlockSignal.from_value(signal) for signal in (raw_signals or ())),
            start_blocks=_str_tuple(value.get("start_blocks")),
            terminal_blocks=_str_tuple(value.get("terminal_blocks")),
            evidence_mappers=tuple(EvidenceMapper.from_value(mapper) for mapper in (raw_mappers or ())),
            result_adapters=tuple(ResultAdapter.from_value(adapter) for adapter in (raw_adapters or ())),
            resource_requirements=_mapping_tuple(value.get("resource_requirements")),
            checkpoint_policy=_mapping(value.get("checkpoint_policy")),
            schema_version=str(value.get("schema_version") or BLOCKS_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "source_plan_id": self.source_plan_id,
            "execution_blocks": [block.to_dict() for block in self.execution_blocks],
            "edges": [edge.to_dict() for edge in self.edges],
            "signals": [signal.to_dict() for signal in self.signals],
            "start_blocks": list(self.start_blocks),
            "terminal_blocks": list(self.terminal_blocks),
            "evidence_mappers": [mapper.to_dict() for mapper in self.evidence_mappers],
            "result_adapters": [adapter.to_dict() for adapter in self.result_adapters],
            "resource_requirements": [dict(item) for item in self.resource_requirements],
            "checkpoint_policy": dict(self.checkpoint_policy),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class BlockCompileRequest:
    """Input envelope for the core-owned Blocks compile contract."""

    execution_id: str | None = None
    task_frame_id: str | None = None
    plan_id: str | None = None
    plan_blocks: tuple[PlanBlockInstance, ...] = field(default_factory=tuple)
    edges: tuple[ExecutionPlanEdge, ...] = field(default_factory=tuple)
    capability_resolution: CapabilityResolution | None = None
    runtime_policy: Mapping[str, Any] = field(default_factory=dict)
    evidence_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    result_contracts: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    budget: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = BLOCKS_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "BlockCompileRequest | Mapping[str, Any]") -> "BlockCompileRequest":
        if isinstance(value, BlockCompileRequest):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"BlockCompileRequest must be a mapping or BlockCompileRequest, got: { type(value) }."
            )
        raw_plan_blocks = value.get("plan_blocks", ())
        raw_edges = value.get("edges", ())
        if raw_plan_blocks is not None and not isinstance(raw_plan_blocks, list | tuple):
            raise TypeError("BlockCompileRequest 'plan_blocks' must be a list/tuple.")
        if raw_edges is not None and not isinstance(raw_edges, list | tuple):
            raise TypeError("BlockCompileRequest 'edges' must be a list/tuple.")
        resolution = value.get("capability_resolution")
        return cls(
            execution_id=_optional_str(value.get("execution_id")),
            task_frame_id=_optional_str(value.get("task_frame_id")),
            plan_id=_optional_str(value.get("plan_id")),
            plan_blocks=tuple(PlanBlockInstance.from_value(block) for block in (raw_plan_blocks or ())),
            edges=tuple(ExecutionPlanEdge.from_value(edge) for edge in (raw_edges or ())),
            capability_resolution=(
                CapabilityResolution.from_value(resolution) if resolution is not None else None
            ),
            runtime_policy=_mapping(value.get("runtime_policy")),
            evidence_requirements=_mapping_tuple(value.get("evidence_requirements")),
            result_contracts=_mapping_tuple(value.get("result_contracts")),
            budget=_mapping(value.get("budget")),
            schema_version=str(value.get("schema_version") or BLOCKS_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "task_frame_id": self.task_frame_id,
            "plan_id": self.plan_id,
            "plan_blocks": [block.to_dict() for block in self.plan_blocks],
            "edges": [edge.to_dict() for edge in self.edges],
            "capability_resolution": (
                self.capability_resolution.to_dict() if self.capability_resolution is not None else None
            ),
            "runtime_policy": dict(self.runtime_policy),
            "evidence_requirements": [dict(item) for item in self.evidence_requirements],
            "result_contracts": [dict(item) for item in self.result_contracts],
            "budget": dict(self.budget),
            "schema_version": self.schema_version,
        }
