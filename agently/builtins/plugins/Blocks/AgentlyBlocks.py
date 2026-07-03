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

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

from agently.core.orchestration.TaskDAG import TaskDAGValidation, TaskDAGValidator
from agently.core.orchestration.TriggerFlow import TriggerFlow
from agently.types.data import (
    EXECUTION_BLOCK_KINDS,
    PLAN_BLOCK_KINDS,
    STANDARD_BLOCK_SIGNALS,
    BlockCompileRequest,
    BlockSignal,
    CapabilityResolution,
    EvidenceEnvelope,
    EvidenceMapper,
    ExecutionBlock,
    ExecutionBlockEdge,
    ExecutionBlockGraph,
    PlanBlock,
    PlanBlockInstance,
    ReplanSignal,
    ResultAdapter,
    TaskDAG,
)
from agently.types.trigger_flow import TriggerFlowRuntimeData


_ACTION_LIKE_PLAN_BLOCK_KINDS = frozenset({"action_call", "mcp_tool_call", "script_action"})
_HANDLER_REQUIRED_EXECUTION_KINDS = frozenset(
    {
        "model_request",
        "action_call",
        "skill_activation",
        "workspace_operation",
        "approval_wait",
        "external_wait",
        "flow_segment",
        "agent_step",
    }
)
_ACTION_REF_ALIAS_FIELDS = frozenset(
    {
        "path",
        "record_id",
        "source_url",
        "selected_url",
        "requested_url",
        "canonical_url",
        "url",
        "href",
        "artifact_id",
        "output_ref",
        "ref",
    }
)


class PlanBlockRegistry:
    def __init__(self, blocks: Mapping[str, PlanBlock] | None = None):
        self._blocks: dict[str, PlanBlock] = {}
        for block in _default_plan_blocks():
            self.register(block)
        if blocks:
            for block in blocks.values():
                self.register(block)

    def register(self, block: PlanBlock | Mapping[str, Any]) -> "PlanBlockRegistry":
        normalized = PlanBlock.from_value(block)
        _validate_plan_block_contract(normalized)
        self._blocks[normalized.id] = normalized
        return self

    def get(self, block_id: str) -> PlanBlock | None:
        return self._blocks.get(str(block_id))

    def list(self) -> list[PlanBlock]:
        return list(self._blocks.values())


class ExecutionBlockRegistry:
    def __init__(self, blocks: Mapping[str, ExecutionBlock] | None = None):
        self._blocks: dict[str, ExecutionBlock] = {}
        for block in _default_execution_blocks():
            self.register(block)
        if blocks:
            for block in blocks.values():
                self.register(block)

    def register(self, block: ExecutionBlock | Mapping[str, Any]) -> "ExecutionBlockRegistry":
        normalized = ExecutionBlock.from_value(block)
        _validate_execution_block_contract(normalized)
        self._blocks[normalized.id] = normalized
        return self

    def get(self, block_id: str) -> ExecutionBlock | None:
        return self._blocks.get(str(block_id))

    def list(self) -> list[ExecutionBlock]:
        return list(self._blocks.values())


class BlockCompiler:
    def __init__(
        self,
        *,
        plan_blocks: PlanBlockRegistry | None = None,
        execution_blocks: ExecutionBlockRegistry | None = None,
        task_dag_validator: TaskDAGValidator | None = None,
    ):
        self.plan_blocks = plan_blocks or PlanBlockRegistry()
        self.execution_blocks = execution_blocks or ExecutionBlockRegistry()
        self.task_dag_validator = task_dag_validator or TaskDAGValidator()

    def compile(self, request: BlockCompileRequest | Mapping[str, Any]) -> ExecutionBlockGraph:
        normalized = BlockCompileRequest.from_value(request)
        _validate_plan_edges(normalized.plan_blocks, normalized.edges)
        _enforce_capability_resolution(normalized.capability_resolution, normalized.plan_blocks)

        execution_blocks: list[ExecutionBlock] = []
        edges: list[ExecutionBlockEdge] = []
        plan_to_execution: dict[str, list[str]] = {}

        for plan_block in normalized.plan_blocks:
            lowered_blocks, lowered_edges = self._compile_plan_block(plan_block)
            execution_blocks.extend(lowered_blocks)
            edges.extend(lowered_edges)
            plan_to_execution[plan_block.id] = [block.id for block in lowered_blocks]

        for edge in normalized.edges:
            from_ids = plan_to_execution.get(edge.from_plan_block, ())
            to_ids = plan_to_execution.get(edge.to_plan_block, ())
            for from_id in from_ids:
                for to_id in to_ids:
                    edges.append(
                        ExecutionBlockEdge(
                            from_execution_block=from_id,
                            to_execution_block=to_id,
                            kind=edge.kind,
                            condition=edge.condition,
                            binding=edge.binding,
                        )
                    )

        block_ids = {block.id for block in execution_blocks}
        downstream = {edge.to_execution_block for edge in edges}
        upstream = {edge.from_execution_block for edge in edges}
        start_blocks = tuple(block.id for block in execution_blocks if block.id not in downstream)
        terminal_blocks = tuple(block.id for block in execution_blocks if block.id not in upstream)
        graph_id = f"blocks:{ normalized.plan_id or normalized.execution_id or 'plan' }"

        signals = tuple(
            BlockSignal(signal=signal, plan_id=normalized.plan_id, task_frame_id=normalized.task_frame_id)
            for signal in sorted(STANDARD_BLOCK_SIGNALS)
        )
        return ExecutionBlockGraph(
            graph_id=graph_id,
            source_plan_id=normalized.plan_id,
            execution_blocks=tuple(execution_blocks),
            edges=tuple(edge for edge in edges if edge.from_execution_block in block_ids and edge.to_execution_block in block_ids),
            signals=signals,
            start_blocks=start_blocks,
            terminal_blocks=terminal_blocks,
            evidence_mappers=tuple(
                EvidenceMapper(
                    id=f"evidence:{ block.id }",
                    source_block_ids=(block.id,),
                    evidence_kinds=tuple(_evidence_kinds_for(block.kind)),
                    mapping_contract=block.evidence_mapping_contract,
                )
                for block in execution_blocks
            ),
            result_adapters=(
                ResultAdapter(
                    id=f"result:{ graph_id }",
                    source_block_ids=terminal_blocks,
                    semantic_output_map={"terminal_blocks": list(terminal_blocks)},
                ),
            ),
            resource_requirements=tuple(
                requirement
                for block in execution_blocks
                for requirement in block.resource_requirements
            ),
            checkpoint_policy=dict(normalized.runtime_policy.get("checkpoint_policy", {})),
        )

    def _compile_plan_block(
        self,
        plan_block: PlanBlockInstance,
    ) -> tuple[list[ExecutionBlock], list[ExecutionBlockEdge]]:
        kind = str(plan_block.kind or plan_block.plan_block_id).strip()
        _validate_plan_block_instance_kind(plan_block, self.plan_blocks)
        if kind == "dag_segment":
            return self._compile_dag_segment(plan_block)
        execution_kind = _execution_kind_for_plan_kind(kind)
        block_id = f"{ plan_block.id }:{ execution_kind }"
        catalog_block = self.execution_blocks.get(execution_kind)
        base = catalog_block if catalog_block is not None else ExecutionBlock(id=execution_kind, kind=execution_kind)
        block = replace(
            base,
            id=block_id,
            input_bindings=_merge_mapping(base.input_bindings, _plan_input_bindings(plan_block)),
            evidence_mapping_contract=_merge_mapping(base.evidence_mapping_contract, plan_block.evidence_contract),
            result_adapter_contract=_merge_mapping(base.result_adapter_contract, plan_block.output_contract),
            resource_requirements=(
                *base.resource_requirements,
                *tuple(dict(item) for item in plan_block.capability_requirements),
            ),
            source_plan_block_id=plan_block.id,
            runtime_limits=dict(plan_block.budget),
        )
        return [block], []

    def _compile_dag_segment(
        self,
        plan_block: PlanBlockInstance,
    ) -> tuple[list[ExecutionBlock], list[ExecutionBlockEdge]]:
        validation = _extract_dag_validation(plan_block.bound_inputs, self.task_dag_validator)
        handler_prefix = _extract_dag_handler_prefix(plan_block.bound_inputs)
        blocks: list[ExecutionBlock] = []
        edges: list[ExecutionBlockEdge] = []
        for task in validation.graph.tasks:
            block_id = f"{ plan_block.id}:dag_node:{ task.id }"
            handler = f"{ handler_prefix }:{ task.id }" if handler_prefix else task.binding or task.kind
            blocks.append(
                ExecutionBlock(
                    id=block_id,
                    kind="dag_node",
                    source_plan_block_id=plan_block.id,
                    source_task_dag_node_id=task.id,
                    input_bindings={
                        "task": task.to_dict(),
                        "graph": validation.graph.to_dict(),
                        "handler": handler,
                    },
                    output_schema={"task_id": task.id},
                    evidence_mapping_contract=plan_block.evidence_contract,
                    result_adapter_contract=plan_block.output_contract,
                    resource_requirements=tuple(dict(item) for item in plan_block.capability_requirements),
                )
            )
            for dependency in task.depends_on:
                edges.append(
                    ExecutionBlockEdge(
                        from_execution_block=f"{ plan_block.id}:dag_node:{ dependency }",
                        to_execution_block=block_id,
                        kind="data",
                        binding={"dependency": dependency},
                    )
                )
        return blocks, edges


