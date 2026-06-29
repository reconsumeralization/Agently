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

from .TaskShared import *


class AgentTaskTaskBoardProjectionMixin(AgentTaskMixinBase):
    """Prompt and stream projections for TaskBoard state and card evidence."""

    @classmethod
    def _compact_taskboard_card_result_for_prompt(cls, result: Any) -> dict[str, Any]:
        try:
            effective = TaskBoardCardResult.from_value(result)
        except Exception:
            return {
                "status": "unknown",
                "preview": cls._compact_verifier_prompt_value(result, max_chars=_TASKBOARD_PROMPT_RESULT_CHARS),
            }
        artifact_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.artifact_refs)[:12]]
        if len(effective.artifact_refs) > 12:
            artifact_refs.append({"omitted": len(effective.artifact_refs) - 12, "reason": "prompt_budget"})
        file_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.file_refs)[:12]]
        if len(effective.file_refs) > 12:
            file_refs.append({"omitted": len(effective.file_refs) - 12, "reason": "prompt_budget"})
        diagnostics = list(effective.diagnostics)
        compact = {
            "schema_version": effective.schema_version,
            "card_id": effective.card_id,
            "status": effective.status,
            "output_digest": effective.output_digest,
            "preview": cls._compact_verifier_prompt_value(
                effective.preview,
                max_chars=_TASKBOARD_PROMPT_RESULT_CHARS,
            ),
            "artifact_refs": artifact_refs,
            "file_refs": file_refs,
            "diagnostics": cls._compact_verifier_prompt_value(diagnostics[:8], max_chars=1200),
            "metadata": cls._compact_verifier_prompt_value(effective.metadata, max_chars=1000),
        }
        if len(diagnostics) > 8:
            compact["diagnostics_omitted"] = {"count": len(diagnostics) - 8, "reason": "prompt_budget"}
        return compact

    @classmethod
    def _compact_taskboard_card_result_for_stream(cls, result: Any) -> dict[str, Any]:
        try:
            effective = TaskBoardCardResult.from_value(result)
        except Exception:
            return {
                "status": "unknown",
                "preview": cls._compact_verifier_prompt_value(result, max_chars=700),
            }
        artifact_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.artifact_refs)[:4]]
        file_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.file_refs)[:4]]
        return {
            "schema_version": effective.schema_version,
            "card_id": effective.card_id,
            "status": effective.status,
            "output_digest": effective.output_digest,
            "preview": cls._compact_verifier_prompt_value(effective.preview, max_chars=700),
            "artifact_refs": artifact_refs,
            "artifact_refs_omitted": max(0, len(effective.artifact_refs) - 4),
            "file_refs": file_refs,
            "file_refs_omitted": max(0, len(effective.file_refs) - 4),
            "diagnostics": cls._compact_verifier_prompt_value(list(effective.diagnostics)[:4], max_chars=700),
            "metadata": cls._compact_verifier_prompt_value(effective.metadata, max_chars=500),
        }

    @classmethod
    def _compact_taskboard_dependency_results(cls, dependency_results: Mapping[str, Any]) -> dict[str, Any]:
        return {
            str(card_id): cls._compact_taskboard_card_result_for_prompt(result)
            for card_id, result in dict(dependency_results).items()
        }

    @classmethod
    def _compact_taskboard_revision_for_prompt(
        cls,
        revision: Any,
        *,
        include_card_results: bool = True,
    ) -> dict[str, Any]:
        effective = TaskBoardRevision.from_value(revision)
        cards = []
        for card in effective.graph.cards:
            cards.append(
                {
                    "id": card.id,
                    "status": card.status,
                    "objective": card.objective,
                    "depends_on": list(card.depends_on),
                    "required_outputs": list(card.required_outputs),
                    "allowed_execution_shape": card.allowed_execution_shape,
                    "failure_policy": card.failure_policy,
                    "evidence_contract": cls._compact_verifier_prompt_value(
                        card.evidence_contract,
                        max_chars=800,
                    ),
                    "metadata": cls._compact_verifier_prompt_value(card.metadata, max_chars=800),
                }
            )
        compact = {
            "schema_version": effective.schema_version,
            "board_id": effective.board_id,
            "revision_id": effective.revision_id,
            "status": effective.status,
            "graph": {
                "schema_version": effective.graph.schema_version,
                "graph_id": effective.graph.graph_id,
                "cards": cards,
                "metadata": cls._compact_verifier_prompt_value(effective.graph.metadata, max_chars=1000),
            },
            "card_result_statuses": {
                str(card_id): str(result.status) for card_id, result in effective.card_results.items()
            },
            "evidence_refs": [
                cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.evidence_refs)[:16]
            ],
            "diagnostics": cls._compact_verifier_prompt_value(list(effective.diagnostics)[:16], max_chars=1600),
            "metadata": cls._compact_verifier_prompt_value(effective.metadata, max_chars=1200),
        }
        if include_card_results:
            compact["card_results"] = {
                str(card_id): cls._compact_taskboard_card_result_for_prompt(result)
                for card_id, result in effective.card_results.items()
            }
        return compact

    @classmethod
    def _compact_taskboard_revision_for_stream(cls, revision: Any) -> dict[str, Any]:
        effective = TaskBoardRevision.from_value(revision)
        return {
            "schema_version": effective.schema_version,
            "board_id": effective.board_id,
            "revision_id": effective.revision_id,
            "status": effective.status,
            "graph_id": effective.graph.graph_id,
            "cards": [
                {
                    "id": card.id,
                    "status": card.status,
                    "depends_on": list(card.depends_on),
                    "failure_policy": card.failure_policy,
                }
                for card in effective.graph.cards
            ],
            "card_result_statuses": {
                str(card_id): str(result.status) for card_id, result in effective.card_results.items()
            },
        }

    @classmethod
    def _compact_taskboard_evidence_view_for_stream(cls, evidence_view: Mapping[str, Any]) -> Any:
        return cls._compact_verifier_prompt_value(evidence_view, max_chars=_TASKBOARD_STREAM_SUMMARY_CHARS)

    @staticmethod
    def _prompt_sequence(value: Any) -> Sequence[Any]:
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return value
        return ()

    @classmethod
    def _compact_taskboard_evidence_view_for_prompt(cls, evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        raw_cards = evidence_view.get("cards")
        cards = []
        for card in cls._prompt_sequence(raw_cards):
            if not isinstance(card, Mapping):
                continue
            diagnostics = []
            for diagnostic in list(cls._prompt_sequence(card.get("diagnostics")))[:4]:
                if isinstance(diagnostic, Mapping):
                    compact_diagnostic = dict(diagnostic)
                    if "block_carrier" in compact_diagnostic:
                        compact_diagnostic["block_carrier"] = cls._compact_block_carrier_for_taskboard_meta(
                            compact_diagnostic.get("block_carrier")
                        )
                    diagnostics.append(compact_diagnostic)
                else:
                    diagnostics.append({"value": diagnostic})
            metadata = card.get("metadata", {})
            if isinstance(metadata, Mapping) and "block_carrier" in metadata:
                metadata = dict(metadata)
                metadata["block_carrier"] = cls._compact_block_carrier_for_taskboard_meta(metadata.get("block_carrier"))
            workspace_operations = cls._taskboard_card_workspace_operations_for_prompt(
                diagnostics=diagnostics,
                metadata=metadata,
            )
            artifact_refs_source = cls._prompt_sequence(card.get("artifact_refs"))
            file_refs_source = cls._prompt_sequence(card.get("file_refs"))
            source_refs_value = card.get("source_refs")
            source_refs_sequence = cls._prompt_sequence(source_refs_value)
            source_refs_source = cls._collect_taskboard_source_refs(source_refs_value, max_refs=8)
            artifact_refs = [
                cls._compact_artifact_ref_for_verifier(ref)
                for ref in list(artifact_refs_source)[:8]
                if isinstance(ref, Mapping)
            ]
            file_refs = [
                cls._compact_artifact_ref_for_verifier(ref)
                for ref in list(file_refs_source)[:8]
                if isinstance(ref, Mapping)
            ]
            cards.append(
                {
                    "card_id": card.get("card_id", card.get("id")),
                    "status": card.get("status"),
                    "output_digest": card.get("output_digest"),
                    "preview": cls._compact_verifier_prompt_value(
                        card.get("preview", card.get("summary", card.get("answer"))),
                        max_chars=_TASKBOARD_PROMPT_RESULT_CHARS,
                    ),
                    "artifact_refs": artifact_refs,
                    "artifact_refs_omitted": max(0, len(artifact_refs_source) - 8),
                    "file_refs": file_refs,
                    "file_refs_omitted": max(0, len(file_refs_source) - 8),
                    "source_refs": source_refs_source,
                    "source_refs_omitted": max(0, len(source_refs_sequence) - 8),
                    "workspace_operations": workspace_operations,
                    "diagnostics": cls._compact_verifier_prompt_value(
                        diagnostics,
                        max_chars=800,
                    ),
                    "metadata": cls._compact_verifier_prompt_value(metadata, max_chars=600),
                }
            )
        artifact_refs_source = cls._prompt_sequence(evidence_view.get("artifact_refs"))
        file_refs_source = cls._prompt_sequence(evidence_view.get("file_refs"))
        evidence_items_source = cls._prompt_sequence(evidence_view.get("evidence_items"))
        source_refs_value = evidence_view.get("source_refs")
        source_refs_sequence = cls._prompt_sequence(source_refs_value)
        source_refs_source = cls._collect_taskboard_source_refs(source_refs_value, max_refs=16)
        artifact_refs = [
            cls._compact_artifact_ref_for_verifier(ref)
            for ref in list(artifact_refs_source)[:16]
            if isinstance(ref, Mapping)
        ]
        file_refs = [
            cls._compact_artifact_ref_for_verifier(ref)
            for ref in list(file_refs_source)[:16]
            if isinstance(ref, Mapping)
        ]
        return {
            "schema_version": evidence_view.get("schema_version"),
            "revision_id": evidence_view.get("revision_id"),
            "status_counts": DataFormatter.sanitize(evidence_view.get("status_counts", {})),
            "metadata": cls._compact_verifier_prompt_value(evidence_view.get("metadata", {}), max_chars=600),
            "cards": cards,
            "cards_omitted": max(0, len(cls._prompt_sequence(raw_cards)) - len(cards)),
            "artifact_refs": artifact_refs,
            "artifact_refs_omitted": max(0, len(artifact_refs_source) - 16),
            "file_refs": file_refs,
            "file_refs_omitted": max(0, len(file_refs_source) - 16),
            "evidence_items": cls._compact_taskboard_evidence_items_for_prompt(evidence_items_source, max_items=32),
            "evidence_items_omitted": max(0, len(evidence_items_source) - 32),
            "source_refs": source_refs_source,
            "source_refs_omitted": max(0, len(source_refs_sequence) - 16),
        }

    @classmethod
    def _compact_taskboard_evidence_items_for_prompt(
        cls,
        evidence_items: Sequence[Any],
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        compact_items: list[dict[str, Any]] = []
        for item in list(evidence_items)[:max_items]:
            if not isinstance(item, Mapping):
                continue
            compact: dict[str, Any] = {
                key: item.get(key)
                for key in (
                    "id",
                    "kind",
                    "status",
                    "raw_status",
                    "body_state",
                    "provenance",
                    "path",
                    "record_id",
                    "source_url",
                    "selected_url",
                    "requested_url",
                    "canonical_url",
                    "url",
                    "href",
                    "content_state",
                    "truncated",
                )
                if item.get(key) not in (None, "", [], {})
            }
            body = item.get("body")
            if isinstance(body, str) and body.strip():
                compact["body"] = cls._truncate_prompt_text(body, 900)
            diagnostics = item.get("diagnostics")
            if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
                compact["diagnostics"] = cls._compact_verifier_prompt_value(list(diagnostics)[:4], max_chars=600)
            compact_items.append(DataFormatter.sanitize(compact))
        return compact_items

    @classmethod
    def _compact_block_carrier_for_taskboard_meta(
        cls,
        block_carrier: Any,
        *,
        blocks: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(block_carrier, Mapping):
            return {}
        raw_work_unit = block_carrier.get("work_unit")
        work_unit: Mapping[str, Any] = raw_work_unit if isinstance(raw_work_unit, Mapping) else {}
        raw_runtime_preferences = work_unit.get("runtime_preferences")
        runtime_preferences: Mapping[str, Any] = (
            raw_runtime_preferences if isinstance(raw_runtime_preferences, Mapping) else {}
        )
        raw_work_unit_result = block_carrier.get("work_unit_result")
        work_unit_result: Mapping[str, Any] = raw_work_unit_result if isinstance(raw_work_unit_result, Mapping) else {}
        raw_carrier_meta = work_unit_result.get("carrier_meta")
        carrier_meta: Mapping[str, Any] = raw_carrier_meta if isinstance(raw_carrier_meta, Mapping) else {}
        return {
            "work_unit": {
                "id": work_unit.get("id"),
                "origin": work_unit.get("origin"),
                "objective": cls._truncate_prompt_text(str(work_unit.get("objective") or ""), 700),
                "input_refs": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit.get("input_refs")))[:8],
                    max_chars=700,
                ),
                "expected_deliverable": cls._compact_verifier_prompt_value(
                    work_unit.get("expected_deliverable", {}),
                    max_chars=700,
                ),
                "evidence_requirements": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit.get("evidence_requirements")))[:8],
                    max_chars=700,
                ),
                "runtime_preferences": {
                    key: runtime_preferences.get(key)
                    for key in (
                        "handler",
                        "plan_block_kind",
                        "preferred_execution_shape",
                        "strategy",
                        "card_id",
                        "attempt_index",
                        "max_attempts",
                    )
                    if key in runtime_preferences
                },
            },
            "work_unit_result": {
                "id": work_unit_result.get("id"),
                "status": work_unit_result.get("status"),
                "summary": cls._compact_verifier_prompt_value(work_unit_result.get("summary"), max_chars=700),
                "candidate_final_result": cls._compact_verifier_prompt_value(
                    work_unit_result.get("candidate_final_result"),
                    max_chars=700,
                ),
                "artifact_manifest": cls._compact_verifier_prompt_value(
                    work_unit_result.get("artifact_manifest", {}),
                    max_chars=700,
                ),
                "evidence": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit_result.get("evidence")))[:8],
                    max_chars=700,
                ),
                "diagnostics": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit_result.get("diagnostics")))[:4],
                    max_chars=700,
                ),
                "carrier_meta": {
                    "snapshot_status": carrier_meta.get("snapshot_status"),
                    "execution_plan": cls._compact_verifier_prompt_value(
                        carrier_meta.get("execution_plan", {}),
                        max_chars=700,
                    ),
                    "execution_block_graph": cls._compact_verifier_prompt_value(
                        carrier_meta.get("execution_block_graph", {}),
                        max_chars=700,
                    ),
                },
            },
            "output_policy": DataFormatter.sanitize(block_carrier.get("output_policy", {})),
            "workspace_operations": cls._compact_taskboard_workspace_operations_for_carrier_meta(
                block_carrier,
                blocks,
            ),
            "block_graph": cls._compact_taskboard_blocks_for_carrier_meta(blocks),
        }

    @classmethod
    def _compact_taskboard_workspace_operations_for_carrier_meta(
        cls,
        block_carrier: Mapping[str, Any],
        blocks: Any,
    ) -> list[dict[str, Any]]:
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                execution_results = [
                    item for item in evidence.get("execution_block_results", []) if isinstance(item, Mapping)
                ]
                operations = [
                    cls._compact_taskboard_workspace_operation(item)
                    for item in execution_results
                    if str(item.get("kind") or "") == "workspace_operation"
                ][:8]
                if operations:
                    return operations
        direct_operations = block_carrier.get("workspace_operations")
        if isinstance(direct_operations, Sequence) and not isinstance(direct_operations, (str, bytes, bytearray)):
            return [
                cls._compact_taskboard_workspace_operation(item)
                for item in list(direct_operations)[:8]
                if isinstance(item, Mapping)
            ]
        return []

    @classmethod
    def _compact_taskboard_workspace_operation(cls, item: Mapping[str, Any]) -> dict[str, Any]:
        output = item.get("output")
        output_summary: dict[str, Any] = {}
        if isinstance(output, Mapping):
            for output_key in (
                "operation",
                "query",
                "filters",
                "locator_ref_count",
                "evidence_snippet_count",
            ):
                if output_key in output:
                    output_summary[output_key] = cls._compact_verifier_prompt_value(
                        output.get(output_key),
                        max_chars=700,
                    )
            diagnostics = output.get("diagnostics")
            if diagnostics is not None:
                output_summary["diagnostics"] = cls._model_hot_diagnostics(diagnostics)
            bounded = output.get("bounded")
            if isinstance(bounded, Mapping):
                output_summary["bounded"] = cls._compact_taskboard_workspace_operation_bounded(bounded)
            for output_key, source_key in (
                ("first_locator_ref", "locator_refs"),
                ("first_evidence_snippet", "evidence_snippets"),
            ):
                max_chars = 1800 if output_key == "first_evidence_snippet" else 900
                if output_key in output:
                    output_summary[output_key] = cls._compact_taskboard_workspace_ref_or_snippet(
                        output.get(output_key),
                        max_chars=max_chars,
                    )
                    continue
                source = output.get(source_key)
                if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)) and source:
                    output_summary[output_key] = cls._compact_taskboard_workspace_ref_or_snippet(
                        source[0],
                        max_chars=max_chars,
                    )
        return {
            key: item.get(key)
            for key in (
                "kind",
                "status",
            )
            if key in item
        } | ({"output": output_summary} if output_summary else {})

    @classmethod
    def _compact_taskboard_workspace_operation_bounded(cls, bounded: Mapping[str, Any]) -> dict[str, Any]:
        keep_keys = (
            "operation",
            "query",
            "filters",
            "path",
            "pattern",
            "search_surface",
            "returned_results",
            "file_returned_results",
            "index_returned_results",
            "index_total_matches",
            "locator_ref_count",
            "evidence_snippet_count",
            "snippet_limit",
            "max_results",
            "context_lines",
            "offset",
            "limit",
            "eof",
            "truncated",
        )
        compact = {key: bounded.get(key) for key in keep_keys if key in bounded}
        diagnostics = bounded.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
            compact["diagnostics"] = cls._model_hot_diagnostics(list(diagnostics)[:4])
        return DataFormatter.sanitize(compact)

    @classmethod
    def _compact_taskboard_workspace_ref_or_snippet(cls, value: Any, *, max_chars: int) -> Any:
        if not isinstance(value, Mapping):
            return cls._compact_verifier_prompt_value(value, max_chars=max_chars)
        compact: dict[str, Any] = {}
        for key in (
            "path",
            "line",
            "line_start",
            "line_end",
            "role",
            "content_state",
            "source",
            "query",
            "record_id",
            "collection",
        ):
            if key in value:
                compact[key] = value.get(key)
        content = value.get("content")
        if not isinstance(content, str):
            content = value.get("snippet")
        if not isinstance(content, str):
            content = value.get("text")
        if isinstance(content, str):
            compact["content"] = cls._truncate_prompt_text(content, max_chars)
        return cls._compact_verifier_prompt_value(compact or value, max_chars=max_chars)

    @classmethod
    def _taskboard_card_workspace_operations_for_prompt(
        cls,
        *,
        diagnostics: Sequence[Any],
        metadata: Any,
    ) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        for container in (*diagnostics, metadata):
            if not isinstance(container, Mapping):
                continue
            block_carrier = container.get("block_carrier")
            if not isinstance(block_carrier, Mapping):
                continue
            raw_operations = block_carrier.get("workspace_operations")
            if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, (str, bytes, bytearray)):
                continue
            for operation in raw_operations:
                if not isinstance(operation, Mapping):
                    continue
                operations.append(cls._compact_taskboard_workspace_operation(operation))
                if len(operations) >= 4:
                    return operations
        return operations

    @staticmethod
    def _compact_taskboard_blocks_for_carrier_meta(blocks: Any) -> dict[str, Any]:
        if not isinstance(blocks, Mapping):
            return {"present": False, "execution_block_count": 0, "execution_block_kinds": []}
        graph = blocks.get("execution_block_graph")
        if not isinstance(graph, Mapping):
            graph = {}
        execution_blocks = [item for item in graph.get("execution_blocks", []) if isinstance(item, Mapping)]
        evidence = blocks.get("evidence")
        if not isinstance(evidence, Mapping):
            evidence = {}
        execution_results = [
            item for item in evidence.get("execution_block_results", []) if isinstance(item, Mapping)
        ]
        return {
            "present": bool(graph),
            "graph_id": graph.get("graph_id") or graph.get("execution_id") or graph.get("id"),
            "execution_block_count": len(execution_blocks),
            "execution_block_kinds": [
                str(item.get("kind") or "") for item in execution_blocks if str(item.get("kind") or "").strip()
            ],
            "execution_block_ids": [
                str(item.get("id") or "") for item in execution_blocks if str(item.get("id") or "").strip()
            ],
            "evidence_present": bool(evidence),
            "execution_block_result_count": len(execution_results),
            "execution_block_result_kinds": [
                str(item.get("kind") or "") for item in execution_results if str(item.get("kind") or "").strip()
            ],
        }

    @classmethod
    def _taskboard_scheduled_stream_payload(
        cls,
        *,
        schedule: Any,
        evidence_view: Mapping[str, Any],
        concurrency: int | None,
    ) -> dict[str, Any]:
        return {
            "schedule": DataFormatter.sanitize(schedule.to_dict()),
            "evidence_view": cls._compact_taskboard_evidence_view_for_stream(evidence_view),
            "concurrency": concurrency,
        }

    @classmethod
    def _taskboard_completed_stream_payload(cls, tick_result: Any) -> dict[str, Any]:
        evidence_view = build_task_board_evidence_view(tick_result.revision).to_dict()
        return {
            "revision": cls._compact_taskboard_revision_for_stream(tick_result.revision),
            "schedule": DataFormatter.sanitize(tick_result.schedule.to_dict()),
            "card_results": {
                str(card_id): cls._compact_taskboard_card_result_for_stream(result)
                for card_id, result in tick_result.card_results.items()
            },
            "evidence_view": cls._compact_taskboard_evidence_view_for_stream(evidence_view),
            "runtime_topology": DataFormatter.sanitize(tick_result.triggerflow_snapshot.get("runtime_topology", {})),
        }


__all__ = ["AgentTaskTaskBoardProjectionMixin"]
