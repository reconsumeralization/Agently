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
            "source_refs": source_refs_source,
            "source_refs_omitted": max(0, len(source_refs_sequence) - 16),
        }


__all__ = ["AgentTaskTaskBoardProjectionMixin"]
