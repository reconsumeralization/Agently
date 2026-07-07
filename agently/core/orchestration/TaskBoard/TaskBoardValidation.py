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
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agently.types.data import (
    TaskBoardCard,
    TaskBoardCardResult,
    TaskBoardGraph,
    TaskBoardPatch,
    TaskBoardRevision,
    TaskBoardSchedulePlan,
)


_TASK_BOARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_TERMINAL_CARD_STATUSES = {"completed", "failed", "blocked", "skipped"}
_SATISFYING_DEGRADED_STATUSES = {"failed", "blocked", "skipped", "setback"}
_SETBACK_CARD_STATUSES = {"setback"}


@dataclass(frozen=True)
class TaskBoardValidation:
    revision: TaskBoardRevision
    card_ids: tuple[str, ...]
    root_card_ids: tuple[str, ...]
    topological_card_ids: tuple[str, ...]
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


class TaskBoardValidator:
    def validate(
        self,
        revision: TaskBoardRevision | Mapping[str, Any],
    ) -> TaskBoardValidation:
        return validate_task_board_revision(revision)

    def schedule(
        self,
        revision: TaskBoardRevision | Mapping[str, Any],
    ) -> TaskBoardSchedulePlan:
        validation = self.validate(revision)
        return schedule_task_board_revision(validation.revision)

    def apply_patch(
        self,
        revision: TaskBoardRevision | Mapping[str, Any],
        patch: TaskBoardPatch | Mapping[str, Any],
    ) -> TaskBoardRevision:
        return apply_task_board_patch(revision, patch)


def validate_task_board_revision(
    revision: TaskBoardRevision | Mapping[str, Any],
) -> TaskBoardValidation:
    normalized = TaskBoardRevision.from_value(revision)
    graph = normalized.graph
    if not graph.cards:
        raise ValueError("TaskBoardGraph must contain at least one card.")

    card_by_id: dict[str, TaskBoardCard] = {}
    duplicates: list[str] = []
    for card in graph.cards:
        if not card.id:
            raise ValueError("TaskBoardCard id must be non-empty.")
        if not _TASK_BOARD_ID_PATTERN.fullmatch(card.id):
            raise ValueError(
                f"TaskBoardCard id '{ card.id }' is invalid. Use letters, digits, underscore, dot, or dash."
            )
        if card.id in card_by_id:
            duplicates.append(card.id)
        card_by_id[card.id] = card
    if duplicates:
        raise ValueError(f"Duplicate TaskBoardCard id(s): { ', '.join(sorted(set(duplicates))) }.")

    for card in graph.cards:
        for dependency in card.depends_on:
            if dependency not in card_by_id:
                raise ValueError(
                    f"TaskBoardCard '{ card.id }' depends on missing card '{ dependency }'."
                )

    roots, ordered = _topological_order(graph.cards)
    for card_id, result in normalized.card_results.items():
        if card_id not in card_by_id:
            raise ValueError(f"TaskBoardRevision contains result for unknown card '{ card_id }'.")
        if result.card_id != card_id:
            raise ValueError(
                f"TaskBoardRevision result key '{ card_id }' does not match result.card_id '{ result.card_id }'."
            )

    return TaskBoardValidation(
        revision=normalized,
        card_ids=tuple(card.id for card in graph.cards),
        root_card_ids=roots,
        topological_card_ids=ordered,
        diagnostics=tuple(normalized.diagnostics),
    )


def schedule_task_board_revision(
    revision: TaskBoardRevision | Mapping[str, Any],
) -> TaskBoardSchedulePlan:
    validation = validate_task_board_revision(revision)
    normalized = validation.revision
    card_by_id = normalized.graph.card_by_id()
    completed = {
        card_id
        for card_id, result in normalized.card_results.items()
        if str(result.status) == "completed"
    }
    terminal = {
        card_id
        for card_id, result in normalized.card_results.items()
        if str(result.status) in _TERMINAL_CARD_STATUSES
    }
    runnable: list[str] = []
    blocked: list[str] = []
    diagnostics: list[Mapping[str, Any]] = []
    for card_id in validation.topological_card_ids:
        card = card_by_id[card_id]
        if card_id in terminal or str(card.status) in _TERMINAL_CARD_STATUSES:
            continue
        missing: list[str] = []
        for dependency in card.depends_on:
            dependency_card = card_by_id[dependency]
            dependency_result = normalized.card_results.get(dependency)
            if task_board_dependency_satisfied(dependency_card, dependency_result):
                status = str(dependency_result.status) if dependency_result is not None else str(dependency_card.status)
                if status in _SATISFYING_DEGRADED_STATUSES:
                    diagnostics.append(
                        {
                            "code": "taskboard.degraded_dependency_satisfied",
                            "card_id": card_id,
                            "dependency": dependency,
                            "dependency_status": status,
                            "failure_policy": task_board_card_failure_policy(dependency_card),
                            "message": (
                                f"TaskBoardCard '{card_id}' can run because dependency "
                                f"'{dependency}' is non-required and ended with status '{status}'."
                            ),
                        }
                    )
                continue
            missing.append(dependency)
        if missing:
            blocked.append(card_id)
            continue
        runnable.append(card_id)
    return TaskBoardSchedulePlan(
        revision_id=normalized.revision_id,
        runnable_card_ids=tuple(runnable),
        blocked_card_ids=tuple(blocked),
        completed_card_ids=tuple(sorted(completed)),
        diagnostics=tuple(diagnostics),
    )


