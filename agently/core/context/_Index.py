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

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Any

from agently.types.data import (
    ContextReadIntent,
    ContextSourceBindingSnapshot,
    ContextSourceChangeSet,
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
)
from agently.types.plugins import ContextSource, ContextSourceChangeFeed


class _UnknownContextSourceKindError(ValueError):
    pass


class _RequiredVectorUnavailableError(RuntimeError):
    pass


class _InvalidContextIndexQueryError(ValueError):
    pass


@dataclass(frozen=True)
class _ContextIndexProfile:
    schema_version: str = "context-index/v1"
    projection_profile: str = "default"
    candidate_strategy: str = "structural"
    embedding_identity: str = "none"

    def source_projection(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "projection_profile": self.projection_profile,
        }


@dataclass(frozen=True)
class _PartitionKey:
    source_id: str
    source_revision: str
    schema_version: str
    projection_profile: str
    embedding_identity: str


@dataclass(frozen=True)
class _Partition:
    key: _PartitionKey
    descriptors: tuple[ContextSourceDescriptor, ...]
    vectors: tuple[tuple[float, ...], ...] | None = None
    vector_error: str | None = None
    embedding_input_tokens: int | None = None
    embedding_input_chars: int = 0
    embedding_build_texts: int = 0
    sync_mode: str = "full"
    sync_fallback: str | None = None


@dataclass(frozen=True)
class _ContextIndexMatch:
    binding: ContextSourceBindingSnapshot
    descriptor: ContextSourceDescriptor


@dataclass(frozen=True)
class _ContextIndexQueryResult:
    matches: tuple[_ContextIndexMatch, ...]
    source_coverage: Mapping[str, Mapping[str, Any]]
    next_offsets: Mapping[str, int]
    source_failures: Mapping[str, Mapping[str, str]]
    diagnostics: Mapping[str, Any]


class _ContextIndexPartitionCache:
    """Process-local immutable partition cache with one-flight construction."""

    def __init__(self) -> None:
        self._partitions: dict[_PartitionKey, _Partition] = {}
        self._inflight: dict[_PartitionKey, asyncio.Future[_Partition]] = {}
        self._lock = asyncio.Lock()

    async def get_or_build(
        self,
        key: _PartitionKey,
        build: Callable[[], Awaitable[_Partition]],
    ) -> tuple[_Partition, bool]:
        async with self._lock:
            cached = self._partitions.get(key)
            if cached is not None:
                return cached, True
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.ensure_future(build())
                self._inflight[key] = task
        try:
            partition = await task
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
        async with self._lock:
            cached = self._partitions.setdefault(key, partition)
        return cached, False

    async def find_compatible(self, key: _PartitionKey) -> _Partition | None:
        async with self._lock:
            partitions = tuple(self._partitions.values())
        for partition in reversed(partitions):
            candidate = partition.key
            if (
                candidate.source_id == key.source_id
                and candidate.source_revision != key.source_revision
                and candidate.schema_version == key.schema_version
                and candidate.projection_profile == key.projection_profile
                and candidate.embedding_identity == key.embedding_identity
            ):
                return partition
        return None


_PARTITION_CACHE = _ContextIndexPartitionCache()

_CALLER_MECHANISM_FILTERS = frozenset(
    {"method", "selection", "rerank", "top_n", "max_candidates"}
)


def _offered_source_kinds(intent: ContextReadIntent) -> frozenset[str] | None:
    raw = intent.filters.get("source_kinds")
    if raw is None:
        return None
    if isinstance(raw, str):
        values = (raw,)
    elif isinstance(raw, Sequence) and not isinstance(raw, bytes | bytearray):
        values = tuple(str(item) for item in raw)
    else:
        raise ValueError("Context source_kinds filter must be a string or sequence.")
    normalized = frozenset(item.strip() for item in values if item.strip())
    if not normalized:
        raise ValueError("Context source_kinds filter cannot be empty.")
    return normalized


