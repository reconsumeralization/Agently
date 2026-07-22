from __future__ import annotations

import hashlib
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
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
    ContextSourceRead,
)


class MemoryContextSource:
    source_kind = "memory_fixture"

    def __init__(
        self,
        candidates: Sequence[ContextCandidate],
        blocks: Mapping[str, ContextBlock],
        *,
        source_id: str | None = None,
        source_revision: str = "rev:1",
    ) -> None:
        identity = hashlib.sha256(
            repr(
                (
                    self.__class__.__qualname__,
                    [(item.source_ref, item.summary, item.estimated_chars) for item in candidates],
                    [(key, block.content, block.completeness) for key, block in blocks.items()],
                )
            ).encode("utf-8")
        ).hexdigest()[:16]
        self.source_id = source_id or f"source:memory:{identity}"
        self.source_revision = source_revision
        self._candidates = list(candidates)
        self._blocks = dict(blocks)
        self.enumerate_calls = 0
        self.read_refs: list[str] = []

    async def async_enumerate_descriptors(
        self,
        *,
        profile: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> ContextSourceDescriptorPage:
        del profile
        self.enumerate_calls += 1
        offset = int(cursor or 0)
        candidates = tuple(self._candidates[offset : offset + limit])
        descriptors = tuple(
            ContextSourceDescriptor(
                descriptor_key=candidate.block_key,
                source_id=self.source_id,
                source_revision=self.source_revision,
                source_ref=candidate.source_ref,
                role=candidate.role,
                title=candidate.source_ref,
                summary=candidate.summary,
                estimated_chars=candidate.estimated_chars,
                required=candidate.required,
                priority=candidate.priority,
                index_text=candidate.summary,
                metadata=candidate.metadata,
            )
            for candidate in candidates
        )
        next_offset = offset + len(candidates)
        return ContextSourceDescriptorPage(
            source_id=self.source_id,
            source_revision=self.source_revision,
            descriptors=descriptors,
            next_cursor=(str(next_offset) if next_offset < len(self._candidates) else None),
        )

    async def async_read_exact(
        self,
        source_ref: str,
        *,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        del representation
        self.read_refs.append(source_ref)
        block = self._blocks[source_ref]
        content = str(block.content)[range_start : range_start + max_chars]
        complete = range_start + len(content) >= len(str(block.content))
        return ContextSourceRead(
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=source_ref,
            content=content,
            completeness=(block.completeness if complete else "truncated"),
            next_range_start=(None if complete else range_start + len(content)),
            refs=block.refs,
            metadata=block.metadata,
        )


class ScopedMemoryContextSource(MemoryContextSource):
    def __init__(
        self,
        candidates: Sequence[ContextCandidate],
        blocks: Mapping[str, ContextBlock],
    ) -> None:
        super().__init__(candidates, blocks)
        self.scoped_queries: list[str] = []

    async def async_read_scoped(
        self,
        source_ref: str,
        *,
        query: str,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        self.scoped_queries.append(query)
        result = await self.async_read_exact(
            source_ref,
            max_chars=max_chars,
            representation=representation,
            range_start=range_start,
        )
        return replace(result, metadata={**dict(result.metadata), "query": query})


class SelfRevisingListSource(MemoryContextSource):
    async def async_enumerate_descriptors(
        self,
        *,
        profile: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> ContextSourceDescriptorPage:
        page = await super().async_enumerate_descriptors(
            profile=profile,
            limit=limit,
            cursor=cursor,
        )
        if self.enumerate_calls == 1:
            self.source_revision = "rev:2"
            self._candidates = [
                replace(candidate, source_revision="rev:2")
                for candidate in self._candidates
            ]
        return page


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
    source_kind = "failing_fixture"
    source_revision = "rev:1"

    async def async_enumerate_descriptors(
        self,
        *,
        profile: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> ContextSourceDescriptorPage:
        del profile, limit, cursor
        raise RuntimeError("source unavailable")

    async def async_read_exact(self, *_args: Any, **_kwargs: Any) -> ContextSourceRead:
        raise AssertionError("failing list source cannot read")


@pytest.mark.asyncio
async def test_one_source_index_failure_does_not_hide_healthy_source_content() -> None:
    required = _candidate(
        "healthy/core",
        role="instruction",
        required=True,
        estimated_chars=12,
    )
    healthy = MemoryContextSource(
        [required],
        {
            required.source_ref: _source_block(
                required.source_ref,
                role=required.role,
                content="healthy core",
                required=True,
            )
        },
        source_id="source:healthy-isolation",
    )
    context = TaskContext("task-source-isolation")
    context.attach(healthy, binding_id="binding:healthy", required=True)
    context.attach(
        FailingListContextSource(),
        binding_id="binding:failing",
        required=False,
    )

    package = await context.reader(consumer="worker").async_read("execute")

    assert [block.content for block in package.blocks] == ["healthy core"]
    failures = [
        item
        for item in package.diagnostics
        if item.code == "context.source_candidates_failed"
    ]
    assert [item.details["binding_id"] for item in failures] == ["binding:failing"]


@pytest.mark.asyncio
async def test_read_intent_can_bound_candidates_and_exact_read_chars() -> None:
    candidates = [_candidate(f"doc/{index}") for index in range(4)]
    source = MemoryContextSource(
        candidates,
        {
            item.source_ref: _source_block(
                item.source_ref,
                content=f"content-{index}-" + ("x" * 40),
            )
            for index, item in enumerate(candidates)
        },
    )
    selector = RecordingSelector("all")
    context = TaskContext("task-read-bounds")
    context.attach(source)

    package = await context.reader(
        consumer="worker",
        budget=ContextBudget(max_chars=1000, max_blocks=8, max_block_chars=500),
        semantic_selector=selector,
    ).async_read(
        ContextReadIntent(
            "read bounded evidence",
            metadata={"candidate_limit": 2, "max_block_chars": 12},
        )
    )

    assert len(selector.calls[0][1]) == 2
    assert len(package.blocks) == 2
    assert all(block.content_chars == 12 for block in package.blocks)
    assert all(block.completeness == "truncated" for block in package.blocks)


@pytest.mark.asyncio
async def test_single_optional_candidate_does_not_spend_semantic_selector() -> None:
    candidate = _candidate("docs/only.md")
    source = MemoryContextSource(
        [candidate],
        {candidate.source_ref: _source_block(candidate.source_ref, content="only source")},
    )
    selector = RecordingSelector("all")
    context = TaskContext("single-candidate-selection")
    context.attach(source)

    package = await context.reader(
        consumer="worker",
        semantic_selector=selector,
    ).async_read(
        ContextReadIntent(
            "Read the exactly scoped source",
            filters={"path": "docs/only.md"},
        )
    )

    assert [block.source_ref for block in package.blocks] == ["docs/only.md"]
    assert selector.calls == []


@pytest.mark.asyncio
async def test_scoped_source_can_disclose_different_ranges_of_the_same_ref() -> None:
    candidate = _candidate("src/router.py")
    source = ScopedMemoryContextSource(
        [candidate],
        {
            candidate.source_ref: _source_block(
                candidate.source_ref,
                content="class RankAllocator:\n    pass\nclass RouterCalibration:\n    pass\n",
            )
        },
    )
    context = TaskContext("scoped-same-ref")
    context.attach(source)
    reader = context.reader(consumer="worker")

    first = await reader.async_read("class RankAllocator")
    second = await reader.async_read("class RouterCalibration")
    repeated = await reader.async_read("class RouterCalibration")

    assert [block.source_ref for block in first.blocks] == ["src/router.py"]
    assert [block.source_ref for block in second.blocks] == ["src/router.py"]
    assert repeated.blocks == ()
    assert source.scoped_queries == ["class RankAllocator", "class RouterCalibration"]


@pytest.mark.asyncio
async def test_scoped_source_uses_single_content_locator_instead_of_semantic_query() -> None:
    candidate = _candidate(
        "src/router.py",
        required=True,
        summary="src/router.py\nclass DynamicRoutingLayer",
        metadata={"path": "src/router.py"},
    )
    source = ScopedMemoryContextSource(
        [candidate],
        {
            candidate.source_ref: _source_block(
                candidate.source_ref,
                content="class DynamicRoutingLayer:\n    pass\n",
            )
        },
    )
    context = TaskContext("scoped-content-locator")
    context.attach(source)

    package = await context.reader(consumer="worker").async_read(
        ContextReadIntent(
            "Explain routing weights and expert selection",
            filters={
                "path": "src/router.py",
                "content_contains": ["class DynamicRoutingLayer"],
            },
        )
    )

    assert [block.source_ref for block in package.blocks] == ["src/router.py"]
    assert source.scoped_queries == ["class DynamicRoutingLayer"]


@pytest.mark.asyncio
async def test_ref_only_read_returns_descriptor_identity_without_exact_read() -> None:
    candidate = _candidate("docs/large.md", estimated_chars=100_000)
    source = MemoryContextSource(
        [candidate],
        {candidate.source_ref: _source_block(candidate.source_ref, content="cold body")},
    )
    context = TaskContext("task-ref-only")
    context.attach(source)

    package = await context.reader(consumer="locator").async_read(
        ContextReadIntent(
            "locate the document",
            metadata={"delivery_mode": "refs_only", "candidate_limit": 1},
        )
    )

    assert len(package.blocks) == 1
    assert package.blocks[0].source_ref == "docs/large.md"
    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert source.read_refs == []


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
    metadata: Mapping[str, Any] | None = None,
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
        metadata=dict(metadata or {}),
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


class MediaContextSource(MemoryContextSource):
    def __init__(self, candidate: ContextCandidate, content: Any) -> None:
        super().__init__(
            [candidate],
            {
                candidate.source_ref: ContextBlock(
                    block_id=f"media:{candidate.source_ref}",
                    block_key=candidate.block_key,
                    source_id="source:memory",
                    source_revision="rev:1",
                    source_ref=candidate.source_ref,
                    binding_id="untrusted-source-binding",
                    role=candidate.role,
                    content="placeholder",
                    completeness="complete",
                    content_chars=11,
                    metadata=candidate.metadata,
                )
            },
        )
        self._media_content = content
        self.representations: list[str | None] = []

    async def async_read_exact(
        self,
        source_ref: str,
        *,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        del max_chars, range_start
        self.read_refs.append(source_ref)
        self.representations.append(representation)
        return ContextSourceRead(
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=source_ref,
            content=self._media_content,
            completeness="complete",
            metadata={
                **dict(self._candidates[0].metadata),
                "context_representation": representation or "text",
            },
        )


class DroppedDocumentProofContextSource(MediaContextSource):
    async def async_read_exact(
        self,
        source_ref: str,
        *,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        del max_chars, range_start
        self.read_refs.append(source_ref)
        self.representations.append(representation)
        return ContextSourceRead(
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=source_ref,
            content="text whose parser provenance was dropped",
            completeness="complete",
            metadata={
                **dict(self._candidates[0].metadata),
                "context_representation": "text",
            },
        )


@pytest.mark.asyncio
async def test_context_reader_hides_image_body_and_untrusted_summary_without_vlm_capability() -> None:
    candidate = _candidate(
        "assets/chart.png",
        summary="\ufffdPNG guessed revenue chart content",
        estimated_chars=40_000,
        metadata={
            "path": "assets/chart.png",
            "media_type": "image/png",
            "content_kind": "image",
            "total_bytes": 40_000,
            "summary": "guessed chart contents",
            "ocr_text": "invented labels",
        },
    )
    source = MediaContextSource(
        candidate,
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    )
    selector = RecordingSelector("all")
    context = TaskContext("task-image-ref-only")
    context.attach(source, binding_id="binding:image")

    package = await context.reader(
        consumer=ContextConsumer(
            "text-only-worker",
            capabilities={"attachments": {"image": False}},
        ),
        semantic_selector=selector,
    ).async_read("inspect the chart")

    assert selector.calls[0][1][0].summary == "assets/chart.png"
    assert len(package.blocks) == 1
    assert package.blocks[0].source_ref == "assets/chart.png"
    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert "summary" not in package.blocks[0].metadata
    assert "ocr_text" not in package.blocks[0].metadata
    assert source.read_refs == []
    assert any(
        item.code == "context.media_content_ref_only"
        for item in package.diagnostics
    )


@pytest.mark.asyncio
async def test_context_reader_delivers_image_attachment_only_to_capable_consumer() -> None:
    attachment = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }
    candidate = _candidate(
        "assets/chart.png",
        estimated_chars=40_000,
        metadata={
            "path": "assets/chart.png",
            "media_type": "image/png",
            "content_kind": "image",
            "total_bytes": 40_000,
            "summary": "guessed chart contents",
            "ocr_text": "invented labels",
        },
    )
    source = MediaContextSource(candidate, [attachment])
    context = TaskContext("task-image-vlm")
    context.attach(source, binding_id="binding:image")

    package = await context.reader(
        consumer=ContextConsumer(
            "vlm-worker",
            capabilities={"attachments": {"image": True}},
        )
    ).async_read("inspect the chart")

    assert source.representations == ["image_attachment"]
    assert package.blocks[0].content == (attachment,)
    assert package.blocks[0].content_chars == 0
    assert package.blocks[0].metadata["context_representation"] == (
        "image_attachment"
    )
    assert "summary" not in package.blocks[0].metadata
    assert "ocr_text" not in package.blocks[0].metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_content",
    [
        [],
        [{"type": "image_url", "image_url": {"url": ""}}],
        [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,%%%%"},
            }
        ],
        [{"type": "text", "text": "guessed image contents"}],
    ],
)
async def test_context_reader_rejects_invalid_image_attachment_payloads(
    invalid_content: Any,
) -> None:
    candidate = _candidate(
        "assets/chart.png",
        metadata={
            "path": "assets/chart.png",
            "media_type": "image/png",
            "content_kind": "image",
        },
    )
    source = MediaContextSource(candidate, invalid_content)
    context = TaskContext("task-image-invalid-attachment")
    context.attach(source, binding_id="binding:image")

    package = await context.reader(
        consumer=ContextConsumer(
            "vlm-worker",
            capabilities={"attachments": {"image": True}},
        )
    ).async_read("inspect the chart")

    assert package.blocks == ()
    assert package.omissions[0].reason == "source_read_failed"
    assert any(
        item.code == "context.image_attachment_read_failed"
        for item in package.diagnostics
    )


@pytest.mark.asyncio
async def test_generic_attachment_capability_does_not_imply_image_understanding() -> None:
    candidate = _candidate(
        "assets/chart.png",
        metadata={
            "path": "assets/chart.png",
            "media_type": "image/png",
            "content_kind": "image",
        },
    )
    source = MediaContextSource(
        candidate,
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    )
    context = TaskContext("task-generic-attachment-not-vlm")
    context.attach(source, binding_id="binding:image")

    package = await context.reader(
        consumer=ContextConsumer(
            "attachment-worker",
            capabilities={"attachments": True},
        )
    ).async_read("inspect the chart")

    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert source.read_refs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_ref", "content_kind", "media_type"),
    [
        ("artifacts/archive.zip", "binary", "application/zip"),
        ("artifacts/custom.pkg", "archive", "application/x-custom"),
        ("artifacts/cache.bin", "unknown", "application/octet-stream"),
    ],
)
async def test_context_reader_keeps_unknown_binary_as_filename_only(
    source_ref: str,
    content_kind: str,
    media_type: str,
) -> None:
    candidate = _candidate(
        source_ref,
        summary="decoded binary guess",
        metadata={
            "path": source_ref,
            "media_type": media_type,
            "content_kind": content_kind,
        },
    )
    source = MediaContextSource(candidate, "must not be read")
    context = TaskContext(f"task-{content_kind}-ref-only")
    context.attach(source, binding_id=f"binding:{content_kind}")

    package = await context.reader(consumer="worker").async_read("inspect files")

    assert package.blocks[0].content is None
    assert package.blocks[0].source_ref == source_ref
    assert source.read_refs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_ref", "media_type"),
    [
        ("assets/unlabeled.png", ""),
        ("artifacts/unlabeled.zip", ""),
        ("artifacts/payload", "application/octet-stream"),
    ],
)
async def test_context_reader_classifies_non_text_files_without_source_kind_hint(
    source_ref: str,
    media_type: str,
) -> None:
    metadata = {"path": source_ref}
    if media_type:
        metadata["media_type"] = media_type
    candidate = _candidate(
        source_ref,
        summary="untrusted decoded payload guess",
        metadata=metadata,
    )
    source = MediaContextSource(candidate, b"raw bytes must not be read")
    selector = RecordingSelector("all")
    context = TaskContext(f"task-conservative-file-kind-{source_ref}")
    context.attach(source, binding_id="binding:unlabeled-media")

    package = await context.reader(
        consumer="text-worker",
        semantic_selector=selector,
    ).async_read("inspect available files")

    assert selector.calls[0][1][0].summary == source_ref
    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert source.read_refs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_ref",
    [
        "src/main.cjs",
        "src/main.cts",
        "src/main.go",
        "src/main.mjs",
        "src/main.mts",
        "src/types.pyi",
        "src/main.c",
        "src/main.cc",
        "src/main.cpp",
        "include/main.h",
        "include/main.hpp",
    ],
)
async def test_context_reader_classifies_mainstream_source_code_as_text(
    source_ref: str,
) -> None:
    candidate = _candidate(
        source_ref,
        summary="source code",
        metadata={"path": source_ref},
    )
    source = MediaContextSource(candidate, "int main() { return 0; }")
    context = TaskContext(f"task-source-code-{source_ref}")
    context.attach(source, binding_id="binding:source-code")

    package = await context.reader(consumer="code-worker").async_read("read source")

    assert package.blocks[0].content == "int main() { return 0; }"
    assert package.blocks[0].completeness == "complete"
    assert source.read_refs == [source_ref]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_ref", "media_type"),
    [
        ("reports/disguised.pdf", "application/pdf"),
        ("assets/disguised.png", "image/png"),
        ("artifacts/disguised.zip", "application/zip"),
    ],
)
async def test_context_reader_fails_closed_on_text_claim_for_non_text_file(
    source_ref: str,
    media_type: str,
) -> None:
    candidate = _candidate(
        source_ref,
        summary="guessed non-text content",
        metadata={
            "path": source_ref,
            "media_type": media_type,
            "content_kind": "text",
            "summary": "guessed non-text content",
        },
    )
    source = MediaContextSource(candidate, "content that must remain cold")
    context = TaskContext(f"task-conflicting-type-{source_ref}")
    context.attach(source, binding_id="binding:conflicting-type")

    package = await context.reader(consumer="text-worker").async_read("inspect file")

    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert "summary" not in package.blocks[0].metadata
    assert source.read_refs == []


