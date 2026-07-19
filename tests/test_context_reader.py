from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from agently.core.context import (
    ContextReader,
    ContextSelection,
    ContextStaleError,
    TaskContext,
)
from agently.types.data import (
    ContextBlock,
    ContextBudget,
    ContextCandidate,
    ContextConsumer,
    ContextReadIntent,
)
from agently.types.plugins import ContextSourceCandidateWindow


class MemoryContextSource:
    def __init__(
        self,
        candidates: Sequence[ContextCandidate],
        blocks: Mapping[str, ContextBlock],
        *,
        source_id: str = "source:memory",
        source_revision: str = "rev:1",
    ) -> None:
        self.source_id = source_id
        self.source_revision = source_revision
        self._candidates = list(candidates)
        self._blocks = dict(blocks)
        self.list_calls = 0
        self.read_refs: list[str] = []

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        cursor: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ContextSourceCandidateWindow:
        self.list_calls += 1
        candidates = tuple(self._candidates[:limit])
        return ContextSourceCandidateWindow(
            source_id=self.source_id,
            source_revision=self.source_revision,
            scope={"query": intent.query, "filters": dict(filters or {})},
            candidates=candidates,
            returned_candidates=len(candidates),
            exhaustive=len(candidates) == len(self._candidates),
            cursor=cursor,
            next_cursor=None,
        )

    async def async_read(
        self,
        candidate: ContextCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
    ) -> ContextBlock:
        self.read_refs.append(candidate.source_ref)
        return self._blocks[candidate.source_ref]


class SelfRevisingListSource(MemoryContextSource):
    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        cursor: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ContextSourceCandidateWindow:
        window = await super().async_list_candidates(
            intent,
            limit=limit,
            cursor=cursor,
            filters=filters,
        )
        if self.list_calls == 1:
            self.source_revision = "rev:2"
            self._candidates = [
                replace(candidate, source_revision="rev:2")
                for candidate in self._candidates
            ]
        return window


class RecordingSelector:
    def __init__(self, selected: Sequence[str] | str = ()) -> None:
        self.selected = selected
        self.calls: list[tuple[ContextReadIntent, tuple[ContextCandidate, ...]]] = []

    async def async_select(
        self,
        *,
        intent: ContextReadIntent,
        candidates: Sequence[ContextCandidate],
        consumer: ContextConsumer,
        phase: str,
    ) -> ContextSelection:
        offered = tuple(candidates)
        self.calls.append((intent, offered))
        if self.selected == "all":
            keys = tuple(candidate.block_key for candidate in offered)
        else:
            keys = tuple(self.selected)
        return ContextSelection(selected_keys=keys)


class PagingContextSource(MemoryContextSource):
    def __init__(self) -> None:
        candidates = [
            _candidate("guide", role="instruction", required=True),
            _candidate("docs/a"),
            _candidate("docs/b"),
            _candidate("docs/c"),
            _candidate("docs/d"),
        ]
        super().__init__(
            candidates,
            {
                item.source_ref: _source_block(
                    item.source_ref,
                    role=item.role,
                    required=item.required,
                )
                for item in candidates
            },
        )
        self.cursors: list[str | None] = []

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        cursor: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ContextSourceCandidateWindow:
        del intent, limit, filters
        self.list_calls += 1
        self.cursors.append(cursor)
        if cursor is None:
            page = (self._candidates[0], self._candidates[1], self._candidates[2])
            next_cursor = "page:2"
            exhaustive = False
        elif cursor == "page:2":
            page = (self._candidates[0], self._candidates[3], self._candidates[4])
            next_cursor = None
            exhaustive = True
        else:
            raise ValueError("unexpected cursor")
        return ContextSourceCandidateWindow(
            source_id=self.source_id,
            source_revision=self.source_revision,
            scope={"query": "repository owner", "path": "."},
            candidates=page,
            returned_candidates=len(page),
            exhaustive=exhaustive,
            cursor=cursor,
            next_cursor=next_cursor,
        )


class ToggleFailSelector(RecordingSelector):
    def __init__(self) -> None:
        super().__init__("all")
        self.fail = True

    async def async_select(
        self,
        *,
        intent: ContextReadIntent,
        candidates: Sequence[ContextCandidate],
        consumer: ContextConsumer,
        phase: str,
    ) -> ContextSelection:
        if self.fail:
            raise RuntimeError("selector unavailable")
        return await super().async_select(
            intent=intent,
            candidates=candidates,
            consumer=consumer,
            phase=phase,
        )


class FailingListContextSource:
    source_id = "source:failing"
    source_revision = "rev:1"

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        cursor: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ContextSourceCandidateWindow:
        del intent, limit, cursor, filters
        raise RuntimeError("source unavailable")

    async def async_read(self, *_args: Any, **_kwargs: Any) -> ContextBlock:
        raise AssertionError("failing list source cannot read")


def test_context_reader_cannot_be_constructed_outside_task_context() -> None:
    context = TaskContext("task-owned-reader")

    with pytest.raises(TypeError, match="TaskContext.reader"):
        ContextReader(
            context,
            consumer=ContextConsumer("planner"),
            phase="planning",
            budget=ContextBudget(),
        )


def test_task_context_restores_its_consumer_bound_reader() -> None:
    context = TaskContext("task-reader-restore", "context:reader-restore")
    reader = context.reader(
        consumer=ContextConsumer(
            "planner",
            model="model:test",
            capabilities={"attachments": False},
        ),
        phase="planning",
        budget=ContextBudget(max_chars=900, max_blocks=4, max_block_chars=300),
    )
    state = reader._export_state()

    restored = context.restore_reader(state)

    assert restored is not reader
    assert restored.task_context is context
    assert restored.consumer == reader.consumer
    assert restored.phase == "planning"
    assert restored.budget == reader.budget
    assert restored._export_state() == state


def test_context_reader_state_cannot_be_restored_outside_task_context() -> None:
    context = TaskContext("task-reader-state-owner")
    reader = context.reader(consumer="planner")
    state = reader._export_state()

    with pytest.raises(TypeError, match="TaskContext.restore_reader"):
        reader._restore_state(state)


@pytest.mark.asyncio
async def test_reader_advances_same_intent_across_source_windows() -> None:
    source = PagingContextSource()
    context = TaskContext("task-progressive-windows")
    context.attach(source, binding_id="binding:memory", required=True)
    reader = context.reader(
        consumer="planner",
        phase="planning",
        semantic_selector=RecordingSelector("all"),
    )

    first = await reader.async_read("repository owner")
    second = await reader.async_read("repository owner")

    assert source.cursors == [None, "page:2"]
    assert [block.source_ref for block in first.blocks] == ["guide", "docs/a", "docs/b"]
    assert [block.source_ref for block in second.blocks] == ["guide", "docs/c", "docs/d"]
    assert first.source_coverage["binding:memory"] == {
        "scope": {"query": "repository owner", "path": "."},
        "returned_candidates": 3,
        "exhaustive": False,
        "continuation_available": True,
    }
    assert second.source_coverage["binding:memory"]["exhaustive"] is True


@pytest.mark.asyncio
async def test_reader_does_not_advance_window_when_selection_fails() -> None:
    source = PagingContextSource()
    selector = ToggleFailSelector()
    context = TaskContext("task-window-selection-failure")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="planner", semantic_selector=selector)

    failed = await reader.async_read("repository owner")
    selector.fail = False
    retried = await reader.async_read("repository owner")
    advanced = await reader.async_read("repository owner")

    assert any(item.code == "context.selection_failed" for item in failed.diagnostics)
    assert source.cursors == [None, None, "page:2"]
    assert [block.source_ref for block in retried.blocks] == ["guide", "docs/a", "docs/b"]
    assert [block.source_ref for block in advanced.blocks] == ["guide", "docs/c", "docs/d"]


@pytest.mark.asyncio
async def test_reader_does_not_advance_window_without_semantic_selector() -> None:
    source = PagingContextSource()
    context = TaskContext("task-window-selector-unavailable")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="planner")

    failed = await reader.async_read("repository owner")
    reader.semantic_selector = RecordingSelector("all")
    retried = await reader.async_read("repository owner")

    assert any(
        item.code == "context.semantic_selector_unavailable"
        for item in failed.diagnostics
    )
    assert source.cursors == [None, None]
    assert [block.source_ref for block in retried.blocks] == ["guide", "docs/a", "docs/b"]


@pytest.mark.asyncio
async def test_reader_advances_successful_source_when_another_source_fails() -> None:
    source = PagingContextSource()
    context = TaskContext("task-independent-source-windows")
    context.attach(source, binding_id="binding:memory")
    context.attach(FailingListContextSource(), binding_id="binding:failing")
    reader = context.reader(
        consumer="planner",
        semantic_selector=RecordingSelector("all"),
    )

    first = await reader.async_read("repository owner")
    second = await reader.async_read("repository owner")

    assert any(item.code == "context.source_candidates_failed" for item in first.diagnostics)
    assert source.cursors == [None, "page:2"]
    assert [block.source_ref for block in second.blocks] == ["guide", "docs/c", "docs/d"]


@pytest.mark.asyncio
async def test_task_context_restored_reader_continues_at_saved_window() -> None:
    source = PagingContextSource()
    context = TaskContext("task-window-resume")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(
        consumer="planner",
        semantic_selector=RecordingSelector("all"),
    )
    await reader.async_read("repository owner")
    state = reader._export_state()

    restored = context.restore_reader(
        state,
        semantic_selector=RecordingSelector("all"),
    )
    package = await restored.async_read("repository owner")

    assert source.cursors == [None, "page:2"]
    assert [block.source_ref for block in package.blocks] == ["guide", "docs/c", "docs/d"]


def test_task_context_reader_restore_rejects_foreign_context() -> None:
    original = TaskContext("task-original", "context:original")
    state = original.reader(consumer="planner")._export_state()

    with pytest.raises(ValueError, match="different TaskContext"):
        TaskContext("task-other", "context:other").restore_reader(state)


def _candidate(
    source_ref: str,
    *,
    role: str = "information",
    required: bool = False,
    estimated_chars: int = 80,
    summary: str | None = None,
) -> ContextCandidate:
    return ContextCandidate(
        block_key=f"untrusted-source-key:{source_ref}",
        source_id="source:memory",
        source_revision="rev:1",
        source_ref=source_ref,
        binding_id="untrusted-source-binding",
        role=role,  # type: ignore[arg-type]
        summary=summary or source_ref,
        estimated_chars=estimated_chars,
        required=required,
    )


def _source_block(
    source_ref: str,
    *,
    role: str = "information",
    content: str | None = None,
    completeness: str = "complete",
    required: bool = False,
) -> ContextBlock:
    resolved = source_ref if content is None else content
    return ContextBlock(
        block_id=f"untrusted-source-block:{source_ref}",
        block_key=f"untrusted-source-key:{source_ref}",
        source_id="source:memory",
        source_revision="rev:1",
        source_ref=source_ref,
        binding_id="untrusted-source-binding",
        role=role,  # type: ignore[arg-type]
        content=resolved,
        completeness=completeness,  # type: ignore[arg-type]
        content_chars=len(resolved),
        required=required,
    )


@pytest.mark.asyncio
async def test_required_and_explicit_blocks_bypass_semantic_dropping() -> None:
    candidates = [
        _candidate("skill/core", role="instruction", required=True),
        _candidate("docs/explicit"),
        _candidate("docs/optional"),
    ]
    source = MemoryContextSource(
        candidates,
        {item.source_ref: _source_block(item.source_ref, role=item.role, required=item.required) for item in candidates},
    )
    selector = RecordingSelector(selected=())
    context = TaskContext("task-1", "context:task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(
        consumer=ContextConsumer("planner"),
        phase="planning",
        semantic_selector=selector,
    )

    package = await reader.async_read(
        ContextReadIntent(query="Plan the task", explicit_refs=("docs/explicit",))
    )

    assert [block.source_ref for block in package.blocks] == ["skill/core", "docs/explicit"]
    assert [block.required for block in package.blocks] == [True, False]
    assert len(selector.calls) == 1
    assert [candidate.source_ref for candidate in selector.calls[0][1]] == ["docs/optional"]
    assert all(
        not candidate.block_key.startswith("untrusted-source-key:")
        for candidate in selector.calls[0][1]
    )


@pytest.mark.asyncio
async def test_optional_blocks_follow_selector_priority_with_actual_remaining_budget() -> None:
    candidates = [
        _candidate("skill/core", role="instruction", required=True, estimated_chars=4),
        _candidate("docs/general", estimated_chars=6),
        _candidate("docs/exact-api", estimated_chars=6),
    ]
    source = MemoryContextSource(
        candidates,
        {
            "skill/core": _source_block(
                "skill/core",
                role="instruction",
                content="core",
                required=True,
            ),
            "docs/general": _source_block("docs/general", content="aaaaaa"),
            "docs/exact-api": _source_block("docs/exact-api", content="bbbbbb"),
        },
    )
    selector = RecordingSelector()
    context = TaskContext("task-priority")
    context.attach(source, binding_id="binding:memory")
    probe_reader = context.reader(
        consumer="probe",
        budget=ContextBudget(max_chars=20, max_blocks=3, max_block_chars=6),
        semantic_selector=RecordingSelector("all"),
    )
    probe = await probe_reader.async_read("probe")
    optional_keys = {
        block.source_ref: block.block_key for block in probe.blocks if not block.required
    }
    selector.selected = (
        optional_keys["docs/exact-api"],
        optional_keys["docs/general"],
    )
    reader = context.reader(
        consumer="planner",
        phase="planning",
        budget=ContextBudget(max_chars=10, max_blocks=2, max_block_chars=6),
        semantic_selector=selector,
    )

    package = await reader.async_read("Use the exact API integration contract")

    assert [block.source_ref for block in package.blocks] == [
        "skill/core",
        "docs/exact-api",
    ]
    selection_intent = selector.calls[0][0]
    assert dict(selection_intent.metadata["selection_budget"]) == {
        "available_chars": 6,
        "available_blocks": 1,
        "max_block_chars": 6,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["unknown", "duplicate"])
async def test_invalid_selector_keys_fail_closed_without_losing_required_blocks(
    invalid_kind: str,
) -> None:
    candidates = [
        _candidate("skill/core", role="instruction", required=True),
        _candidate("docs/a"),
        _candidate("docs/b"),
    ]
    source = MemoryContextSource(
        candidates,
        {item.source_ref: _source_block(item.source_ref, role=item.role, required=item.required) for item in candidates},
    )
    selector = RecordingSelector()
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="worker", semantic_selector=selector)

    offered_reader = context.reader(consumer="probe", semantic_selector=RecordingSelector("all"))
    probe = await offered_reader.async_read("probe")
    optional_keys = [block.block_key for block in probe.blocks if not block.required]
    selector.selected = (
        ("not-offered",)
        if invalid_kind == "unknown"
        else (optional_keys[0], optional_keys[0])
    )

    package = await reader.async_read("Use the relevant documents")

    assert [block.source_ref for block in package.blocks] == ["skill/core"]
    assert any(item.code == "context.selection_invalid" for item in package.diagnostics)
    assert {item.source_ref for item in package.omissions} == {"docs/a", "docs/b"}


@pytest.mark.asyncio
async def test_optional_prose_relevance_never_falls_back_to_keyword_matching() -> None:
    candidates = [
        _candidate("docs/obvious", summary="Exact release audit instructions"),
        _candidate("docs/other", summary="Gardening notes"),
    ]
    source = MemoryContextSource(
        candidates,
        {item.source_ref: _source_block(item.source_ref) for item in candidates},
    )
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="planner")

    package = await reader.async_read("Perform the exact release audit")

    assert package.blocks == ()
    assert source.read_refs == []
    assert any(item.code == "context.semantic_selector_unavailable" for item in package.diagnostics)