def apply_task_board_patch(
    revision: TaskBoardRevision | Mapping[str, Any],
    patch: TaskBoardPatch | Mapping[str, Any],
) -> TaskBoardRevision:
    normalized = validate_task_board_revision(revision).revision
    normalized_patch = TaskBoardPatch.from_value(patch)
    if normalized_patch.base_revision != normalized.revision_id:
        raise ValueError(
            "TaskBoardPatch base_revision mismatch: "
            f"expected '{ normalized.revision_id }', got '{ normalized_patch.base_revision }'."
        )

    cards = list(normalized.graph.cards)
    card_results = dict(normalized.card_results)
    evidence_refs = [dict(item) for item in normalized.evidence_refs]
    diagnostics = [dict(item) for item in normalized.diagnostics]
    metadata = dict(normalized.metadata)
    status = str(normalized.status)
    explicit_board_status = False

    for operation in normalized_patch.operations:
        op = str(operation.get("op") or "").strip()
        if op == "add_card":
            card = TaskBoardCard.from_value(operation.get("card") or {})
            if any(existing.id == card.id for existing in cards):
                raise ValueError(f"TaskBoardPatch add_card duplicates card '{ card.id }'.")
            cards.append(card)
        elif op == "update_card":
            card = TaskBoardCard.from_value(operation.get("card") or {})
            index = _card_index(cards, card.id)
            cards[index] = card
        elif op == "set_card_status":
            card_id = _operation_card_id(operation)
            index = _card_index(cards, card_id)
            cards[index] = cards[index].with_status(str(operation.get("status") or "pending"))
        elif op == "add_dependency":
            card_id = _operation_card_id(operation)
            dependency = _operation_dependency(operation)
            index = _card_index(cards, card_id)
            cards[index] = cards[index].with_dependencies((*cards[index].depends_on, dependency))
        elif op == "remove_dependency":
            card_id = _operation_card_id(operation)
            dependency = _operation_dependency(operation)
            index = _card_index(cards, card_id)
            cards[index] = cards[index].with_dependencies(
                tuple(item for item in cards[index].depends_on if item != dependency)
            )
        elif op == "record_card_result":
            result = TaskBoardCardResult.from_value(operation.get("result") or {})
            _card_index(cards, result.card_id)
            card_results[result.card_id] = result
            index = _card_index(cards, result.card_id)
            cards[index] = cards[index].with_status(result.status)
        elif op == "append_evidence_ref":
            ref = operation.get("ref")
            evidence_refs.append(dict(ref) if isinstance(ref, Mapping) else {"value": ref})
        elif op == "append_diagnostic":
            diagnostic = operation.get("diagnostic")
            diagnostics.append(dict(diagnostic) if isinstance(diagnostic, Mapping) else {"message": str(diagnostic)})
        elif op == "set_board_status":
            status = str(operation.get("status") or status)
            explicit_board_status = True
        elif op == "update_metadata":
            value = operation.get("metadata") or {}
            if not isinstance(value, Mapping):
                raise ValueError("TaskBoardPatch update_metadata requires mapping metadata.")
            metadata.update(dict(value))
        else:
            raise ValueError(f"Unsupported TaskBoardPatch operation '{ op }'.")

    evidence_refs.extend(dict(item) for item in normalized_patch.evidence_refs)
    diagnostics.extend(dict(item) for item in normalized_patch.diagnostics)
    metadata.setdefault("applied_patches", [])
    if isinstance(metadata["applied_patches"], list):
        metadata["applied_patches"].append(normalized_patch.patch_id)
    if not explicit_board_status:
        status = _derive_board_status(cards, card_results, current_status=status)

    next_revision = normalized.next_revision(
        normalized.graph.with_cards(cards),
        status=status,
        card_results=card_results,
        evidence_refs=evidence_refs,
        diagnostics=diagnostics,
        metadata=metadata,
    )
    validate_task_board_revision(next_revision)
    return next_revision


