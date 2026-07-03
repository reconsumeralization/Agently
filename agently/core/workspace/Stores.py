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

import inspect
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, AsyncIterator, Literal, cast

from agently.types.data.workspace import WorkspaceContentSegment, WorkspaceRecordRef, WorkspaceReferenceEnvelope

from .Errors import WorkspacePolicyError


VectorSimilarity = Literal["cosine", "dot", "l2"]
EmbeddingFunction = Callable[
    [str | list[str]],
    Sequence[float] | Sequence[Sequence[float]] | Awaitable[Sequence[float] | Sequence[Sequence[float]]],
]


class LocalWorkspacePolicyEngine:
    def __init__(self, content_root: Path, *, read_only: bool = False):
        self.content_root = content_root
        self.read_only = read_only

    def ensure_writable(self) -> None:
        if self.read_only:
            raise WorkspacePolicyError("Workspace is configured read-only.")

    def resolve_content_path(self, path: str | Path):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.content_root / candidate
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(self.content_root)
        except ValueError as error:
            raise WorkspacePolicyError(f"Path is outside workspace content root: { path }") from error
        return resolved

    async def filter_records(
        self,
        records: list[WorkspaceRecordRef],
        *,
        purpose: str = "prompt",
    ) -> list[WorkspaceRecordRef]:
        _ = purpose
        return records


class LocalContentStore:
    def __init__(self, content_root: Path, policy: LocalWorkspacePolicyEngine):
        self.content_root = content_root
        self.policy = policy

    def ensure_collection(self, collection: str):
        collection_path = self.content_root / collection
        collection_path.mkdir(parents=True, exist_ok=True)
        return collection_path

    async def write_content(self, relative_path: str, content: bytes) -> str:
        self.policy.ensure_writable()
        target = self.policy.resolve_content_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return str(target.relative_to(self.content_root))

    async def read_content(self, path: str) -> Any:
        target = self.policy.resolve_content_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Workspace content not found: { path }")
        return target.read_text(encoding="utf-8", errors="replace")

    async def read_content_segment(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment:
        target = self.policy.resolve_content_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Workspace content not found: { path }")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0.")
        if limit is not None and limit < 0:
            raise ValueError("limit must be greater than or equal to 0.")
        total_size = target.stat().st_size
        read_size = max(0, total_size - offset) if limit is None else limit
        with target.open("rb") as file:
            file.seek(offset)
            raw = file.read(read_size)
        placeholder_ref: WorkspaceReferenceEnvelope = {
            "workspace_id": "",
            "kind": "content",
            "collection": "",
            "record_id": "",
            "version": None,
            "content_ref": path,
            "digest": None,
            "size": total_size,
            "created_at": "",
            "policy_labels": [],
            "backend_capabilities": {},
        }
        segment: WorkspaceContentSegment = {
            "ref": placeholder_ref,
            "content": raw.decode("utf-8", errors="replace"),
            "offset": offset,
            "size": len(raw),
            "total_size": total_size,
            "eof": offset + len(raw) >= total_size,
            "digest": None,
            "content_type": "text/plain",
        }
        return segment

    async def stream_content(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0.")
        remaining = limit
        current_offset = offset
        while remaining is None or remaining > 0:
            next_limit = chunk_size if remaining is None else min(chunk_size, remaining)
            segment = await self.read_content_segment(path, offset=current_offset, limit=next_limit)
            if segment["size"] == 0:
                break
            yield segment
            current_offset += segment["size"]
            if remaining is not None:
                remaining -= segment["size"]
            if segment["eof"]:
                break


class NoopVectorIndex:
    name = "noop"

    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
        _ = (ref, content)

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        _ = (query, filters, limit)
        return []


class LocalVectorIndex:
    name = "local_vector"

    def __init__(
        self,
        embedding_function: EmbeddingFunction,
        *,
        similarity: VectorSimilarity = "cosine",
    ):
        self.embedding_function = embedding_function
        self.similarity: VectorSimilarity = similarity if similarity in {"cosine", "dot", "l2"} else "cosine"
        self._records: dict[str, tuple[WorkspaceRecordRef, list[float]]] = {}

    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
        vector = await self._embed_one(content)
        if vector:
            self._records[ref["id"]] = (ref, vector)

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        query_vector = await self._embed_one(query)
        if not query_vector:
            return []
        scored: list[tuple[float, WorkspaceRecordRef]] = []
        for ref, vector in self._records.values():
            if not self._matches_filters(ref, filters or {}):
                continue
            scored.append((self._score(query_vector, vector), ref))
        scored.sort(key=lambda item: item[0], reverse=True)
        if limit is not None:
            scored = scored[: max(0, limit)]
        return [ref for _, ref in scored]

    async def _embed_one(self, text: str) -> list[float]:
        try:
            result = self.embedding_function([text])
        except TypeError:
            result = self.embedding_function(text)
        if inspect.isawaitable(result):
            result = await cast(Awaitable[Sequence[float] | Sequence[Sequence[float]]], result)
        return self._coerce_one_vector(result)

    def _coerce_one_vector(self, value: Sequence[float] | Sequence[Sequence[float]]) -> list[float]:
        if not value:
            return []
        first = value[0]
        if isinstance(first, Sequence) and not isinstance(first, (str, bytes)):
            return [float(item) for item in first]
        return [float(item) for item in cast(Sequence[float], value)]

    def _score(self, left: Sequence[float], right: Sequence[float]) -> float:
        size = min(len(left), len(right))
        if size == 0:
            return float("-inf")
        left_values = [float(value) for value in left[:size]]
        right_values = [float(value) for value in right[:size]]
        if self.similarity == "dot":
            return sum(a * b for a, b in zip(left_values, right_values))
        if self.similarity == "l2":
            return -math.sqrt(sum((a - b) ** 2 for a, b in zip(left_values, right_values)))
        left_norm = math.sqrt(sum(value * value for value in left_values))
        right_norm = math.sqrt(sum(value * value for value in right_values))
        if left_norm == 0 or right_norm == 0:
            return float("-inf")
        return sum(a * b for a, b in zip(left_values, right_values)) / (left_norm * right_norm)

    def _matches_filters(self, ref: WorkspaceRecordRef, filters: Mapping[str, Any]) -> bool:
        for key, expected in filters.items():
            actual = self._resolve_ref_filter_value(ref, str(key))
            if isinstance(expected, (list, tuple, set, frozenset)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _resolve_ref_filter_value(self, ref: WorkspaceRecordRef, key: str) -> Any:
        if key.startswith("scope."):
            return self._mapping_dot_get(ref.get("scope") or {}, key.removeprefix("scope."))
        if key.startswith("meta."):
            return self._mapping_dot_get(ref.get("meta") or {}, key.removeprefix("meta."))
        return ref.get(key)

    def _mapping_dot_get(self, mapping: Mapping[str, Any], key: str) -> Any:
        current: Any = mapping
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return current
