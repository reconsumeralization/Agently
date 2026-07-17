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
from dataclasses import dataclass
from typing import Any

from agently.types.data import (
    ContextBlock,
    ContextBudget,
    ContextCandidate,
    ContextConsumer,
    ContextDiagnostic,
    ContextOmission,
    ContextPackage,
    ContextReadIntent,
    ContextSourceBindingSnapshot,
    TaskContextEntrySnapshot,
    TaskContextSnapshot,
)

from .Selection import ContextSelection, ContextSemanticSelector


class ContextStaleError(RuntimeError):
    """Raised when a reader's pinned TaskContext/source snapshot is no longer current."""


@dataclass(frozen=True)
class _CollectedCandidate:
    offered: ContextCandidate
    source_candidate: ContextCandidate | None
    direct_entry: TaskContextEntrySnapshot | None


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    return len(str(content))


class ContextReader:
    """Consumer-bound progressive-disclosure session over one TaskContext snapshot."""

    def __init__(
        self,
        task_context: Any,
        *,
        consumer: ContextConsumer,
        phase: str,
        budget: ContextBudget,
        semantic_selector: ContextSemanticSelector | None = None,
    ) -> None:
        self.task_context = task_context
        self.consumer = consumer
        self.phase = str(phase)
        self.budget = budget
        self.semantic_selector = semantic_selector
        self._snapshot: TaskContextSnapshot = task_context.snapshot()
        self._disclosed: set[tuple[str, str, str, str]] = set()
        self._packages: list[ContextPackage] = []

    @property
    def snapshot(self) -> TaskContextSnapshot:
        return self._snapshot

    @property
    def packages(self) -> tuple[ContextPackage, ...]:
        return tuple(self._packages)

    @property
    def is_current(self) -> bool:
        return self.task_context.is_snapshot_current(self._snapshot)

    def refresh(self) -> None:
        """Explicitly rebase this consumer session onto the current source snapshot.

        Disclosure identities include source revisions, so unchanged content stays
        disclosed while a changed source revision becomes eligible for a new read.
        Prior packages remain available as the audit history for this reader.
        """

        self._snapshot = self.task_context.snapshot()

    def _export_state(self) -> dict[str, Any]:
        """Serialize consumer-local progressive disclosure state for durable resume."""

        return {
            "task_context_id": self._snapshot.context_id,
            "consumer": {
                "consumer_id": self.consumer.consumer_id,
                "model": self.consumer.model,
                "capabilities": dict(self.consumer.capabilities),
            },
            "phase": self.phase,
            "budget": {
                "max_chars": self.budget.max_chars,
                "max_blocks": self.budget.max_blocks,
                "max_block_chars": self.budget.max_block_chars,
            },
            "disclosed": [list(identity) for identity in sorted(self._disclosed)],
        }

    def _restore_state(
        self,
        state: Mapping[str, Any],
        *,
        packages: Sequence[ContextPackage] = (),
    ) -> None:
        """Restore only state owned by this exact consumer/phase reader."""

        if str(state.get("task_context_id") or "") != self._snapshot.context_id:
            raise ValueError("ContextReader state belongs to a different TaskContext.")
        consumer = state.get("consumer")
        consumer_id = (
            str(consumer.get("consumer_id") or "")
            if isinstance(consumer, Mapping)
            else ""
        )
        if consumer_id != self.consumer.consumer_id or str(state.get("phase") or "") != self.phase:
            raise ValueError("ContextReader state belongs to a different consumer or phase.")
        disclosed: set[tuple[str, str, str, str]] = set()
        raw_disclosed = state.get("disclosed")
        if isinstance(raw_disclosed, Sequence) and not isinstance(
            raw_disclosed,
            str | bytes | bytearray,
        ):
            for raw_identity in raw_disclosed:
                if not isinstance(raw_identity, Sequence) or isinstance(
                    raw_identity,
                    str | bytes | bytearray,
                ):
                    raise ValueError("ContextReader disclosed identities must be sequences.")
                identity = tuple(str(item) for item in raw_identity)
                if len(identity) != 4 or any(not item for item in identity):
                    raise ValueError("ContextReader disclosed identities require four non-empty fields.")
                disclosed.add((identity[0], identity[1], identity[2], identity[3]))
        self._disclosed = disclosed
        self._packages = [
            package
            for package in packages
            if package.consumer_id == self.consumer.consumer_id
            and package.phase == self.phase
        ]

    def _assert_current(self) -> None:
        if self.is_current:
            return
        current = self.task_context.snapshot()
        if current.revision != self._snapshot.revision:
            raise ContextStaleError(
                "TaskContext revision changed after this ContextReader was created."
            )
        raise ContextStaleError(
            "A bound Context source revision changed after this ContextReader was created."
        )

    @staticmethod
    def _coerce_intent(intent: str | ContextReadIntent) -> ContextReadIntent:
        if isinstance(intent, ContextReadIntent):
            return intent
        return ContextReadIntent(query=str(intent))

    async def _collect(
        self,
        intent: ContextReadIntent,
    ) -> tuple[list[_CollectedCandidate], list[ContextDiagnostic]]:
        collected: list[_CollectedCandidate] = []
        diagnostics: list[ContextDiagnostic] = []
        sequence = 0

        for entry in self.task_context._entry_snapshots():
            if (
                bool(intent.metadata.get("exclude_already_in_prompt"))
                and bool(entry.metadata.get("already_in_prompt"))
            ):
                continue
            if intent.roles and entry.role not in intent.roles:
                continue
            sequence += 1
            source_ref = entry.source_ref or entry.entry_id
            offered = ContextCandidate(
                block_key=f"context-block:{sequence}",
                source_id=f"task-context:{self._snapshot.context_id}",
                source_revision=f"context-revision:{self._snapshot.revision}",
                source_ref=source_ref,
                binding_id=entry.entry_id,
                role=entry.role,
                summary=str(entry.metadata.get("summary") or source_ref),
                estimated_chars=_content_chars(entry.content),
                required=entry.required,
                priority=entry.priority,
                metadata=entry.metadata,
            )
            collected.append(
                _CollectedCandidate(
                    offered=offered,
                    source_candidate=None,
                    direct_entry=entry,
                )
            )

        source_limit = max(self.budget.max_blocks * 4, self.budget.max_blocks)
        for binding in self._snapshot.bindings:
            source = self.task_context._binding_source(binding.binding_id)
            try:
                candidates = await source.async_list_candidates(
                    intent,
                    limit=source_limit,
                    filters={
                        **dict(intent.filters),
                        "context_binding_scope": binding.scope,
                    },
                )
            except Exception as error:
                diagnostics.append(
                    ContextDiagnostic(
                        code="context.source_candidates_failed",
                        message="A Context source could not list candidates.",
                        details={
                            "binding_id": binding.binding_id,
                            "source_id": binding.source_id,
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                        },
                    )
                )
                continue
            for source_candidate in candidates:
                if intent.roles and source_candidate.role not in intent.roles:
                    continue
                sequence += 1
                offered = ContextCandidate(
                    block_key=f"context-block:{sequence}",
                    source_id=binding.source_id,
                    source_revision=binding.source_revision,
                    source_ref=source_candidate.source_ref,
                    binding_id=binding.binding_id,
                    role=source_candidate.role,
                    summary=source_candidate.summary,
                    estimated_chars=source_candidate.estimated_chars,
                    required=bool(source_candidate.required),
                    priority=max(binding.priority, source_candidate.priority),
                    completeness=source_candidate.completeness,
                    metadata=source_candidate.metadata,
                )
                collected.append(
                    _CollectedCandidate(
                        offered=offered,
                        source_candidate=source_candidate,
                        direct_entry=None,
                    )
                )
        return collected, diagnostics

    @staticmethod
    def _identity(candidate: ContextCandidate) -> tuple[str, str, str, str]:
        return (
            candidate.binding_id,
            candidate.source_revision,
            candidate.source_ref,
            candidate.role,
        )

    async def _select_optional(
        self,
        intent: ContextReadIntent,
        candidates: list[_CollectedCandidate],
    ) -> tuple[set[str], list[ContextDiagnostic], str | None]:
        if not candidates:
            return set(), [], None
        if len(candidates) == 1 and self.semantic_selector is None:
            return {candidates[0].offered.block_key}, [], None
        if self.semantic_selector is None:
            return (
                set(),
                [
                    ContextDiagnostic(
                        code="context.semantic_selector_unavailable",
                        message=(
                            "Optional prose relevance required semantic selection, "
                            "but no selector was available."
                        ),
                        details={"candidate_count": len(candidates)},
                    )
                ],
                "semantic_selector_unavailable",
            )
        offered = tuple(item.offered for item in candidates)
        try:
            result = await self.semantic_selector.async_select(
                intent=intent,
                candidates=offered,
                consumer=self.consumer,
                phase=self.phase,
            )
        except Exception as error:
            return (
                set(),
                [
                    ContextDiagnostic(
                        code="context.selection_failed",
                        message="Context semantic selection failed closed.",
                        details={
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                            "candidate_count": len(candidates),
                            "offered_refs": [
                                item.offered.source_ref for item in candidates[:16]
                            ],
                        },
                    )
                ],
                "selection_failed",
            )
        if not isinstance(result, ContextSelection):
            return (
                set(),
                [
                    ContextDiagnostic(
                        code="context.selection_invalid",
                        message="Context selector returned an invalid result type.",
                        details={"result_type": result.__class__.__name__},
                    )
                ],
                "selection_invalid",
            )
        keys = tuple(result.selected_keys)
        offered_keys = {item.offered.block_key for item in candidates}
        unknown = sorted(set(keys) - offered_keys)
        duplicate = len(keys) != len(set(keys))
        if unknown or duplicate:
            return (
                set(),
                [
                    ContextDiagnostic(
                        code="context.selection_invalid",
                        message="Context selector returned unknown or duplicate offered keys.",
                        details={"unknown_keys": unknown, "duplicate_keys": duplicate},
                    )
                ],
                "selection_invalid",
            )
        return set(keys), [], None

    async def _read_block(
        self,
        item: _CollectedCandidate,
        *,
        max_chars: int,
    ) -> ContextBlock:
        candidate = item.offered
        if item.direct_entry is not None:
            entry = item.direct_entry
            return ContextBlock(
                block_id=f"context_block:{uuid.uuid4().hex}",
                block_key=candidate.block_key,
                source_id=candidate.source_id,
                source_revision=candidate.source_revision,
                source_ref=candidate.source_ref,
                binding_id=candidate.binding_id,
                role=candidate.role,
                content=entry.content,
                completeness="complete",
                content_chars=_content_chars(entry.content),
                required=candidate.required,
                refs=(candidate.source_ref,),
                metadata=entry.metadata,
            )
        if item.source_candidate is None:
            raise RuntimeError("Collected Context candidate has no readable source.")
        source = self.task_context._binding_source(candidate.binding_id)
        raw = await source.async_read(
            item.source_candidate,
            max_chars=max_chars,
            representation=None,
        )
        self._assert_current()
        return ContextBlock(
            block_id=f"context_block:{uuid.uuid4().hex}",
            block_key=candidate.block_key,
            source_id=candidate.source_id,
            source_revision=candidate.source_revision,
            source_ref=candidate.source_ref,
            binding_id=candidate.binding_id,
            role=candidate.role,
            content=raw.content,
            completeness=raw.completeness,
            content_chars=raw.content_chars,
            required=candidate.required,
            refs=raw.refs or (candidate.source_ref,),
            metadata=raw.metadata,
        )

    async def async_read(self, intent: str | ContextReadIntent) -> ContextPackage:
        self._assert_current()
        resolved_intent = self._coerce_intent(intent)
        collected, diagnostics = await self._collect(resolved_intent)
        self._assert_current()

        explicit_refs = set(resolved_intent.explicit_refs)
        selected: list[_CollectedCandidate] = []
        optional: list[_CollectedCandidate] = []
        omissions: list[ContextOmission] = []

        for item in collected:
            candidate = item.offered
            is_explicit = candidate.source_ref in explicit_refs
            if not candidate.required and not is_explicit and self._identity(candidate) in self._disclosed:
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        reason="already_disclosed",
                    )
                )
                continue
            if candidate.required or is_explicit:
                selected.append(item)
            else:
                optional.append(item)

        optional_keys, selection_diagnostics, selection_failure = await self._select_optional(
            resolved_intent,
            optional,
        )
        diagnostics.extend(selection_diagnostics)
        for item in optional:
            if item.offered.block_key in optional_keys:
                selected.append(item)
            else:
                omissions.append(
                    ContextOmission(
                        block_key=item.offered.block_key,
                        source_ref=item.offered.source_ref,
                        reason=selection_failure or "not_selected",
                    )
                )

        selected.sort(
            key=lambda item: (
                not item.offered.required,
                item.offered.source_ref not in explicit_refs,
                -item.offered.priority,
                item.offered.block_key,
            )
        )
        blocks: list[ContextBlock] = []
        remaining_chars = self.budget.max_chars

        for item in selected:
            candidate = item.offered
            if len(blocks) >= self.budget.max_blocks:
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason="block_budget_exhausted",
                    )
                )
                continue
            read_limit = min(self.budget.max_block_chars, remaining_chars)
            if read_limit <= 0 or candidate.estimated_chars > read_limit:
                reason = (
                    "required_content_incompatible"
                    if candidate.required
                    else "character_budget_exhausted"
                )
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason=reason,
                        details={
                            "estimated_chars": candidate.estimated_chars,
                            "available_chars": read_limit,
                        },
                    )
                )
                if candidate.required:
                    diagnostics.append(
                        ContextDiagnostic(
                            code="context.required_content_incompatible",
                            message=(
                                "Required Context content cannot be delivered completely "
                                "within this consumer budget."
                            ),
                            details={
                                "source_ref": candidate.source_ref,
                                "estimated_chars": candidate.estimated_chars,
                                "available_chars": read_limit,
                            },
                        )
                    )
                continue
            try:
                block = await self._read_block(item, max_chars=read_limit)
            except ContextStaleError:
                raise
            except Exception as error:
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason="source_read_failed",
                        details={
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                        },
                    )
                )
                diagnostics.append(
                    ContextDiagnostic(
                        code="context.source_read_failed",
                        message="A selected Context candidate could not be read.",
                        details={
                            "source_ref": candidate.source_ref,
                            "error_type": error.__class__.__name__,
                            "error": str(error),
                        },
                    )
                )
                continue
            if block.content_chars > read_limit:
                completeness = "truncated"
            else:
                completeness = block.completeness
            if candidate.required and completeness != "complete":
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=True,
                        reason="required_content_incompatible",
                        details={"completeness": completeness},
                    )
                )
                diagnostics.append(
                    ContextDiagnostic(
                        code="context.required_content_incompatible",
                        message="Required Context content was not returned completely.",
                        details={
                            "source_ref": candidate.source_ref,
                            "completeness": completeness,
                        },
                    )
                )
                continue
            if block.content_chars > remaining_chars:
                omissions.append(
                    ContextOmission(
                        block_key=candidate.block_key,
                        source_ref=candidate.source_ref,
                        required=candidate.required,
                        reason="character_budget_exhausted",
                    )
                )
                continue
            blocks.append(block)
            remaining_chars -= block.content_chars
            self._disclosed.add(self._identity(candidate))

        package = ContextPackage(
            package_id=f"context_package:{uuid.uuid4().hex}",
            task_context_id=self._snapshot.context_id,
            context_revision=self._snapshot.revision,
            consumer_id=self.consumer.consumer_id,
            phase=self.phase,
            source_revisions=self._snapshot.source_revisions,
            blocks=tuple(blocks),
            omissions=tuple(omissions),
            diagnostics=tuple(diagnostics),
        )
        self._packages.append(package)
        return package

    async def read(self, intent: str | ContextReadIntent) -> ContextPackage:
        return await self.async_read(intent)


__all__ = ["ContextReader", "ContextStaleError"]