def _filter_values(value: Any, *, name: str) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        values = tuple(value)
    else:
        values = (value,)
    if not values:
        raise ValueError(f"Context {name} filter cannot be empty.")
    return values


def _descriptor_field(descriptor: ContextSourceDescriptor, key: str) -> Any:
    if key in {"id", "record_id"}:
        return descriptor.metadata.get("record_id", descriptor.source_ref)
    if key == "source_ref":
        return descriptor.source_ref
    if key == "descriptor_key":
        return descriptor.descriptor_key
    if key == "role":
        return descriptor.role
    current: Any = descriptor.metadata
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _matches_exact_filter(actual: Any, expected: Any) -> bool:
    expected_values = _filter_values(expected, name="value")
    if isinstance(actual, Sequence) and not isinstance(actual, str | bytes | bytearray):
        actual_values = tuple(actual)
        return all(item in actual_values for item in expected_values)
    return actual in expected_values


def _matches_structural_filters(
    descriptor: ContextSourceDescriptor,
    filters: Mapping[str, Any],
) -> bool:
    path = str(descriptor.metadata.get("path") or descriptor.source_ref).replace("\\", "/")
    for raw_key, expected in filters.items():
        key = str(raw_key).strip()
        if key == "source_kinds":
            continue
        if key == "path":
            scopes = tuple(
                str(item).strip().replace("\\", "/").rstrip("/")
                for item in _filter_values(expected, name="path")
                if str(item).strip()
            )
            if not scopes or not any(path == scope or path.startswith(f"{scope}/") for scope in scopes):
                return False
            continue
        if key == "pattern":
            patterns = tuple(
                str(item).strip()
                for item in _filter_values(expected, name="pattern")
                if str(item).strip()
            )
            if not patterns or not any(fnmatchcase(path, pattern) for pattern in patterns):
                return False
            continue
        if key == "include_hidden":
            if not isinstance(expected, bool):
                raise ValueError("Context include_hidden filter must be boolean.")
            if not expected and any(part.startswith(".") for part in path.split("/") if part):
                return False
            continue
        if key == "max_file_bytes":
            if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
                raise ValueError("Context max_file_bytes filter must be a positive integer.")
            actual_size = descriptor.metadata.get("total_bytes", descriptor.estimated_chars)
            if not isinstance(actual_size, int) or isinstance(actual_size, bool) or actual_size > expected:
                return False
            continue
        if key == "content_contains":
            terms = tuple(
                str(item).casefold()
                for item in _filter_values(expected, name="content_contains")
                if str(item)
            )
            content = str(descriptor.index_text or "").casefold()
            if not terms or not all(term in content for term in terms):
                return False
            continue
        if not key:
            raise ValueError("Context structural filter key cannot be empty.")
        if not _matches_exact_filter(_descriptor_field(descriptor, key), expected):
            return False
    return True


def _authorized_descriptors(
    descriptors: tuple[ContextSourceDescriptor, ...],
    *,
    binding: ContextSourceBindingSnapshot,
    intent: ContextReadIntent,
) -> tuple[ContextSourceDescriptor, ...]:
    allowed_refs_raw = binding.metadata.get("allowed_refs")
    allowed_refs: frozenset[str] | None = None
    if allowed_refs_raw is not None:
        if not isinstance(allowed_refs_raw, Sequence) or isinstance(
            allowed_refs_raw,
            str | bytes | bytearray,
        ):
            raise ValueError("Context binding allowed_refs must be a sequence.")
        allowed_refs = frozenset(str(item) for item in allowed_refs_raw)
    return tuple(
        descriptor
        for descriptor in descriptors
        if (not intent.roles or descriptor.role in intent.roles)
        and (allowed_refs is None or descriptor.source_ref in allowed_refs)
        and _matches_structural_filters(descriptor, intent.filters)
    )


