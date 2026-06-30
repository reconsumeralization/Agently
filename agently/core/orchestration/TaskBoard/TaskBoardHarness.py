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

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from agently.types.data import TaskBoardRevision


TASK_BOARD_ACCEPTANCE_INDEX_SCHEMA_VERSION = "task_board_acceptance_index/v1"
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
_ACCEPTANCE_STATUSES = {"unknown", "active", "satisfied", "blocked", "deferred", "not_applicable"}
_TRUTHY = {"true", "yes", "pass", "passed", "satisfied", "complete", "completed", "ok", "accepted"}


def build_task_board_acceptance_index(
    revision: TaskBoardRevision | Mapping[str, Any],
    *,
    success_criteria: Sequence[str] | None = None,
    verification: Mapping[str, Any] | None = None,
    evidence_view: Mapping[str, Any] | None = None,
    evidence_ledger: Mapping[str, Any] | None = None,
    explicit_state_facts: Sequence[Mapping[str, Any]] | None = None,
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
    status_counts = {status: 0 for status in sorted(_ACCEPTANCE_STATUSES)}
    for item in items:
        status = _normalize_acceptance_status(item.get("status"))
        item["status"] = status
        status_counts[status] = status_counts.get(status, 0) + 1
        item["linked_card_ids"] = _sorted_unique_strings(item.get("linked_card_ids"))
        item["linked_evidence_ids"] = _sorted_unique_strings(item.get("linked_evidence_ids"))
        item["linked_locator_ids"] = _sorted_unique_strings(item.get("linked_locator_ids"))

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
            "full_state_sources": ["TaskBoardRevision", "EvidenceEnvelope", "Workspace checkpoint records"],
            "bounded": True,
        },
    }


def task_board_preflight_diagnostics(
    revision: TaskBoardRevision | Mapping[str, Any],
    *,
    mounted_capabilities: Sequence[Mapping[str, Any] | str] | None = None,
    workspace_refs: Sequence[Mapping[str, Any] | str] | None = None,
) -> list[dict[str, Any]]:
    effective_revision = TaskBoardRevision.from_value(revision)
    capability_ids = _capability_ids(mounted_capabilities)
    workspace_ref_ids = _capability_ids(workspace_refs)
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
            for ref_id in _clean_str_sequence(metadata.get("requires_workspace_refs"))
            if ref_id not in workspace_ref_ids
        ]
        if missing_refs:
            diagnostics.append(
                {
                    "code": "taskboard.preflight.missing_workspace_ref",
                    "card_id": card.id,
                    "missing_workspace_refs": missing_refs,
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
        if str(item.get("status") or "unknown") in {"unknown", "active", "blocked", "deferred"}
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
        satisfied = _boolish(check.get("satisfied", check.get("is_satisfied", check.get("passed"))))
        item["status"] = "satisfied" if satisfied else "blocked"
        item["source"] = "verifier"
        item["status_reason"] = _clean_str(check.get("reason")) or (
            "Verifier marked the criterion satisfied." if satisfied else "Verifier marked the criterion missing."
        )
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
        if bool(fact.get("blocking")) or status in {"dirty", "unresolved", "failed", "blocked"}:
            item["status"] = "blocked"
            item["source"] = "host_policy"
            item["status_reason"] = criterion
        elif status in {"clean", "ok", "satisfied"}:
            item["status"] = "satisfied"
            item["source"] = "host_policy"
            item["status_reason"] = criterion


def _ensure_acceptance_item(index_items: dict[str, dict[str, Any]], criterion: str, *, source: str) -> dict[str, Any]:
    normalized = _normalize_text(criterion)
    for item in index_items.values():
        if _normalize_text(item.get("criterion")) == normalized:
            return item
    item_id = f"{source}:{len(index_items) + 1}:{_slug(criterion)}"
    item = {
        "id": item_id,
        "criterion": criterion,
        "status": "unknown",
        "status_reason": "Projected from TaskBoard state.",
        "source": source,
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
    changed = [item for item in items if str(item.get("status") or "") in {"active", "blocked", "deferred", "satisfied"}]
    return {
        "schema_version": acceptance_index.get("schema_version"),
        "total_items": len(items),
        "status_counts": dict(acceptance_index.get("status_counts") or {}),
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
    return scope in {"task", "task_scoped", "execution", "workspace"}


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


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUTHY
