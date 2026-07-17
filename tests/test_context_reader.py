from __future__ import annotations

from collections.abc import Mapping, Sequence
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
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ContextCandidate]:
        self.list_calls += 1
        return self._candidates[:limit]

    async def async_read(
        self,
        candidate: ContextCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
    ) -> ContextBlock:
        self.read_refs.append(candidate.source_ref)
        return self._blocks[candidate.source_ref]


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
async def test_reader_explicit_refresh_rebases_source_revision_and_preserves_history() -> None:
    candidate = _candidate("docs/one")
    source = MemoryContextSource([candidate], {"docs/one": _source_block("docs/one")})
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="worker")

    first = await reader.async_read("Read the source")
    source.source_revision = "rev:2"

    assert reader.is_current is False
    reader.refresh()
    second = await reader.async_read("Read the changed source")

    assert [block.source_ref for block in first.blocks] == ["docs/one"]
    assert [block.source_ref for block in second.blocks] == ["docs/one"]
    assert reader.snapshot.source_revisions["source:memory"] == "rev:2"
    assert len(reader.packages) == 2