@pytest.mark.asyncio
async def test_explicit_optional_none_skips_semantic_selector() -> None:
    candidate = _candidate("docs/optional")
    source = MemoryContextSource(
        [candidate],
        {candidate.source_ref: _source_block(candidate.source_ref)},
    )
    selector = RecordingSelector("all")
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="planner", semantic_selector=selector)

    package = await reader.async_read(
        ContextReadIntent(
            query="Use required core only",
            metadata={"optional_selection": "none"},
        )
    )

    assert package.blocks == ()
    assert selector.calls == []
    assert package.omissions[0].reason == "explicitly_skipped"


@pytest.mark.asyncio
async def test_readers_over_one_task_context_have_independent_disclosure_histories() -> None:
    candidate = _candidate("docs/one")
    source = MemoryContextSource([candidate], {"docs/one": _source_block("docs/one")})
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    planner = context.reader(consumer="planner")
    verifier = context.reader(consumer="verifier")

    planner_first = await planner.async_read("Read the only source")
    planner_second = await planner.async_read("Read the only source again")
    verifier_first = await verifier.async_read("Read the only source")

    assert [block.source_ref for block in planner_first.blocks] == ["docs/one"]
    assert planner_second.blocks == ()
    assert planner_second.omissions[0].reason == "already_disclosed"
    assert [block.source_ref for block in verifier_first.blocks] == ["docs/one"]


@pytest.mark.asyncio
async def test_cold_resources_are_read_only_when_explicitly_selected() -> None:
    candidates = [
        _candidate("skill/resources", role="index", required=True),
        _candidate("skill/reference/large", estimated_chars=3000),
    ]
    source = MemoryContextSource(
        candidates,
        {
            "skill/resources": _source_block("skill/resources", role="index", required=True),
            "skill/reference/large": _source_block(
                "skill/reference/large",
                content="large reference body",
            ),
        },
    )
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:skill")
    reader = context.reader(consumer="worker", semantic_selector=RecordingSelector(()))

    catalog = await reader.async_read("Inspect the Skill catalog")
    explicit = await reader.async_read(
        ContextReadIntent(
            query="Read the selected reference",
            explicit_refs=("skill/reference/large",),
        )
    )

    assert [block.source_ref for block in catalog.blocks] == ["skill/resources"]
    assert "skill/reference/large" not in source.read_refs[:-1]
    assert [block.source_ref for block in explicit.blocks] == [
        "skill/resources",
        "skill/reference/large",
    ]


@pytest.mark.asyncio
async def test_required_incomplete_block_is_reported_as_incompatible_not_delivered() -> None:
    candidate = _candidate(
        "skill/oversized-core",
        role="instruction",
        required=True,
        estimated_chars=10000,
    )
    source = MemoryContextSource(
        [candidate],
        {
            "skill/oversized-core": _source_block(
                "skill/oversized-core",
                role="instruction",
                content="truncated core",
                completeness="truncated",
                required=True,
            )
        },
    )
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:skill", required=True)
    reader = context.reader(
        consumer="worker",
        budget=ContextBudget(max_chars=100, max_block_chars=100),
    )

    package = await reader.async_read("Apply the Skill")

    assert package.blocks == ()
    assert package.omissions[0].reason == "required_content_incompatible"
    assert any(item.code == "context.required_content_incompatible" for item in package.diagnostics)


