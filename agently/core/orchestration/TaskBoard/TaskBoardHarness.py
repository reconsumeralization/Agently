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

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from agently.types.data import TaskBoardRevision


TASK_BOARD_ACCEPTANCE_INDEX_SCHEMA_VERSION = "task_board_acceptance_index/v1"
TASK_BOARD_INCREMENTAL_VERIFICATION_PLAN_SCHEMA_VERSION = "task_board_incremental_verification_plan/v1"
TASK_BOARD_SCOPED_EVIDENCE_VIEW_SCHEMA_VERSION = "task_board_scoped_evidence_view/v1"
TASK_BOARD_HANDOFF_PROJECTION_SCHEMA_VERSION = "task_board_handoff_projection/v1"
TASK_BOARD_FOCUS_PAYLOAD_SCHEMA_VERSION = "task_board_focus_payload/v1"

_HOT_REF_CONTENT_KEYS = {
    "body",
    "content",
    "data",
    "html",
    "markdown",
    "preview",
    "prompt",
    "raw",
    "request",
    "response",
    "result",
    "text",
    "transcript",
}
_COLD_INTEGRITY_KEYS = {"bytes", "checksum", "digest", "read_bytes", "raw_bytes", "sha256"}
_ACCEPTANCE_STATUSES = {
    "unknown",
    "active",
    "satisfied",
    "setback",
    "blocked",
    "deferred",
    "not_applicable",
}
_ACCEPTANCE_GREEN_STATUSES = {"satisfied", "not_applicable"}