@pytest.mark.asyncio
async def test_context_reader_keeps_unparsed_document_bytes_ref_only() -> None:
    candidate = _candidate(
        "reports/raw.pdf",
        metadata={
            "path": "reports/raw.pdf",
            "media_type": "application/pdf",
        },
    )
    source = MediaContextSource(candidate, b"%PDF raw bytes")
    context = TaskContext("task-unparsed-pdf")
    context.attach(source, binding_id="binding:pdf")

    package = await context.reader(consumer="text-worker").async_read("read report")

    assert package.blocks[0].source_ref == "reports/raw.pdf"
    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert package.blocks[0].metadata["context_representation"] == "metadata_only"
    assert source.read_refs == []
    assert any(
        item.code == "context.media_content_ref_only"
        for item in package.diagnostics
    )


@pytest.mark.asyncio
async def test_context_reader_keeps_unverified_document_text_ref_only() -> None:
    candidate = _candidate(
        "reports/unverified.pdf",
        summary="untrusted document content guess",
        metadata={
            "path": "reports/unverified.pdf",
            "media_type": "application/pdf",
            "content_kind": "pdf",
            "summary": "untrusted document content guess",
        },
    )
    source = MediaContextSource(candidate, "text without parser provenance")
    context = TaskContext("task-unverified-document-text")
    context.attach(source, binding_id="binding:unverified-pdf")

    package = await context.reader(consumer="text-worker").async_read("read report")

    assert package.blocks[0].source_ref == "reports/unverified.pdf"
    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert package.blocks[0].metadata["context_representation"] == "metadata_only"
    assert "summary" not in package.blocks[0].metadata
    assert source.read_refs == []