@pytest.mark.asyncio
async def test_explicit_lossy_required_overflow_returns_auditable_digest() -> None:
    candidate = _candidate(
        "skill/oversized-core",
        role="instruction",
        required=True,
        estimated_chars=10000,
    )

    class LossySource(MemoryContextSource):
        async def async_read(
            self,
            candidate: ContextCandidate,
            *,
            max_chars: int,
            representation: str | None = None,
        ) -> ContextBlock:
            if representation != "lossy_digest":
                return await super().async_read(
                    candidate,
                    max_chars=max_chars,
                    representation=representation,
                )
            content = "Lossy core digest with scoped refs"[:max_chars]
            return ContextBlock(
                block_id="lossy-core-block",
                block_key=candidate.block_key,
                source_id=self.source_id,
                source_revision=self.source_revision,
                source_ref=candidate.source_ref,
                binding_id=candidate.binding_id,
                role="instruction",
                content=content,
                completeness="lossy",
                content_chars=len(content),
                required=True,
                refs=(candidate.source_ref, "skill/oversized-core#section-1"),
                metadata={
                    "original_chars": candidate.estimated_chars,
                    "representation": "lossy_digest",
                },
            )

    source = LossySource(
        [candidate],
        {
            candidate.source_ref: _source_block(
                candidate.source_ref,
                role="instruction",
                content="full protected core",
                required=True,
            )
        },
    )
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:skill", required=True)
    reader = context.reader(
        consumer="worker",
        budget=ContextBudget(max_chars=100, max_block_chars=100),
    )

    package = await reader.async_read(
        ContextReadIntent(
            query="Apply the Skill",
            metadata={"required_overflow": "lossy_digest"},
        )
    )

    assert len(package.blocks) == 1
    assert package.blocks[0].completeness == "lossy"
    assert package.blocks[0].refs == (
        "skill/oversized-core",
        "skill/oversized-core#section-1",
    )
    assert not any(item.required for item in package.omissions)
    assert any(item.code == "context.required_content_lossy" for item in package.diagnostics)


@pytest.mark.asyncio
async def test_reader_rejects_stale_source_revision() -> None:
    candidate = _candidate("docs/one")
    source = MemoryContextSource([candidate], {"docs/one": _source_block("docs/one")})
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="worker")

    source.source_revision = "rev:2"

    with pytest.raises(ContextStaleError, match="source revision"):
        await reader.async_read("Read the source")


@pytest.mark.asyncio
async def test_reader_rebases_once_when_source_revision_changes_during_listing() -> None:
    candidate = _candidate("docs/one")
    source = SelfRevisingListSource(
        [candidate],
        {"docs/one": _source_block("docs/one")},
    )
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="worker")

    package = await reader.async_read("Read the source")

    assert source.list_calls == 2
    assert package.source_revisions == {"binding:memory": "rev:2"}
    assert [block.source_ref for block in package.blocks] == ["docs/one"]


@pytest.mark.asyncio
async def test_reader_does_not_rebase_structural_task_context_mutation_during_listing() -> None:
    candidate = _candidate("docs/one")
    context = TaskContext("task-1")

    class StructurallyMutatingListSource(MemoryContextSource):
        async def async_list_candidates(
            self,
            intent: ContextReadIntent,
            *,
            limit: int,
            cursor: str | None = None,
            filters: Mapping[str, Any] | None = None,
        ) -> ContextSourceCandidateWindow:
            window = await super().async_list_candidates(
                intent,
                limit=limit,
                cursor=cursor,
                filters=filters,
            )
            if self.list_calls == 1:
                context.put(
                    role="information",
                    content="concurrent structural entry",
                    entry_id="entry:concurrent",
                )
            return window

    source = StructurallyMutatingListSource(
        [candidate],
        {"docs/one": _source_block("docs/one")},
    )
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="worker")

    with pytest.raises(ContextStaleError, match="TaskContext revision"):
        await reader.async_read("Read the source")

    assert source.list_calls == 1


@pytest.mark.asyncio
async def test_reader_explicit_refresh_rebases_source_revision_and_preserves_history() -> None:
    candidate = _candidate("docs/one")
    source = MemoryContextSource([candidate], {"docs/one": _source_block("docs/one")})
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="worker")

    first = await reader.async_read("Read the source")
    source.source_revision = "rev:2"
    source._candidates = [
        replace(item, source_revision="rev:2")
        for item in source._candidates
    ]

    assert reader.is_current is False
    reader.refresh()
    second = await reader.async_read("Read the changed source")

    assert [block.source_ref for block in first.blocks] == ["docs/one"]
    assert [block.source_ref for block in second.blocks] == ["docs/one"]
    assert reader.snapshot.source_revisions["binding:memory"] == "rev:2"
    assert len(reader.packages) == 2
