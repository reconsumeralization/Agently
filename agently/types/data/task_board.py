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

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeAlias


TASK_BOARD_SCHEMA_VERSION = "task_board/v1"

TaskBoardCardStatus: TypeAlias = Literal[
    "pending",
    "ready",
    "running",
    "completed",
    "blocked",
    "failed",
    "skipped",
]
TaskBoardStatus: TypeAlias = Literal["running", "completed", "blocked", "failed"]
TaskBoardCardFailurePolicy: TypeAlias = Literal["required", "optional", "degradable"]


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        item = _clean_str(value)
        return (item,) if item else ()
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = _clean_str(item)
            if text and text not in seen:
                result.append(text)
                seen.add(text)
        return tuple(result)
    item = _clean_str(value)
    return (item,) if item else ()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_tuple(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (dict(value),)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(dict(item) if isinstance(item, Mapping) else {"value": item} for item in value)
    return ({"value": value},)


def _failure_policy(value: Any) -> str:
    text = str(value or "required").strip().lower().replace("-", "_")
    aliases = {
        "must": "required",
        "mandatory": "required",
        "critical": "required",
        "nice_to_have": "optional",
        "best_effort": "optional",
        "non_blocking": "optional",
        "nonblocking": "optional",
        "soft": "degradable",
        "fallback": "degradable",
        "degrade": "degradable",
    }
    normalized = aliases.get(text, text)
    if normalized not in {"required", "optional", "degradable"}:
        return "required"
    return normalized


@dataclass(frozen=True)
class TaskBoardCard:
    id: str
    objective: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    input_refs: tuple[str, ...] = field(default_factory=tuple)
    required_outputs: tuple[str, ...] = field(default_factory=tuple)
    allowed_execution_shape: str = "auto"
    policy_scope_refs: tuple[str, ...] = field(default_factory=tuple)
    evidence_contract: Mapping[str, Any] = field(default_factory=dict)
    failure_policy: TaskBoardCardFailurePolicy | str = "required"
    status: TaskBoardCardStatus | str = "pending"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_BOARD_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "TaskBoardCard | Mapping[str, Any]") -> "TaskBoardCard":
        if isinstance(value, TaskBoardCard):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"TaskBoardCard must be a mapping or TaskBoardCard, got: { type(value) }.")
        card_id = _clean_str(value.get("id") or value.get("card_id"))
        if card_id is None:
            raise ValueError("TaskBoardCard requires non-empty 'id'.")
        objective = _clean_str(value.get("objective") or value.get("goal"))
        if objective is None:
            raise ValueError("TaskBoardCard requires non-empty 'objective'.")
        return cls(
            id=card_id,
            objective=objective,
            depends_on=_str_tuple(value.get("depends_on")),
            input_refs=_str_tuple(value.get("input_refs")),
            required_outputs=_str_tuple(value.get("required_outputs")),
            allowed_execution_shape=str(value.get("allowed_execution_shape") or "auto"),
            policy_scope_refs=_str_tuple(value.get("policy_scope_refs")),
            evidence_contract=_mapping(value.get("evidence_contract")),
            failure_policy=_failure_policy(
                value.get("failure_policy")
                or _mapping(value.get("evidence_contract")).get("failure_policy")
                or _mapping(value.get("metadata")).get("failure_policy")
            ),
            status=str(value.get("status") or "pending"),
            metadata=_mapping(value.get("metadata")),
            schema_version=str(value.get("schema_version") or TASK_BOARD_SCHEMA_VERSION),
        )

    def with_status(self, status: TaskBoardCardStatus | str) -> "TaskBoardCard":
        return replace(self, status=status)

    def with_dependencies(self, dependencies: Sequence[str]) -> "TaskBoardCard":
        return replace(self, depends_on=_str_tuple(dependencies))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "input_refs": list(self.input_refs),
            "required_outputs": list(self.required_outputs),
            "allowed_execution_shape": self.allowed_execution_shape,
            "policy_scope_refs": list(self.policy_scope_refs),
            "evidence_contract": dict(self.evidence_contract),
            "failure_policy": self.failure_policy,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskBoardGraph:
    graph_id: str
    cards: tuple[TaskBoardCard, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_BOARD_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "TaskBoardGraph | Mapping[str, Any]") -> "TaskBoardGraph":
        if isinstance(value, TaskBoardGraph):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"TaskBoardGraph must be a mapping or TaskBoardGraph, got: { type(value) }.")
        raw_cards = value.get("cards")
        if raw_cards is None:
            raise ValueError("TaskBoardGraph requires 'cards'.")
        if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, str | bytes | bytearray):
            raise TypeError(f"TaskBoardGraph 'cards' must be a sequence, got: { type(raw_cards) }.")
        graph_id = _clean_str(value.get("graph_id")) or f"task_board_graph-{ uuid.uuid4().hex[:12] }"
        return cls(
            graph_id=graph_id,
            cards=tuple(TaskBoardCard.from_value(card) for card in raw_cards),
            metadata=_mapping(value.get("metadata")),
            schema_version=str(value.get("schema_version") or TASK_BOARD_SCHEMA_VERSION),
        )

    def card_by_id(self) -> dict[str, TaskBoardCard]:
        return {card.id: card for card in self.cards}

    def with_cards(self, cards: Sequence[TaskBoardCard]) -> "TaskBoardGraph":
        return replace(self, cards=tuple(cards))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "cards": [card.to_dict() for card in self.cards],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskBoardCardResult:
    card_id: str
    status: TaskBoardCardStatus | str
    output_digest: str | None = None
    preview: Any = None
    artifact_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    file_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    patch_proposal: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_BOARD_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "TaskBoardCardResult | Mapping[str, Any]") -> "TaskBoardCardResult":
        if isinstance(value, TaskBoardCardResult):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"TaskBoardCardResult must be a mapping or TaskBoardCardResult, got: { type(value) }."
            )
        card_id = _clean_str(value.get("card_id"))
        if card_id is None:
            raise ValueError("TaskBoardCardResult requires non-empty 'card_id'.")
        status = _clean_str(value.get("status")) or "completed"
        return cls(
            card_id=card_id,
            status=status,
            output_digest=_clean_str(value.get("output_digest")),
            preview=value.get("preview"),
            artifact_refs=_mapping_tuple(value.get("artifact_refs")),
            file_refs=_mapping_tuple(value.get("file_refs")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
            patch_proposal=dict(value["patch_proposal"]) if isinstance(value.get("patch_proposal"), Mapping) else None,
            metadata=_mapping(value.get("metadata")),
            schema_version=str(value.get("schema_version") or TASK_BOARD_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "card_id": self.card_id,
            "status": self.status,
            "output_digest": self.output_digest,
            "preview": self.preview,
            "artifact_refs": [dict(item) for item in self.artifact_refs],
            "file_refs": [dict(item) for item in self.file_refs],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "patch_proposal": dict(self.patch_proposal) if self.patch_proposal is not None else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskBoardPatch:
    base_revision: str
    operations: tuple[Mapping[str, Any], ...]
    patch_id: str = field(default_factory=lambda: f"patch-{ uuid.uuid4().hex[:12] }")
    source: str = "task_board"
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    evidence_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_BOARD_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "TaskBoardPatch | Mapping[str, Any]") -> "TaskBoardPatch":
        if isinstance(value, TaskBoardPatch):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"TaskBoardPatch must be a mapping or TaskBoardPatch, got: { type(value) }.")
        base_revision = _clean_str(value.get("base_revision"))
        if base_revision is None:
            raise ValueError("TaskBoardPatch requires non-empty 'base_revision'.")
        operations = value.get("operations")
        if not isinstance(operations, Sequence) or isinstance(operations, str | bytes | bytearray):
            raise TypeError("TaskBoardPatch requires 'operations' as a sequence.")
        patch_id = _clean_str(value.get("patch_id")) or f"patch-{ uuid.uuid4().hex[:12] }"
        return cls(
            base_revision=base_revision,
            operations=tuple(dict(item) if isinstance(item, Mapping) else {"op": str(item)} for item in operations),
            patch_id=patch_id,
            source=str(value.get("source") or "task_board"),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
            evidence_refs=_mapping_tuple(value.get("evidence_refs")),
            metadata=_mapping(value.get("metadata")),
            schema_version=str(value.get("schema_version") or TASK_BOARD_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "patch_id": self.patch_id,
            "base_revision": self.base_revision,
            "source": self.source,
            "operations": [dict(item) for item in self.operations],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskBoardRevision:
    board_id: str
    revision_id: str
    graph: TaskBoardGraph
    status: TaskBoardStatus | str = "running"
    card_results: Mapping[str, TaskBoardCardResult] = field(default_factory=dict)
    evidence_refs: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_BOARD_SCHEMA_VERSION

    @classmethod
    def from_value(cls, value: "TaskBoardRevision | Mapping[str, Any]") -> "TaskBoardRevision":
        if isinstance(value, TaskBoardRevision):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(
                f"TaskBoardRevision must be a mapping or TaskBoardRevision, got: { type(value) }."
            )
        board_id = _clean_str(value.get("board_id"))
        if board_id is None:
            raise ValueError("TaskBoardRevision requires non-empty 'board_id'.")
        graph = TaskBoardGraph.from_value(value.get("graph") or {})
        raw_results = value.get("card_results") or {}
        if not isinstance(raw_results, Mapping):
            raise TypeError("TaskBoardRevision 'card_results' must be a mapping.")
        card_results = {
            str(card_id): TaskBoardCardResult.from_value(result)
            for card_id, result in raw_results.items()
            if isinstance(result, Mapping) or isinstance(result, TaskBoardCardResult)
        }
        return cls(
            board_id=board_id,
            revision_id=_clean_str(value.get("revision_id")) or f"rev-{ uuid.uuid4().hex[:12] }",
            graph=graph,
            status=str(value.get("status") or "running"),
            card_results=card_results,
            evidence_refs=_mapping_tuple(value.get("evidence_refs")),
            diagnostics=_mapping_tuple(value.get("diagnostics")),
            metadata=_mapping(value.get("metadata")),
            schema_version=str(value.get("schema_version") or TASK_BOARD_SCHEMA_VERSION),
        )

    @classmethod
    def create(
        cls,
        *,
        board_id: str,
        graph: TaskBoardGraph | Mapping[str, Any],
        revision_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TaskBoardRevision":
        return cls(
            board_id=board_id,
            revision_id=revision_id or "rev-0",
            graph=TaskBoardGraph.from_value(graph),
            metadata=dict(metadata or {}),
        )

    def next_revision(
        self,
        graph: TaskBoardGraph,
        *,
        status: TaskBoardStatus | str | None = None,
        card_results: Mapping[str, TaskBoardCardResult] | None = None,
        evidence_refs: Sequence[Mapping[str, Any]] | None = None,
        diagnostics: Sequence[Mapping[str, Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "TaskBoardRevision":
        next_id = _next_revision_id(self.revision_id)
        return TaskBoardRevision(
            board_id=self.board_id,
            revision_id=next_id,
            graph=graph,
            status=status or self.status,
            card_results=card_results if card_results is not None else dict(self.card_results),
            evidence_refs=tuple(evidence_refs) if evidence_refs is not None else tuple(self.evidence_refs),
            diagnostics=tuple(diagnostics) if diagnostics is not None else tuple(self.diagnostics),
            metadata=dict(metadata) if metadata is not None else dict(self.metadata),
            schema_version=self.schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "board_id": self.board_id,
            "revision_id": self.revision_id,
            "status": self.status,
            "graph": self.graph.to_dict(),
            "card_results": {card_id: result.to_dict() for card_id, result in self.card_results.items()},
            "evidence_refs": [dict(item) for item in self.evidence_refs],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskBoardSchedulePlan:
    revision_id: str
    runnable_card_ids: tuple[str, ...]
    blocked_card_ids: tuple[str, ...] = field(default_factory=tuple)
    completed_card_ids: tuple[str, ...] = field(default_factory=tuple)
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_BOARD_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision_id": self.revision_id,
            "runnable_card_ids": list(self.runnable_card_ids),
            "blocked_card_ids": list(self.blocked_card_ids),
            "completed_card_ids": list(self.completed_card_ids),
            "diagnostics": [dict(item) for item in self.diagnostics],
            "metadata": dict(self.metadata),
        }


def _next_revision_id(revision_id: str) -> str:
    if revision_id.startswith("rev-"):
        suffix = revision_id[4:]
        if suffix.isdigit():
            return f"rev-{ int(suffix) + 1 }"
    return f"rev-{ uuid.uuid4().hex[:12] }"
