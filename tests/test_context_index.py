from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from agently.core.context import ContextSelection, TaskContext
from agently.types.data import (
    ContextBudget,
    ContextReadIntent,
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
    ContextSourceChange,
    ContextSourceChangeSet,
    ContextSourceRead,
)


def descriptor(
    ref: str,
    *,
    required: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> ContextSourceDescriptor:
    return ContextSourceDescriptor(
        descriptor_key=f"descriptor:{ref}",
        source_id="source:fixture",
        source_revision="revision:1",
        source_ref=ref,
        role="information",
        title=ref,
        summary=f"Fixture {ref}",
        estimated_chars=len(ref),
        required=required,
        index_text=f"Fixture {ref}",
        metadata=dict(metadata or {}),
    )


class CountingDescriptorSource:
    source_kind = "fixture"

    def __init__(self, descriptors: Sequence[ContextSourceDescriptor]) -> None:
        digest = hashlib.sha256(
            "\0".join(item.source_ref for item in descriptors).encode("utf-8")
        ).hexdigest()
        self.source_id = f"source:fixture:{digest[:16]}"
        self.source_revision = f"revision:{digest}"
        self.descriptors = tuple(
            replace(
                item,
                source_id=self.source_id,
                source_revision=self.source_revision,
            )
            for item in descriptors
        )
        self.enumeration_count = 0
        self.received_profiles: list[dict[str, Any]] = []
        self.read_refs: list[str] = []

    async def async_enumerate_descriptors(
        self,
        *,
        profile: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> ContextSourceDescriptorPage:
        self.enumeration_count += 1
        self.received_profiles.append(dict(profile))
        offset = int(cursor or 0)
        page = self.descriptors[offset : offset + limit]
        next_offset = offset + len(page)
        return ContextSourceDescriptorPage(
            source_id=self.source_id,
            source_revision=self.source_revision,
            descriptors=page,
            next_cursor=(str(next_offset) if next_offset < len(self.descriptors) else None),
        )

    async def async_read_exact(
        self,
        source_ref: str,
        *,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        self.read_refs.append(source_ref)
        content = source_ref[range_start : range_start + max_chars]
        return ContextSourceRead(
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=source_ref,
            content=content,
            completeness="complete",
        )


class CountingEmbeddingProvider:
    provider_id = "fixture-embedding"
    model = "fixture-v1"

    def __init__(self) -> None:
        self.calls = 0
        self.embedded_texts = 0
        self.last_usage: dict[str, int] = {}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.embedded_texts += len(texts)
        self.last_usage = {
            "input_tokens": sum(max(1, len(text.split())) for text in texts)
        }
        return [
            [
                float(text.lower().count("alpha")),
                float(text.lower().count("beta")),
                float(max(1, len(text))),
            ]
            for text in texts
        ]


class FailingEmbeddingProvider:
    provider_id = "failing-embedding"
    model = "fixture-v1"

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("embedding unavailable")


class RecordingSelector:
    def __init__(self) -> None:
        self.candidate_counts: list[int] = []

    async def async_select(self, *, candidates, **kwargs) -> ContextSelection:
        del kwargs
        self.candidate_counts.append(len(candidates))
        return ContextSelection(selected_keys=())


class ChangingDescriptorSource(CountingDescriptorSource):
    def __init__(self) -> None:
        super().__init__([descriptor("a"), descriptor("b")])
        self.source_id = "source:changing"
        self.source_revision = "revision:1"
        self.descriptors = tuple(
            replace(
                item,
                source_id=self.source_id,
                source_revision=self.source_revision,
            )
            for item in self.descriptors
        )
        self.change_request: tuple[str, str] | None = None

    def advance(self) -> None:
        self.source_revision = "revision:2"
        self.descriptors = tuple(
            replace(
                descriptor(ref),
                source_id=self.source_id,
                source_revision=self.source_revision,
            )
            for ref in ("a", "c")
        )

    async def async_changes(
        self,
        *,
        from_revision: str,
        to_revision: str,
        profile: Mapping[str, Any],
    ) -> ContextSourceChangeSet:
        del profile
        self.change_request = (from_revision, to_revision)
        changed = next(item for item in self.descriptors if item.source_ref == "c")
        return ContextSourceChangeSet(
            source_id=self.source_id,
            from_revision=from_revision,
            to_revision=to_revision,
            changes=(
                ContextSourceChange(operation="remove", descriptor_key="descriptor:b"),
                ContextSourceChange(
                    operation="upsert",
                    descriptor_key=changed.descriptor_key,
                    descriptor=changed,
                ),
            ),
        )


class FailingChangeFeedSource(ChangingDescriptorSource):
    def __init__(self) -> None:
        super().__init__()
        self.source_id = "source:failing-change-feed"
        self.descriptors = tuple(
            replace(item, source_id=self.source_id) for item in self.descriptors
        )

    async def async_changes(
        self,
        *,
        from_revision: str,
        to_revision: str,
        profile: Mapping[str, Any],
    ) -> ContextSourceChangeSet:
        del from_revision, to_revision, profile
        raise RuntimeError("change feed unavailable")


@pytest.mark.asyncio
async def test_two_task_contexts_reuse_one_source_revision_partition() -> None:
    source = CountingDescriptorSource([descriptor("a"), descriptor("b")])
    first = TaskContext("first")
    second = TaskContext("second")
    first.attach(source, metadata={"allowed_refs": ["a", "b"]})
    second.attach(source, metadata={"allowed_refs": ["a", "b"]})

    await first.reader(consumer="one").async_read("alpha")
    await second.reader(consumer="two").async_read("alpha")

    assert source.enumeration_count == 1
    assert all("query" not in profile for profile in source.received_profiles)


@pytest.mark.asyncio
async def test_reader_continuation_advances_index_not_source_cursor() -> None:
    source = CountingDescriptorSource(
        [descriptor(str(index)) for index in range(12)]
    )
    context = TaskContext("task")
    context.attach(source)
    reader = context.reader(
        consumer="worker",
        budget=ContextBudget(max_chars=100, max_blocks=1, max_block_chars=100),
    )

    intent = ContextReadIntent("same", metadata={"optional_selection": "none"})
    first = await reader.async_read(intent)
    second = await reader.async_read(intent)
    changed_intent = await reader.async_read(
        ContextReadIntent("different", metadata={"optional_selection": "none"})
    )

    assert first.source_coverage != second.source_coverage
    first_coverage = next(iter(first.source_coverage.values()))
    second_coverage = next(iter(second.source_coverage.values()))
    changed_coverage = next(iter(changed_intent.source_coverage.values()))
    assert first_coverage["scope"]["offset"] == 0
    assert second_coverage["scope"]["offset"] > 0
    assert changed_coverage["scope"]["offset"] == 0
    assert source.enumeration_count == 1
    assert all("query" not in profile for profile in source.received_profiles)


@pytest.mark.asyncio
async def test_reused_partition_cannot_leak_an_unbound_entry() -> None:
    source = CountingDescriptorSource(
        [descriptor("public"), descriptor("private")]
    )
    first = TaskContext("first")
    second = TaskContext("second")
    first.attach(source, metadata={"allowed_refs": ["public", "private"]})
    second.attach(source, metadata={"allowed_refs": ["public"]})

    await first.reader(consumer="one").async_read("private")
    reads_before_second = len(source.read_refs)
    package = await second.reader(consumer="two").async_read("private")

    assert {block.source_ref for block in package.blocks} == {"public"}
    assert source.read_refs[reads_before_second:] == ["public"]


@pytest.mark.asyncio
async def test_required_descriptor_is_anchored_ahead_of_optional_pages() -> None:
    source = CountingDescriptorSource(
        [descriptor(str(index)) for index in range(10)]
        + [descriptor("required", required=True)]
    )
    context = TaskContext("required")
    context.attach(source)

    package = await context.reader(
        consumer="worker",
        budget=ContextBudget(max_chars=100, max_blocks=1, max_block_chars=100),
    ).async_read("required")

    assert [block.source_ref for block in package.blocks] == ["required"]


@pytest.mark.asyncio
async def test_unknown_source_kind_fails_before_index_or_source_read() -> None:
    source = CountingDescriptorSource([descriptor("known")])
    context = TaskContext("unknown-kind")
    context.attach(source)

    with pytest.raises(ValueError, match="unknown TaskContext source kind"):
        await context.reader(consumer="worker").async_read(
            ContextReadIntent("known", filters={"source_kinds": ["invented"]})
        )

    assert source.enumeration_count == 0
    assert source.read_refs == []


@pytest.mark.asyncio
async def test_structural_filters_are_applied_by_context_index_not_source() -> None:
    source = CountingDescriptorSource(
        [
            descriptor(
                "docs/guide.md",
                required=True,
                metadata={"path": "docs/guide.md", "collection": "guides"},
            ),
            descriptor(
                "src/runtime.py",
                required=True,
                metadata={"path": "src/runtime.py", "collection": "source"},
            ),
        ]
    )
    context = TaskContext("structural-filter")
    context.attach(source)

    package = await context.reader(consumer="worker").async_read(
        ContextReadIntent(
            "inspect documentation",
            filters={"path": "docs", "pattern": "*.md", "collection": "guides"},
        )
    )

    assert [block.source_ref for block in package.blocks] == ["docs/guide.md"]
    assert all("query" not in profile for profile in source.received_profiles)


@pytest.mark.asyncio
async def test_context_index_rejects_caller_selected_retrieval_mechanism() -> None:
    source = CountingDescriptorSource([descriptor("known", required=True)])
    context = TaskContext("mechanism-filter")
    context.attach(source)

    with pytest.raises(ValueError, match="owned by ContextIndex"):
        await context.reader(consumer="worker").async_read(
            ContextReadIntent("known", filters={"method": "vector"})
        )

    assert source.enumeration_count == 0


def _index_diagnostic(package) -> Mapping[str, Any]:
    return next(
        item.details
        for item in package.diagnostics
        if item.code == "context.index_query"
    )


@pytest.mark.asyncio
async def test_structural_strategy_does_not_claim_observed_embedding_tokens() -> None:
    source = CountingDescriptorSource([descriptor("structural", required=True)])
    context = TaskContext("structural-accounting")
    context.attach(source)

    package = await context.reader(consumer="worker").async_read("structural")
    facts = _index_diagnostic(package)

    assert facts["embedding_input_tokens"] is None
    assert facts["embedding_token_coverage"] == "not_applicable"


@pytest.mark.asyncio
async def test_warm_hybrid_reuses_partition_embeddings_and_counts_query_separately() -> None:
    embedder = CountingEmbeddingProvider()
    source = CountingDescriptorSource([descriptor("alpha"), descriptor("beta")])
    first = TaskContext("hybrid-cold")
    second = TaskContext("hybrid-warm")
    first.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    second.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    first.attach(source)
    second.attach(source)

    cold = await first.reader(consumer="cold").async_read("alpha")
    warm = await second.reader(consumer="warm").async_read("alpha")
    cold_facts = _index_diagnostic(cold)
    warm_facts = _index_diagnostic(warm)

    assert cold_facts["cache"] == "miss"
    assert cold_facts["embedding_build_texts"] == 2
    assert cold_facts["embedding_query_texts"] == 1
    assert cold_facts["embedding_input_tokens"] is not None
    assert warm_facts["cache"] == "hit"
    assert warm_facts["embedding_build_texts"] == 0
    assert warm_facts["embedding_query_texts"] == 1
    assert embedder.embedded_texts == 4


@pytest.mark.asyncio
async def test_hybrid_index_bounds_semantic_candidate_disclosure_to_reader_capacity() -> None:
    embedder = CountingEmbeddingProvider()
    selector = RecordingSelector()
    source = CountingDescriptorSource(
        [descriptor(f"alpha-{index:02d}") for index in range(40)]
    )
    context = TaskContext("hybrid-candidate-window")
    context.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    context.attach(source)

    await context.reader(
        consumer="worker",
        budget=ContextBudget(
            max_chars=20_000,
            max_blocks=4,
            max_block_chars=5_000,
        ),
        semantic_selector=selector,
    ).async_read("alpha")

    assert selector.candidate_counts == [4]


@pytest.mark.asyncio
async def test_hybrid_index_skips_query_embedding_when_filters_leave_one_candidate() -> None:
    embedder = CountingEmbeddingProvider()
    source = CountingDescriptorSource([descriptor("src/only.py")])
    context = TaskContext("hybrid-single-candidate")
    context.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    context.attach(source)

    package = await context.reader(consumer="worker").async_read(
        ContextReadIntent("exact symbol", filters={"path": "src/only.py"})
    )
    facts = _index_diagnostic(package)

    assert facts["effective_strategy"] == "hybrid"
    assert facts["embedding_build_texts"] == 1
    assert facts["embedding_query_texts"] == 0
    assert embedder.embedded_texts == 1


@pytest.mark.asyncio
async def test_hybrid_index_prioritizes_complete_literal_locator_over_term_frequency() -> None:
    embedder = CountingEmbeddingProvider()
    exact = replace(
        descriptor("src/exact.py"),
        index_text="class DynamicRoutingLayer",
    )
    distractor = replace(
        descriptor("src/distractor.py"),
        index_text=("class Other\n" * 12) + ("DynamicRoutingLayer import\n" * 12),
    )
    source = CountingDescriptorSource([distractor, exact])
    context = TaskContext("hybrid-literal-locator")
    context.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    context.attach(source)

    package = await context.reader(
        consumer="worker",
        budget=ContextBudget(
            max_chars=5_000,
            max_blocks=1,
            max_block_chars=5_000,
        ),
    ).async_read("class DynamicRoutingLayer")

    assert [block.source_ref for block in package.blocks] == ["src/exact.py"]


@pytest.mark.asyncio
async def test_structural_index_preserves_wider_semantic_candidate_window() -> None:
    selector = RecordingSelector()
    source = CountingDescriptorSource(
        [descriptor(f"item-{index:02d}") for index in range(40)]
    )
    context = TaskContext("structural-candidate-window")
    context.attach(source)

    await context.reader(
        consumer="worker",
        budget=ContextBudget(
            max_chars=20_000,
            max_blocks=4,
            max_block_chars=5_000,
        ),
        semantic_selector=selector,
    ).async_read("inspect candidates")

    assert selector.candidate_counts == [16]


@pytest.mark.asyncio
async def test_optional_vector_failure_degrades_and_required_vector_fails_closed() -> None:
    optional_source = CountingDescriptorSource([descriptor("alpha")])
    optional = TaskContext("optional-vector")
    optional.configure_index(
        embedding_provider=FailingEmbeddingProvider(),
        strategy="hybrid",
    )
    optional.attach(optional_source)

    package = await optional.reader(consumer="optional").async_read("alpha")
    assert _index_diagnostic(package)["effective_strategy"] == "lexical"

    required_source = CountingDescriptorSource([descriptor("required-alpha")])
    required = TaskContext("required-vector")
    required.configure_index(
        embedding_provider=FailingEmbeddingProvider(),
        strategy="hybrid",
    )
    required.attach(required_source)
    with pytest.raises(RuntimeError, match="required vector"):
        await required.reader(consumer="required").async_read(
            ContextReadIntent("alpha", metadata={"vector_policy": "required"})
        )


@pytest.mark.asyncio
async def test_trustworthy_delta_reembeds_only_changed_descriptors() -> None:
    embedder = CountingEmbeddingProvider()
    source = ChangingDescriptorSource()
    first = TaskContext("delta-first")
    first.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    first.attach(source)
    first_package = await first.reader(consumer="first").async_read("a")
    assert _index_diagnostic(first_package)["embedding_build_texts"] == 2

    source.advance()
    second = TaskContext("delta-second")
    second.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    second.attach(source)
    second_package = await second.reader(consumer="second").async_read("c")
    second_facts = _index_diagnostic(second_package)

    assert source.change_request == ("revision:1", "revision:2")
    assert second_facts["sync_mode"] == "delta"
    assert second_facts["embedding_build_texts"] == 1
    assert embedder.embedded_texts == 5


@pytest.mark.asyncio
async def test_change_feed_failure_records_explicit_full_rebuild_fallback() -> None:
    embedder = CountingEmbeddingProvider()
    source = FailingChangeFeedSource()
    first = TaskContext("delta-fallback-first")
    first.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    first.attach(source)
    await first.reader(consumer="first").async_read("a")

    source.advance()
    second = TaskContext("delta-fallback-second")
    second.configure_index(
        embedding_provider=embedder,
        strategy="hybrid",
    )
    second.attach(source)
    package = await second.reader(consumer="second").async_read("c")
    facts = _index_diagnostic(package)

    assert facts["sync_mode"] == "full_after_delta_failure"
    assert facts["sync_fallbacks"] == (
        "RuntimeError: change feed unavailable",
    )
