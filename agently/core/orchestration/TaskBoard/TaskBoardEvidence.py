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

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agently.types.data import TaskBoardRevision


TASK_BOARD_EVIDENCE_VIEW_SCHEMA_VERSION = "task_board_evidence_view/v1"

_REF_CONTENT_KEYS = {
    "body",
    "content",
    "data",
    "html",
    "markdown",
    "preview",
    "raw",
    "result",
    "text",
}


@dataclass(frozen=True)
class TaskBoardEvidenceView:
    board_id: str
    revision_id: str
    cards: tuple[Mapping[str, Any], ...]
    status_counts: Mapping[str, int]
    artifact_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    file_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    evidence_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    preview_chars: int = 900
    truncated: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_BOARD_EVIDENCE_VIEW_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "board_id": self.board_id,
            "revision_id": self.revision_id,
            "cards": [dict(card) for card in self.cards],
            "status_counts": dict(self.status_counts),
            "artifact_refs": [dict(ref) for ref in self.artifact_refs],
            "file_refs": [dict(ref) for ref in self.file_refs],
            "evidence_refs": [dict(ref) for ref in self.evidence_refs],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "preview_chars": self.preview_chars,
            "truncated": self.truncated,
            "metadata": dict(self.metadata),
        }


def build_task_board_evidence_view(
    revision: TaskBoardRevision | Mapping[str, Any],
    *,
    card_ids: Sequence[str] | None = None,
    preview_chars: int = 900,
    diagnostic_limit: int = 16,
) -> TaskBoardEvidenceView:
    """Build a bounded hot evidence view for verifier/replan/model prompts.

    The returned view intentionally preserves refs and metadata while bounding
    previews. Full artifacts stay behind Workspace/Action refs.
    """

    if preview_chars <= 0:
        raise ValueError("TaskBoard evidence preview_chars must be greater than 0.")
    effective_revision = TaskBoardRevision.from_value(revision)
    requested_ids = tuple(str(item) for item in card_ids) if card_ids is not None else None
    known_ids = {card.id for card in effective_revision.graph.cards}
    if requested_ids is not None:
        unknown = [card_id for card_id in requested_ids if card_id not in known_ids]
        if unknown:
            raise ValueError(f"TaskBoard evidence view requested unknown card ids: { unknown }.")
        include_ids = set(requested_ids)
    else:
        include_ids = known_ids

    cards: list[Mapping[str, Any]] = []
    status_counts: dict[str, int] = {}
    artifact_refs: list[Mapping[str, Any]] = []
    file_refs: list[Mapping[str, Any]] = []
    evidence_refs: list[Mapping[str, Any]] = []
    diagnostics: list[Mapping[str, Any]] = []
    truncated = False

    for card in effective_revision.graph.cards:
        if card.id not in include_ids:
            continue
        result = effective_revision.card_results.get(card.id)
        status = str(result.status if result is not None else card.status)
        status_counts[status] = status_counts.get(status, 0) + 1
        preview = _bounded_preview(result.preview if result is not None else None, preview_chars=preview_chars)
        truncated = truncated or bool(preview["truncated"])
        card_artifact_refs = _dedupe_refs(result.artifact_refs if result is not None else ())
        card_file_refs = _dedupe_refs(result.file_refs if result is not None else ())
        card_diagnostics = _bounded_diagnostics(result.diagnostics if result is not None else (), diagnostic_limit)
        truncated = truncated or bool(card_diagnostics.get("truncated"))
        artifact_refs.extend(card_artifact_refs)
        file_refs.extend(card_file_refs)
        diagnostics.extend(card_diagnostics["items"])
        cards.append(
            {
                "card_id": card.id,
                "status": status,
                "objective": card.objective,
                "depends_on": list(card.depends_on),
                "required_outputs": list(card.required_outputs),
                "failure_policy": card.failure_policy,
                "output_digest": result.output_digest if result is not None else None,
                "preview": preview,
                "artifact_refs": card_artifact_refs,
                "file_refs": card_file_refs,
                "diagnostics": card_diagnostics,
                "has_cold_refs": bool(card_artifact_refs or card_file_refs),
            }
        )

    for ref in effective_revision.evidence_refs:
        evidence_refs.append(_sanitize_ref(ref))
    revision_diagnostics = _bounded_diagnostics(effective_revision.diagnostics, diagnostic_limit)
    truncated = truncated or bool(revision_diagnostics.get("truncated"))
    diagnostics.extend(revision_diagnostics["items"])

    return TaskBoardEvidenceView(
        board_id=effective_revision.board_id,
        revision_id=effective_revision.revision_id,
        cards=tuple(cards),
        status_counts=status_counts,
        artifact_refs=tuple(_dedupe_refs(artifact_refs)),
        file_refs=tuple(_dedupe_refs(file_refs)),
        evidence_refs=tuple(_dedupe_refs(evidence_refs)),
        diagnostics=tuple(diagnostics[:diagnostic_limit]),
        preview_chars=preview_chars,
        truncated=truncated or len(diagnostics) > diagnostic_limit,
        metadata={
            "evidence_policy": "hot_summary_with_cold_refs",
            "card_scope": list(requested_ids) if requested_ids is not None else "all",
            "full_content_location": "artifact_refs/file_refs/evidence_refs",
        },
    )


def _bounded_preview(value: Any, *, preview_chars: int) -> dict[str, Any]:
    if value is None:
        text = ""
        media_type = "text/plain"
    elif isinstance(value, str):
        text = value
        media_type = "text/plain"
    else:
        text = _json_text(value)
        media_type = "application/json"
    preview = text[:preview_chars]
    return {
        "content": preview,
        "truncated": len(text) > preview_chars,
        "original_chars": len(text),
        "preview_chars": len(preview),
        "media_type": media_type,
    }


def _bounded_diagnostics(
    diagnostics: Sequence[Mapping[str, Any]],
    limit: int,
) -> dict[str, Any]:
    items = [_sanitize_mapping(item) for item in diagnostics[:limit]]
    return {
        "items": items,
        "truncated": len(diagnostics) > limit,
        "original_count": len(diagnostics),
        "preview_count": len(items),
    }


def _dedupe_refs(refs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        clean = _sanitize_ref(ref)
        key = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _sanitize_ref(ref: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        str(key): _sanitize_ref_value(value)
        for key, value in dict(ref).items()
        if str(key).strip().lower() not in _REF_CONTENT_KEYS
    }


def _sanitize_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        str(key): _sanitize_ref_value(item)
        for key, item in dict(value).items()
        if str(key).strip().lower() not in _REF_CONTENT_KEYS
    }


def _sanitize_ref_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_ref_value(item) for item in value]
    return value


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(value)
