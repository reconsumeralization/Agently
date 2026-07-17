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
    evidence_items: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
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
            "evidence_items": [dict(item) for item in self.evidence_items],
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
    previews. Full artifacts stay behind TaskWorkspace/Action refs.
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
    evidence_items: list[Mapping[str, Any]] = []
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
        card_evidence_items = _card_result_evidence_items(
            result,
            board_id=effective_revision.board_id,
            revision_id=effective_revision.revision_id,
            card_id=card.id,
            artifact_refs=card_artifact_refs,
            file_refs=card_file_refs,
            diagnostics=card_diagnostics["items"],
        )
        evidence_items.extend(card_evidence_items)
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
                "evidence_item_ids": [str(item.get("id") or "") for item in card_evidence_items if item.get("id")],
                "diagnostics": card_diagnostics,
                "has_cold_refs": bool(card_artifact_refs or card_file_refs),
            }
        )

    for ref in effective_revision.evidence_refs:
        evidence_refs.append(_sanitize_ref(ref))
        evidence_items.append(
            _evidence_item(
                "taskboard_evidence_ref",
                board_id=effective_revision.board_id,
                revision_id=effective_revision.revision_id,
                card_id="revision",
                index=len(evidence_items),
                status="ok",
                body_state=_body_state_from_ref(ref),
                ref=ref,
            )
        )
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
        evidence_items=tuple(_dedupe_evidence_items(evidence_items)),
        diagnostics=tuple(diagnostics[:diagnostic_limit]),
        preview_chars=preview_chars,
        truncated=truncated or len(diagnostics) > diagnostic_limit,
        metadata={
            "evidence_policy": "hot_summary_with_cold_refs",
            "card_scope": list(requested_ids) if requested_ids is not None else "all",
            "full_content_location": "artifact_refs/file_refs/evidence_refs",
            "ledger_policy": "evidence_items_are_authoritative_hot_projection",
        },
    )


def _card_result_evidence_items(
    result: Any,
    *,
    board_id: str,
    revision_id: str,
    card_id: str,
    artifact_refs: Sequence[Mapping[str, Any]],
    file_refs: Sequence[Mapping[str, Any]],
    diagnostics: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    metadata = result.metadata if result is not None else {}
    if isinstance(metadata, Mapping):
        ledger = metadata.get("evidence_ledger")
        if isinstance(ledger, Mapping):
            raw_items = ledger.get("items")
            if isinstance(raw_items, Sequence) and not isinstance(raw_items, str | bytes | bytearray):
                for item in raw_items:
                    if not isinstance(item, Mapping):
                        continue
                    copied = _sanitize_evidence_item(item)
                    provenance = copied.get("provenance")
                    provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
                    provenance.update({"taskboard_board_id": board_id, "taskboard_revision_id": revision_id, "taskboard_card_id": card_id})
                    copied["provenance"] = provenance
                    items.append(copied)
    for ref in [*artifact_refs, *file_refs]:
        items.append(
            _evidence_item(
                "taskboard_ref",
                board_id=board_id,
                revision_id=revision_id,
                card_id=card_id,
                index=len(items),
                status="ok",
                body_state=_body_state_from_ref(ref),
                ref=ref,
            )
        )
    for diagnostic in diagnostics:
        items.append(
            _evidence_item(
                "taskboard_diagnostic",
                board_id=board_id,
                revision_id=revision_id,
                card_id=card_id,
                index=len(items),
                status="failed",
                body_state="bounded",
                ref=diagnostic,
            )
        )
    return items


def _evidence_item(
    kind: str,
    *,
    board_id: str,
    revision_id: str,
    card_id: str,
    index: int,
    status: str,
    body_state: str,
    ref: Mapping[str, Any],
) -> Mapping[str, Any]:
    clean_ref = _sanitize_mapping(ref)
    path = str(clean_ref.get("path") or clean_ref.get("value") or clean_ref.get("id") or "").strip()
    evidence_id = _evidence_item_id(kind, board_id, revision_id, card_id, path, index)
    item: dict[str, Any] = {
        "id": evidence_id,
        "kind": kind,
        "status": status,
        "raw_status": clean_ref.get("status", status),
        "body_state": body_state,
        "provenance": {
            "source": "taskboard_evidence_view",
            "taskboard_board_id": board_id,
            "taskboard_revision_id": revision_id,
            "taskboard_card_id": card_id,
        },
    }
    for key in (
        "path",
        "field",
        "value",
        "source_url",
        "selected_url",
        "requested_url",
        "canonical_url",
        "url",
        "href",
        "record_id",
        "artifact_id",
        "content_state",
        "truncated",
        "role",
        "source",
    ):
        if clean_ref.get(key) not in (None, "", [], {}):
            item[key] = clean_ref.get(key)
    message = clean_ref.get("message") or clean_ref.get("summary") or clean_ref.get("preview")
    if message not in (None, "", [], {}) and body_state != "ref_only":
        item["body"] = str(message)
    return item


def _body_state_from_ref(ref: Mapping[str, Any]) -> str:
    raw = str(ref.get("body_state") or ref.get("content_state") or "").strip()
    if raw == "ref_only":
        return "ref_only"
    if raw in {"full", "bounded", "truncated"}:
        return raw
    if ref.get("truncated") is True:
        return "truncated"
    for key in ("content", "content_preview", "snippet", "text", "preview", "summary", "message"):
        value = ref.get(key)
        if isinstance(value, str) and value.strip():
            return "bounded"
    return "ref_only"


def _evidence_item_id(kind: str, board_id: str, revision_id: str, card_id: str, path: str, index: int) -> str:
    raw = ":".join(part for part in (kind, board_id, revision_id, card_id, path, str(index)) if part)
    return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in raw)[:240]


def _dedupe_evidence_items(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    deduped: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        evidence_id = str(item.get("id") or "")
        if evidence_id and evidence_id in seen:
            continue
        if evidence_id:
            seen.add(evidence_id)
        deduped.append(_sanitize_evidence_item(item))
    return deduped


def _sanitize_evidence_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _sanitize_ref_value(value) for key, value in dict(item).items()}


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