@pytest.mark.asyncio
async def test_context_reader_rejects_document_exact_read_that_drops_parser_proof() -> None:
    candidate = _candidate(
        "reports/provenance-dropped.pdf",
        metadata={
            "path": "reports/provenance-dropped.pdf",
            "media_type": "application/pdf",
            "content_kind": "pdf",
            "context_representation": "parsed_text",
        },
    )
    source = DroppedDocumentProofContextSource(candidate, "unused")
    context = TaskContext("task-document-proof-dropped")
    context.attach(source, binding_id="binding:proof-dropped")

    package = await context.reader(consumer="text-worker").async_read("read report")

    assert package.blocks == ()
    assert package.omissions[0].reason == "source_read_failed"
    assert source.representations == ["parsed_text"]
    assert any(
        item.code == "context.source_read_failed"
        and item.details["error_type"] == "ValueError"
        for item in package.diagnostics
    )


@pytest.mark.asyncio
async def test_context_reader_hides_unverified_direct_document_guess() -> None:
    context = TaskContext("task-direct-unverified-document")
    context.put(
        role="information",
        content="guessed document body",
        entry_id="entry:unverified-document",
        source_ref="reports/direct.pdf",
        metadata={
            "path": "reports/direct.pdf",
            "media_type": "application/pdf",
            "content_kind": "pdf",
            "summary": "guessed direct document contents",
        },
    )

    package = await context.reader(consumer="text-worker").async_read("read report")

    assert package.blocks[0].source_ref == "reports/direct.pdf"
    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"
    assert package.blocks[0].metadata["context_representation"] == "metadata_only"
    assert "summary" not in package.blocks[0].metadata