def build_task_board_acceptance_index(
    revision: TaskBoardRevision | Mapping[str, Any],
    *,
    success_criteria: Sequence[str] | None = None,
    verification: Mapping[str, Any] | None = None,
    evidence_view: Mapping[str, Any] | None = None,
    evidence_ledger: Mapping[str, Any] | None = None,
    explicit_state_facts: Sequence[Mapping[str, Any]] | None = None,
    previous_acceptance_index: Mapping[str, Any] | None = None,
    cost_telemetry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a non-authoritative TaskBoard acceptance projection.

    The projection is rebuilt from revision, verifier, and evidence views. It is
    intentionally not an EvidenceEnvelope item and never decides completion by
    itself.
    """

    effective_revision = TaskBoardRevision.from_value(revision)
    index_items: dict[str, dict[str, Any]] = {}

    for position, criterion in enumerate(_clean_str_sequence(success_criteria), start=1):
        item_id = f"criterion:{position}:{_slug(criterion)}"
        index_items[item_id] = {
            "id": item_id,
            "criterion": criterion,
            "status": "unknown",
            "status_reason": "Criterion is declared but not yet verifier-satisfied.",
            "source": "user",
            "requirement_level": "required",
            "linked_card_ids": [],
            "linked_evidence_ids": [],
            "linked_locator_ids": [],
        }

    for card in effective_revision.graph.cards:
        criteria = _card_acceptance_criteria(card.to_dict())
        if not criteria:
            continue
        result = effective_revision.card_results.get(card.id)
        card_status = str(result.status if result is not None else card.status)
        for criterion in criteria:
            item = _ensure_acceptance_item(index_items, criterion, source="taskboard_card")
            _append_unique(item["linked_card_ids"], card.id)
            if item["status"] == "unknown" and card_status in {"completed", "running", "ready"}:
                item["status"] = "active"
                item["status_reason"] = f"Linked TaskBoard card {card.id!r} is {card_status}."
                item["source"] = "taskboard_card"
            if card_status == "setback":
                item["status"] = "setback"
                item["status_reason"] = f"Linked TaskBoard card {card.id!r} encountered a recoverable setback."
                item["source"] = "taskboard_card"
            if card_status in {"blocked", "failed"}:
                item["status"] = "blocked"
                item["status_reason"] = f"Linked TaskBoard card {card.id!r} is {card_status}."
                item["source"] = "taskboard_card"

    locator_items = _acceptance_locator_items(evidence_view=evidence_view, evidence_ledger=evidence_ledger)
    for locator in locator_items:
        criterion = _clean_str(locator.get("criterion") or locator.get("claim") or locator.get("acceptance_criterion"))
        if not criterion:
            continue
        item = _ensure_acceptance_item(index_items, criterion, source="artifact_locator")
        locator_id = _clean_str(locator.get("id") or locator.get("evidence_id") or locator.get("locator_id"))
        if locator_id:
            _append_unique(item["linked_locator_ids"], locator_id)
            _append_unique(item["linked_evidence_ids"], locator_id)
        if item["status"] == "unknown":
            item["status"] = "active"
            item["status_reason"] = "Acceptance locator exists; semantic completion still requires verifier support."
            item["source"] = "artifact_locator"

    _apply_verification_to_acceptance_items(index_items, verification)
    _apply_explicit_state_facts_to_acceptance_items(index_items, explicit_state_facts)

    items = sorted(index_items.values(), key=lambda item: item["id"])
    for item in items:
        status = _normalize_acceptance_status(item.get("status"))
        if item.get("requirement_level") == "advisory" and status in {"unknown", "active"}:
            status = "not_applicable"
            item["status_reason"] = (
                "Advisory TaskBoard/card locator point; required progress is driven by declared success criteria."
            )
        item["status"] = status
        item["linked_card_ids"] = _sorted_unique_strings(item.get("linked_card_ids"))
        item["linked_evidence_ids"] = _sorted_unique_strings(item.get("linked_evidence_ids"))
        item["linked_locator_ids"] = _sorted_unique_strings(item.get("linked_locator_ids"))

    _annotate_acceptance_incremental_fields(
        items,
        revision=effective_revision,
        evidence_view=evidence_view,
        evidence_ledger=evidence_ledger,
        verification=verification,
        previous_acceptance_index=previous_acceptance_index,
        cost_telemetry=cost_telemetry,
    )
    status_counts = _acceptance_status_counts(items)
    metadata = _acceptance_incremental_metadata(items, previous_acceptance_index=previous_acceptance_index)

    return {
        "schema_version": TASK_BOARD_ACCEPTANCE_INDEX_SCHEMA_VERSION,
        "board_id": effective_revision.board_id,
        "revision_id": effective_revision.revision_id,
        "items": items,
        "status_counts": {status: count for status, count in status_counts.items() if count},
        "metadata": {
            "authority": "projection_only",
            "semantic_owner": "taskboard_final_verifier",
            "source": "taskboard_harness_projection",
            **metadata,
        },
    }


def build_task_board_incremental_verification_plan(acceptance_index: Mapping[str, Any]) -> dict[str, Any]:
    """Select only dirty acceptance items for the next verifier pass."""

    items = [dict(item) for item in _sequence_of_mappings(acceptance_index.get("items"))]
    dirty_items = [item for item in items if _acceptance_item_dirty(item)]
    satisfied_items = [
        item for item in items if _normalize_acceptance_status(item.get("status")) in _ACCEPTANCE_GREEN_STATUSES
    ]
    total = len(items)
    green_count = len(satisfied_items)
    dirty_count = len(dirty_items)
    return {
        "schema_version": TASK_BOARD_INCREMENTAL_VERIFICATION_PLAN_SCHEMA_VERSION,
        "board_id": acceptance_index.get("board_id"),
        "revision_id": acceptance_index.get("revision_id"),
        "all_satisfied": total > 0 and dirty_count == 0 and green_count == total,
        "dirty_item_ids": [str(item.get("id")) for item in dirty_items if item.get("id")],
        "satisfied_item_ids": [str(item.get("id")) for item in satisfied_items if item.get("id")],
        "status_counts": dict(acceptance_index.get("status_counts") or _acceptance_status_counts(items)),
        "metadata": {
            "authority": "projection_only",
            "semantic_owner": "taskboard_final_verifier",
            "dirty_count": dirty_count,
            "green_count": green_count,
            "total_items": total,
            "acceptance_progress_percent": _acceptance_progress_percent(green_count, total),
            "verifier_cache_hit_count": sum(1 for item in items if item.get("cache_status") == "hit"),
            "verifier_cache_miss_count": sum(1 for item in items if item.get("cache_status") == "miss"),
        },
    }


def build_task_board_scoped_evidence_view(
    acceptance_index: Mapping[str, Any],
    *,
    evidence_view: Mapping[str, Any] | None = None,
    evidence_ledger: Mapping[str, Any] | None = None,
    max_items: int = 32,
    body_chars: int = 480,
) -> dict[str, Any]:
    """Build a bounded hot evidence projection for dirty acceptance items only."""

    evidence_items = _evidence_items_by_id(evidence_view=evidence_view, evidence_ledger=evidence_ledger)
    selected_items = [
        dict(item)
        for item in _sequence_of_mappings(acceptance_index.get("items"))
        if _acceptance_item_dirty(item)
    ][:max_items]
    selected_evidence_ids: list[str] = []
    for item in selected_items:
        for key in ("linked_evidence_ids", "linked_locator_ids"):
            for evidence_id in _clean_str_sequence(item.get(key)):
                _append_unique(selected_evidence_ids, evidence_id)
                for related_id in _expanded_related_evidence_ids([evidence_id], evidence_items):
                    _append_unique(selected_evidence_ids, related_id)
    if not selected_evidence_ids:
        for evidence_id in _fallback_scoped_evidence_ids(
            selected_items,
            evidence_items=evidence_items,
            evidence_view=evidence_view,
            max_items=max_items,
        ):
            _append_unique(selected_evidence_ids, evidence_id)

    scoped_items: list[dict[str, Any]] = []
    for evidence_id in selected_evidence_ids:
        evidence_item = evidence_items.get(evidence_id)
        if evidence_item is None:
            continue
        scoped_items.append(_scoped_evidence_item(evidence_item, body_chars=body_chars))
        if len(scoped_items) >= max_items:
            break

    return {
        "schema_version": TASK_BOARD_SCOPED_EVIDENCE_VIEW_SCHEMA_VERSION,
        "board_id": acceptance_index.get("board_id"),
        "revision_id": acceptance_index.get("revision_id"),
        "selected_acceptance_item_ids": [str(item.get("id")) for item in selected_items if item.get("id")],
        "evidence_items": scoped_items,
        "metadata": {
            "authority": "projection_only",
            "source": "taskboard_acceptance_dirty_set",
            "scoped_to_dirty_acceptance_items": True,
            "evidence_item_count": len(scoped_items),
            "evidence_bytes_sent_to_verifier": sum(len(str(item.get("snippet") or "")) for item in scoped_items),
            "body_chars": max(body_chars, 0),
        },
    }


def build_task_board_handoff_projection(
    *,
    task_id: str,
    execution_strategy: str,
    effective_execution_strategy: str,
    stage: str,
    tick_index: int,
    revision: TaskBoardRevision | Mapping[str, Any],
    schedule: Any = None,
    evidence_view: Mapping[str, Any] | None = None,
    acceptance_index: Mapping[str, Any] | None = None,
    runtime_topology: Mapping[str, Any] | None = None,
    final_result: Mapping[str, Any] | None = None,
    checkpoint_refs: Sequence[Mapping[str, Any]] | None = None,
    observation_refs: Sequence[Mapping[str, Any]] | None = None,
    explicit_state_facts: Sequence[Mapping[str, Any]] | None = None,
    max_items: int = 12,
) -> dict[str, Any]:
    effective_revision = TaskBoardRevision.from_value(revision)
    schedule_view = _schedule_view(schedule)
    evidence_view = evidence_view if isinstance(evidence_view, Mapping) else {}
    acceptance_index = acceptance_index if isinstance(acceptance_index, Mapping) else {}
    explicit_facts = [_sanitize_mapping(item) for item in (explicit_state_facts or ())]

    completed_card_ids = _ids_from_schedule(schedule_view, "completed_card_ids") or [
        card_id
        for card_id, result in effective_revision.card_results.items()
        if str(result.status) == "completed"
    ]
    blocked_card_ids = _ids_from_schedule(schedule_view, "blocked_card_ids")
    runnable_card_ids = _ids_from_schedule(schedule_view, "runnable_card_ids")
    active_card_ids = _sorted_unique_strings(
        [
            *runnable_card_ids,
            *[
                card.id
                for card in effective_revision.graph.cards
                if str(effective_revision.card_results.get(card.id, card).status) in {"ready", "running"}
            ],
        ]
    )

    focus_payload = build_task_board_focus_payload(
        effective_revision,
        acceptance_index=acceptance_index,
        schedule=schedule_view,
        preflight_diagnostics=explicit_facts,
        max_items=max_items,
    )

    return {
        "schema_version": TASK_BOARD_HANDOFF_PROJECTION_SCHEMA_VERSION,
        "task_id": str(task_id),
        "board_id": effective_revision.board_id,
        "revision_id": effective_revision.revision_id,
        "execution_strategy": str(execution_strategy),
        "effective_execution_strategy": str(effective_execution_strategy),
        "stage": str(stage),
        "tick_index": int(tick_index),
        "status": str(effective_revision.status),
        "acceptance_index_summary": _acceptance_index_summary(acceptance_index, max_items=max_items),
        "active_card_ids": active_card_ids[:max_items],
        "runnable_card_ids": runnable_card_ids[:max_items],
        "completed_card_ids": _sorted_unique_strings(completed_card_ids)[:max_items],
        "blocked_card_ids": _sorted_unique_strings(blocked_card_ids)[:max_items],
        "deferred_card_ids": _deferred_card_ids(effective_revision)[:max_items],
        "next_focus_candidates": focus_payload.get("next_focus_candidates", [])[:max_items],
        "evidence_refs": _bounded_refs(evidence_view.get("evidence_refs"), max_items=max_items),
        "locator_refs": _bounded_refs(_locator_refs_from_evidence_view(evidence_view), max_items=max_items),
        "artifact_refs": _bounded_refs(evidence_view.get("artifact_refs"), max_items=max_items),
        "file_refs": _bounded_refs(evidence_view.get("file_refs"), max_items=max_items),
        "checkpoint_refs": _bounded_refs(checkpoint_refs, max_items=max_items),
        "observation_refs": _bounded_refs(observation_refs, max_items=max_items),
        "verifier_gaps": _verifier_gaps(final_result, max_items=max_items),
        "capability_preflight_facts": explicit_facts[:max_items],
        "explicit_state_facts": explicit_facts[:max_items],
        "runtime_topology": _sanitize_mapping(runtime_topology or {}),
        "raw_record_refs": {
            "revision_id": effective_revision.revision_id,
            "checkpoint_ref_ids": _ref_ids(checkpoint_refs)[:max_items],
            "observation_ref_ids": _ref_ids(observation_refs)[:max_items],
        },
        "metadata": {
            "authority": "orientation_only",
            "full_state_sources": ["TaskBoardRevision", "EvidenceEnvelope", "RecordStore checkpoint records"],
            "bounded": True,
        },
    }


def task_board_preflight_diagnostics(
    revision: TaskBoardRevision | Mapping[str, Any],
    *,
    mounted_capabilities: Sequence[Mapping[str, Any] | str] | None = None,
    task_workspace_refs: Sequence[Mapping[str, Any] | str] | None = None,
) -> list[dict[str, Any]]:
    effective_revision = TaskBoardRevision.from_value(revision)
    capability_ids = _capability_ids(mounted_capabilities)
    task_workspace_ref_ids = _capability_ids(task_workspace_refs)
    diagnostics: list[dict[str, Any]] = []

    for card in effective_revision.graph.cards:
        metadata = card.metadata if isinstance(card.metadata, Mapping) else {}
        if not metadata.get("preflight_kind"):
            continue
        missing_capabilities = [
            capability_id
            for capability_id in _clean_str_sequence(metadata.get("requires_capability_ids"))
            if capability_id not in capability_ids
        ]
        if missing_capabilities:
            diagnostics.append(
                {
                    "code": "taskboard.preflight.unmounted_capability",
                    "card_id": card.id,
                    "missing_capability_ids": missing_capabilities,
                    "status": "blocked",
                }
            )
        missing_refs = [
            ref_id
            for ref_id in _clean_str_sequence(metadata.get("requires_task_workspace_refs"))
            if ref_id not in task_workspace_ref_ids
        ]
        if missing_refs:
            diagnostics.append(
                {
                    "code": "taskboard.preflight.missing_task_workspace_ref",
                    "card_id": card.id,
                    "missing_task_workspace_refs": missing_refs,
                    "status": "blocked",
                }
            )
    return diagnostics


def build_task_board_focus_payload(
    revision: TaskBoardRevision | Mapping[str, Any],
    *,
    acceptance_index: Mapping[str, Any] | None = None,
    schedule: Any = None,
    preflight_diagnostics: Sequence[Mapping[str, Any]] | None = None,
    max_items: int = 12,
) -> dict[str, Any]:
    effective_revision = TaskBoardRevision.from_value(revision)
    schedule_view = _schedule_view(schedule)
    acceptance_index = acceptance_index if isinstance(acceptance_index, Mapping) else {}
    items = [item for item in acceptance_index.get("items", []) if isinstance(item, Mapping)]
    selected_item_ids = [
        str(item.get("id"))
        for item in items
        if str(item.get("status") or "unknown") in {"unknown", "active", "setback", "blocked", "deferred"}
        and item.get("id")
    ][:max_items]
    runnable_card_ids = _ids_from_schedule(schedule_view, "runnable_card_ids")
    blocked_card_ids = _ids_from_schedule(schedule_view, "blocked_card_ids")
    diagnostics = [_sanitize_mapping(item) for item in (preflight_diagnostics or ())]

    candidates: list[dict[str, Any]] = []
    for card_id in runnable_card_ids:
        candidates.append({"kind": "card", "id": card_id, "reason": "dependency_ready"})
    for item_id in selected_item_ids:
        candidates.append({"kind": "acceptance_item", "id": item_id, "reason": "not_satisfied"})
    for diagnostic in diagnostics:
        card_id = _clean_str(diagnostic.get("card_id"))
        if card_id:
            candidates.append({"kind": "preflight", "id": card_id, "reason": str(diagnostic.get("code") or "diagnostic")})

    return {
        "schema_version": TASK_BOARD_FOCUS_PAYLOAD_SCHEMA_VERSION,
        "board_id": effective_revision.board_id,
        "revision_id": effective_revision.revision_id,
        "selected_acceptance_item_ids": selected_item_ids,
        "runnable_card_ids": runnable_card_ids[:max_items],
        "blocked_card_ids": blocked_card_ids[:max_items],
        "deferred_card_ids": _deferred_card_ids(effective_revision)[:max_items],
        "preflight_diagnostics": diagnostics[:max_items],
        "next_focus_candidates": candidates[:max_items],
        "metadata": {
            "selection_policy": "status_dependency_capability_projection",
            "semantic_owner": "structured_taskboard_control_or_verifier_request",
        },
    }


def task_board_explicit_state_facts(
    revision: TaskBoardRevision | Mapping[str, Any],
    *,
    evidence_view: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    effective_revision = TaskBoardRevision.from_value(revision)
    facts: list[dict[str, Any]] = []
    for item in [*effective_revision.diagnostics, *_card_diagnostics(effective_revision)]:
        if _is_explicit_state_fact(item):
            facts.append(_sanitize_mapping(item))
    if isinstance(evidence_view, Mapping):
        for item in evidence_view.get("diagnostics") or ():
            if isinstance(item, Mapping) and _is_explicit_state_fact(item):
                facts.append(_sanitize_mapping(item))
    return _dedupe_mappings(facts)


def task_board_blocking_state_facts(facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blocking: list[dict[str, Any]] = []
    for fact in facts:
        status = str(fact.get("status") or "").strip().lower()
        if bool(fact.get("blocking")) or status in {"dirty", "unresolved", "failed", "blocked"}:
            blocking.append(_sanitize_mapping(fact))
    return blocking


def _apply_verification_to_acceptance_items(index_items: dict[str, dict[str, Any]], verification: Mapping[str, Any] | None) -> None:
    if not isinstance(verification, Mapping):
        return
    for check in _sequence_of_mappings(verification.get("criterion_checks")):
        criterion = _clean_str(check.get("criterion") or check.get("name") or check.get("claim"))
        if not criterion:
            continue
        item = _ensure_acceptance_item(index_items, criterion, source="verifier")
        satisfied = check.get("satisfied") is True
        item["status"] = "satisfied" if satisfied else "blocked"
        item["source"] = "verifier"
        item["status_reason"] = _clean_str(check.get("reason")) or (
            "Verifier marked the criterion satisfied."
            if satisfied
            else "Verifier criterion check did not provide satisfied=true."
        )
        verification_ref = _clean_str(
            check.get("verification_ref")
            or check.get("ref")
            or check.get("id")
            or verification.get("verification_ref")
            or verification.get("id")
        )
        verification_ref = verification_ref or f"verification:{_stable_fingerprint(check)[7:19]}"
        if verification_ref:
            item["last_verification_ref"] = verification_ref
        for evidence_id in _clean_str_sequence(check.get("evidence_ids")):
            _append_unique(item["linked_evidence_ids"], evidence_id)
        for locator_id in _clean_str_sequence(check.get("locator_ids")):
            _append_unique(item["linked_locator_ids"], locator_id)
            _append_unique(item["linked_evidence_ids"], locator_id)
    for missing in _clean_str_sequence(verification.get("missing_criteria")):
        item = _ensure_acceptance_item(index_items, missing, source="verifier")
        if item["status"] != "satisfied":
            item["status"] = "blocked"
            item["source"] = "verifier"
            item["status_reason"] = "Verifier reported this criterion as missing."


def _apply_explicit_state_facts_to_acceptance_items(
    index_items: dict[str, dict[str, Any]],
    explicit_state_facts: Sequence[Mapping[str, Any]] | None,
) -> None:
    for fact in explicit_state_facts or ():
        if not isinstance(fact, Mapping):
            continue
        criterion = _clean_str(fact.get("criterion") or fact.get("claim") or fact.get("reason"))
        if not criterion:
            continue
        item = _ensure_acceptance_item(index_items, criterion, source="host_policy")
        status = str(fact.get("status") or "").strip().lower()
        if status == "setback" and not bool(fact.get("blocking")):
            item["status"] = "setback"
            item["source"] = "host_policy"
            item["status_reason"] = criterion
        elif bool(fact.get("blocking")) or status in {"dirty", "unresolved", "failed", "blocked"}:
            item["status"] = "blocked"
            item["source"] = "host_policy"
            item["status_reason"] = criterion
        elif status in {"clean", "ok", "satisfied"}:
            item["status"] = "satisfied"
            item["source"] = "host_policy"
            item["status_reason"] = criterion


def _annotate_acceptance_incremental_fields(
    items: Sequence[dict[str, Any]],
    *,
    revision: TaskBoardRevision,
    evidence_view: Mapping[str, Any] | None,
    evidence_ledger: Mapping[str, Any] | None,
    verification: Mapping[str, Any] | None,
    previous_acceptance_index: Mapping[str, Any] | None,
    cost_telemetry: Mapping[str, Any] | None,
) -> None:
    previous = _previous_acceptance_items(previous_acceptance_index)
    evidence_items = _evidence_items_by_id(evidence_view=evidence_view, evidence_ledger=evidence_ledger)
    usage_summary = _verification_usage_summary(verification, cost_telemetry)

    for item in items:
        previous_item = _matching_previous_acceptance_item(item, previous)
        current_verification_ref = _clean_str(item.get("last_verification_ref"))
        if previous_item is not None and not current_verification_ref:
            for key in ("linked_card_ids", "linked_evidence_ids", "linked_locator_ids"):
                item[key] = _sorted_unique_strings([*_clean_str_sequence(item.get(key)), *_clean_str_sequence(previous_item.get(key))])
            previous_ref = _clean_str(previous_item.get("last_verification_ref"))
            if previous_ref:
                item["last_verification_ref"] = previous_ref
        item["verdict_fingerprint"] = _acceptance_item_fingerprint(
            item,
            revision=revision,
            evidence_items=evidence_items,
        )
        _annotate_acceptance_item_cache_state(
            item,
            previous_item=previous_item,
            current_verification_ref=current_verification_ref,
            usage_summary=usage_summary,
        )


def _annotate_acceptance_item_cache_state(
    item: dict[str, Any],
    *,
    previous_item: Mapping[str, Any] | None,
    current_verification_ref: str | None,
    usage_summary: Mapping[str, Any],
) -> None:
    status = _normalize_acceptance_status(item.get("status"))
    item["status"] = status
    item.setdefault("last_verification_ref", "")
    item.setdefault("cost_summary", {})

    if current_verification_ref:
        item["cache_status"] = "refresh"
        item["dirty_reason"] = "" if status in _ACCEPTANCE_GREEN_STATUSES else "verifier_reported_missing"
        item["cost_summary"] = _acceptance_item_cost_summary(item, usage_summary=usage_summary)
        return

    if status in {"setback", "blocked"}:
        item["cache_status"] = "miss"
        item["dirty_reason"] = "host_or_card_setback" if status == "setback" else "host_or_card_blocked"
        if previous_item is not None:
            item["cost_summary"] = dict(previous_item.get("cost_summary") or {})
        return

    if previous_item is None:
        item["cache_status"] = "miss" if status not in _ACCEPTANCE_GREEN_STATUSES else "unavailable"
        item["dirty_reason"] = "" if status in _ACCEPTANCE_GREEN_STATUSES else "no_cached_verdict"
        return

    previous_status = _normalize_acceptance_status(previous_item.get("status"))
    previous_fingerprint = _clean_str(previous_item.get("verdict_fingerprint"))
    current_fingerprint = _clean_str(item.get("verdict_fingerprint"))
    item["cost_summary"] = dict(previous_item.get("cost_summary") or {})

    if status == "not_applicable":
        item["cache_status"] = (
            "hit"
            if previous_status == "not_applicable" and previous_fingerprint and previous_fingerprint == current_fingerprint
            else "unavailable"
        )
        item["dirty_reason"] = ""
        previous_ref = _clean_str(previous_item.get("last_verification_ref"))
        if previous_ref:
            item["last_verification_ref"] = previous_ref
        return

    if previous_status in _ACCEPTANCE_GREEN_STATUSES and previous_fingerprint and previous_fingerprint == current_fingerprint:
        item["status"] = previous_status
        item["source"] = "verdict_cache"
        item["status_reason"] = "Reused prior verifier verdict because the acceptance fingerprint is unchanged."
        item["cache_status"] = "hit"
        item["dirty_reason"] = ""
        previous_ref = _clean_str(previous_item.get("last_verification_ref"))
        if previous_ref:
            item["last_verification_ref"] = previous_ref
        return

    item["cache_status"] = "miss"
    if previous_status in _ACCEPTANCE_GREEN_STATUSES:
        item["dirty_reason"] = "verdict_fingerprint_changed"
    elif status in _ACCEPTANCE_GREEN_STATUSES:
        item["dirty_reason"] = ""
    else:
        item["dirty_reason"] = "previous_verdict_not_green"


def _previous_acceptance_items(
    acceptance_index: Mapping[str, Any] | None,
) -> dict[str, dict[str, Mapping[str, Any]]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    by_criterion: dict[str, Mapping[str, Any]] = {}
    if isinstance(acceptance_index, Mapping):
        for item in _sequence_of_mappings(acceptance_index.get("items")):
            item_id = _clean_str(item.get("id"))
            criterion = _normalize_text(item.get("criterion"))
            if item_id:
                by_id[item_id] = item
            if criterion:
                by_criterion[criterion] = item
    return {"by_id": by_id, "by_criterion": by_criterion}


def _matching_previous_acceptance_item(
    item: Mapping[str, Any],
    previous: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    item_id = _clean_str(item.get("id"))
    if item_id:
        by_id = previous.get("by_id") or {}
        match = by_id.get(item_id)
        if match is not None:
            return match
    criterion = _normalize_text(item.get("criterion"))
    if criterion:
        by_criterion = previous.get("by_criterion") or {}
        return by_criterion.get(criterion)
    return None


def _acceptance_item_fingerprint(
    item: Mapping[str, Any],
    *,
    revision: TaskBoardRevision,
    evidence_items: Mapping[str, Mapping[str, Any]],
) -> str:
    card_by_id = revision.graph.card_by_id()
    linked_cards: list[Mapping[str, Any]] = []
    for card_id in _clean_str_sequence(item.get("linked_card_ids")):
        card = card_by_id.get(card_id)
        result = revision.card_results.get(card_id)
        linked_cards.append(
            {
                "card": card.to_dict() if card is not None else {"id": card_id, "missing": True},
                "result": result.to_dict() if result is not None else None,
            }
        )
    linked_evidence: list[Mapping[str, Any]] = []
    for evidence_id in _expanded_related_evidence_ids(item.get("linked_evidence_ids"), evidence_items):
        evidence_item = evidence_items.get(evidence_id)
        if evidence_item is None:
            linked_evidence.append({"id": evidence_id, "missing": True})
        else:
            linked_evidence.append(_evidence_fingerprint_payload(evidence_item))
    return _stable_fingerprint(
        {
            "criterion": item.get("criterion"),
            "linked_card_ids": _clean_str_sequence(item.get("linked_card_ids")),
            "linked_evidence_ids": _clean_str_sequence(item.get("linked_evidence_ids")),
            "linked_locator_ids": _clean_str_sequence(item.get("linked_locator_ids")),
            "linked_cards": linked_cards,
            "linked_evidence": linked_evidence,
        }
    )


def _evidence_items_by_id(
    *,
    evidence_view: Mapping[str, Any] | None,
    evidence_ledger: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for source in (evidence_view, evidence_ledger):
        if not isinstance(source, Mapping):
            continue
        for key in ("evidence_items", "items"):
            for item in _sequence_of_mappings(source.get(key)):
                aliases = _evidence_item_aliases(item)
                for alias in aliases:
                    result.setdefault(alias, item)
    return result


def _evidence_item_aliases(item: Mapping[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ("id", "evidence_id", "locator_id", "cite_as", "path", "record_id", "artifact_id"):
        text = _clean_str(item.get(key))
        if text:
            _append_unique(aliases, text)
    return aliases


def _expanded_related_evidence_ids(
    evidence_ids: Any,
    evidence_items: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    selected_ids = _clean_str_sequence(evidence_ids)
    expanded = list(selected_ids)
    selected_paths = {
        path
        for evidence_id in selected_ids
        if (item := evidence_items.get(evidence_id)) is not None
        if (path := _clean_str(item.get("path")))
    }
    if not selected_paths:
        return expanded
    seen_item_ids: set[str] = set(expanded)
    for item in _unique_evidence_items(evidence_items):
        item_id = _clean_str(item.get("id"))
        path = _clean_str(item.get("path"))
        if not item_id or item_id in seen_item_ids or path not in selected_paths:
            continue
        _append_unique(expanded, item_id)
        seen_item_ids.add(item_id)
    return expanded


def _fallback_scoped_evidence_ids(
    selected_acceptance_items: Sequence[Mapping[str, Any]],
    *,
    evidence_items: Mapping[str, Mapping[str, Any]],
    evidence_view: Mapping[str, Any] | None,
    max_items: int,
) -> list[str]:
    if not selected_acceptance_items:
        return []
    has_required_dirty_item = any(
        str(item.get("requirement_level") or "").strip().lower() in {"", "required"}
        for item in selected_acceptance_items
    )
    if not has_required_dirty_item:
        return []

    scoped_paths = _evidence_scope_paths(evidence_view)
    candidates: list[tuple[int, str, Mapping[str, Any]]] = []
    for item in _unique_evidence_items(evidence_items):
        item_id = _clean_str(item.get("id") or item.get("evidence_id") or item.get("locator_id") or item.get("cite_as"))
        if not item_id:
            continue
        path = _clean_str(item.get("path") or item.get("artifact_path"))
        if scoped_paths and path and path not in scoped_paths:
            continue
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        priority: int | None = None
        if kind == "task_workspace_artifact.readback":
            priority = 0
        elif "acceptance_locator" in kind or "artifact_locator" in kind:
            priority = 1
        elif kind == "task_workspace_artifact.targeted_readback":
            priority = 2
        elif kind == "task_workspace_artifact.acceptance_coverage":
            priority = 3
        if priority is None:
            continue
        candidates.append((priority, item_id, item))

    candidates.sort(key=lambda entry: (entry[0], entry[1]))
    selected: list[str] = []
    for _priority, item_id, _item in candidates:
        _append_unique(selected, item_id)
        if len(selected) >= max_items:
            break
    return selected


def _evidence_scope_paths(evidence_view: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(evidence_view, Mapping):
        return set()
    paths: set[str] = set()
    for key in ("artifact_refs", "file_refs", "evidence_refs", "evidence_items"):
        for item in _sequence_of_mappings(evidence_view.get(key)):
            path = _clean_str(item.get("path") or item.get("artifact_path"))
            if path:
                paths.add(path)
    return paths


def _unique_evidence_items(evidence_items: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in evidence_items.values():
        item_id = _clean_str(item.get("id")) or _stable_fingerprint(item)
        if item_id in seen:
            continue
        seen.add(item_id)
        unique.append(item)
    return unique


def _evidence_fingerprint_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "id",
        "kind",
        "type",
        "status",
        "raw_status",
        "body_state",
        "content_state",
        "path",
        "field",
        "value",
        "claim",
        "criterion",
        "artifact_id",
        "record_id",
        "url",
        "source_url",
        "canonical_url",
        "truncated",
        "sha256",
        "digest",
        "checksum",
    ):
        if item.get(key) not in (None, "", [], {}):
            payload[key] = _fingerprint_value(item.get(key))
    for key in ("body", "content", "markdown", "preview", "raw", "result", "text"):
        if item.get(key) not in (None, "", [], {}):
            payload[f"{key}_fingerprint"] = _stable_fingerprint(item.get(key))
    return payload


def _verification_usage_summary(
    verification: Mapping[str, Any] | None,
    cost_telemetry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for source in (verification, cost_telemetry):
        if not isinstance(source, Mapping):
            continue
        usage = source.get("usage")
        if isinstance(usage, Mapping):
            summary.update(_numeric_mapping(usage))
        summary.update(_numeric_mapping({key: source.get(key) for key in ("model_requests", "input_chars", "output_chars", "tokens", "cost")}))
    return summary


def _acceptance_item_cost_summary(
    item: Mapping[str, Any],
    *,
    usage_summary: Mapping[str, Any],
) -> dict[str, Any]:
    summary = dict(usage_summary)
    if not summary:
        return {}
    summary.setdefault("newly_green_items", 1 if _normalize_acceptance_status(item.get("status")) in _ACCEPTANCE_GREEN_STATUSES else 0)
    model_requests = _coerce_float(summary.get("model_requests"))
    newly_green = _coerce_float(summary.get("newly_green_items"))
    if model_requests is not None and newly_green and newly_green > 0:
        summary["model_requests_per_newly_green_item"] = model_requests / newly_green
    return summary


def _numeric_mapping(value: Mapping[str, Any]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for key, raw in value.items():
        number = _coerce_float(raw)
        if number is None:
            continue
        result[str(key)] = int(number) if number.is_integer() else number
    return result


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, "", [], {}):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _acceptance_status_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    status_counts = {status: 0 for status in sorted(_ACCEPTANCE_STATUSES)}
    for item in items:
        status = _normalize_acceptance_status(item.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1
    return status_counts


def _acceptance_incremental_metadata(
    items: Sequence[Mapping[str, Any]],
    *,
    previous_acceptance_index: Mapping[str, Any] | None,
) -> dict[str, Any]:
    total = len(items)
    green_count = sum(1 for item in items if _normalize_acceptance_status(item.get("status")) in _ACCEPTANCE_GREEN_STATUSES)
    dirty_count = sum(1 for item in items if _acceptance_item_dirty(item))
    cache_hit_count = sum(1 for item in items if item.get("cache_status") == "hit")
    cache_miss_count = sum(1 for item in items if item.get("cache_status") == "miss")
    previous_green_count = 0
    if isinstance(previous_acceptance_index, Mapping):
        previous_green_count = sum(
            1
            for item in _sequence_of_mappings(previous_acceptance_index.get("items"))
            if _normalize_acceptance_status(item.get("status")) in _ACCEPTANCE_GREEN_STATUSES
        )
    newly_green_count = max(green_count - previous_green_count, 0)
    model_requests = 0.0
    seen_verification_refs: set[str] = set()
    for item in items:
        if item.get("cache_status") != "refresh" or not isinstance(item.get("cost_summary"), Mapping):
            continue
        ref = _clean_str(item.get("last_verification_ref")) or _clean_str(item.get("id")) or str(len(seen_verification_refs))
        if ref in seen_verification_refs:
            continue
        seen_verification_refs.add(ref)
        model_requests += _coerce_float((item.get("cost_summary") or {}).get("model_requests")) or 0
    return {
        "total_items": total,
        "dirty_count": dirty_count,
        "green_count": green_count,
        "acceptance_progress_percent": _acceptance_progress_percent(green_count, total),
        "verifier_cache_hit_count": cache_hit_count,
        "verifier_cache_miss_count": cache_miss_count,
        "newly_green_count": newly_green_count,
        "model_calls_without_green_delta": int(model_requests) if model_requests and newly_green_count == 0 else 0,
    }


def _acceptance_progress_percent(green_count: int, total: int) -> int:
    if total <= 0:
        return 0
    return int(round((green_count / total) * 100))


def _acceptance_item_dirty(item: Mapping[str, Any]) -> bool:
    dirty_reason = _clean_str(item.get("dirty_reason"))
    if dirty_reason:
        return True
    return _normalize_acceptance_status(item.get("status")) not in _ACCEPTANCE_GREEN_STATUSES


def _scoped_evidence_item(item: Mapping[str, Any], *, body_chars: int) -> dict[str, Any]:
    scoped: dict[str, Any] = {}
    for key, value in item.items():
        key_text = str(key)
        if key_text in _HOT_REF_CONTENT_KEYS or key_text in _COLD_INTEGRITY_KEYS:
            continue
        if key_text == "provenance" and isinstance(value, Mapping):
            scoped[key_text] = _sanitize_mapping(value)
            continue
        scoped[key_text] = _sanitize_value(value)
    snippet = _snippet_value(item, body_chars=body_chars)
    if snippet:
        scoped["snippet"] = snippet
        scoped["body"] = snippet
    return scoped


def _snippet_value(item: Mapping[str, Any], *, body_chars: int) -> str:
    if body_chars <= 0:
        return ""
    for key in ("body", "content", "markdown", "text", "preview", "result", "raw"):
        value = item.get(key)
        if value in (None, "", [], {}):
            continue
        text = value if isinstance(value, str) else json.dumps(_fingerprint_value(value), ensure_ascii=False, sort_keys=True, default=str)
        if len(text) <= body_chars:
            return text
        return text[:body_chars] + "...[truncated]"
    return ""


def _stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(_fingerprint_value(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprint_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _fingerprint_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return {"bytes_sha256": hashlib.sha256(bytes(value)).hexdigest(), "length": len(value)}
    return value


def _ensure_acceptance_item(index_items: dict[str, dict[str, Any]], criterion: str, *, source: str) -> dict[str, Any]:
    normalized = _normalize_text(criterion)
    for item in index_items.values():
        if _normalize_text(item.get("criterion")) == normalized:
            if source == "user":
                item["requirement_level"] = "required"
            return item
    item_id = f"{source}:{len(index_items) + 1}:{_slug(criterion)}"
    item = {
        "id": item_id,
        "criterion": criterion,
        "status": "unknown",
        "status_reason": "Projected from TaskBoard state.",
        "source": source,
        "requirement_level": "required" if source == "user" else "advisory",
        "linked_card_ids": [],
        "linked_evidence_ids": [],
        "linked_locator_ids": [],
    }
    index_items[item_id] = item
    return item


def _card_acceptance_criteria(card: Mapping[str, Any]) -> list[str]:
    raw_metadata = card.get("metadata")
    raw_evidence_contract = card.get("evidence_contract")
    metadata: Mapping[str, Any] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    evidence_contract: Mapping[str, Any] = (
        raw_evidence_contract if isinstance(raw_evidence_contract, Mapping) else {}
    )
    criteria: list[str] = []
    for source in (metadata, evidence_contract):
        for key in ("acceptance_criteria", "criteria", "acceptance_points", "done_when"):
            criteria.extend(_clean_str_sequence(source.get(key)))
    return _sorted_unique_strings(criteria)


def _acceptance_locator_items(
    *,
    evidence_view: Mapping[str, Any] | None,
    evidence_ledger: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for source in (evidence_view, evidence_ledger):
        if not isinstance(source, Mapping):
            continue
        for item in _sequence_of_mappings(source.get("evidence_items") or source.get("items")):
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
            if "acceptance_locator" in kind or "artifact_locator" in kind:
                items.append(item)
    return items


def _locator_refs_from_evidence_view(evidence_view: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    refs: list[Mapping[str, Any]] = []
    for item in _sequence_of_mappings(evidence_view.get("evidence_items")):
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        if "acceptance_locator" in kind or "artifact_locator" in kind:
            refs.append(item)
    return refs


def _schedule_view(schedule: Any) -> Mapping[str, Any]:
    if schedule is None:
        return {}
    if isinstance(schedule, Mapping):
        return schedule
    to_dict = getattr(schedule, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return value if isinstance(value, Mapping) else {}
    return {}


def _ids_from_schedule(schedule: Mapping[str, Any], key: str) -> list[str]:
    return _sorted_unique_strings(schedule.get(key))


def _acceptance_index_summary(acceptance_index: Mapping[str, Any], *, max_items: int) -> dict[str, Any]:
    items = [dict(item) for item in _sequence_of_mappings(acceptance_index.get("items"))]
    changed = [
        item
        for item in items
        if str(item.get("status") or "") in {"active", "setback", "blocked", "deferred", "satisfied"}
    ]
    metadata = dict(acceptance_index.get("metadata") or {})
    return {
        "schema_version": acceptance_index.get("schema_version"),
        "total_items": len(items),
        "status_counts": dict(acceptance_index.get("status_counts") or {}),
        "dirty_count": metadata.get("dirty_count"),
        "green_count": metadata.get("green_count"),
        "acceptance_progress_percent": metadata.get("acceptance_progress_percent"),
        "verifier_cache_hit_count": metadata.get("verifier_cache_hit_count"),
        "verifier_cache_miss_count": metadata.get("verifier_cache_miss_count"),
        "items": [_sanitize_mapping(item) for item in changed[:max_items]],
    }


def _deferred_card_ids(revision: TaskBoardRevision) -> list[str]:
    card_ids = []
    for card_id, result in revision.card_results.items():
        if str(result.status) == "skipped" or str(result.status) == "blocked" and result.metadata.get("deferred"):
            card_ids.append(card_id)
    return _sorted_unique_strings(card_ids)


def _verifier_gaps(final_result: Mapping[str, Any] | None, *, max_items: int) -> list[str]:
    if not isinstance(final_result, Mapping):
        return []
    gaps: list[str] = []
    for key in ("missing_criteria", "acceptance_delta", "repair_constraints", "next_step_requirements"):
        gaps.extend(_clean_str_sequence(final_result.get(key)))
    return _sorted_unique_strings(gaps)[:max_items]


def _bounded_refs(value: Any, *, max_items: int) -> list[dict[str, Any]]:
    return [_sanitize_ref(item) for item in _sequence_of_mappings(value)[:max_items]]


def _sanitize_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _sanitize_value(value) for key, value in ref.items() if str(key) not in _HOT_REF_CONTENT_KEYS}


def _sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _sanitize_value(item) for key, item in value.items() if str(key) not in _HOT_REF_CONTENT_KEYS}


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_sanitize_value(item) for item in value[:16] if not isinstance(item, bytes | bytearray)]
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "...[truncated]"
    return value


def _ref_ids(refs: Sequence[Mapping[str, Any]] | None) -> list[str]:
    return _sorted_unique_strings(
        str(ref.get("id") or ref.get("checkpoint_id") or ref.get("record_id") or "")
        for ref in refs or ()
        if isinstance(ref, Mapping)
    )


def _card_diagnostics(revision: TaskBoardRevision) -> list[Mapping[str, Any]]:
    diagnostics: list[Mapping[str, Any]] = []
    for result in revision.card_results.values():
        diagnostics.extend(item for item in result.diagnostics if isinstance(item, Mapping))
    return diagnostics


def _is_explicit_state_fact(value: Mapping[str, Any]) -> bool:
    kind = str(value.get("kind") or value.get("type") or "").strip().lower()
    if kind != "explicit_state_fact":
        return False
    scope = str(value.get("scope") or "task").strip().lower()
    return scope in {"task", "task_scoped", "execution", "task_workspace"}


def _dedupe_mappings(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        sanitized = _sanitize_mapping(item)
        key = repr(sorted(sanitized.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sanitized)
    return deduped


def _capability_ids(values: Sequence[Mapping[str, Any] | str] | None) -> set[str]:
    result: set[str] = set()
    for value in values or ():
        if isinstance(value, Mapping):
            for key in ("id", "name", "kind", "capability_id"):
                text = _clean_str(value.get(key))
                if text:
                    result.add(text)
        else:
            text = _clean_str(value)
            if text:
                result.add(text)
    return result


def _sequence_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _clean_str_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = _clean_str(value)
        return [text] if text else []
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [text for item in value if (text := _clean_str(item))]
    if isinstance(value, Mapping):
        text = _clean_str(value.get("criterion") or value.get("claim") or value.get("value"))
        return [text] if text else []
    text = _clean_str(value)
    return [text] if text else []


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sorted_unique_strings(value: Any) -> list[str]:
    if isinstance(value, Iterable) and not isinstance(value, str | bytes | bytearray | Mapping):
        return sorted({text for item in value if (text := _clean_str(item))})
    return sorted({text for text in _clean_str_sequence(value)})


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _normalize_acceptance_status(value: Any) -> str:
    status = str(value or "unknown").strip().lower().replace("-", "_")
    return status if status in _ACCEPTANCE_STATUSES else "unknown"


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _slug(value: Any) -> str:
    normalized = _normalize_text(value)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug[:48] or "item"
