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
    def _compact_taskboard_evidence_use_guard_summary(cls, guard: Any) -> Any:
        if not isinstance(guard, Mapping):
            return {}
        diagnostics = cls._prompt_sequence(guard.get("diagnostics"))
        return DataFormatter.sanitize(
            {
                "valid": guard.get("valid"),
                "checked_claims": guard.get("checked_claims"),
                "blocking_count": guard.get("blocking_count"),
                "diagnostic_count": len(diagnostics),
            }
        )

    @classmethod
    def _compact_taskboard_ledger_summary(cls, ledger: Any) -> Any:
        if not isinstance(ledger, Mapping):
            return {}
        items = cls._prompt_sequence(ledger.get("items") or ledger.get("evidence_items"))
        return DataFormatter.sanitize(
            {
                "item_count": len(items),
                "status_counts": ledger.get("status_counts", {}),
                "body_state_counts": ledger.get("body_state_counts", {}),
            }
        )

    @classmethod
    def _compact_taskboard_card_metadata_for_prompt(cls, metadata: Any) -> dict[str, Any]:
        if not isinstance(metadata, Mapping):
            return {}
        compact: dict[str, Any] = {}
        for key in (
            "execution_id",
            "execution_kind",
            "execution_strategy",
            "next_board_action",
            "attempt_index",
            "max_attempts",
            "partial_child_evidence",
            "partial_child_status",
        ):
            value = metadata.get(key)
            if value not in (None, "", [], {}):
                compact[key] = DataFormatter.sanitize(value)
        if isinstance(metadata.get("block_carrier"), Mapping):
            compact["block_carrier"] = cls._compact_block_carrier_for_taskboard_meta(metadata.get("block_carrier"))
        guard_summary = cls._compact_taskboard_evidence_use_guard_summary(metadata.get("evidence_use_guard"))
        if guard_summary:
            compact["evidence_use_guard"] = guard_summary
        ledger_summary = cls._compact_taskboard_ledger_summary(metadata.get("evidence_ledger"))
        if ledger_summary:
            compact["evidence_ledger"] = ledger_summary
        return DataFormatter.sanitize(compact)

    @classmethod
    def _compact_taskboard_card_diagnostic_for_prompt(cls, diagnostic: Any) -> dict[str, Any]:
        if not isinstance(diagnostic, Mapping):
            return {"value": cls._truncate_prompt_text(diagnostic, 360)}
        compact = {
            key: diagnostic.get(key)
            for key in (
                "kind",
                "type",
                "code",
                "status",
                "message",
                "card_id",
                "stage",
                "attempt_index",
                "max_attempts",
            )
            if diagnostic.get(key) not in (None, "", [], {})
        }
        if isinstance(diagnostic.get("block_carrier"), Mapping):
            compact["block_carrier"] = cls._compact_block_carrier_for_taskboard_meta(diagnostic.get("block_carrier"))
        guard_summary = cls._compact_taskboard_evidence_use_guard_summary(diagnostic.get("evidence_use_guard"))
        if guard_summary:
            compact["evidence_use_guard"] = guard_summary
        evidence_summary = diagnostic.get("evidence_summary")
        if isinstance(evidence_summary, Mapping):
            compact["evidence_summary"] = cls._compact_verifier_evidence_summary(evidence_summary)
        return DataFormatter.sanitize(compact)

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
            "preview": cls._compact_taskboard_card_preview_for_prompt(effective.preview),
            "artifact_refs": artifact_refs,
            "file_refs": file_refs,
            "diagnostics": [
                cls._compact_taskboard_card_diagnostic_for_prompt(item)
                for item in diagnostics[:8]
            ],
            "metadata": cls._compact_taskboard_card_metadata_for_prompt(effective.metadata),
        }
        if len(diagnostics) > 8:
            compact["diagnostics_omitted"] = {"count": len(diagnostics) - 8, "reason": "prompt_budget"}
        return compact

    @classmethod
    def _compact_taskboard_card_preview_for_prompt(cls, preview: Any) -> Any:
        if not isinstance(preview, Mapping):
            return cls._compact_verifier_prompt_value(preview, max_chars=_TASKBOARD_PROMPT_RESULT_CHARS)
        compact: dict[str, Any] = {}
        text_limits = {
            "status": 80,
            "answer": 1200,
            "content": 1200,
            "summary": 800,
            "short_summary": 800,
            "self_check": 500,
            "progress_message": 320,
        }
        for key, max_chars in text_limits.items():
            value = preview.get(key)
            if value not in (None, "", [], {}):
                compact[key] = cls._truncate_prompt_text(value, max_chars)
        for key in ("candidate_final_result", "final_result", "artifact_markdown"):
            value = preview.get(key)
            if value not in (None, "", [], {}):
                compact["deliverable_preview"] = cls._truncate_prompt_text(value, 1600)
                compact["deliverable_source_field"] = key
                break
        for key in ("remaining_work", "gaps", "evidence"):
            values = cls._prompt_sequence(preview.get(key))
            if values:
                compact[key] = [cls._truncate_prompt_text(item, 320) for item in list(values)[:8]]
                if len(values) > 8:
                    compact[f"{key}_omitted"] = len(values) - 8
        evidence_use = cls._prompt_sequence(preview.get("evidence_use"))
        if evidence_use:
            compact["evidence_use_count"] = len(evidence_use)
        manifest = preview.get("artifact_manifest")
        if isinstance(manifest, Mapping):
            manifest_projection: dict[str, Any] = {}
            path = str(manifest.get("path") or "").strip()
            if path:
                manifest_projection["path"] = path
            sections = cls._prompt_sequence(manifest.get("sections"))
            if sections:
                manifest_projection["sections"] = [
                    {
                        key: cls._truncate_prompt_text(section.get(key), 240)
                        for key in ("id", "title", "intent")
                        if isinstance(section, Mapping) and section.get(key) not in (None, "", [], {})
                    }
                    for section in list(sections)[:8]
                    if isinstance(section, Mapping)
                ]
            if manifest_projection:
                compact["artifact_manifest"] = manifest_projection
        return DataFormatter.sanitize(compact)

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
        metadata: dict[str, Any] = {}
        if isinstance(effective.metadata, Mapping):
            for key in (
                "execution_id",
                "execution_kind",
                "execution_strategy",
                "next_board_action",
                "attempt_index",
                "max_attempts",
                "partial_child_evidence",
                "partial_child_status",
            ):
                value = effective.metadata.get(key)
                if value not in (None, "", [], {}):
                    metadata[key] = DataFormatter.sanitize(value)
        diagnostics = []
        for item in list(effective.diagnostics)[:3]:
            if isinstance(item, Mapping):
                diagnostics.append(
                    {
                        key: item.get(key)
                        for key in ("kind", "type", "code", "status", "message", "card_id", "stage")
                        if item.get(key) not in (None, "", [], {})
                    }
                )
            else:
                diagnostics.append({"value": cls._truncate_prompt_text(item, 240)})
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
            "diagnostics": DataFormatter.sanitize(diagnostics),
            "diagnostics_omitted": max(0, len(effective.diagnostics) - 3),
            "metadata": metadata,
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
            evidence_contract: dict[str, Any] = {}
            for key in (
                "kind",
                "done_when",
                "failure_policy",
                "preflight_kind",
                "reason",
            ):
                value = card.evidence_contract.get(key)
                if value not in (None, "", [], {}):
                    evidence_contract[key] = cls._truncate_prompt_text(value, 500)
            for key in (
                "evidence_to_use",
                "requires_capability_ids",
                "requires_task_workspace_refs",
                "focus_item_ids",
                "missing_criteria",
                "next_step_requirements",
                "acceptance_delta",
            ):
                values = cls._prompt_sequence(card.evidence_contract.get(key))
                if values:
                    evidence_contract[key] = [cls._truncate_prompt_text(item, 320) for item in list(values)[:8]]
            cards.append(
                {
                    "id": card.id,
                    "status": card.status,
                    "objective": cls._truncate_prompt_text(card.objective, 800),
                    "depends_on": list(card.depends_on),
                    "required_outputs": [
                        cls._truncate_prompt_text(item, 400) for item in list(card.required_outputs)[:8]
                    ],
                    "allowed_execution_shape": card.allowed_execution_shape,
                    "failure_policy": card.failure_policy,
                    "evidence_contract": DataFormatter.sanitize(evidence_contract),
                }
            )
        diagnostics = [
            cls._compact_taskboard_card_diagnostic_for_prompt(item)
            for item in list(effective.diagnostics)[:16]
        ]
        revision_metadata = {
            key: DataFormatter.sanitize(effective.metadata.get(key))
            for key in (
                "terminal_repair_count",
                "terminal_convergence_subject",
                "repair_source",
                "generated_by",
                "previous_revision_id",
            )
            if effective.metadata.get(key) not in (None, "", [], {})
        }
        compact = {
            "schema_version": effective.schema_version,
            "board_id": effective.board_id,
            "revision_id": effective.revision_id,
            "status": effective.status,
            "graph": {
                "schema_version": effective.graph.schema_version,
                "graph_id": effective.graph.graph_id,
                "cards": cards,
            },
            "card_result_statuses": {
                str(card_id): str(result.status) for card_id, result in effective.card_results.items()
            },
            "evidence_refs": [
                cls._compact_artifact_ref_for_verifier(ref) for ref in list(effective.evidence_refs)[:16]
            ],
            "diagnostics": DataFormatter.sanitize(diagnostics),
            "metadata": revision_metadata,
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
                    "objective": cls._truncate_prompt_text(card.objective, 160),
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
        raw_cards = cls._prompt_sequence(evidence_view.get("cards"))
        cards = []
        for card in list(raw_cards)[:4]:
            if not isinstance(card, Mapping):
                continue
            artifact_refs_source = cls._prompt_sequence(card.get("artifact_refs"))
            file_refs_source = cls._prompt_sequence(card.get("file_refs"))
            cards.append(
                {
                    "card_id": card.get("card_id", card.get("id")),
                    "status": card.get("status"),
                    "output_digest": card.get("output_digest"),
                    "preview": cls._compact_verifier_prompt_value(
                        card.get("preview", card.get("summary", card.get("answer"))),
                        max_chars=360,
                    ),
                    "artifact_refs": [
                        cls._compact_artifact_ref_for_verifier(ref)
                        for ref in list(artifact_refs_source)[:2]
                        if isinstance(ref, Mapping)
                    ],
                    "artifact_refs_omitted": max(0, len(artifact_refs_source) - 2),
                    "file_refs": [
                        cls._compact_artifact_ref_for_verifier(ref)
                        for ref in list(file_refs_source)[:2]
                        if isinstance(ref, Mapping)
                    ],
                    "file_refs_omitted": max(0, len(file_refs_source) - 2),
                }
            )
        artifact_refs_source = cls._prompt_sequence(evidence_view.get("artifact_refs"))
        file_refs_source = cls._prompt_sequence(evidence_view.get("file_refs"))
        evidence_items_source = cls._prompt_sequence(evidence_view.get("evidence_items"))
        diagnostics_source = cls._prompt_sequence(evidence_view.get("diagnostics"))
        return DataFormatter.sanitize(
            {
                "schema_version": evidence_view.get("schema_version"),
                "revision_id": evidence_view.get("revision_id"),
                "status_counts": evidence_view.get("status_counts", {}),
                "cards": cards,
                "cards_omitted": max(0, len(raw_cards) - len(cards)),
                "artifact_refs": [
                    cls._compact_artifact_ref_for_verifier(ref)
                    for ref in list(artifact_refs_source)[:4]
                    if isinstance(ref, Mapping)
                ],
                "artifact_refs_omitted": max(0, len(artifact_refs_source) - 4),
                "file_refs": [
                    cls._compact_artifact_ref_for_verifier(ref)
                    for ref in list(file_refs_source)[:4]
                    if isinstance(ref, Mapping)
                ],
                "file_refs_omitted": max(0, len(file_refs_source) - 4),
                "evidence_item_count": len(evidence_items_source),
                "diagnostic_count": len(diagnostics_source),
                "metadata": cls._compact_verifier_prompt_value(evidence_view.get("metadata", {}), max_chars=240),
            }
        )

    @staticmethod
    def _prompt_sequence(value: Any) -> Sequence[Any]:
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return value
        return ()

    @classmethod
    def _compact_taskboard_evidence_view_for_prompt(cls, evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        raw_cards = evidence_view.get("cards")
        cards = []
        raw_card_sequence = cls._prompt_sequence(raw_cards)
        for card in list(raw_card_sequence)[:16]:
            if not isinstance(card, Mapping):
                continue
            raw_metadata = card.get("metadata", {})
            metadata_has_block_carrier = isinstance(raw_metadata, Mapping) and isinstance(
                raw_metadata.get("block_carrier"),
                Mapping,
            )
            diagnostics = []
            for diagnostic in list(cls._prompt_sequence(card.get("diagnostics")))[:4]:
                compact_diagnostic = cls._compact_taskboard_card_diagnostic_for_prompt(diagnostic)
                if metadata_has_block_carrier:
                    compact_diagnostic.pop("block_carrier", None)
                if compact_diagnostic:
                    diagnostics.append(compact_diagnostic)
            metadata = cls._compact_taskboard_card_metadata_for_prompt(raw_metadata)
            context_reads = cls._taskboard_card_context_reads_for_prompt(
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
                    "preview": cls._compact_taskboard_card_preview_for_prompt(
                        card.get("preview", card.get("summary", card.get("answer")))
                    ),
                    "artifact_refs": artifact_refs,
                    "artifact_refs_omitted": max(0, len(artifact_refs_source) - 8),
                    "file_refs": file_refs,
                    "file_refs_omitted": max(0, len(file_refs_source) - 8),
                    "source_refs": source_refs_source,
                    "source_refs_omitted": max(0, len(source_refs_sequence) - 8),
                    "context_reads": context_reads,
                    "diagnostics": diagnostics,
                    "metadata": metadata,
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
            "cards_omitted": max(0, len(raw_card_sequence) - len(cards)),
            "artifact_refs": artifact_refs,
            "artifact_refs_omitted": max(0, len(artifact_refs_source) - 16),
            "file_refs": file_refs,
            "file_refs_omitted": max(0, len(file_refs_source) - 16),
            "evidence_items": cls._compact_taskboard_evidence_items_for_prompt(evidence_items_source, max_items=16),
            "evidence_items_omitted": max(0, len(evidence_items_source) - 16),
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
                compact["body_preview"] = cls._truncate_prompt_text(body, 320)
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
                "objective": cls._truncate_prompt_text(str(work_unit.get("objective") or ""), 240),
                "input_refs": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit.get("input_refs")))[:8],
                    max_chars=240,
                ),
                "expected_deliverable": cls._compact_verifier_prompt_value(
                    work_unit.get("expected_deliverable", {}),
                    max_chars=240,
                ),
                "evidence_requirements": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit.get("evidence_requirements")))[:8],
                    max_chars=240,
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
                "summary": cls._compact_verifier_prompt_value(work_unit_result.get("summary"), max_chars=240),
                "candidate_final_result": cls._compact_verifier_prompt_value(
                    work_unit_result.get("candidate_final_result"),
                    max_chars=240,
                ),
                "artifact_manifest": cls._compact_verifier_prompt_value(
                    work_unit_result.get("artifact_manifest", {}),
                    max_chars=240,
                ),
                "evidence": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit_result.get("evidence")))[:8],
                    max_chars=240,
                ),
                "diagnostics": cls._compact_verifier_prompt_value(
                    list(cls._prompt_sequence(work_unit_result.get("diagnostics")))[:4],
                    max_chars=240,
                ),
                "carrier_meta": {
                    "snapshot_status": carrier_meta.get("snapshot_status"),
                    "execution_plan": cls._compact_verifier_prompt_value(
                        carrier_meta.get("execution_plan", {}),
                        max_chars=240,
                    ),
                    "execution_block_graph": cls._compact_verifier_prompt_value(
                        carrier_meta.get("execution_block_graph", {}),
                        max_chars=240,
                    ),
                },
            },
            "output_policy": DataFormatter.sanitize(block_carrier.get("output_policy", {})),
            "context_reads": cls._compact_taskboard_context_reads_for_carrier_meta(
                block_carrier,
                blocks,
            ),
            "block_graph": cls._compact_taskboard_blocks_for_carrier_meta(blocks),
        }

    @classmethod
    def _compact_taskboard_context_reads_for_carrier_meta(
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
                    cls._compact_taskboard_context_read(item)
                    for item in execution_results
                    if str(item.get("kind") or "") == "context_read"
                ]
                if operations:
                    return operations
        direct_operations = block_carrier.get("context_reads")
        if isinstance(direct_operations, Sequence) and not isinstance(direct_operations, (str, bytes, bytearray)):
            return [
                cls._compact_taskboard_context_read(item)
                for item in direct_operations
                if isinstance(item, Mapping)
            ]
        return []

    @classmethod
    def _compact_taskboard_context_read(cls, item: Mapping[str, Any]) -> dict[str, Any]:
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
                output_summary["bounded"] = cls._compact_taskboard_context_read_bounded(bounded)
            for output_key, source_key in (
                ("first_locator_ref", "locator_refs"),
                ("first_evidence_snippet", "evidence_snippets"),
            ):
                max_chars = 1800 if output_key == "first_evidence_snippet" else 900
                if output_key in output:
                    output_summary[output_key] = cls._compact_taskboard_task_workspace_ref_or_snippet(
                        output.get(output_key),
                        max_chars=max_chars,
                    )
                    continue
                source = output.get(source_key)
                if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)) and source:
                    output_summary[output_key] = cls._compact_taskboard_task_workspace_ref_or_snippet(
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
    def _compact_taskboard_context_read_bounded(cls, bounded: Mapping[str, Any]) -> dict[str, Any]:
        keep_keys = (
            "operation",
            "query",
            "filters",
            "path",
            "pattern",
            "source_kinds",
            "retrieval_strategy",
            "retrieval_method",
            "retrieval_selection",
            "retrieval_rerank",
            "retrieval_candidate_count",
            "retrieval_selected_count",
            "retrieval_omitted",
            "returned_results",
            "continuation_available",
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
        raw_coverage = bounded.get("source_coverage")
        if isinstance(raw_coverage, Mapping):
            compact["source_coverage"] = {
                str(binding_id): {
                    "scope": cls._compact_verifier_prompt_value(
                        record.get("scope", {}),
                        max_chars=700,
                    ),
                    "returned_candidates": record.get("returned_candidates", 0),
                    "exhaustive": bool(record.get("exhaustive")),
                    "continuation_available": bool(
                        record.get("continuation_available")
                    ),
                }
                for binding_id, record in list(raw_coverage.items())[:16]
                if isinstance(record, Mapping)
            }
        diagnostics = bounded.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
            compact["diagnostics"] = cls._model_hot_diagnostics(list(diagnostics)[:4])
        return DataFormatter.sanitize(compact)

    @classmethod
    def _compact_taskboard_task_workspace_ref_or_snippet(cls, value: Any, *, max_chars: int) -> Any:
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
    def _taskboard_card_context_reads_for_prompt(
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
            raw_operations = block_carrier.get("context_reads")
            if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, (str, bytes, bytearray)):
                continue
            for operation in raw_operations:
                if not isinstance(operation, Mapping):
                    continue
                operations.append(cls._compact_taskboard_context_read(operation))
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