@pytest.mark.asyncio
async def test_context_reader_keeps_direct_image_bytes_ref_only_without_vlm() -> None:
    context = TaskContext("task-direct-image")
    context.put(
        role="information",
        content=b"\x89PNG raw bytes",
        entry_id="entry:image",
        source_ref="assets/direct.png",
        metadata={
            "path": "assets/direct.png",
            "media_type": "image/png",
            "summary": "guessed direct image contents",
        },
    )

    package = await context.reader(consumer="text-worker").async_read("inspect image")

    assert package.blocks[0].source_ref == "assets/direct.png"
    assert package.blocks[0].content is None
    assert package.blocks[0].completeness == "ref_only"


@pytest.mark.asyncio
async def test_context_reader_rejects_direct_raw_image_bytes_even_for_vlm() -> None:
    context = TaskContext("task-direct-image-vlm")
    context.put(
        role="information",
        content=b"\x89PNG raw bytes",
        entry_id="entry:image",
        source_ref="assets/direct.png",
        metadata={
            "path": "assets/direct.png",
            "media_type": "image/png",
        },
    )

    package = await context.reader(
        consumer=ContextConsumer(
            "vlm-worker",
            capabilities={"attachments": {"image": True}},
        )
    ).async_read("inspect image")

    assert package.blocks == ()
    assert package.omissions[0].reason == "source_read_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_ref", "content_kind", "media_type"),
    [
        ("report.pdf", "pdf", "application/pdf"),
        (
            "forecast.xlsx",
            "office",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
async def test_context_reader_accepts_source_parsed_document_text(
    source_ref: str,
    content_kind: str,
    media_type: str,
) -> None:
    candidate = _candidate(
        source_ref,
        estimated_chars=24,
        metadata={
            "path": source_ref,
            "media_type": media_type,
            "content_kind": content_kind,
            "context_representation": "parsed_text",
        },
    )
    source = MediaContextSource(candidate, "authoritative parsed text")
    context = TaskContext(f"task-{content_kind}-text")
    context.attach(source, binding_id=f"binding:{content_kind}")

    package = await context.reader(consumer="worker").async_read("read report")

    assert package.blocks[0].content == "authoritative parsed text"
    assert source.representations == ["parsed_text"]


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
        async def async_read_exact(
            self,
            source_ref: str,
            *,
            max_chars: int,
            representation: str | None = None,
            range_start: int = 0,
        ) -> ContextSourceRead:
            if representation != "lossy_digest":
                return await super().async_read_exact(
                    source_ref,
                    max_chars=max_chars,
                    representation=representation,
                    range_start=range_start,
                )
            content = "Lossy core digest with scoped refs"[:max_chars]
            return ContextSourceRead(
                source_id=self.source_id,
                source_revision=self.source_revision,
                source_ref=source_ref,
                content=content,
                completeness="lossy",
                refs=(source_ref, "skill/oversized-core#section-1"),
                metadata={
                    "original_chars": 10000,
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

    assert source.enumerate_calls == 2
    assert package.source_revisions == {"binding:memory": "rev:2"}
    assert [block.source_ref for block in package.blocks] == ["docs/one"]


@pytest.mark.asyncio
async def test_reader_does_not_rebase_structural_task_context_mutation_during_listing() -> None:
    candidate = _candidate("docs/one")
    context = TaskContext("task-1")

    class StructurallyMutatingListSource(MemoryContextSource):
        async def async_enumerate_descriptors(
            self,
            *,
            profile: Mapping[str, Any],
            cursor: str | None,
            limit: int,
        ) -> ContextSourceDescriptorPage:
            page = await super().async_enumerate_descriptors(
                profile=profile,
                limit=limit,
                cursor=cursor,
            )
            if self.enumerate_calls == 1:
                context.put(
                    role="information",
                    content="concurrent structural entry",
                    entry_id="entry:concurrent",
                )
            return page

    source = StructurallyMutatingListSource(
        [candidate],
        {"docs/one": _source_block("docs/one")},
    )
    context.attach(source, binding_id="binding:memory")
    reader = context.reader(consumer="worker")

    with pytest.raises(ContextStaleError, match="TaskContext revision"):
        await reader.async_read("Read the source")

    assert source.enumerate_calls == 1


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