class BlocksRuntimeBinder:
    def bind(self, graph: ExecutionBlockGraph, flow: TriggerFlow | None = None) -> TriggerFlow:
        target_flow = flow if flow is not None else TriggerFlow(name=graph.graph_id)
        blocks_by_id = {block.id: block for block in graph.execution_blocks}
        handlers = {block.id: self._make_block_handler(graph, block) for block in graph.execution_blocks}
        for block_id in graph.start_blocks:
            if block_id in handlers:
                target_flow.to(handlers[block_id], name=_chunk_name(graph.graph_id, block_id))
        incoming_edges: dict[str, list[ExecutionBlockEdge]] = {}
        for edge in graph.edges:
            if edge.to_execution_block in handlers and edge.from_execution_block in blocks_by_id:
                incoming_edges.setdefault(edge.to_execution_block, []).append(edge)
        for block_id, edges in incoming_edges.items():
            completed_events = [
                _block_event(graph.graph_id, edge.from_execution_block, "completed")
                for edge in edges
            ]
            if len(completed_events) == 1:
                target_flow.when(completed_events[0]).to(
                    handlers[block_id],
                    name=_chunk_name(graph.graph_id, block_id),
                )
            else:
                target_flow.when(cast(Any, completed_events), mode="and").to(
                    handlers[block_id],
                    name=_chunk_name(graph.graph_id, block_id),
                )
        if graph.terminal_blocks:
            terminal_events = [
                _block_event(graph.graph_id, block_id, "completed")
                for block_id in graph.terminal_blocks
            ]
            if len(terminal_events) == 1:
                target_flow.when(terminal_events[0], mode="and").to(
                    self._make_finalize_handler(graph),
                    name=_chunk_name(graph.graph_id, "__finalize__"),
                )
            else:
                target_flow.when(cast(Any, terminal_events), mode="and").to(
                    self._make_finalize_handler(graph),
                    name=_chunk_name(graph.graph_id, "__finalize__"),
                )
        return target_flow

    def _make_block_handler(self, graph: ExecutionBlockGraph, block: ExecutionBlock):
        async def run_block(data: TriggerFlowRuntimeData):
            if not data.get_state("blocks.graph_input_set", False):
                await data.async_set_state("blocks.graph_input", data.value, emit=False)
                await data.async_set_state("blocks.graph_input_set", True, emit=False)
            if block.id in _cancelled_block_ids(data):
                result = {
                    "execution_block_id": block.id,
                    "source_plan_block_id": block.source_plan_block_id,
                    "source_task_dag_node_id": block.source_task_dag_node_id,
                    "kind": block.kind,
                    "output": {
                        "cancelled": True,
                        "reason": "Cancelled by structured ReplanSignal.",
                    },
                    "success": False,
                    "cancelled": True,
                }
                await _record_block_result(data, result)
                await _emit_block_event(data, graph, block, "cancelled", result)
                await _emit_block_event(data, graph, block, "completed", result)
                return result
            await _emit_block_event(data, graph, block, "started", {"input": data.value})
            try:
                output = await _execute_block(block, data)
            except Exception as error:
                diagnostics = {
                    "code": "blocks.execution_failed",
                    "message": str(error),
                    "execution_block_id": block.id,
                    "kind": block.kind,
                    "source_plan_block_id": block.source_plan_block_id,
                    "source_task_dag_node_id": block.source_task_dag_node_id,
                }
                await _append_state_item(data, "blocks.diagnostics", diagnostics)
                await _emit_block_event(data, graph, block, "failed", diagnostics)
                raise
            if data.execution.is_waiting():
                result = {
                    "execution_block_id": block.id,
                    "source_plan_block_id": block.source_plan_block_id,
                    "source_task_dag_node_id": block.source_task_dag_node_id,
                    "kind": block.kind,
                    "output": output,
                    "success": False,
                    "waiting": True,
                }
                await _record_block_result(data, result)
                await _emit_block_event(data, graph, block, "waiting", result)
                return output
            result = {
                "execution_block_id": block.id,
                "source_plan_block_id": block.source_plan_block_id,
                "source_task_dag_node_id": block.source_task_dag_node_id,
                "kind": block.kind,
                "output": output,
                "success": True,
            }
            await _record_block_result(data, result)
            await _emit_block_event(data, graph, block, "output", result)
            await _emit_block_event(data, graph, block, "evidence", _evidence_payload(block, result))
            replan_signals = _replan_signals_from_output(output)
            for replan_signal in replan_signals:
                await _append_state_item(data, "blocks.replan_signals", replan_signal)
                await _emit_block_event(data, graph, block, "replan_requested", replan_signal)
            cancelled_ids = _cancelled_ids_for_replan_signals(graph, block.id, replan_signals)
            if cancelled_ids:
                existing_cancelled = set(_cancelled_block_ids(data))
                for cancelled_id in cancelled_ids:
                    if cancelled_id in existing_cancelled or cancelled_id == block.id:
                        continue
                    existing_cancelled.add(cancelled_id)
                    await _append_state_item(data, "blocks.cancelled_execution_block_ids", cancelled_id)
                    cancelled_block = next(
                        (candidate for candidate in graph.execution_blocks if candidate.id == cancelled_id),
                        None,
                    )
                    if cancelled_block is not None:
                        await _emit_block_event(
                            data,
                            graph,
                            cancelled_block,
                            "cancelled",
                            {
                                "execution_block_id": cancelled_id,
                                "reason": "Cancelled by structured ReplanSignal.",
                                "source_replan_block_id": block.id,
                            },
                        )
            await _emit_block_event(data, graph, block, "completed", result)
            return result

        return run_block

    def _make_finalize_handler(self, graph: ExecutionBlockGraph):
        async def finalize(data: TriggerFlowRuntimeData):
            snapshot = data.get_state("blocks", {}) or {}
            semantic_outputs = _semantic_outputs_from_snapshot(graph, snapshot)
            await data.async_set_state("blocks.semantic_outputs", semantic_outputs, emit=False)
            result = {
                "graph_id": graph.graph_id,
                "source_plan_id": graph.source_plan_id,
                "terminal_blocks": list(graph.terminal_blocks),
                "semantic_outputs": semantic_outputs,
                "execution_block_results": snapshot.get("execution_block_results", []),
                "plan_block_results": snapshot.get("plan_block_results", []),
                "diagnostics": snapshot.get("diagnostics", []),
            }
            await data.execution.async_put_into_stream(
                {"type": "blocks.graph.completed", **result},
                _skip_contract_validation=True,
            )
            await data.async_set_state("blocks.result", result, emit=False)
            return result

        return finalize


