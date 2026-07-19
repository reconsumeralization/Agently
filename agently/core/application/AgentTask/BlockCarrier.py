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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast


WorkUnitOrigin = Literal["flat_step", "taskboard_card"]
CarrierControlFormat = Literal["json", "hybrid", "xml_field", "flat_markdown", "yaml_literal"]


def scoped_retrieval_policy(
    source_catalog: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compact policy shared by Flat and TaskBoard work-unit carriers."""

    source_kinds = {
        str(source_kind): {
            "binding_ids": tuple(str(item) for item in details.get("binding_ids", ())),
            "required": bool(details.get("required", False)),
            "description": str(details.get("description") or source_kind),
        }
        for source_kind, details in dict(source_catalog or {}).items()
    }
    return {
        "schema_version": "agent_task_scoped_retrieval/v2",
        "query_owner": "planner_or_control_model",
        "executor_owner": "ContextReader through Blocks context_read",
        "optimization_goal": "reduce hot prompt input by retrieving scoped refs before bulk reads",
        "roles": {
            "locator_ref": "discovered target; content not read",
            "evidence_snippet": "bounded readable excerpt",
        },
        "source_kinds": source_kinds,
        "rules": [
            "Use scoped retrieval before full file/resource reads when it can reduce input volume.",
            "Return only source kinds offered in source_kinds.",
            "Express business filters and exact refs; do not choose lexical, vector, rerank, or source-native search mechanisms.",
            "Treat locator_ref as discovery only until a bounded readback/snippet is available.",
            "Treat truncated evidence snippets as factual partial context; downstream consumers decide whether to request wider scoped retrieval or readback.",
            "Do not let retrieval hits decide semantic usefulness or task acceptance.",
            "For record_store records, put record collection in filters.collection; path is only for file search or exact record paths; use filters.kind only when the exact record kind is provided, never infer a generic kind such as note.",
        ],
        "bounded_defaults": {
            "max_results": 8,
            "snippet_limit": 1200,
            "file_context_lines": 3,
        },
    }


@dataclass(frozen=True)
class WorkUnitIntent:
    """Internal strategy-to-carrier boundary for one bounded unit of work."""

    id: str
    origin: WorkUnitOrigin | str
    objective: str
    input_payload: Mapping[str, Any] = field(default_factory=dict)
    input_refs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    expected_deliverable: Mapping[str, Any] = field(default_factory=dict)
    evidence_requirements: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    capability_scope: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    delivery_contract: Mapping[str, Any] = field(default_factory=dict)
    quality_gates: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    runtime_preferences: Mapping[str, Any] = field(default_factory=dict)
    retrieval_policy: Mapping[str, Any] = field(default_factory=scoped_retrieval_policy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "objective": self.objective,
            "input_payload": dict(self.input_payload),
            "input_refs": [dict(item) for item in self.input_refs],
            "expected_deliverable": dict(self.expected_deliverable),
            "evidence_requirements": [dict(item) for item in self.evidence_requirements],
            "capability_scope": [dict(item) for item in self.capability_scope],
            "delivery_contract": dict(self.delivery_contract),
            "quality_gates": [dict(item) for item in self.quality_gates],
            "runtime_preferences": dict(self.runtime_preferences),
            "retrieval_policy": dict(self.retrieval_policy),
        }


@dataclass(frozen=True)
class WorkUnitResult:
    """Internal carrier-to-strategy result for one bounded unit of work."""

    id: str
    status: str
    summary: Any = None
    candidate_final_result: Any = None
    artifact_manifest: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Any, ...] = field(default_factory=tuple)
    diagnostics: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    carrier_meta: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_output(
        cls,
        *,
        intent: WorkUnitIntent,
        output: Any,
        execution_meta: Mapping[str, Any],
        carrier_meta: Mapping[str, Any],
    ) -> "WorkUnitResult":
        status = str(execution_meta.get("status") or "completed").strip().lower() or "completed"
        diagnostics: list[dict[str, Any]] = []
        raw_diagnostics = execution_meta.get("diagnostics")
        if isinstance(raw_diagnostics, Mapping):
            diagnostics.append(dict(raw_diagnostics))
        elif isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics)

        summary: Any = output
        candidate_final_result: Any = None
        artifact_manifest: Mapping[str, Any] = {}
        evidence: list[Any] = []
        if isinstance(output, Mapping):
            summary = output.get("step_result", output.get("answer", output))
            candidate_final_result = output.get("candidate_final_result", output.get("final_result"))
            raw_manifest = output.get("artifact_manifest")
            if isinstance(raw_manifest, Mapping):
                artifact_manifest = dict(raw_manifest)
            raw_evidence = output.get("evidence")
            if isinstance(raw_evidence, Sequence) and not isinstance(raw_evidence, str | bytes | bytearray):
                evidence.extend(raw_evidence)
        return cls(
            id=intent.id,
            status=status,
            summary=summary,
            candidate_final_result=candidate_final_result,
            artifact_manifest=artifact_manifest,
            evidence=tuple(evidence),
            diagnostics=tuple(diagnostics),
            carrier_meta=dict(carrier_meta),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "candidate_final_result": self.candidate_final_result,
            "artifact_manifest": dict(self.artifact_manifest),
            "evidence": list(self.evidence),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "carrier_meta": dict(self.carrier_meta),
        }


def _is_non_string_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


@dataclass(frozen=True)
class CarrierOutputPolicy:
    """Internal carrier decision for model output and long-body transport."""

    control_format: CarrierControlFormat | None
    body_transport: Literal["structured_control", "plain_text", "task_workspace_artifact"]
    body_uses_output: bool
    requires_structured_judge: bool = False
    requires_task_workspace_readback: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_format": self.control_format,
            "body_transport": self.body_transport,
            "body_uses_output": self.body_uses_output,
            "requires_structured_judge": self.requires_structured_judge,
            "requires_task_workspace_readback": self.requires_task_workspace_readback,
            "reason": self.reason,
        }


def select_carrier_output_policy(intent: WorkUnitIntent) -> CarrierOutputPolicy:
    """Choose output handling from owned structure, not from free-text semantics."""

    runtime_preferences = dict(intent.runtime_preferences)
    delivery_contract = dict(intent.delivery_contract)
    expected_deliverable = dict(intent.expected_deliverable)
    deliverable_mode = (
        str(
            runtime_preferences.get("deliverable_mode")
            or delivery_contract.get("deliverable_mode")
            or expected_deliverable.get("deliverable_mode")
            or ""
        )
        .strip()
        .lower()
    )

    if deliverable_mode in {"task_workspace_artifact", "sectioned_task_workspace_artifact", "file_backed"}:
        return CarrierOutputPolicy(
            control_format="json",
            body_transport="task_workspace_artifact",
            body_uses_output=False,
            requires_structured_judge=True,
            requires_task_workspace_readback=True,
            reason="task_workspace_artifact_body_is_generated_as_plain_text_and_read_back",
        )
    if deliverable_mode in {"plain_text", "freeform_text", "natural_text"}:
        return CarrierOutputPolicy(
            control_format=None,
            body_transport="plain_text",
            body_uses_output=False,
            requires_structured_judge=True,
            reason="single_freeform_body_uses_natural_text_then_structured_judge",
        )

    execution_prompt = delivery_contract.get("execution_prompt")
    if not isinstance(execution_prompt, Mapping):
        execution_prompt = {}
    output_schema = execution_prompt.get("output")
    declared_format = str(execution_prompt.get("output_format") or "").strip().lower()
    if declared_format in {"json_object", "application/json"}:
        declared_format = "json"
    if declared_format in {"json", "hybrid", "xml_field", "flat_markdown", "yaml_literal"}:
        control_format = cast(CarrierControlFormat, declared_format)
        reason = "caller_output_format_preserved"
    elif declared_format == "auto":
        control_format = _resolve_contract_auto_format(output_schema)
        reason = "caller_auto_output_format_resolved_by_carrier"
    else:
        control_format = _resolve_contract_auto_format(output_schema) if isinstance(output_schema, Mapping) else "json"
        reason = "carrier_default_structured_control_format"
    return CarrierOutputPolicy(
        control_format=control_format,
        body_transport="structured_control",
        body_uses_output=True,
        reason=reason,
    )


def _resolve_contract_auto_format(output_schema: Any) -> CarrierControlFormat:
    if not isinstance(output_schema, Mapping) or not output_schema:
        return "json"
    if all(_is_string_field_spec(value) for value in output_schema.values()):
        return "xml_field"
    has_string_field = False
    has_non_string_field = False
    for value in output_schema.values():
        if _is_string_field_spec(value):
            has_string_field = True
        else:
            has_non_string_field = True
    return "hybrid" if has_string_field and has_non_string_field else "json"


def _is_string_field_spec(field_spec: Any) -> bool:
    if isinstance(field_spec, tuple) and field_spec:
        return field_spec[0] is str
    return field_spec is str