class _ContextIndex:
    """TaskContext-owned logical view over reusable source-revision partitions."""

    def __init__(
        self,
        *,
        profile: _ContextIndexProfile | None = None,
        embedding_provider: Any = None,
    ) -> None:
        self.profile = profile or _ContextIndexProfile()
        self.embedding_provider = embedding_provider

    @staticmethod
    def embedding_identity(embedding_provider: Any) -> str:
        if embedding_provider is None:
            return "none"
        provider_id = str(
            getattr(embedding_provider, "provider_id", None)
            or getattr(embedding_provider, "name", None)
            or f"{embedding_provider.__class__.__module__}."
            f"{embedding_provider.__class__.__qualname__}"
        )
        model = str(getattr(embedding_provider, "model", None) or "default")
        return f"{provider_id}:{model}"

    @staticmethod
    def _observed_input_tokens(embedding_provider: Any) -> int | None:
        usage = getattr(embedding_provider, "last_usage", None)
        if not isinstance(usage, Mapping):
            return None
        value = usage.get("input_tokens")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        return value

    def _partition_key(self, binding: ContextSourceBindingSnapshot) -> _PartitionKey:
        return _PartitionKey(
            source_id=binding.source_id,
            source_revision=binding.source_revision,
            schema_version=self.profile.schema_version,
            projection_profile=self.profile.projection_profile,
            embedding_identity=self.profile.embedding_identity,
        )

    async def _build_partition(
        self,
        source: ContextSource,
        binding: ContextSourceBindingSnapshot,
    ) -> _Partition:
        descriptors: list[ContextSourceDescriptor] = []
        descriptor_keys: set[str] = set()
        cursors: set[str] = set()
        cursor: str | None = None
        profile = self.profile.source_projection()
        while True:
            page = await source.async_enumerate_descriptors(
                profile=profile,
                cursor=cursor,
                limit=256,
            )
            if not isinstance(page, ContextSourceDescriptorPage):
                raise TypeError(
                    "ContextSource.async_enumerate_descriptors must return "
                    "ContextSourceDescriptorPage."
                )
            if page.source_id != binding.source_id:
                raise ValueError("Context descriptor page source_id changed.")
            if page.source_revision != binding.source_revision:
                raise ValueError("Context descriptor page source_revision changed.")
            for descriptor in page.descriptors:
                if descriptor.descriptor_key in descriptor_keys:
                    raise ValueError(
                        "Context descriptor pages cannot repeat descriptor_key values."
                    )
                descriptor_keys.add(descriptor.descriptor_key)
                descriptors.append(descriptor)
            if page.next_cursor is None:
                break
            if page.next_cursor in cursors:
                raise ValueError("Context descriptor cursor did not advance.")
            cursors.add(page.next_cursor)
            cursor = page.next_cursor
        vectors: tuple[tuple[float, ...], ...] | None = None
        vector_error: str | None = None
        embedding_input_tokens: int | None = None
        embedding_input_chars = 0
        if self.profile.candidate_strategy == "hybrid":
            if self.embedding_provider is None:
                vector_error = "embedding provider unavailable"
            elif descriptors:
                texts = [
                    descriptor.index_text
                    or f"{descriptor.title}\n{descriptor.summary}"
                    for descriptor in descriptors
                ]
                embedding_input_chars = sum(len(text) for text in texts)
                try:
                    raw_vectors = await self.embedding_provider.embed_texts(texts)
                    if len(raw_vectors) != len(descriptors):
                        raise ValueError(
                            "embedding provider returned a different vector count"
                        )
                    vectors = tuple(
                        tuple(float(value) for value in vector)
                        for vector in raw_vectors
                    )
                    if any(not vector for vector in vectors):
                        raise ValueError("embedding provider returned an empty vector")
                    embedding_input_tokens = self._observed_input_tokens(
                        self.embedding_provider
                    )
                except Exception as error:
                    vectors = None
                    vector_error = f"{error.__class__.__name__}: {error}"
        return _Partition(
            key=self._partition_key(binding),
            descriptors=tuple(descriptors),
            vectors=vectors,
            vector_error=vector_error,
            embedding_input_tokens=embedding_input_tokens,
            embedding_input_chars=embedding_input_chars,
            embedding_build_texts=(
                len(descriptors)
                if self.profile.candidate_strategy == "hybrid" and descriptors
                else 0
            ),
            sync_mode="full",
        )

    async def _build_or_sync_partition(
        self,
        source: ContextSource,
        binding: ContextSourceBindingSnapshot,
    ) -> _Partition:
        key = self._partition_key(binding)
        previous = await _PARTITION_CACHE.find_compatible(key)
        if previous is None or not isinstance(source, ContextSourceChangeFeed):
            return await self._build_partition(source, binding)
        try:
            change_set = await source.async_changes(
                from_revision=previous.key.source_revision,
                to_revision=binding.source_revision,
                profile=self.profile.source_projection(),
            )
            if not isinstance(change_set, ContextSourceChangeSet):
                raise TypeError(
                    "ContextSourceChangeFeed.async_changes must return "
                    "ContextSourceChangeSet."
                )
            if (
                change_set.source_id != binding.source_id
                or change_set.from_revision != previous.key.source_revision
                or change_set.to_revision != binding.source_revision
            ):
                raise ValueError("Context source change-set identity changed.")
        except Exception as error:
            rebuilt = await self._build_partition(source, binding)
            return replace(
                rebuilt,
                sync_mode="full_after_delta_failure",
                sync_fallback=f"{error.__class__.__name__}: {error}",
            )

        descriptor_order = [item.descriptor_key for item in previous.descriptors]
        descriptors = {
            item.descriptor_key: replace(
                item,
                source_revision=binding.source_revision,
            )
            for item in previous.descriptors
        }
        for change in change_set.changes:
            if change.operation == "remove":
                descriptors.pop(change.descriptor_key, None)
                descriptor_order = [
                    key for key in descriptor_order if key != change.descriptor_key
                ]
                continue
            descriptor = change.descriptor
            if descriptor is None:
                raise ValueError("Context descriptor upsert is missing its descriptor.")
            if change.descriptor_key not in descriptors:
                descriptor_order.append(change.descriptor_key)
            descriptors[change.descriptor_key] = descriptor
        resolved_descriptors = tuple(
            descriptors[descriptor_key]
            for descriptor_key in descriptor_order
            if descriptor_key in descriptors
        )

        vectors: tuple[tuple[float, ...], ...] | None = None
        vector_error: str | None = None
        embedding_input_tokens: int | None = None
        embedding_input_chars = 0
        embedding_build_texts = 0
        if self.profile.candidate_strategy == "hybrid":
            previous_vectors = (
                {
                    descriptor.descriptor_key: vector
                    for descriptor, vector in zip(
                        previous.descriptors,
                        previous.vectors,
                    )
                }
                if previous.vectors is not None
                else {}
            )
            previous_descriptors = {
                descriptor.descriptor_key: descriptor
                for descriptor in previous.descriptors
            }
            changed = tuple(
                descriptor
                for descriptor in resolved_descriptors
                if descriptor.descriptor_key not in previous_vectors
                or (
                    previous_descriptors[descriptor.descriptor_key].index_text
                    != descriptor.index_text
                )
                or (
                    previous_descriptors[descriptor.descriptor_key].content_digest
                    != descriptor.content_digest
                )
            )
            changed_vectors: dict[str, tuple[float, ...]] = {}
            if self.embedding_provider is None:
                vector_error = "embedding provider unavailable"
            elif changed:
                texts = [
                    descriptor.index_text
                    or f"{descriptor.title}\n{descriptor.summary}"
                    for descriptor in changed
                ]
                embedding_input_chars = sum(len(text) for text in texts)
                embedding_build_texts = len(texts)
                try:
                    raw_vectors = await self.embedding_provider.embed_texts(texts)
                    if len(raw_vectors) != len(changed):
                        raise ValueError(
                            "embedding provider returned a different vector count"
                        )
                    changed_vectors = {
                        descriptor.descriptor_key: tuple(
                            float(value) for value in vector
                        )
                        for descriptor, vector in zip(changed, raw_vectors)
                    }
                    if any(not vector for vector in changed_vectors.values()):
                        raise ValueError("embedding provider returned an empty vector")
                    embedding_input_tokens = self._observed_input_tokens(
                        self.embedding_provider
                    )
                except Exception as error:
                    vector_error = f"{error.__class__.__name__}: {error}"
            if vector_error is None:
                vectors = tuple(
                    (
                        changed_vectors[descriptor.descriptor_key]
                        if descriptor.descriptor_key in changed_vectors
                        else previous_vectors[descriptor.descriptor_key]
                    )
                    for descriptor in resolved_descriptors
                )
        return _Partition(
            key=key,
            descriptors=resolved_descriptors,
            vectors=vectors,
            vector_error=vector_error,
            embedding_input_tokens=embedding_input_tokens,
            embedding_input_chars=embedding_input_chars,
            embedding_build_texts=embedding_build_texts,
            sync_mode="delta",
        )

    @staticmethod
    def _lexical_score(query: str, descriptor: ContextSourceDescriptor) -> float:
        terms = frozenset(re.findall(r"[\w.-]+", query.casefold()))
        if not terms:
            return 0.0
        text = (
            descriptor.index_text
            or f"{descriptor.title}\n{descriptor.summary}"
        ).casefold()
        return float(sum(text.count(term) for term in terms))

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return numerator / (left_norm * right_norm)

    async def _rank_descriptors(
        self,
        descriptors: tuple[ContextSourceDescriptor, ...],
        *,
        partition: _Partition,
        intent: ContextReadIntent,
    ) -> tuple[
        tuple[ContextSourceDescriptor, ...],
        str,
        int,
        int | None,
        int,
        str | None,
    ]:
        requested = self.profile.candidate_strategy
        if not descriptors:
            return (), requested, 0, None, 0, None
        if requested == "structural":
            return descriptors, "structural", 0, 0, 0, None
        lexical_scores = {
            descriptor.descriptor_key: self._lexical_score(intent.query, descriptor)
            for descriptor in descriptors
        }
        if requested == "lexical":
            ranked = tuple(
                sorted(
                    descriptors,
                    key=lambda item: (
                        -lexical_scores[item.descriptor_key],
                        -item.priority,
                        item.descriptor_key,
                    ),
                )
            )
            return ranked, "lexical", 0, 0, 0, None
        vector_policy = str(intent.metadata.get("vector_policy") or "optional").strip()
        if partition.vectors is None:
            if vector_policy == "required":
                raise _RequiredVectorUnavailableError(
                    "required vector Context recall is unavailable: "
                    + str(partition.vector_error or "unknown error")
                )
            ranked = tuple(
                sorted(
                    descriptors,
                    key=lambda item: (
                        -lexical_scores[item.descriptor_key],
                        -item.priority,
                        item.descriptor_key,
                    ),
                )
            )
            return ranked, "lexical", 0, 0, 0, partition.vector_error
        if len(descriptors) == 1:
            # Authorization and structural filters already reduced the scope
            # to one canonical descriptor. Once the configured hybrid
            # partition is known to be available, there is no remaining order
            # to improve and no reason to spend a query embedding.
            return descriptors, "hybrid", 0, 0, 0, None
        query_chars = len(intent.query)
        try:
            raw_query_vectors = await self.embedding_provider.embed_texts([intent.query])
            if len(raw_query_vectors) != 1 or not raw_query_vectors[0]:
                raise ValueError("embedding provider returned no query vector")
            query_vector = tuple(float(value) for value in raw_query_vectors[0])
            query_tokens = self._observed_input_tokens(self.embedding_provider)
        except Exception as error:
            if vector_policy == "required":
                raise _RequiredVectorUnavailableError(
                    f"required vector Context recall is unavailable: {error}"
                ) from error
            ranked = tuple(
                sorted(
                    descriptors,
                    key=lambda item: (
                        -lexical_scores[item.descriptor_key],
                        -item.priority,
                        item.descriptor_key,
                    ),
                )
            )
            return ranked, "lexical", 1, None, query_chars, str(error)
        vector_by_key = {
            descriptor.descriptor_key: vector
            for descriptor, vector in zip(partition.descriptors, partition.vectors)
        }
        ranked = tuple(
            sorted(
                descriptors,
                key=lambda item: (
                    -(
                        self._cosine(query_vector, vector_by_key[item.descriptor_key])
                        + min(1.0, lexical_scores[item.descriptor_key])
                    ),
                    -item.priority,
                    item.descriptor_key,
                ),
            )
        )
        return ranked, "hybrid", 1, query_tokens, query_chars, None

    async def async_query(
        self,
        *,
        bindings: Sequence[tuple[ContextSourceBindingSnapshot, ContextSource]],
        intent: ContextReadIntent,
        offsets: Mapping[str, int],
        limit: int,
    ) -> _ContextIndexQueryResult:
        if limit <= 0:
            raise ValueError("Context index query limit must be positive.")
        mechanism_filters = sorted(
            key
            for key in _CALLER_MECHANISM_FILTERS
            if intent.filters.get(key) is not None
        )
        if mechanism_filters:
            raise _InvalidContextIndexQueryError(
                "Context retrieval mechanisms are owned by ContextIndex; remove: "
                + ", ".join(mechanism_filters)
            )
        offered_kinds = _offered_source_kinds(intent)
        available_kinds = frozenset(binding.source_kind for binding, _source in bindings)
        if offered_kinds is not None:
            unknown_kinds = offered_kinds - available_kinds
            if unknown_kinds:
                raise _UnknownContextSourceKindError(
                    "unknown TaskContext source kind(s): "
                    + ", ".join(sorted(unknown_kinds))
                )
        matches: list[_ContextIndexMatch] = []
        coverage: dict[str, Mapping[str, Any]] = {}
        next_offsets: dict[str, int] = {}
        source_failures: dict[str, Mapping[str, str]] = {}
        cache_states: list[bool] = []
        sync_modes: list[str] = []
        sync_fallbacks: list[str] = []
        effective_strategies: list[str] = []
        vector_errors: list[str] = []
        embedding_build_texts = 0
        embedding_build_chars = 0
        embedding_query_texts = 0
        embedding_query_chars = 0
        observed_token_parts: list[int] = []
        token_coverage_complete = True
        for binding, source in bindings:
            if offered_kinds is not None and binding.source_kind not in offered_kinds:
                continue
            key = self._partition_key(binding)
            try:
                partition, cache_hit = await _PARTITION_CACHE.get_or_build(
                    key,
                    lambda source=source, binding=binding: self._build_or_sync_partition(
                        source,
                        binding,
                    ),
                )
            except Exception as error:
                source_failures[binding.binding_id] = {
                    "source_id": binding.source_id,
                    "error_type": error.__class__.__name__,
                    "error": str(error),
                }
                continue
            cache_states.append(cache_hit)
            sync_modes.append("cache" if cache_hit else partition.sync_mode)
            if not cache_hit and partition.sync_fallback:
                sync_fallbacks.append(partition.sync_fallback)
            if not cache_hit and self.profile.candidate_strategy == "hybrid":
                embedding_build_texts += partition.embedding_build_texts
                embedding_build_chars += partition.embedding_input_chars
                if partition.embedding_input_tokens is None:
                    token_coverage_complete = False
                else:
                    observed_token_parts.append(partition.embedding_input_tokens)
            authorized = _authorized_descriptors(
                partition.descriptors,
                binding=binding,
                intent=intent,
            )
            (
                authorized,
                effective_strategy,
                query_texts,
                query_tokens,
                query_chars,
                vector_error,
            ) = await self._rank_descriptors(
                authorized,
                partition=partition,
                intent=intent,
            )
            effective_strategies.append(effective_strategy)
            embedding_query_texts += query_texts
            embedding_query_chars += query_chars
            if query_texts:
                if query_tokens is None:
                    token_coverage_complete = False
                else:
                    observed_token_parts.append(query_tokens)
            if vector_error:
                vector_errors.append(vector_error)
            offset = offsets.get(binding.binding_id, 0)
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ValueError("Context index offset must be a non-negative integer.")
            explicit_refs = frozenset(intent.explicit_refs)
            anchors = tuple(
                descriptor
                for descriptor in authorized
                if descriptor.required or descriptor.source_ref in explicit_refs
            )
            optional = tuple(
                descriptor
                for descriptor in authorized
                if not descriptor.required and descriptor.source_ref not in explicit_refs
            )
            optional_limit = max(0, limit - len(anchors))
            optional_page = optional[offset : offset + optional_limit]
            page = (*anchors, *optional_page)
            next_offset = offset + len(optional_page)
            exhaustive = next_offset >= len(optional)
            next_offsets[binding.binding_id] = next_offset
            coverage[binding.binding_id] = {
                "scope": {
                    "source_kind": binding.source_kind,
                    "strategy": self.profile.candidate_strategy,
                    "offset": offset,
                },
                "returned_candidates": len(page),
                "exhaustive": exhaustive,
                "continuation_available": not exhaustive,
            }
            matches.extend(
                _ContextIndexMatch(binding=binding, descriptor=descriptor)
                for descriptor in page
            )
        return _ContextIndexQueryResult(
            matches=tuple(matches),
            source_coverage=coverage,
            next_offsets=next_offsets,
            source_failures=source_failures,
            diagnostics={
                "requested_strategy": self.profile.candidate_strategy,
                "effective_strategy": (
                    effective_strategies[0]
                    if len(set(effective_strategies)) == 1 and effective_strategies
                    else "mixed"
                    if effective_strategies
                    else self.profile.candidate_strategy
                ),
                "cache": (
                    "hit"
                    if cache_states and all(cache_states)
                    else "miss"
                    if cache_states and not any(cache_states)
                    else "mixed"
                    if cache_states
                    else "not_applicable"
                ),
                "descriptor_count": sum(
                    int(record["returned_candidates"])
                    for record in coverage.values()
                ),
                "source_failure_count": len(source_failures),
                "embedding_build_texts": embedding_build_texts,
                "embedding_build_chars": embedding_build_chars,
                "embedding_query_texts": embedding_query_texts,
                "embedding_query_chars": embedding_query_chars,
                "embedding_input_tokens": (
                    sum(observed_token_parts)
                    if self.profile.candidate_strategy == "hybrid"
                    and (embedding_build_texts or embedding_query_texts)
                    and token_coverage_complete
                    else None
                ),
                "embedding_token_coverage": (
                    "not_applicable"
                    if self.profile.candidate_strategy != "hybrid"
                    or not (embedding_build_texts or embedding_query_texts)
                    else "observed"
                    if token_coverage_complete
                    else "unavailable"
                ),
                "vector_errors": tuple(vector_errors),
                "sync_mode": (
                    sync_modes[0]
                    if len(set(sync_modes)) == 1 and sync_modes
                    else "mixed"
                    if sync_modes
                    else "not_applicable"
                ),
                "sync_fallbacks": tuple(sync_fallbacks),
            },
        )


__all__: list[str] = []
