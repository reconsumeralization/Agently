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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import (
    ContextBudget,
    ContextConsumer,
    ContextPackage,
    ContextRole,
    ContextSourceBindingSnapshot,
    TaskContextEntrySnapshot,
    TaskContextSnapshot,
)
from agently.types.plugins import ContextSource, EmbeddingProvider

from ._Index import _ContextIndex, _ContextIndexProfile, _ContextIndexQueryResult

if TYPE_CHECKING:
    from .ContextReader import ContextReader


def _require_text(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty.")
    return normalized


@dataclass(frozen=True)
class _SourceBinding:
    source: ContextSource
    binding_id: str
    required: bool
    priority: int
    scope: str
    metadata: Mapping[str, Any]


class TaskContext:
    """Revisioned task-scoped aggregate of source bindings and direct entries."""

    def __init__(self, task_id: str, context_id: str | None = None):
        self.task_id = _require_text(task_id, "task_id")
        if context_id is None:
            context_id = f"task_context:{uuid.uuid4().hex}"
        self.context_id = _require_text(context_id, "context_id")
        self._revision = 0
        self._bindings: dict[str, _SourceBinding] = {}
        self._entries: dict[str, TaskContextEntrySnapshot] = {}
        self._index = _ContextIndex()
        self.__reader_owner_token = object()

    @property
    def revision(self) -> int:
        return self._revision

    @staticmethod
    def _source_id(source: ContextSource) -> str:
        return _require_text(getattr(source, "source_id", None), "source_id")

    @staticmethod
    def _source_revision(source: ContextSource) -> str:
        return _require_text(getattr(source, "source_revision", None), "source_revision")

    @staticmethod
    def _source_kind(source: ContextSource) -> str:
        return _require_text(getattr(source, "source_kind", None), "source_kind")

    def attach(
        self,
        source: ContextSource,
        *,
        binding_id: str | None = None,
        required: bool = False,
        priority: int = 0,
        scope: str = "task",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        self._source_id(source)
        self._source_kind(source)
        self._source_revision(source)
        if not callable(getattr(source, "async_enumerate_descriptors", None)):
            raise TypeError("ContextSource must provide async_enumerate_descriptors(...).")
        if not callable(getattr(source, "async_read_exact", None)):
            raise TypeError("ContextSource must provide async_read_exact(...).")
        resolved_id = _require_text(
            binding_id or f"context_binding:{uuid.uuid4().hex}",
            "binding_id",
        )
        if resolved_id in self._bindings or resolved_id in self._entries:
            raise ValueError(f"binding_id already exists: {resolved_id!r}.")
        snapshot = ContextSourceBindingSnapshot(
            binding_id=resolved_id,
            source_id=self._source_id(source),
            source_kind=self._source_kind(source),
            source_revision=self._source_revision(source),
            required=bool(required),
            priority=int(priority),
            scope=_require_text(scope, "scope"),
            metadata=metadata or {},
        )
        self._bindings[resolved_id] = _SourceBinding(
            source=source,
            binding_id=snapshot.binding_id,
            required=snapshot.required,
            priority=snapshot.priority,
            scope=snapshot.scope,
            metadata=snapshot.metadata,
        )
        self._revision += 1
        return resolved_id

    def put(
        self,
        *,
        role: ContextRole,
        content: Any,
        entry_id: str | None = None,
        required: bool = False,
        source_ref: str | None = None,
        priority: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        resolved_id = _require_text(
            entry_id or f"context_entry:{uuid.uuid4().hex}",
            "entry_id",
        )
        if resolved_id in self._entries or resolved_id in self._bindings:
            raise ValueError(f"entry_id already exists: {resolved_id!r}.")
        entry = TaskContextEntrySnapshot(
            entry_id=resolved_id,
            role=role,
            content=content,
            required=bool(required),
            source_ref=source_ref,
            priority=int(priority),
            metadata=metadata or {},
        )
        self._entries[resolved_id] = entry
        self._revision += 1
        return resolved_id

    def remove(self, entry_or_binding_id: str) -> bool:
        resolved_id = str(entry_or_binding_id or "").strip()
        removed = self._entries.pop(resolved_id, None)
        if removed is None:
            removed = self._bindings.pop(resolved_id, None)
        if removed is None:
            return False
        self._revision += 1
        return True

    def snapshot(self) -> TaskContextSnapshot:
        bindings = tuple(
            ContextSourceBindingSnapshot(
                binding_id=binding.binding_id,
                source_id=self._source_id(binding.source),
                source_kind=self._source_kind(binding.source),
                source_revision=self._source_revision(binding.source),
                required=binding.required,
                priority=binding.priority,
                scope=binding.scope,
                metadata=binding.metadata,
            )
            for binding in self._bindings.values()
        )
        return TaskContextSnapshot(
            context_id=self.context_id,
            task_id=self.task_id,
            revision=self._revision,
            bindings=bindings,
            entries=tuple(self._entries.values()),
        )

    def is_snapshot_current(self, snapshot: TaskContextSnapshot) -> bool:
        if snapshot.context_id != self.context_id or snapshot.task_id != self.task_id:
            return False
        if snapshot.revision != self._revision:
            return False
        current = self.snapshot()
        snapshot_sources = {
            binding.binding_id: (
                binding.source_id,
                binding.source_kind,
                binding.source_revision,
            )
            for binding in snapshot.bindings
        }
        current_sources = {
            binding.binding_id: (
                binding.source_id,
                binding.source_kind,
                binding.source_revision,
            )
            for binding in current.bindings
        }
        return snapshot_sources == current_sources

    def source_catalog(self) -> dict[str, dict[str, Any]]:
        """Project the source kinds actually bound to this TaskContext."""

        catalog: dict[str, dict[str, Any]] = {}
        for binding in self._bindings.values():
            source_kind = self._source_kind(binding.source)
            entry = catalog.setdefault(
                source_kind,
                {
                    "binding_ids": [],
                    "required": False,
                    "description": source_kind,
                },
            )
            entry["binding_ids"].append(binding.binding_id)
            entry["required"] = bool(entry["required"] or binding.required)
            description = str(binding.metadata.get("description") or "").strip()
            if description:
                entry["description"] = description
        return {
            source_kind: {
                **entry,
                "binding_ids": tuple(entry["binding_ids"]),
            }
            for source_kind, entry in catalog.items()
        }

    def configure_index(
        self,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        strategy: str = "structural",
    ) -> None:
        """Configure the TaskContext-owned derived candidate index.

        The index only narrows and orders source-owned descriptors.  A
        consumer-bound ContextReader remains responsible for semantic
        selection, exact readback, and ContextPackage construction.
        """

        normalized_strategy = str(strategy or "structural").strip().lower()
        if normalized_strategy not in {"structural", "lexical", "hybrid"}:
            raise ValueError(
                "Context index strategy must be structural, lexical, or hybrid."
            )
        self._index = _ContextIndex(
            profile=_ContextIndexProfile(
                candidate_strategy=normalized_strategy,
                embedding_identity=_ContextIndex.embedding_identity(
                    embedding_provider
                ),
            ),
            embedding_provider=embedding_provider,
        )

    def _index_candidate_limit(self, max_blocks: int) -> int:
        """Return the reader-facing candidate window for this index profile."""

        if self._index.profile.candidate_strategy == "hybrid":
            # Hybrid ranking has already provided a mechanism-level shortlist;
            # do not multiply it before the model-owned semantic selection.
            return max_blocks
        return max_blocks * 4

    def reader(
        self,
        *,
        consumer: ContextConsumer | str,
        phase: str = "execution",
        budget: ContextBudget | Mapping[str, Any] | None = None,
        semantic_selector: Any = None,
    ) -> "ContextReader":
        from .ContextReader import ContextReader

        resolved_consumer = (
            consumer if isinstance(consumer, ContextConsumer) else ContextConsumer(str(consumer))
        )
        if budget is None:
            resolved_budget = ContextBudget()
        elif isinstance(budget, ContextBudget):
            resolved_budget = budget
        else:
            resolved_budget = ContextBudget(**dict(budget))
        return ContextReader(
            self,
            consumer=resolved_consumer,
            phase=_require_text(phase, "phase"),
            budget=resolved_budget,
            semantic_selector=semantic_selector,
            _owner_token=self.__reader_owner_token,
        )

    def restore_reader(
        self,
        state: Mapping[str, Any],
        *,
        packages: Sequence[ContextPackage] = (),
        semantic_selector: Any = None,
    ) -> "ContextReader":
        if not isinstance(state, Mapping):
            raise ValueError("ContextReader state must be a mapping.")
        if str(state.get("task_context_id") or "") != self.context_id:
            raise ValueError("ContextReader state belongs to a different TaskContext.")
        raw_consumer = state.get("consumer")
        if not isinstance(raw_consumer, Mapping):
            raise ValueError("ContextReader state requires a consumer mapping.")
        raw_capabilities = raw_consumer.get("capabilities")
        if raw_capabilities is None:
            raw_capabilities = {}
        if not isinstance(raw_capabilities, Mapping):
            raise ValueError("ContextReader consumer capabilities must be a mapping.")
        raw_model = raw_consumer.get("model")
        consumer = ContextConsumer(
            consumer_id=_require_text(raw_consumer.get("consumer_id"), "consumer_id"),
            model=(
                _require_text(raw_model, "model")
                if raw_model is not None
                else None
            ),
            capabilities=dict(raw_capabilities),
        )
        raw_budget = state.get("budget")
        if not isinstance(raw_budget, Mapping):
            raise ValueError("ContextReader state requires a budget mapping.")
        try:
            budget = ContextBudget(
                max_chars=int(raw_budget["max_chars"]),
                max_blocks=int(raw_budget["max_blocks"]),
                max_block_chars=int(raw_budget["max_block_chars"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("ContextReader state has an invalid budget mapping.") from error
        reader = self.reader(
            consumer=consumer,
            phase=_require_text(state.get("phase"), "phase"),
            budget=budget,
            semantic_selector=semantic_selector,
        )
        reader._restore_state(
            state,
            packages=packages,
            _owner_token=self.__reader_owner_token,
        )
        return reader

    def _owns_reader_token(self, token: object | None) -> bool:
        return token is self.__reader_owner_token

    def _iter_source_bindings(self) -> Iterator[_SourceBinding]:
        return iter(tuple(self._bindings.values()))

    def _entry_snapshots(self) -> tuple[TaskContextEntrySnapshot, ...]:
        return tuple(self._entries.values())

    def _binding_source(self, binding_id: str) -> ContextSource:
        try:
            return self._bindings[binding_id].source
        except KeyError as error:
            raise KeyError(f"Unknown Context source binding: {binding_id!r}.") from error

    def _binding_snapshot(self, binding_id: str) -> ContextSourceBindingSnapshot:
        binding = self._bindings.get(binding_id)
        if binding is None:
            raise KeyError(f"Unknown Context source binding: {binding_id!r}.")
        return ContextSourceBindingSnapshot(
            binding_id=binding.binding_id,
            source_id=self._source_id(binding.source),
            source_kind=self._source_kind(binding.source),
            source_revision=self._source_revision(binding.source),
            required=binding.required,
            priority=binding.priority,
            scope=binding.scope,
            metadata=cast(Mapping[str, Any], binding.metadata),
        )

    async def _query_index(
        self,
        snapshot: TaskContextSnapshot,
        intent: Any,
        *,
        offsets: Mapping[str, int],
        limit: int,
    ) -> _ContextIndexQueryResult:
        if snapshot.context_id != self.context_id or snapshot.task_id != self.task_id:
            raise ValueError("Context index query snapshot belongs to another TaskContext.")
        bindings = tuple(
            (
                binding,
                self._binding_source(binding.binding_id),
            )
            for binding in snapshot.bindings
        )
        return await self._index.async_query(
            bindings=bindings,
            intent=intent,
            offsets=offsets,
            limit=limit,
        )


__all__ = ["TaskContext"]