def _derive_board_status(
    cards: Sequence[TaskBoardCard],
    card_results: Mapping[str, TaskBoardCardResult],
    *,
    current_status: str,
) -> str:
    statuses: dict[str, str] = {}
    for card in cards:
        result = card_results.get(card.id)
        statuses[card.id] = str(result.status if result is not None else card.status).strip().lower()

    required_statuses = [
        status
        for card in cards
        for status in (statuses.get(card.id, "pending"),)
        if task_board_card_required(card)
    ]
    if any(status in {"failed", "error"} for status in required_statuses):
        return "failed"
    if any(status in _SETBACK_CARD_STATUSES for status in statuses.values()):
        return "running"
    if any(status == "blocked" for status in required_statuses):
        return "blocked"

    terminal_statuses = {"completed", "failed", "blocked", "skipped"}
    if all(status in terminal_statuses for status in statuses.values()):
        if not required_statuses or all(status == "completed" for status in required_statuses):
            return "completed"
    return current_status


def _topological_order(cards: Sequence[TaskBoardCard]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    adjacency: dict[str, list[str]] = {card.id: [] for card in cards}
    indegree: dict[str, int] = {card.id: 0 for card in cards}
    for card in cards:
        for dependency in card.depends_on:
            adjacency[dependency].append(card.id)
            indegree[card.id] += 1
    roots = tuple(card.id for card in cards if not card.depends_on)
    if not roots:
        raise ValueError("TaskBoardGraph must contain at least one root card.")
    queue = deque(roots)
    ordered: list[str] = []
    while queue:
        card_id = queue.popleft()
        ordered.append(card_id)
        for child_id in adjacency[card_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                queue.append(child_id)
    if len(ordered) != len(cards):
        cycle_ids = sorted(card_id for card_id, count in indegree.items() if count > 0)
        raise ValueError(f"TaskBoardGraph contains a dependency cycle: { ', '.join(cycle_ids) }.")
    return roots, tuple(ordered)


def _card_index(cards: Sequence[TaskBoardCard], card_id: str) -> int:
    for index, card in enumerate(cards):
        if card.id == card_id:
            return index
    raise ValueError(f"Unknown TaskBoardCard '{ card_id }'.")


def _operation_card_id(operation: Mapping[str, Any]) -> str:
    card_id = str(operation.get("card_id") or "").strip()
    if not card_id:
        raise ValueError(f"TaskBoardPatch operation '{ operation.get('op') }' requires card_id.")
    return card_id


def _operation_dependency(operation: Mapping[str, Any]) -> str:
    dependency = str(operation.get("dependency") or "").strip()
    if not dependency:
        raise ValueError(f"TaskBoardPatch operation '{ operation.get('op') }' requires dependency.")
    return dependency


def task_board_card_failure_policy(card: TaskBoardCard) -> str:
    value = getattr(card, "failure_policy", None)
    if value is None and isinstance(getattr(card, "evidence_contract", None), Mapping):
        value = card.evidence_contract.get("failure_policy")
    if value is None and isinstance(getattr(card, "metadata", None), Mapping):
        value = card.metadata.get("failure_policy")
    text = str(value or "required").strip().lower().replace("-", "_")
    if text in {"nice_to_have", "best_effort", "non_blocking", "nonblocking"}:
        return "optional"
    if text in {"soft", "fallback", "degrade"}:
        return "degradable"
    if text in {"optional", "degradable"}:
        return text
    return "required"


def task_board_card_required(card: TaskBoardCard) -> bool:
    return task_board_card_failure_policy(card) == "required"


def task_board_dependency_satisfied(
    dependency_card: TaskBoardCard,
    dependency_result: TaskBoardCardResult | None,
) -> bool:
    if dependency_result is None:
        return str(getattr(dependency_card, "status", "")).strip().lower() == "skipped" and not task_board_card_required(dependency_card)
    status = str(dependency_result.status).strip().lower()
    if status == "completed":
        return True
    if status in _SATISFYING_DEGRADED_STATUSES and not task_board_card_required(dependency_card):
        return True
    return False