class ResultAdapterRegistry:
    def map_result(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        output = dict(runtime_output or {})
        blocks_state = _mapping_or_empty(output.get("blocks"))
        semantic_outputs = output.get("semantic_outputs") or blocks_state.get("semantic_outputs") or {}
        return {
            "graph_id": graph.graph_id,
            "source_plan_id": graph.source_plan_id,
            "semantic_outputs": dict(semantic_outputs) if isinstance(semantic_outputs, Mapping) else semantic_outputs,
            "terminal_blocks": list(graph.terminal_blocks),
        }


class EvidenceMapperRegistry:
    def map_evidence(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> EvidenceEnvelope:
        output = dict(runtime_output or {})
        blocks_state = _mapping_or_empty(output.get("blocks"))
        execution_results = blocks_state.get("execution_block_results", output.get("execution_block_results", ()))
        plan_results = blocks_state.get("plan_block_results", output.get("plan_block_results", ()))
        diagnostics = blocks_state.get("diagnostics", output.get("diagnostics", ()))
        semantic_outputs = blocks_state.get("semantic_outputs", output.get("semantic_outputs", {}))
        diagnostic_items = _ledger_items_from_blocks_state(
            execution_results=(),
            plan_results=(),
            diagnostics=_diagnostics_with_replan_signals(diagnostics, blocks_state.get("replan_signals", ())),
        )
        evidence_items = _mapping_sequence(blocks_state.get("evidence_items"))
        if evidence_items:
            evidence_items = [*evidence_items, *_new_ledger_items(evidence_items, diagnostic_items)]
        else:
            evidence_items = _ledger_items_from_blocks_state(
                execution_results=execution_results,
                plan_results=plan_results,
                diagnostics=_diagnostics_with_replan_signals(
                    diagnostics,
                    blocks_state.get("replan_signals", ()),
                ),
            )
        return EvidenceEnvelope.from_value(
            {
                "plan_id": graph.source_plan_id,
                "evidence_items": evidence_items,
                "execution_block_results": execution_results,
                "plan_block_results": plan_results,
                "semantic_outputs": semantic_outputs if isinstance(semantic_outputs, Mapping) else {},
                "skill_evidence": blocks_state.get("skill_evidence", ()),
                "action_evidence": blocks_state.get("action_evidence", ()),
                "capability_evidence": blocks_state.get("capability_evidence", ()),
                "workspace_refs": blocks_state.get("workspace_refs", ()),
                "artifact_refs": blocks_state.get("artifact_refs", ()),
                "runtime_event_refs": blocks_state.get("runtime_event_refs", ()),
                "validation_results": blocks_state.get("validation_results", ()),
                "diagnostics": _diagnostics_with_replan_signals(
                    diagnostics,
                    blocks_state.get("replan_signals", ()),
                ),
            }
        )


class AgentlyBlocks:
    name = "AgentlyBlocks"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self):
        self.plan_block_registry = PlanBlockRegistry()
        self.execution_block_registry = ExecutionBlockRegistry()
        self.compiler = BlockCompiler(
            plan_blocks=self.plan_block_registry,
            execution_blocks=self.execution_block_registry,
        )
        self.runtime_binder = BlocksRuntimeBinder()
        self.result_adapters = ResultAdapterRegistry()
        self.evidence_mappers = EvidenceMapperRegistry()

    @staticmethod
    def _on_register():
        return None

    @staticmethod
    def _on_unregister():
        return None

    def list_plan_block_summaries(self, context: Mapping[str, Any] | None = None) -> list[PlanBlock]:
        return self.plan_block_registry.list()

    def compile(self, request: BlockCompileRequest | Mapping[str, Any]) -> ExecutionBlockGraph:
        return self.compiler.compile(request)

    def bind_runtime(self, graph: ExecutionBlockGraph, flow: TriggerFlow | None = None) -> TriggerFlow:
        return self.runtime_binder.bind(graph, flow)

    def map_evidence(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> EvidenceEnvelope:
        return self.evidence_mappers.map_evidence(graph, runtime_output)

    def map_result(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return self.result_adapters.map_result(graph, runtime_output)


def _default_plan_blocks() -> tuple[PlanBlock, ...]:
    descriptions = {
        "model_request": "Run one structured model request.",
        "action_call": "Run one controlled Action call or bounded action segment.",
        "mcp_tool_call": "Run an MCP tool through controlled ActionRuntime policy.",
        "script_action": "Run a host-approved script resource as a scoped Action.",
        "workspace_operation": "Perform a scoped Workspace read/write/link/checkpoint operation.",
        "skill_activation": "Load selected Skill guidance and resources under budget.",
        "approval_wait": "Open a durable policy approval wait.",
        "external_wait": "Wait for an external callback, webhook, or human event.",
        "validation": "Validate prior evidence deterministically or with a model judge.",
        "observation": "Produce a compact observation snapshot.",
        "dag_segment": "Run a bounded TaskDAG segment after TaskDAG validation.",
        "flow_segment": "Run a trusted TriggerFlow-backed block segment.",
        "emit": "Project progress, stream, or result information.",
        "agent_step": "Run one bounded child Agent step under parent lineage.",
    }
    return tuple(
        PlanBlock(
            id=kind,
            kind=kind,
            name=kind.replace("_", " ").title(),
            description=descriptions[kind],
            planner_summary=descriptions[kind],
            runtime_binding_options=({"execution_block_kind": _execution_kind_for_plan_kind(kind)},),
        )
        for kind in sorted(PLAN_BLOCK_KINDS)
    )


def _default_execution_blocks() -> tuple[ExecutionBlock, ...]:
    return tuple(
        ExecutionBlock(
            id=kind,
            kind=kind,
            composition="atomic" if kind not in {"action_call", "fan_out", "fan_in"} else "composite",
            signal_contract={"emits": sorted(STANDARD_BLOCK_SIGNALS)},
        )
        for kind in (
            "model_request",
            "action_call",
            "skill_activation",
            "workspace_operation",
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
        )
    )


def _validate_plan_block_contract(block: PlanBlock) -> None:
    if str(block.kind) not in PLAN_BLOCK_KINDS:
        raise ValueError(
            f"PlanBlock '{ block.id }' kind must be one of { sorted(PLAN_BLOCK_KINDS) }, got: { block.kind }."
        )
    for option in block.runtime_binding_options:
        if not any(
            key in option
            for key in (
                "execution_block_kind",
                "execution_block_id",
                "task_kind",
                "action_id",
                "chunk_binding",
                "flow_segment",
            )
        ):
            raise ValueError(
                f"PlanBlock '{ block.id }' runtime binding option must reference a trusted runtime binding."
            )
        execution_kind = option.get("execution_block_kind")
        if execution_kind is not None and str(execution_kind) not in EXECUTION_BLOCK_KINDS:
            raise ValueError(
                f"PlanBlock '{ block.id }' references unknown execution block kind: { execution_kind }."
            )
    if _contains_generated_code(block.input_schema) or _contains_generated_code(block.runtime_binding_options):
        raise ValueError(f"PlanBlock '{ block.id }' cannot embed generated runtime code.")


def _validate_execution_block_contract(block: ExecutionBlock) -> None:
    if str(block.kind) not in EXECUTION_BLOCK_KINDS:
        raise ValueError(
            f"ExecutionBlock '{ block.id }' kind must be one of { sorted(EXECUTION_BLOCK_KINDS) }, got: { block.kind }."
        )
    emits = block.signal_contract.get("emits", ())
    if isinstance(emits, str):
        normalized_emits = (emits,)
    elif emits is None:
        normalized_emits = ()
    elif isinstance(emits, (list, tuple, set)):
        normalized_emits = emits
    else:
        raise TypeError(f"ExecutionBlock '{ block.id }' signal_contract.emits must be a sequence.")
    unknown_signals = sorted(str(signal) for signal in normalized_emits if str(signal) not in STANDARD_BLOCK_SIGNALS)
    if unknown_signals:
        raise ValueError(
            f"ExecutionBlock '{ block.id }' emits unknown block signal(s): { ', '.join(unknown_signals) }."
        )
    for requirement in block.resource_requirements:
        if not any(
            key in requirement
            for key in (
                "resource",
                "resource_id",
                "capability",
                "capability_id",
                "need",
                "required_capability",
                "action_id",
            )
        ):
            raise ValueError(
                f"ExecutionBlock '{ block.id }' resource requirement must name a resource or capability."
            )


def _validate_plan_block_instance_kind(
    plan_block: PlanBlockInstance,
    registry: PlanBlockRegistry,
) -> None:
    kind = str(plan_block.kind or plan_block.plan_block_id).strip()
    if kind in PLAN_BLOCK_KINDS:
        return
    if registry.get(plan_block.plan_block_id) is not None:
        return
    raise ValueError(
        f"PlanBlockInstance '{ plan_block.id }' references unknown PlanBlock kind or id: { kind }."
    )


def _validate_plan_edges(
    plan_blocks: tuple[PlanBlockInstance, ...],
    edges: tuple[Any, ...],
) -> None:
    block_ids = {block.id for block in plan_blocks}
    for edge in edges:
        if edge.from_plan_block not in block_ids:
            raise ValueError(
                f"ExecutionPlan edge references missing from_plan_block: { edge.from_plan_block }."
            )
        if edge.to_plan_block not in block_ids:
            raise ValueError(
                f"ExecutionPlan edge references missing to_plan_block: { edge.to_plan_block }."
            )


def _contains_generated_code(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in {"code", "python_code", "generated_code", "chunk_code"}:
                return True
            if _contains_generated_code(item):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_contains_generated_code(item) for item in value)
    return False


def _enforce_capability_resolution(
    resolution: CapabilityResolution | None,
    plan_blocks: tuple[PlanBlockInstance, ...],
) -> None:
    if resolution is None:
        return
    denied = set(resolution.denied_capabilities)
    allowed = set(resolution.allowed_capabilities)
    pending = _pending_capability_names(resolution.pending_approvals)
    scoped = _scoped_capability_names(resolution.scoped_action_candidates)
    approval_capabilities = _approval_wait_capability_names(plan_blocks)
    allowlist_active = bool(allowed or scoped or pending)
    for block in plan_blocks:
        if str(block.kind or block.plan_block_id) == "approval_wait":
            continue
        for requirement in block.capability_requirements:
            names = _capability_names_from_requirement(requirement)
            if denied.intersection(names):
                raise PermissionError(
                    f"PlanBlockInstance '{ block.id }' requires denied capability: "
                    f"{ ', '.join(sorted(denied.intersection(names))) }."
                )
            pending_names = pending.intersection(names)
            unresolved_pending = pending_names.difference(approval_capabilities)
            if unresolved_pending:
                raise PermissionError(
                    f"PlanBlockInstance '{ block.id }' requires capability pending approval: "
                    f"{ ', '.join(sorted(unresolved_pending)) }."
                )
            # Empty CapabilityResolution means no allow-list gate is active.
            # Denied capabilities are always enforced above; positive allow-list
            # enforcement starts only when the host supplies allowed, scoped, or
            # pending entries.
            if names and allowlist_active:
                unresolved = names.difference(allowed).difference(scoped).difference(approval_capabilities)
                if unresolved:
                    raise PermissionError(
                        f"PlanBlockInstance '{ block.id }' requires unresolved capability: "
                        f"{ ', '.join(sorted(unresolved)) }."
                    )


def _capability_names_from_requirement(requirement: Mapping[str, Any]) -> set[str]:
    return {
        str(requirement.get(key)).strip()
        for key in ("capability", "capability_id", "need", "required_capability", "action_id")
        if requirement.get(key) is not None and str(requirement.get(key)).strip()
    }


def _pending_capability_names(items: tuple[dict[str, Any], ...]) -> set[str]:
    names: set[str] = set()
    for item in items:
        names.update(_capability_names_from_requirement(item))
    return names


def _scoped_capability_names(items: tuple[dict[str, Any], ...]) -> set[str]:
    names: set[str] = set()
    for item in items:
        names.update(_capability_names_from_requirement(item))
    return names


def _approval_wait_capability_names(plan_blocks: tuple[PlanBlockInstance, ...]) -> set[str]:
    names: set[str] = set()
    for block in plan_blocks:
        if str(block.kind or block.plan_block_id) != "approval_wait":
            continue
        bound_inputs = block.bound_inputs if isinstance(block.bound_inputs, Mapping) else {}
        request = bound_inputs.get("request")
        if isinstance(request, Mapping):
            names.update(_capability_names_from_requirement(request))
        names.update(_capability_names_from_requirement(bound_inputs))
    return names


def _execution_kind_for_plan_kind(kind: str) -> str:
    if kind in _ACTION_LIKE_PLAN_BLOCK_KINDS:
        return "action_call"
    if kind == "dag_segment":
        return "dag_node"
    if kind == "observation":
        return "snapshot"
    return kind if kind in {
        "model_request",
        "action_call",
        "skill_activation",
        "workspace_operation",
        "validation",
        "approval_wait",
        "external_wait",
        "flow_segment",
        "emit",
        "agent_step",
    } else "emit"


def _plan_input_bindings(plan_block: PlanBlockInstance) -> dict[str, Any]:
    bindings = {
        "bound_inputs": plan_block.bound_inputs,
        "intent": plan_block.intent,
        "runtime_preferences": dict(plan_block.runtime_preferences),
    }
    handler = plan_block.runtime_preferences.get("handler") or plan_block.runtime_preferences.get("chunk_binding")
    if handler is not None:
        bindings["handler"] = handler
    return bindings


def _extract_dag_graph(bound_inputs: Any) -> TaskDAG | Mapping[str, Any]:
    if isinstance(bound_inputs, TaskDAG):
        return bound_inputs
    if isinstance(bound_inputs, Mapping):
        graph = bound_inputs.get("task_dag", bound_inputs.get("graph", bound_inputs))
        if isinstance(graph, (TaskDAG, Mapping)):
            return graph
    raise ValueError("dag_segment PlanBlockInstance requires bound_inputs.task_dag or bound_inputs.graph.")


def _extract_dag_validation(bound_inputs: Any, validator: TaskDAGValidator) -> TaskDAGValidation:
    if isinstance(bound_inputs, Mapping):
        validation = bound_inputs.get("task_dag_validation")
        if isinstance(validation, TaskDAGValidation):
            return validation
    return validator.validate(_extract_dag_graph(bound_inputs))


def _extract_dag_handler_prefix(bound_inputs: Any) -> str | None:
    if not isinstance(bound_inputs, Mapping):
        return None
    prefix = bound_inputs.get("handler_prefix")
    if prefix is None:
        return None
    text = str(prefix).strip()
    return text or None


def _merge_mapping(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    merged.update(dict(right))
    return merged


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _ledger_items_from_blocks_state(
    *,
    execution_results: Any,
    plan_results: Any,
    diagnostics: Any,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, result in enumerate(_mapping_sequence(execution_results)):
        items.extend(_ledger_items_from_execution_result(result, index=index))
    for index, result in enumerate(_mapping_sequence(plan_results)):
        items.append(
            _ledger_item(
                "plan_block",
                index=len(items),
                status=_ledger_status_from_result(result),
                body_state=_ledger_body_state_from_value(result),
                body=_ledger_body_from_value(result),
                provenance={
                    "plan_block_id": result.get("plan_block_id"),
                    "source_plan_block_id": result.get("source_plan_block_id"),
                },
                raw_status=result.get("status", result.get("success")),
                extra={
                    key: result.get(key)
                    for key in ("plan_block_id", "source_plan_block_id", "output_ref")
                    if result.get(key) not in (None, "", [], {})
                },
            )
        )
    for index, diagnostic in enumerate(_mapping_sequence(diagnostics)):
        items.append(
            _ledger_item(
                "diagnostic",
                index=len(items),
                status="failed",
                body_state="bounded",
                body=diagnostic.get("message") or diagnostic.get("reason") or diagnostic,
                provenance={
                    "source": "blocks.diagnostics",
                    "execution_block_id": diagnostic.get("execution_block_id"),
                    "source_plan_block_id": diagnostic.get("source_plan_block_id"),
                },
                raw_status="failed",
                diagnostics=(diagnostic,),
                extra={"diagnostic_index": index, "code": diagnostic.get("code")},
            )
        )
    return items


def _ledger_items_from_execution_result(result: Mapping[str, Any], *, index: int) -> list[dict[str, Any]]:
    output = result.get("output")
    output_map = dict(output) if isinstance(output, Mapping) else {}
    operation = str(output_map.get("operation") or "").strip()
    block_kind = str(result.get("kind") or "execution_block").strip() or "execution_block"
    kind = f"{block_kind}.{operation}" if operation else block_kind
    diagnostics = _mapping_sequence(output_map.get("diagnostics"))
    status = _ledger_status_from_result(result)
    if operation in {"search", "scoped_search"}:
        bounded = output_map.get("bounded")
        returned_results = 0
        if isinstance(bounded, Mapping):
            try:
                returned_results = int(bounded.get("returned_results") or 0)
            except (TypeError, ValueError):
                returned_results = 0
        if returned_results <= 0:
            status = "failed" if diagnostics else "empty"
    body_state = _ledger_body_state_from_value(output_map) if output_map else "ref_only"
    items = [
        _ledger_item(
            kind,
            index=index,
            status=status,
            body_state=body_state,
            body=_ledger_body_from_value(output_map),
            provenance=_ledger_provenance(result, source="blocks.execution_block_results"),
            raw_status=result.get("status", result.get("success")),
            diagnostics=diagnostics,
            extra={
                **({"block_kind": result.get("kind")} if result.get("kind") not in (None, "", [], {}) else {}),
                **{
                    key: result.get(key)
                    for key in (
                        "id",
                        "execution_block_id",
                        "source_plan_block_id",
                        "source_task_dag_node_id",
                        "cancelled",
                        "waiting",
                    )
                    if result.get(key) not in (None, "", [], {})
                },
            },
        )
    ]
    if operation in {"search", "scoped_search"}:
        items.extend(_ledger_items_from_workspace_search(result, output_map, start_index=len(items)))
    elif operation == "read_bounded":
        items.extend(_ledger_items_from_workspace_readback(result, output_map, start_index=len(items)))
    items.extend(_ledger_items_from_nested_execution_meta(result, output_map, start_index=len(items)))
    return items


def _ledger_items_from_workspace_search(
    result: Mapping[str, Any],
    output: Mapping[str, Any],
    *,
    start_index: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, ref in enumerate(_mapping_sequence(output.get("locator_refs"))):
        items.append(
            _ledger_item(
                "locator_ref",
                index=start_index + len(items),
                status="ok",
                body_state="ref_only",
                body=ref.get("summary") or ref.get("title") or ref.get("label"),
                provenance=_ledger_workspace_provenance(result, ref, source="blocks.workspace_operation.search"),
                raw_status="ok",
                extra=_ledger_ref_fields(ref, evidence_role="locator_ref", query=output.get("query")),
            )
        )
    for index, snippet in enumerate(_mapping_sequence(output.get("evidence_snippets"))):
        body = (
            snippet.get("content")
            if snippet.get("content") is not None
            else snippet.get("snippet", snippet.get("text"))
        )
        body_state = "truncated" if snippet.get("truncated") is True else "bounded"
        items.append(
            _ledger_item(
                "evidence_snippet",
                index=start_index + len(items),
                status="ok",
                body_state=body_state,
                body=body,
                provenance=_ledger_workspace_provenance(result, snippet, source="blocks.workspace_operation.search"),
                raw_status="ok",
                extra=_ledger_ref_fields(snippet, evidence_role="evidence_snippet", query=output.get("query")),
            )
        )
    for diagnostic in _mapping_sequence(output.get("diagnostics")):
        items.append(
            _ledger_item(
                "workspace_operation.diagnostic",
                index=start_index + len(items),
                status="failed",
                body_state="bounded",
                body=diagnostic.get("message") or diagnostic,
                provenance=_ledger_workspace_provenance(result, diagnostic, source="blocks.workspace_operation.search"),
                raw_status="failed",
                diagnostics=(diagnostic,),
                extra=_ledger_ref_fields(diagnostic, evidence_role="diagnostic", query=output.get("query")),
            )
        )
    return items


def _ledger_items_from_workspace_readback(
    result: Mapping[str, Any],
    output: Mapping[str, Any],
    *,
    start_index: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    locator = output.get("locator_ref")
    if isinstance(locator, Mapping):
        items.append(
            _ledger_item(
                "locator_ref",
                index=start_index + len(items),
                status="ok",
                body_state="ref_only",
                provenance=_ledger_workspace_provenance(result, locator, source="blocks.workspace_operation.read_bounded"),
                raw_status="ok",
                extra=_ledger_ref_fields(locator, evidence_role="locator_ref"),
            )
        )
    for snippet in _mapping_sequence(output.get("evidence_snippets")):
        body = (
            snippet.get("content")
            if snippet.get("content") is not None
            else snippet.get("snippet", snippet.get("text"))
        )
        body_state = "truncated" if snippet.get("truncated") is True else "bounded"
        items.append(
            _ledger_item(
                "readback",
                index=start_index + len(items),
                status="ok",
                body_state=body_state,
                body=body,
                provenance=_ledger_workspace_provenance(result, snippet, source="blocks.workspace_operation.read_bounded"),
                raw_status="ok",
                extra=_ledger_ref_fields(snippet, evidence_role="readback"),
            )
        )
    return items


def _ledger_items_from_nested_execution_meta(
    result: Mapping[str, Any],
    output: Mapping[str, Any],
    *,
    start_index: int,
) -> list[dict[str, Any]]:
    execution_meta = output.get("execution_meta")
    if not isinstance(execution_meta, Mapping):
        return []
    logs = execution_meta.get("logs")
    logs = logs if isinstance(logs, Mapping) else {}
    items: list[dict[str, Any]] = []
    for action_index, action in enumerate(_ledger_action_records(logs)):
        action_id = str(action.get("id") or action.get("name") or "").strip()
        action_call_id = action.get("action_call_id")
        status = _ledger_status_from_action(action)
        body = action.get("result_preview")
        if body in (None, "", [], {}):
            body = action.get("error") or action.get("message")
        body_state = "bounded" if body not in (None, "", [], {}) else "ref_only"
        aliases = _ledger_action_ref_aliases(
            action,
            action_id=action_id,
            action_call_id=action_call_id,
        )
        extra = {
            "action_id": action_id,
            "action_call_id": action_call_id,
            "action_type": action.get("action_type"),
        }
        if aliases:
            extra["aliases"] = aliases
        items.append(
            _ledger_item(
                "action_evidence",
                index=start_index + len(items),
                status=status,
                body_state=body_state,
                body=body,
                provenance={
                    **_ledger_provenance(result, source="execution_meta.logs"),
                    "execution_id": execution_meta.get("execution_id"),
                    "action_id": action_id,
                    "action_call_id": action_call_id,
                },
                raw_status=action.get("status"),
                diagnostics=_action_diagnostics(action),
                extra=extra,
            )
        )
    for ref in _mapping_sequence(logs.get("artifact_refs")):
        items.append(
            _ledger_item(
                "artifact_ref",
                index=start_index + len(items),
                status=_ledger_status_from_ref(ref),
                body_state=_ledger_body_state_from_ref(ref),
                body=_ledger_body_from_value(ref),
                provenance={**_ledger_provenance(result, source="execution_meta.logs.artifact_refs")},
                raw_status=ref.get("status", "ok"),
                extra=_ledger_ref_fields(ref, evidence_role="artifact_ref"),
            )
        )
    for ref in _mapping_sequence(logs.get("source_refs")):
        items.append(
            _ledger_item(
                "source_ref",
                index=start_index + len(items),
                status="ok",
                body_state=_ledger_body_state_from_ref(ref),
                body=_ledger_body_from_value(ref),
                provenance={**_ledger_provenance(result, source="execution_meta.logs.source_refs")},
                raw_status=ref.get("status", "ok"),
                extra=_ledger_ref_fields(ref, evidence_role="source_ref"),
            )
        )
    for error in _error_sequence(logs.get("errors")):
        items.append(
            _ledger_item(
                "execution_error",
                index=start_index + len(items),
                status="failed",
                body_state="bounded",
                body=error.get("message") or error,
                provenance={**_ledger_provenance(result, source="execution_meta.logs.errors")},
                raw_status="failed",
                diagnostics=(error,),
                extra={"type": error.get("type")},
            )
        )
    diagnostics = execution_meta.get("diagnostics")
    if isinstance(diagnostics, Mapping) and diagnostics.get("execution_error") is not None:
        error = diagnostics.get("execution_error")
        error_map = dict(error) if isinstance(error, Mapping) else {"message": str(error)}
        items.append(
            _ledger_item(
                "execution_error",
                index=start_index + len(items),
                status="failed",
                body_state="bounded",
                body=error_map.get("message") or error_map,
                provenance={**_ledger_provenance(result, source="execution_meta.diagnostics.execution_error")},
                raw_status="failed",
                diagnostics=(error_map,),
                extra={"type": error_map.get("type")},
            )
        )
    return items


def _ledger_item(
    kind: str,
    *,
    index: int,
    status: str,
    body_state: str,
    provenance: Mapping[str, Any] | None = None,
    raw_status: Any = None,
    body: Any = None,
    diagnostics: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    clean_kind = str(kind or "evidence").strip() or "evidence"
    provenance_dict = {
        str(key): value
        for key, value in dict(provenance or {}).items()
        if value not in (None, "", [], {})
    }
    item = {
        "id": _ledger_item_id(clean_kind, index, provenance_dict, extra),
        "kind": clean_kind,
        "status": status if status in {"ok", "failed", "empty"} else "ok",
        "raw_status": raw_status if raw_status is not None else status,
        "body_state": body_state if body_state in {"full", "bounded", "truncated", "ref_only"} else "ref_only",
        "provenance": provenance_dict,
    }
    if body not in (None, "", [], {}):
        item["body"] = body if isinstance(body, str) else str(body)
    diagnostics_items = _mapping_sequence(diagnostics)
    if diagnostics_items:
        item["diagnostics"] = diagnostics_items
    for key, value in dict(extra or {}).items():
        if value not in (None, "", [], {}):
            item[str(key)] = value
    return item


def _ledger_item_id(
    kind: str,
    index: int,
    provenance: Mapping[str, Any],
    extra: Mapping[str, Any] | None,
) -> str:
    parts = [
        kind,
        provenance.get("execution_block_id"),
        provenance.get("source_plan_block_id"),
        provenance.get("action_call_id"),
        provenance.get("action_id"),
    ]
    extra = extra or {}
    parts.extend(extra.get(key) for key in ("record_id", "path", "artifact_id", "evidence_role"))
    parts.append(str(index))
    raw = ":".join(str(part).strip() for part in parts if str(part or "").strip())
    if not raw:
        raw = f"{kind}:{index}"
    return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in raw)[:240]


def _ledger_provenance(result: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "execution_block_id": result.get("execution_block_id") or result.get("id"),
        "source_plan_block_id": result.get("source_plan_block_id"),
        "source_task_dag_node_id": result.get("source_task_dag_node_id"),
    }


def _ledger_workspace_provenance(
    result: Mapping[str, Any],
    ref: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    provenance = _ledger_provenance(result, source=source)
    for key in ("record_id", "path", "collection", "kind", "source_url", "selected_url", "requested_url", "url", "href"):
        if ref.get(key) not in (None, "", [], {}):
            provenance[key] = ref.get(key)
    raw_ref = ref.get("ref")
    if isinstance(raw_ref, Mapping):
        for key in ("id", "path", "collection", "kind"):
            if raw_ref.get(key) not in (None, "", [], {}):
                provenance[f"ref_{key}"] = raw_ref.get(key)
    return provenance


def _ledger_ref_fields(ref: Mapping[str, Any], *, evidence_role: str, query: Any = None) -> dict[str, Any]:
    fields: dict[str, Any] = {"evidence_role": evidence_role}
    for key in (
        "path",
        "record_id",
        "collection",
        "source_url",
        "selected_url",
        "requested_url",
        "canonical_url",
        "url",
        "href",
        "artifact_id",
        "content_state",
        "truncated",
        "line",
        "line_start",
        "line_end",
    ):
        if ref.get(key) not in (None, "", [], {}):
            fields[key] = ref.get(key)
    if query not in (None, "", [], {}):
        fields["query"] = query
    raw_ref = ref.get("ref")
    if isinstance(raw_ref, Mapping):
        fields["ref"] = {
            key: raw_ref.get(key)
            for key in ("id", "path", "collection", "kind", "summary", "title", "label")
            if raw_ref.get(key) not in (None, "", [], {})
        }
    return fields


def _ledger_body_from_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in (
            "body",
            "content",
            "content_preview",
            "snippet",
            "text",
            "preview",
            "result_preview",
            "answer",
            "step_result",
        ):
            item = value.get(key)
            if item not in (None, "", [], {}):
                return item
    return None


def _ledger_body_state_from_value(value: Any) -> str:
    if isinstance(value, Mapping):
        raw = str(value.get("body_state") or value.get("content_state") or "").strip()
        if raw == "ref_only":
            return "ref_only"
        if value.get("truncated") is True:
            return "truncated"
        if _ledger_body_from_value(value) not in (None, "", [], {}):
            return "bounded"
    return "ref_only"


def _ledger_body_state_from_ref(ref: Mapping[str, Any]) -> str:
    raw = str(ref.get("body_state") or ref.get("content_state") or "").strip()
    if raw == "ref_only":
        return "ref_only"
    if raw in {"full", "bounded", "truncated"}:
        return raw
    if ref.get("truncated") is True:
        return "truncated"
    if _ledger_body_from_value(ref) not in (None, "", [], {}):
        return "bounded"
    return "ref_only"


def _ledger_status_from_result(result: Mapping[str, Any]) -> str:
    raw_status = str(result.get("status") or "").strip().lower()
    if raw_status in {"failed", "failure", "error", "timed_out", "timeout", "blocked"}:
        return "failed"
    if raw_status in {"empty", "not_found", "no_results", "missing"}:
        return "empty"
    if result.get("success") is False or result.get("cancelled") is True:
        return "failed"
    return "ok"


def _ledger_status_from_action(action: Mapping[str, Any]) -> str:
    raw_status = str(action.get("status") or "").strip().lower()
    if raw_status in {"failed", "failure", "error", "timed_out", "timeout", "blocked"}:
        return "failed"
    if raw_status in {"empty", "not_found", "no_results", "missing"}:
        return "empty"
    if action.get("error") not in (None, "", [], {}):
        return "failed"
    return "ok"


def _ledger_status_from_ref(ref: Mapping[str, Any]) -> str:
    raw_status = str(ref.get("status") or "").strip().lower()
    if raw_status in {"failed", "failure", "error", "timed_out", "timeout", "blocked"}:
        return "failed"
    if raw_status in {"empty", "not_found", "no_results", "missing"}:
        return "empty"
    return "ok"


def _mapping_sequence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _new_ledger_items(existing: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen = {str(item.get("id") or "") for item in existing if str(item.get("id") or "").strip()}
    additions: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence_id = str(candidate.get("id") or "").strip()
        if evidence_id and evidence_id in seen:
            continue
        if evidence_id:
            seen.add(evidence_id)
        additions.append(dict(candidate))
    return additions


def _error_sequence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) if isinstance(item, Mapping) else {"message": str(item)} for item in value]
    return [{"message": str(value)}]


def _ledger_action_records(logs: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def add_entries(entries: Any) -> None:
        if isinstance(entries, Mapping):
            for action_id, record in entries.items():
                if isinstance(record, Mapping):
                    action_record = dict(record)
                    action_record.setdefault("id", str(action_id))
                    records.append(action_record)
                else:
                    records.append({"id": str(action_id), "name": str(action_id), "status": str(record or "")})
        elif isinstance(entries, Sequence) and not isinstance(entries, str | bytes | bytearray):
            for item in entries:
                if isinstance(item, Mapping):
                    records.append(dict(item))

    add_entries(logs.get("action_logs", {}))
    route_logs = logs.get("route_logs", {})
    if isinstance(route_logs, Mapping):
        add_entries(route_logs.get("action_logs", {}))
        route_output = route_logs.get("output", {})
        if isinstance(route_output, Mapping):
            add_entries(route_output.get("history", []))
    return records


def _action_diagnostics(action: Mapping[str, Any]) -> list[dict[str, Any]]:
    error = action.get("error")
    if error is None:
        return []
    if isinstance(error, Mapping):
        return [dict(error)]
    return [{"message": str(error)}]


def _ledger_action_ref_aliases(
    action: Mapping[str, Any],
    *,
    action_id: str,
    action_call_id: Any,
) -> list[str]:
    prefixes = _dedupe_strings(
        (
            action_id,
            action_call_id,
            f"action_{ action_id }" if action_id else "",
            f"action_result_{ action_id }" if action_id else "",
            f"action_{ action_call_id }" if action_call_id else "",
            f"action_result_{ action_call_id }" if action_call_id else "",
        )
    )
    refs = _ledger_action_ref_values(action)
    aliases: list[str] = []
    seen: set[str] = set()
    for prefix in prefixes:
        for ref in refs:
            alias = f"{ prefix }:{ ref }"
            if alias in seen:
                continue
            seen.add(alias)
            aliases.append(alias)
            if len(aliases) >= 32:
                return aliases
    return aliases


def _ledger_action_ref_values(action: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, Mapping):
            for nested_key in _ACTION_REF_ALIAS_FIELDS:
                nested = value.get(nested_key)
                if nested not in (None, "", [], {}):
                    add(nested)
            return
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for item in list(value)[:24]:
                add(item)
            return
        text = str(value or "").strip()
        if not text or "\n" in text or len(text) > 500:
            return
        if text in seen:
            return
        seen.add(text)
        refs.append(text)

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key)
                if key_text in _ACTION_REF_ALIAS_FIELDS:
                    add(item)
                    continue
                if key_text in {"body", "content", "text", "snippet", "preview", "result_preview"}:
                    continue
                visit(item, depth=depth + 1)
            return
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for item in list(value)[:24]:
                visit(item, depth=depth + 1)

    for key in ("input_preview", "result_preview", "raw", "artifact_refs", "file_refs"):
        value = action.get(key)
        if value not in (None, "", [], {}):
            visit(value)
    return refs[:32]


def _dedupe_strings(values: Sequence[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _evidence_kinds_for(kind: str) -> tuple[str, ...]:
    if kind == "action_call":
        return ("action_evidence", "capability_evidence")
    if kind == "skill_activation":
        return ("skill_evidence",)
    if kind == "workspace_operation":
        return ("workspace_refs",)
    if kind == "validation":
        return ("validation_results",)
    return ("execution_block_results",)


def _block_event(graph_id: str, block_id: str, event: str) -> str:
    return f"blocks.{ graph_id }.{ block_id }.{ event }"


def _chunk_name(graph_id: str, block_id: str) -> str:
    sanitized = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in f"{ graph_id }:{ block_id }")
    return f"blocks:{ sanitized }"


async def _execute_block(block: ExecutionBlock, data: TriggerFlowRuntimeData) -> Any:
    handler = _resolve_runtime_handler(block, data)
    if handler is not None:
        blocks_state = data.get_state("blocks", {}) or {}
        result = handler(
            {
                "block": block,
                "input": data.value,
                "graph_input": data.get_state("blocks.graph_input", data.value),
                "dependency_results": _dependency_results_for_block(block, blocks_state),
                "runtime_data": data,
                "state": blocks_state,
            }
        )
        if inspect.isawaitable(result):
            result = await result
        return result
    if block.kind == "skill_activation":
        return await _execute_skill_activation_block(block, data)
    if block.kind == "workspace_operation":
        return await _execute_workspace_operation_block(block, data)
    if block.kind == "approval_wait":
        return await _execute_approval_wait_block(block, data)
    if block.kind == "external_wait":
        return await _execute_external_wait_block(block, data)
    if block.kind in _HANDLER_REQUIRED_EXECUTION_KINDS:
        raise RuntimeError(
            f"ExecutionBlock '{ block.id }' kind '{ block.kind }' requires a trusted runtime handler."
        )
    if block.kind == "validation":
        return {"ok": True, "input": data.value, "contract": dict(block.evidence_mapping_contract)}
    if block.kind in {"emit", "snapshot"}:
        return {
            "input": data.value,
            "bindings": dict(block.input_bindings),
            "state": data.get_state("blocks", {}) or {},
        }
    if block.kind in {"fan_out", "fan_in"}:
        return {"input": data.value, "bindings": dict(block.input_bindings)}
    if block.kind == "dag_node":
        task = block.input_bindings.get("task")
        if isinstance(task, Mapping):
            task_kind = str(task.get("kind") or "")
            task_id = str(task.get("id") or block.source_task_dag_node_id or block.id)
            if task_kind == "validate":
                return {
                    "ok": True,
                    "task_id": task_id,
                    "inputs": task.get("inputs", {}),
                    "dependency_payload": data.value,
                }
            if task_kind == "emit":
                return {
                    "task_id": task_id,
                    "payload": task.get("inputs", {}),
                    "dependency_payload": data.value,
                }
        raise RuntimeError(
            f"ExecutionBlock '{ block.id }' kind 'dag_node' requires a TaskDAG resolver runtime handler."
        )
    return {"input": data.value, "bindings": dict(block.input_bindings)}


async def _execute_workspace_operation_block(block: ExecutionBlock, data: TriggerFlowRuntimeData) -> dict[str, Any]:
    workspace = data.get_resource("workspace", None)
    if workspace is None:
        raise RuntimeError(
            f"ExecutionBlock '{ block.id }' kind 'workspace_operation' requires runtime resource 'workspace'."
        )
    bound_inputs = block.input_bindings.get("bound_inputs", {})
    if not isinstance(bound_inputs, Mapping):
        bound_inputs = {}
    operation = str(bound_inputs.get("operation") or bound_inputs.get("op") or "ingest").strip()
    if operation == "ingest":
        ref = await workspace.ingest(
            content=bound_inputs.get("content", data.value),
            collection=str(bound_inputs.get("collection") or "observations"),
            kind=str(bound_inputs.get("kind") or "blocks_workspace_operation"),
            scope=dict(bound_inputs.get("scope") or {}),
            source=dict(bound_inputs.get("source") or {"type": "blocks", "execution_block_id": block.id}),
            summary=bound_inputs.get("summary"),
            meta=dict(bound_inputs.get("meta") or {}),
        )
        return {"operation": operation, "workspace_refs": [ref], "ref": ref}
    if operation == "put_checkpoint":
        ref = await workspace.put_checkpoint(
            str(bound_inputs.get("run_id") or block.source_plan_block_id or block.id),
            bound_inputs.get("state", data.value),
            step_id=bound_inputs.get("step_id"),
        )
        return {"operation": operation, "workspace_refs": [ref], "ref": ref}
    if operation == "get_data":
        ref_or_path = bound_inputs.get("ref") or bound_inputs.get("path")
        if ref_or_path is None:
            raise ValueError(f"ExecutionBlock '{ block.id }' workspace get_data requires ref or path.")
        return {"operation": operation, "data": await workspace.get_data(ref_or_path)}
    if operation in {"search", "scoped_search"}:
        query = bound_inputs.get("query")
        filters = bound_inputs.get("filters")
        if not isinstance(filters, Mapping):
            filters = {}
        max_results = _bounded_int(bound_inputs.get("max_results"), default=8, minimum=1, maximum=50)
        snippet_limit = _bounded_int(bound_inputs.get("snippet_limit"), default=1200, minimum=1, maximum=12000)
        snippet_offset = _bounded_int(bound_inputs.get("snippet_offset"), default=0, minimum=0, maximum=10_000_000)
        include_snippets = bool(bound_inputs.get("include_snippets", False))
        raw_surface = str(
            bound_inputs.get("search_surface")
            or bound_inputs.get("surface")
            or filters.get("search_surface")
            or "auto"
        ).strip().lower()
        surface_aliases = {
            "records": "workspace_index",
            "index": "workspace_index",
            "sqlite": "workspace_index",
            "fts": "workspace_index",
            "files": "workspace_files",
            "grep": "workspace_files",
            "both": "workspace_index_and_files",
            "records_and_files": "workspace_index_and_files",
            "files_and_records": "workspace_index_and_files",
        }
        search_surface = surface_aliases.get(raw_surface, raw_surface or "auto")
        path_scope = bound_inputs.get("path", filters.get("path", "."))
        pattern = str(bound_inputs.get("pattern", filters.get("pattern", "*")) or "*")
        include_hidden = bool(bound_inputs.get("include_hidden", filters.get("include_hidden", False)))
        max_file_bytes = _bounded_int(
            bound_inputs.get("max_file_bytes", filters.get("max_file_bytes")),
            default=200000,
            minimum=1,
            maximum=5_000_000,
        )
        file_scope_requested = bound_inputs.get("path") is not None or bound_inputs.get("pattern") is not None
        search_index = search_surface not in {"workspace_files"}
        search_files = search_surface in {"workspace_files", "workspace_index_and_files"} or (
            search_surface == "auto" and file_scope_requested
        )
        context_lines = _bounded_int(
            bound_inputs.get("context_lines", filters.get("context_lines")),
            default=3 if search_files else 0,
            minimum=0,
            maximum=20,
        )
        evidence_snippets: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        retrieve_func = getattr(workspace, "retrieve", None)
        if not callable(retrieve_func):
            raise RuntimeError("Workspace does not expose retrieve(...), which is required for workspace_operation.search.")
        sources: list[str] = []
        if search_index:
            sources.append("records")
        if search_files:
            sources.append("files")
        retrieval_selection = str(bound_inputs.get("selection") or "top_n").strip().lower()
        if retrieval_selection not in {"length", "top_n"}:
            retrieval_selection = "top_n"
        top_n_value = bound_inputs.get("top_n", max_results)
        top_n = _bounded_int(top_n_value, default=max_results, minimum=1, maximum=50)
        max_candidates = _bounded_int(
            bound_inputs.get("max_candidates"),
            default=max(max_results * 3, max_results),
            minimum=1,
            maximum=500,
        )
        retrieval_snippets = include_snippets or search_files
        retrieval_budget = {
            "chars": max(1, max_results * max(1, snippet_limit if retrieval_snippets else 200)),
            "item_chars": max(1, snippet_limit if retrieval_snippets else 1),
            "max_candidates": max_candidates,
            "top_n": top_n,
        }
        method = str(bound_inputs.get("method") or "auto").strip().lower()
        if method not in {"auto", "keyword", "vector", "hybrid"}:
            method = "auto"
        rerank = _optional_bool(bound_inputs.get("rerank"))
        package = retrieve_func(
            str(query) if query is not None else None,
            tags=_string_list(bound_inputs.get("tags", filters.get("tags"))),
            filters=dict(filters),
            sources=sources,
            budget=retrieval_budget,
            selection=cast(Any, retrieval_selection),
            top_n=top_n,
            method=cast(Any, method),
            rerank=rerank,
            file_options={
                "path": path_scope,
                "pattern": pattern,
                "max_results": max_candidates,
                "include_hidden": include_hidden,
                "max_file_bytes": max_file_bytes,
                "context_lines": context_lines,
                "max_snippet_bytes": snippet_limit,
            },
            max_candidates=max_candidates,
        )
        if inspect.isawaitable(package):
            package = await package
        if not isinstance(package, Mapping):
            package = {}
        retrieval_items = [
            dict(item)
            for item in package.get("items", [])
            if isinstance(item, Mapping)
        ][:max_results]
        retrieval_diagnostics = dict(package.get("diagnostics") or {}) if isinstance(package.get("diagnostics"), Mapping) else {}
        retrieval_omitted = [
            dict(item)
            for item in package.get("omitted", [])
            if isinstance(item, Mapping)
        ]
        selected_refs: list[dict[str, Any]] = []
        file_results: list[dict[str, Any]] = []
        locator_refs: list[dict[str, Any]] = []
        for index, item in enumerate(retrieval_items):
            if item.get("source") == "record" and isinstance(item.get("ref"), Mapping):
                ref = dict(cast(Mapping[str, Any], item["ref"]))
                selected_refs.append(ref)
                locator_ref = _workspace_locator_ref(
                    ref,
                    query=query,
                    filters=filters,
                    index=index,
                    source="blocks.workspace_operation.search",
                )
                locator_ref["workspace_source"] = "workspace.retrieve"
                _copy_optional_retrieval_rank(locator_ref, item)
                locator_refs.append(locator_ref)
                if include_snippets:
                    evidence_snippets.append(
                        _workspace_evidence_snippet_from_retrieval_item(
                            item,
                            locator_ref=locator_ref,
                            query=query,
                            filters=filters,
                            snippet_offset=snippet_offset,
                            source="blocks.workspace_operation.search",
                        )
                    )
                continue
            if item.get("source") == "file" and isinstance(item.get("file"), Mapping):
                file_result = dict(cast(Mapping[str, Any], item["file"]))
                file_results.append(file_result)
                locator_ref = _workspace_file_locator_ref_from_search_result(
                    file_result,
                    query=query,
                    index=index,
                    workspace_source="workspace.retrieve",
                )
                _copy_optional_retrieval_rank(locator_ref, item)
                locator_refs.append(locator_ref)
                if include_snippets or search_files:
                    evidence_snippets.append(
                        _workspace_file_evidence_snippet_from_retrieval_item(
                            item,
                            file_result=file_result,
                            locator_ref=locator_ref,
                        )
                    )
        results = [
            {
                **locator_ref,
                "evidence_snippet": evidence_snippets[index] if index < len(evidence_snippets) else None,
            }
            for index, locator_ref in enumerate(locator_refs)
        ]
        returned_snippet_bytes = sum(
            int(item.get("snippet_bytes") or len(str(item.get("content") or "").encode("utf-8")))
            for item in evidence_snippets
            if isinstance(item, Mapping)
        )
        candidate_bytes = sum(
            int(ref.get("size") or 0) for ref in selected_refs if isinstance(ref, Mapping)
        ) + sum(int(item.get("bytes") or 0) for item in file_results if isinstance(item, Mapping))
        search_engines = []
        if search_index:
            search_engines.append("workspace_retrieve_records")
        file_search_engines = sorted(
            {
                str(item.get("search_engine") or "workspace_file_search")
                for item in file_results
                if isinstance(item, Mapping)
            }
        )
        if search_files:
            search_engines.extend(file_search_engines or ["workspace_file_search"])
        return {
            "operation": "search",
            "query": query,
            "filters": dict(filters),
            "bounded": {
                "retrieval_strategy": "workspace.retrieve",
                "retrieval_method": retrieval_diagnostics.get("effective_method", method),
                "retrieval_requested_method": retrieval_diagnostics.get("method", method),
                "retrieval_effective_method": retrieval_diagnostics.get("effective_method", method),
                "retrieval_method_resolution": retrieval_diagnostics.get("method_resolution"),
                "retrieval_selection": retrieval_selection,
                "retrieval_rerank": retrieval_diagnostics.get("rerank"),
                "retrieval_candidate_count": retrieval_diagnostics.get("candidate_count"),
                "retrieval_selected_count": retrieval_diagnostics.get("selected_count"),
                "retrieval_omitted": retrieval_omitted,
                "search_surface": search_surface,
                "search_engines": search_engines,
                "max_results": max_results,
                "total_matches": int(retrieval_diagnostics.get("candidate_count") or len(retrieval_items)),
                "index_total_matches": int(
                    retrieval_diagnostics.get("deterministic_record_candidates") or len(selected_refs)
                ),
                "index_returned_results": len(selected_refs),
                "file_returned_results": len(file_results),
                "returned_results": len(locator_refs),
                "include_snippets": include_snippets,
                "snippet_offset": snippet_offset,
                "snippet_limit": snippet_limit,
                "file_path": str(path_scope),
                    "file_pattern": pattern,
                    "max_file_bytes": max_file_bytes,
                    "context_lines": context_lines,
                    "candidate_bytes": candidate_bytes,
                    "returned_snippet_bytes": returned_snippet_bytes,
                },
            "locator_refs": locator_refs,
            "evidence_snippets": evidence_snippets,
            "file_search_results": file_results,
            "results": results,
            "workspace_refs": selected_refs,
            "diagnostics": diagnostics,
        }
    if operation == "read_bounded":
        ref_or_path = bound_inputs.get("ref") or bound_inputs.get("path")
        if ref_or_path is None:
            raise ValueError(f"ExecutionBlock '{ block.id }' workspace read_bounded requires ref or path.")
        offset = _bounded_int(bound_inputs.get("offset"), default=0, minimum=0, maximum=10_000_000)
        limit = _bounded_int(bound_inputs.get("limit"), default=1200, minimum=1, maximum=12000)
        segment = await workspace.read_bounded(ref_or_path, offset=offset, limit=limit)
        locator_ref = _workspace_locator_ref_from_segment(
            segment,
            ref_or_path=ref_or_path,
            source="blocks.workspace_operation.read_bounded",
        )
        evidence_snippet = _workspace_evidence_snippet(
            segment,
            locator_ref=locator_ref,
            source="blocks.workspace_operation.read_bounded",
        )
        workspace_ref = locator_ref.get("ref")
        workspace_refs = [workspace_ref] if isinstance(workspace_ref, Mapping) else []
        return {
            "operation": operation,
            "bounded": {
                "offset": offset,
                "limit": limit,
            },
            "locator_ref": locator_ref,
            "evidence_snippet": evidence_snippet,
            "evidence_snippets": [evidence_snippet],
            "workspace_refs": workspace_refs,
        }
    if operation == "link_evidence":
        source_ref = bound_inputs.get("source_ref")
        target_ref = bound_inputs.get("target_ref")
        if not isinstance(source_ref, Mapping) or not isinstance(target_ref, Mapping):
            raise ValueError(
                f"ExecutionBlock '{ block.id }' workspace link_evidence requires source_ref and target_ref."
            )
        ref = await workspace.link_evidence(
            dict(source_ref),
            dict(target_ref),
            relation=str(bound_inputs.get("relation") or "blocks_relation"),
            meta=dict(bound_inputs.get("meta") or {}),
        )
        return {"operation": operation, "workspace_refs": [ref], "ref": ref}
    raise ValueError(f"Unsupported workspace_operation '{ operation }'.")


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _workspace_locator_ref(
    ref: Mapping[str, Any],
    *,
    query: Any = None,
    filters: Mapping[str, Any] | None = None,
    index: int = 0,
    source: str,
) -> dict[str, Any]:
    return {
        "role": "locator_ref",
        "content_state": "ref_only",
        "source": source,
        "query": query,
        "filters": dict(filters or {}),
        "rank": index + 1,
        "ref": dict(ref),
        "record_id": str(ref.get("id") or ""),
        "path": ref.get("path"),
        "collection": ref.get("collection"),
        "kind": ref.get("kind"),
        "summary": ref.get("summary") or "",
        "size": ref.get("size"),
        "sha256": ref.get("sha256"),
    }


def _workspace_locator_ref_from_segment(
    segment: Mapping[str, Any],
    *,
    ref_or_path: Any,
    source: str,
) -> dict[str, Any]:
    envelope = segment.get("ref")
    if isinstance(ref_or_path, Mapping):
        ref = dict(ref_or_path)
    elif isinstance(envelope, Mapping):
        ref = {
            "id": str(envelope.get("record_id") or ref_or_path or ""),
            "path": envelope.get("content_ref"),
            "collection": envelope.get("collection"),
            "kind": envelope.get("kind"),
            "size": envelope.get("size"),
            "sha256": envelope.get("digest"),
            "created_at": envelope.get("created_at"),
            "summary": "",
            "scope": {},
            "source": {},
            "meta": {},
        }
    else:
        ref = {"id": str(ref_or_path or ""), "path": str(ref_or_path or "")}
    locator = _workspace_locator_ref(ref, source=source)
    if isinstance(envelope, Mapping):
        locator["ref"] = ref
        locator["envelope"] = dict(envelope)
        locator["record_id"] = str(envelope.get("record_id") or locator.get("record_id") or "")
        locator["path"] = envelope.get("content_ref") or locator.get("path")
    return locator


def _workspace_evidence_snippet(
    segment: Mapping[str, Any],
    *,
    locator_ref: Mapping[str, Any],
    query: Any = None,
    filters: Mapping[str, Any] | None = None,
    source: str,
) -> dict[str, Any]:
    content = str(segment.get("content") or "")
    return {
        "role": "evidence_snippet",
        "content_state": "bounded_readback_available",
        "source": source,
        "query": query,
        "filters": dict(filters or {}),
        "locator_ref": dict(locator_ref),
        "content": content,
        "snippet": content,
        "snippet_chars": len(content),
        "snippet_bytes": len(content.encode("utf-8")),
        "truncated": not bool(segment.get("eof", True)),
        "offset": segment.get("offset"),
        "size": segment.get("size"),
        "total_size": segment.get("total_size"),
        "eof": segment.get("eof"),
        "digest": segment.get("digest"),
        "content_type": segment.get("content_type"),
    }


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    return None


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or None
    return None


def _copy_optional_retrieval_rank(target: dict[str, Any], item: Mapping[str, Any]) -> None:
    if item.get("score") is not None:
        target["score"] = item.get("score")
    if item.get("reason") not in (None, "", [], {}):
        target["reason"] = item.get("reason")


def _workspace_evidence_snippet_from_retrieval_item(
    item: Mapping[str, Any],
    *,
    locator_ref: Mapping[str, Any],
    query: Any = None,
    filters: Mapping[str, Any] | None = None,
    snippet_offset: int = 0,
    source: str,
) -> dict[str, Any]:
    content = str(item.get("content") or "")
    truncated = bool(item.get("truncated")) or content.endswith("\n[truncated]")
    snippet = {
        "role": "evidence_snippet",
        "content_state": item.get("content_state") or "bounded_readback_available",
        "source": source,
        "workspace_source": "workspace.retrieve",
        "query": query,
        "filters": dict(filters or {}),
        "locator_ref": dict(locator_ref),
        "record_id": locator_ref.get("record_id"),
        "path": locator_ref.get("path"),
        "collection": locator_ref.get("collection"),
        "kind": locator_ref.get("kind"),
        "content": content,
        "snippet": content,
        "snippet_chars": len(content),
        "snippet_bytes": len(content.encode("utf-8")),
        "truncated": truncated,
        "offset": snippet_offset,
        "size": len(content.encode("utf-8")),
        "total_size": item.get("raw_chars", item.get("chars")),
        "eof": not truncated,
        "digest": locator_ref.get("sha256"),
    }
    for key in ("body_state", "raw_chars", "projected_chars"):
        if item.get(key) not in (None, "", [], {}):
            snippet[key] = item.get(key)
    for key in ("projection", "original_ref"):
        value = item.get(key)
        if isinstance(value, Mapping):
            snippet[key] = dict(value)
    return snippet


def _workspace_file_locator_ref_from_search_result(
    item: Mapping[str, Any],
    *,
    query: Any,
    index: int,
    workspace_source: str,
) -> dict[str, Any]:
    raw_locator = item.get("locator_ref")
    locator_ref = dict(raw_locator) if isinstance(raw_locator, Mapping) else {}
    locator_ref.update(
        {
            "role": "locator_ref",
            "content_state": "ref_only",
            "source": "blocks.workspace_operation.search_files",
            "workspace_source": workspace_source,
            "query": query,
            "index": index,
        }
    )
    return locator_ref


def _workspace_file_evidence_snippet_from_retrieval_item(
    item: Mapping[str, Any],
    *,
    file_result: Mapping[str, Any],
    locator_ref: Mapping[str, Any],
) -> dict[str, Any]:
    content = str(item.get("content") or file_result.get("snippet") or file_result.get("text") or "")
    snippet = dict(file_result)
    snippet.update(
        {
            "role": "evidence_snippet",
            "content_state": "bounded_readback_available",
            "source": "blocks.workspace_operation.search_files",
            "workspace_source": "workspace.retrieve",
            "content": content,
            "snippet": content,
            "snippet_chars": len(content),
            "snippet_bytes": len(content.encode("utf-8")),
            "locator_ref": locator_ref,
        }
    )
    return snippet


async def _execute_approval_wait_block(block: ExecutionBlock, data: TriggerFlowRuntimeData) -> Any:
    policy_approval = data.get_resource("policy_approval", None)
    if policy_approval is None:
        from agently.base import policy_approval as default_policy_approval

        policy_approval = default_policy_approval
    bound_inputs = block.input_bindings.get("bound_inputs", {})
    if not isinstance(bound_inputs, Mapping):
        bound_inputs = {}
    request = bound_inputs.get("request")
    request_payload = dict(request) if isinstance(request, Mapping) else {}
    request_payload.setdefault("source", "blocks")
    request_payload.setdefault("capability", str(bound_inputs.get("capability") or "approval"))
    request_payload.setdefault("subject", str(bound_inputs.get("subject") or block.source_plan_block_id or block.id))
    request_payload.setdefault("payload", dict(bound_inputs.get("payload") or {}))
    request_payload.setdefault(
        "lineage",
        {
            "execution_block_id": block.id,
            "source_plan_block_id": block.source_plan_block_id,
        },
    )
    gate_result = await policy_approval.async_gate(
        data,
        request_payload,
        handler=bound_inputs.get("handler"),
        interrupt_id=bound_inputs.get("interrupt_id"),
        resume_to="self",
    )
    if data.execution.is_waiting():
        return gate_result
    decision = dict(gate_result) if isinstance(gate_result, Mapping) else {"value": gate_result}
    output: dict[str, Any] = {
        "decision": decision,
        "approved": decision.get("status") == "approved" or decision.get("approved") is True,
    }
    if not output["approved"]:
        output["replan_signal"] = {
            "status": "blocked",
            "reason": str(decision.get("reason") or "Policy approval was not approved."),
            "affected_execution_block_ids": [block.id],
        }
    return output


async def _execute_external_wait_block(block: ExecutionBlock, data: TriggerFlowRuntimeData) -> Any:
    bound_inputs = block.input_bindings.get("bound_inputs", {})
    if not isinstance(bound_inputs, Mapping):
        bound_inputs = {}
    if getattr(data, "is_resume", False):
        resume = getattr(data, "resume", None)
        return {"resumed": True, "payload": getattr(resume, "value", None)}
    return await data.async_pause_for(
        type=str(bound_inputs.get("type") or "external_wait"),
        exchange_kind=str(bound_inputs.get("exchange_kind") or "external"),
        payload=bound_inputs.get("payload", {}),
        interrupt_id=bound_inputs.get("interrupt_id"),
        resume_to="self",
    )


async def _execute_skill_activation_block(block: ExecutionBlock, data: TriggerFlowRuntimeData) -> dict[str, Any]:
    adapter = (
        data.get_resource("skills.capability_adapter", None)
        or data.get_resource("skills.adapter", None)
    )
    skills_executor = (
        data.get_resource("skills.executor", None)
        or data.get_resource("skills_executor", None)
    )
    if adapter is None and skills_executor is not None:
        factory = getattr(skills_executor, "capability_adapter", None)
        adapter = factory() if callable(factory) else skills_executor
    activate = getattr(adapter, "activate", None)
    if adapter is None or not callable(activate):
        raise RuntimeError(
            f"ExecutionBlock '{ block.id }' kind 'skill_activation' requires "
            "runtime resource 'skills.capability_adapter' or 'skills.executor'."
        )

    bound_inputs = block.input_bindings.get("bound_inputs", {})
    if not isinstance(bound_inputs, Mapping):
        bound_inputs = {}
    skill_ids = _skill_ids_from_bound_inputs(bound_inputs)
    if not skill_ids:
        raise ValueError(f"ExecutionBlock '{ block.id }' skill_activation requires skill_id or skill_ids.")
    task = str(bound_inputs.get("task") or block.input_bindings.get("intent") or "").strip() or None
    raw_budget = bound_inputs.get("budget_chars")
    try:
        budget_chars = int(raw_budget) if raw_budget is not None else 4000
    except (TypeError, ValueError):
        budget_chars = 4000

    activations: list[dict[str, Any]] = []
    skill_evidence: list[dict[str, Any]] = []
    capability_needs: list[dict[str, Any]] = []
    plan_block_recommendations: list[dict[str, Any]] = []
    recorder = getattr(adapter, "evidence_recorder", None)
    record_activation = getattr(recorder, "record_activation", None)
    for skill_id in skill_ids:
        activation = activate(skill_id, task=task, budget_chars=budget_chars)
        if inspect.isawaitable(activation):
            activation = await activation
        activation_dict = _dict_from_mapping_like(activation)
        activations.append(activation_dict)
        if callable(record_activation):
            evidence = _dict_from_mapping_like(record_activation(activation))
        else:
            evidence = {
                "skill_id": activation_dict.get("skill_id"),
                "loaded_guidance_refs": activation_dict.get("loaded_guidance_refs", []),
                "selected_resource_refs": activation_dict.get("selected_resource_refs", []),
                "capability_needs": activation_dict.get("capability_needs", []),
                "citations": activation_dict.get("citations", []),
                "proves_side_effect": False,
                "evidence_kind": "skill_context",
            }
        skill_evidence.append(evidence)
        capability_needs.extend(
            dict(item)
            for item in activation_dict.get("capability_needs", [])
            if isinstance(item, Mapping)
        )
        plan_block_recommendations.extend(
            dict(item)
            for item in activation_dict.get("plan_block_recommendations", [])
            if isinstance(item, Mapping)
        )
    return {
        "activations": activations,
        "skill_evidence": skill_evidence,
        "capability_needs": capability_needs,
        "plan_block_recommendations": plan_block_recommendations,
        "proves_side_effect": False,
    }


def _dict_from_mapping_like(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {"value": value}


def _skill_ids_from_bound_inputs(bound_inputs: Mapping[str, Any]) -> list[str]:
    raw = (
        bound_inputs.get("skill_ids")
        or bound_inputs.get("skills")
        or bound_inputs.get("skill_id")
        or bound_inputs.get("selected_skill_ids")
    )
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    skill_ids: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in skill_ids:
            skill_ids.append(text)
    return skill_ids


def _dependency_results_for_block(block: ExecutionBlock, blocks_state: Mapping[str, Any]) -> dict[str, Any]:
    task = block.input_bindings.get("task")
    if not isinstance(task, Mapping):
        return {}
    dependency_ids = task.get("depends_on", ())
    if isinstance(dependency_ids, str):
        dependency_ids = (dependency_ids,)
    if not isinstance(dependency_ids, (list, tuple)):
        return {}
    results = blocks_state.get("execution_block_results", ())
    by_task_id: dict[str, Any] = {}
    if isinstance(results, (list, tuple)):
        for item in results:
            if not isinstance(item, Mapping):
                continue
            task_id = item.get("source_task_dag_node_id")
            if task_id is not None:
                by_task_id[str(task_id)] = item.get("output")
    return {
        str(task_id): by_task_id[str(task_id)]
        for task_id in dependency_ids
        if str(task_id) in by_task_id
    }


def _resolve_runtime_handler(block: ExecutionBlock, data: TriggerFlowRuntimeData) -> Callable[[Mapping[str, Any]], Any] | None:
    handler_key = block.input_bindings.get("handler") or block.chunk_binding
    handlers = data.get_resource("blocks.handlers", {}) or {}
    if callable(handler_key):
        return handler_key
    if isinstance(handler_key, str):
        if isinstance(handlers, Mapping) and callable(handlers.get(handler_key)):
            return handlers[handler_key]
        resource = data.get_resource(handler_key, None)
        if callable(resource):
            return resource
    kind_handlers = data.get_resource("blocks.kind_handlers", {}) or {}
    if isinstance(kind_handlers, Mapping) and callable(kind_handlers.get(str(block.kind))):
        return kind_handlers[str(block.kind)]
    return None


async def _record_block_result(data: TriggerFlowRuntimeData, result: Mapping[str, Any]) -> None:
    current_results = data.get_state("blocks.execution_block_results", []) or []
    result_index = len(current_results) if isinstance(current_results, list) else 0
    await _append_state_item(data, "blocks.execution_block_results", dict(result))
    for evidence_item in _ledger_items_from_execution_result(result, index=result_index):
        await _append_state_item(data, "blocks.evidence_items", evidence_item)
    plan_block_id = result.get("source_plan_block_id")
    if plan_block_id:
        await _append_state_item(
            data,
            "blocks.plan_block_results",
            {
                "plan_block_id": plan_block_id,
                "execution_block_id": result.get("execution_block_id"),
                "output": result.get("output"),
                "success": result.get("success"),
            },
        )
    kind = result.get("kind")
    evidence_key = {
        "action_call": "blocks.action_evidence",
        "skill_activation": "blocks.skill_evidence",
        "workspace_operation": "blocks.workspace_refs",
        "validation": "blocks.validation_results",
    }.get(str(kind))
    if evidence_key:
        evidence_items = _evidence_items_for_result(result)
        for evidence_item in evidence_items:
            if evidence_key == "blocks.workspace_refs":
                await _append_state_item(data, evidence_key, _workspace_ref_id(evidence_item))
            else:
                await _append_state_item(data, evidence_key, evidence_item)
    for extra_key, state_key in (
        ("action_evidence", "blocks.action_evidence"),
        ("capability_evidence", "blocks.capability_evidence"),
        ("validation_results", "blocks.validation_results"),
        ("workspace_refs", "blocks.workspace_refs"),
    ):
        for evidence_item in _explicit_evidence_items(result, extra_key):
            if state_key == "blocks.workspace_refs":
                await _append_state_item(data, state_key, _workspace_ref_id(evidence_item))
            else:
                await _append_state_item(data, state_key, evidence_item)


def _evidence_items_for_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    output = result.get("output")
    kind = str(result.get("kind") or "")
    source_key = {
        "action_call": "action_evidence",
        "skill_activation": "skill_evidence",
        "workspace_operation": "workspace_refs",
        "validation": "validation_results",
    }.get(kind)
    records: Any = None
    if isinstance(output, Mapping) and source_key:
        records = output.get(source_key)
    if not isinstance(records, (list, tuple)):
        records = [dict(result)]
    items: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        evidence = dict(record)
        evidence.setdefault("execution_block_id", result.get("execution_block_id"))
        evidence.setdefault("source_plan_block_id", result.get("source_plan_block_id"))
        evidence.setdefault("source_task_dag_node_id", result.get("source_task_dag_node_id"))
        evidence.setdefault("block_kind", result.get("kind"))
        items.append(evidence)
    return items


def _explicit_evidence_items(result: Mapping[str, Any], source_key: str) -> list[dict[str, Any]]:
    output = result.get("output")
    if not isinstance(output, Mapping):
        return []
    records = output.get(source_key)
    if records is None:
        return []
    if not isinstance(records, (list, tuple)):
        records = [records]
    items: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        evidence = dict(record)
        evidence.setdefault("execution_block_id", result.get("execution_block_id"))
        evidence.setdefault("source_plan_block_id", result.get("source_plan_block_id"))
        evidence.setdefault("source_task_dag_node_id", result.get("source_task_dag_node_id"))
        evidence.setdefault("block_kind", result.get("kind"))
        items.append(evidence)
    return items


def _workspace_ref_id(value: Mapping[str, Any]) -> str:
    ref = value.get("ref")
    if isinstance(ref, Mapping) and ref.get("id") is not None:
        return str(ref.get("id"))
    if value.get("id") is not None:
        return str(value.get("id"))
    return str(value)


def _replan_signals_from_output(output: Any) -> list[dict[str, Any]]:
    if not isinstance(output, Mapping):
        return []
    raw = output.get("replan_signals", output.get("replan_signal"))
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    signals: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        try:
            signals.append(ReplanSignal.from_value(value).to_dict())
        except Exception as error:
            signals.append(
                {
                    "status": "blocked",
                    "reason": f"Invalid ReplanSignal payload: { error }",
                    "diagnostics": [{"type": error.__class__.__name__, "message": str(error)}],
                }
            )
    return signals


def _cancelled_block_ids(data: TriggerFlowRuntimeData) -> set[str]:
    raw = data.get_state("blocks.cancelled_execution_block_ids", []) or []
    if isinstance(raw, str):
        return {raw}
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(item) for item in raw if str(item).strip()}


def _cancelled_ids_for_replan_signals(
    graph: ExecutionBlockGraph,
    current_block_id: str,
    replan_signals: list[dict[str, Any]],
) -> list[str]:
    if not replan_signals:
        return []
    block_ids = {block.id for block in graph.execution_blocks}
    cancellable_statuses = {"repair", "replan_segment", "replan_goal", "blocked", "clarify"}
    affected: set[str] = set()
    current_downstream = _downstream_block_ids(graph, current_block_id)
    for signal in replan_signals:
        status = str(signal.get("status") or "")
        if status not in cancellable_statuses:
            continue
        raw_explicit = signal.get("affected_execution_block_ids", [])
        if isinstance(raw_explicit, str):
            raw_explicit = [raw_explicit]
        explicit = {
            str(item)
            for item in raw_explicit
            if str(item).strip()
        } if isinstance(raw_explicit, (list, tuple, set)) else set()
        if explicit:
            for block_id in explicit:
                if block_id in block_ids and block_id != current_block_id:
                    affected.add(block_id)
                    affected.update(_downstream_block_ids(graph, block_id))
        elif status in {"replan_segment", "replan_goal", "blocked", "clarify"}:
            affected.update(current_downstream)
    return [block.id for block in graph.execution_blocks if block.id in affected and block.id != current_block_id]


def _downstream_block_ids(graph: ExecutionBlockGraph, block_id: str) -> set[str]:
    children: dict[str, set[str]] = {}
    for edge in graph.edges:
        children.setdefault(edge.from_execution_block, set()).add(edge.to_execution_block)
    visited: set[str] = set()
    pending = list(children.get(block_id, ()))
    while pending:
        candidate = pending.pop(0)
        if candidate in visited:
            continue
        visited.add(candidate)
        pending.extend(sorted(children.get(candidate, ())))
    return visited


def _diagnostics_with_replan_signals(diagnostics: Any, replan_signals: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(diagnostics, (list, tuple)):
        items.extend(dict(item) for item in diagnostics if isinstance(item, Mapping))
    elif isinstance(diagnostics, Mapping):
        items.append(dict(diagnostics))
    if isinstance(replan_signals, (list, tuple)):
        items.extend(
            {"kind": "replan_signal", **dict(signal)}
            for signal in replan_signals
            if isinstance(signal, Mapping)
        )
    return items


async def _append_state_item(data: TriggerFlowRuntimeData, dotted_key: str, item: Any) -> None:
    current = data.get_state(dotted_key, []) or []
    if not isinstance(current, list):
        current = [current]
    await data.async_set_state(dotted_key, [*current, item], emit=False)


async def _emit_block_event(
    data: TriggerFlowRuntimeData,
    graph: ExecutionBlockGraph,
    block: ExecutionBlock,
    phase: str,
    payload: Mapping[str, Any],
) -> None:
    event_type = f"block.{ phase }"
    stream_item = {
        "type": event_type,
        "graph_id": graph.graph_id,
        "source_plan_id": graph.source_plan_id,
        "execution_block_id": block.id,
        "source_plan_block_id": block.source_plan_block_id,
        "source_task_dag_node_id": block.source_task_dag_node_id,
        "payload": dict(payload),
    }
    await data.execution.async_put_into_stream(stream_item, _skip_contract_validation=True)
    await data.async_emit(
        _block_event(graph.graph_id, block.id, phase),
        stream_item,
        _meta={
            "block_signal": event_type,
            "execution_block_id": block.id,
            "source_plan_block_id": block.source_plan_block_id,
            "source_task_dag_node_id": block.source_task_dag_node_id,
        },
    )


def _evidence_payload(block: ExecutionBlock, result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "execution_block_id": block.id,
        "source_plan_block_id": block.source_plan_block_id,
        "source_task_dag_node_id": block.source_task_dag_node_id,
        "evidence_kinds": _evidence_kinds_for(str(block.kind)),
        "output_present": "output" in result,
    }


def _semantic_outputs_from_snapshot(graph: ExecutionBlockGraph, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    results = snapshot.get("execution_block_results", [])
    by_id = {
        str(item.get("execution_block_id")): item.get("output")
        for item in results
        if isinstance(item, Mapping) and item.get("execution_block_id") is not None
    }
    task_dag_outputs = _task_dag_semantic_outputs_from_snapshot(graph, results)
    if task_dag_outputs:
        return task_dag_outputs
    return {
        block_id: by_id.get(block_id)
        for block_id in graph.terminal_blocks
        if block_id in by_id
    }


def _task_dag_semantic_outputs_from_snapshot(
    graph: ExecutionBlockGraph,
    results: Any,
) -> dict[str, Any]:
    semantic_outputs: Any = None
    for block in graph.execution_blocks:
        graph_data = block.input_bindings.get("graph")
        if isinstance(graph_data, Mapping) and graph_data.get("semantic_outputs"):
            semantic_outputs = graph_data.get("semantic_outputs")
            break
    refs = _semantic_output_task_refs(semantic_outputs)
    if not refs:
        return {}
    by_task_id: dict[str, Any] = {}
    if isinstance(results, (list, tuple)):
        for item in results:
            if not isinstance(item, Mapping):
                continue
            task_id = item.get("source_task_dag_node_id")
            if task_id is not None:
                by_task_id[str(task_id)] = item.get("output")
    outputs: dict[str, Any] = {}
    for role, task_id in refs.items():
        if task_id in by_task_id:
            outputs[role] = {"task_id": task_id, "result": by_task_id[task_id]}
    return outputs


def _semantic_output_task_refs(semantic_outputs: Any) -> dict[str, str]:
    refs: dict[str, str] = {}
    if isinstance(semantic_outputs, Mapping):
        for role, spec in semantic_outputs.items():
            if isinstance(spec, str):
                refs[str(role)] = spec
            elif isinstance(spec, Mapping):
                task_id = spec.get("task_id") or spec.get("from_task")
                if task_id is not None:
                    refs[str(role)] = str(task_id)
    elif isinstance(semantic_outputs, (list, tuple)):
        for item in semantic_outputs:
            if not isinstance(item, Mapping):
                continue
            role = item.get("role") or item.get("name")
            task_id = item.get("task_id") or item.get("from_task")
            if role is not None and task_id is not None:
                refs[str(role)] = str(task_id)
    return refs
