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

import importlib
import inspect
import errno
import math
import os
import sqlite3
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, AsyncIterator, Literal, cast

from agently.types.data.workspace import WorkspaceContentSegment, WorkspaceRecordRef, WorkspaceReferenceEnvelope

from .Errors import WorkspaceConfigurationError, WorkspacePolicyError
from ._utils import json_dumps, json_loads


VectorSimilarity = Literal["cosine", "dot", "l2"]
EmbeddingFunction = Callable[
    [str | list[str]],
    Sequence[float] | Sequence[Sequence[float]] | Awaitable[Sequence[float] | Sequence[Sequence[float]]],
]


class EmbeddingProviderUnavailableError(RuntimeError):
    """Raised when a vector pipeline has a vector store but no embedder."""


class VectorStoreProviderUnavailableError(RuntimeError):
    """Raised when a vector pipeline has an embedder but no vector store."""


_DESCRIPTOR_RELATIVE_DELETE_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
)


def supports_descriptor_relative_delete() -> bool:
    """Return whether the host can delete owned files without pathname traversal."""

    return _DESCRIPTOR_RELATIVE_DELETE_SUPPORTED


def delete_owned_file_descriptor_relative(
    root: Path,
    relative_path: str | Path,
    *,
    protected_parent_depth: int = 0,
) -> bool:
    """Delete one regular file below an owned root using held directory fds.

    ``protected_parent_depth`` counts path components below ``root`` that must
    remain (for example, the ``lineage`` area root). Missing paths are an
    idempotent no-op. Every opened descriptor is closed on every exit path.
    """

    if not supports_descriptor_relative_delete():
        raise WorkspaceConfigurationError(
            "Descriptor-relative no-follow Workspace deletion is unavailable on this platform."
        )
    relative = Path(relative_path)
    parts = relative.parts
    if (
        relative.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or protected_parent_depth < 0
        or protected_parent_depth > len(parts) - 1
    ):
        raise WorkspacePolicyError(
            f"Path is outside the owned Workspace root: {relative_path}"
        )

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    descriptors: list[int] = []
    body_failed = False
    try:
        try:
            descriptors.append(os.open(root, directory_flags))
        except FileNotFoundError:
            return False
        except OSError as error:
            raise WorkspacePolicyError(
                f"Workspace owned root is not a directly owned directory: {root}"
            ) from error

        for part in parts[:-1]:
            try:
                descriptors.append(
                    os.open(part, directory_flags, dir_fd=descriptors[-1])
                )
            except FileNotFoundError:
                return False
            except OSError as error:
                raise WorkspacePolicyError(
                    f"Workspace cleanup parent is not a directly owned directory: {relative_path}"
                ) from error

        parent_fd = descriptors[-1]
        final_name = parts[-1]
        try:
            target_stat = os.stat(
                final_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(target_stat.st_mode):
            raise WorkspacePolicyError(
                f"Workspace cleanup target is not a regular file: {relative_path}"
            )
        try:
            os.unlink(final_name, dir_fd=parent_fd)
        except FileNotFoundError:
            return False

        for depth in range(len(parts) - 1, protected_parent_depth, -1):
            try:
                held_child = os.fstat(descriptors[depth])
                named_child = os.stat(
                    parts[depth - 1],
                    dir_fd=descriptors[depth - 1],
                    follow_symlinks=False,
                )
            except OSError:
                break
            if (
                not stat.S_ISDIR(named_child.st_mode)
                or (held_child.st_dev, held_child.st_ino)
                != (named_child.st_dev, named_child.st_ino)
            ):
                break
            try:
                os.rmdir(parts[depth - 1], dir_fd=descriptors[depth - 1])
            except OSError:
                break
        return True
    except BaseException:
        body_failed = True
        raise
    finally:
        close_errors: list[OSError] = []
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as error:
                close_errors.append(error)
        if close_errors and not body_failed:
            raise OSError(
                errno.EIO,
                "Workspace descriptor close failed: "
                + "; ".join(str(error) for error in close_errors),
            )


def _coerce_embedding_vector(value: Sequence[float] | Sequence[Sequence[float]]) -> list[float]:
    if not value:
        return []
    first = value[0]
    if isinstance(first, Sequence) and not isinstance(first, (str, bytes, bytearray)):
        return [float(item) for item in first]
    return [float(item) for item in cast(Sequence[float], value)]


def _coerce_embedding_vectors(
    value: Sequence[float] | Sequence[Sequence[float]],
    expected_count: int,
) -> list[list[float]]:
    if expected_count <= 0:
        return []
    if not value:
        return [[] for _ in range(expected_count)]
    first = value[0]
    if isinstance(first, Sequence) and not isinstance(first, (str, bytes, bytearray)):
        vectors = [[float(item) for item in cast(Sequence[float], vector)] for vector in cast(Sequence[Sequence[float]], value)]
    else:
        vectors = [[float(item) for item in cast(Sequence[float], value)]]
    if len(vectors) < expected_count:
        vectors.extend([[] for _ in range(expected_count - len(vectors))])
    return vectors[:expected_count]


async def _maybe_await_embedding_result(
    result: Sequence[float] | Sequence[Sequence[float]] | Awaitable[Sequence[float] | Sequence[Sequence[float]]],
) -> Sequence[float] | Sequence[Sequence[float]]:
    if inspect.isawaitable(result):
        return await cast(Awaitable[Sequence[float] | Sequence[Sequence[float]]], result)
    return cast(Sequence[float] | Sequence[Sequence[float]], result)


class CallableEmbeddingProvider:
    name = "callable"

    def __init__(self, embedding_function: EmbeddingFunction):
        self.embedding_function = embedding_function

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        try:
            result = self.embedding_function(texts)
        except TypeError:
            if len(texts) != 1:
                vectors = []
                for text in texts:
                    single = await _maybe_await_embedding_result(self.embedding_function(text))
                    vectors.append(_coerce_embedding_vector(single))
                return vectors
            result = self.embedding_function(texts[0])
        resolved = await _maybe_await_embedding_result(result)
        return _coerce_embedding_vectors(cast(Sequence[float] | Sequence[Sequence[float]], resolved), len(texts))


class AgentEmbeddingProvider:
    name = "agent"

    def __init__(self, agent: Any):
        self.agent = agent

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        execution = self.agent.input(texts)
        async_start = getattr(execution, "async_start", None)
        if callable(async_start):
            result = async_start()
            resolved = await result if inspect.isawaitable(result) else result
        else:
            result = execution.start()
            resolved = await result if inspect.isawaitable(result) else result
        embedding_result = cast(Sequence[float] | Sequence[Sequence[float]], resolved)
        return _coerce_embedding_vectors(embedding_result, len(texts))


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

    async def delete_content(self, relative_path: str) -> bool:
        """Idempotently delete one directly owned content file."""
        self.policy.ensure_writable()
        return delete_owned_file_descriptor_relative(
            self.content_root,
            relative_path,
        )

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


class VectorIndexPipeline:
    name = "vector_pipeline"

    def __init__(
        self,
        *,
        embedding_provider: Any | None,
        vector_store_provider: Any | None,
    ):
        self.embedding_provider = embedding_provider
        self.vector_store_provider = vector_store_provider
        self.similarity = getattr(vector_store_provider, "similarity", None)

    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
        if self.vector_store_provider is None:
            raise VectorStoreProviderUnavailableError("Workspace vector store is unavailable.")
        if self.embedding_provider is None:
            return
        embeddings = await self.embedding_provider.embed_texts([content])
        embedding = embeddings[0] if embeddings else []
        if embedding:
            await self.vector_store_provider.index_record(ref, embedding)

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        if self.vector_store_provider is None:
            raise VectorStoreProviderUnavailableError("Workspace vector store is unavailable.")
        if self.embedding_provider is None:
            return []
        embeddings = await self.embedding_provider.embed_texts([query])
        embedding = embeddings[0] if embeddings else []
        if not embedding:
            return []
        return await self.vector_store_provider.search_by_embedding(embedding, filters=filters, limit=limit)


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


class SQLiteVectorStoreProvider:
    name = "sqlite"

    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool = False,
        create: bool = True,
        similarity: VectorSimilarity = "cosine",
    ):
        self.db_path = Path(db_path).expanduser().resolve()
        self.read_only = read_only
        self.similarity: VectorSimilarity = similarity if similarity in {"cosine", "dot", "l2"} else "cosine"
        if create and not read_only:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_vectors (
                    record_id TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    kind TEXT,
                    path TEXT,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    ref_json TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS workspace_vectors_collection_kind_idx
                ON workspace_vectors(collection, kind, record_id)
                """
            )
            conn.commit()

    async def index_record(self, ref: WorkspaceRecordRef, embedding: list[float]) -> None:
        if self.read_only:
            raise WorkspacePolicyError("Workspace vector store is configured read-only.")
        vector = [float(value) for value in embedding]
        if not vector:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO workspace_vectors (
                    record_id, collection, kind, path, scope_json, meta_json,
                    ref_json, embedding_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref["id"],
                    ref["collection"],
                    ref["kind"],
                    ref["path"],
                    json_dumps(ref.get("scope") or {}),
                    json_dumps(ref.get("meta") or {}),
                    json_dumps(ref),
                    json_dumps(vector),
                    ref["created_at"],
                ),
            )
            conn.commit()

    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []
        if filters.get("id") is not None:
            clauses.append("record_id = ?")
            params.append(str(filters["id"]))
        if filters.get("path") is not None:
            clauses.append("path = ?")
            params.append(str(filters["path"]))
        if filters.get("collection") is not None:
            clauses.append("collection = ?")
            params.append(str(filters["collection"]))
        if filters.get("kind") is not None:
            clauses.append("kind = ?")
            params.append(str(filters["kind"]))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM workspace_vectors { where }", params).fetchall()
        query_vector = [float(value) for value in embedding]
        scored: list[tuple[float, WorkspaceRecordRef]] = []
        for row in rows:
            ref = cast(WorkspaceRecordRef, json_loads(row["ref_json"], {}))
            if not self._matches_filters(ref, filters):
                continue
            vector = [float(value) for value in json_loads(row["embedding_json"], [])]
            scored.append((self._score(query_vector, vector), ref))
        scored.sort(key=lambda item: item[0], reverse=True)
        if limit is not None:
            scored = scored[: max(0, limit)]
        return [ref for _, ref in scored]

    async def delete_records(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        if self.read_only:
            raise WorkspacePolicyError("Workspace vector store is configured read-only.")
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM workspace_vectors WHERE record_id IN ({placeholders})", list(record_ids))
            conn.commit()

    def _score(self, left: Sequence[float], right: Sequence[float]) -> float:
        return _vector_score(left, right, self.similarity)

    def _matches_filters(self, ref: WorkspaceRecordRef, filters: Mapping[str, Any]) -> bool:
        return _record_matches_filters(ref, filters)


class ChromaVectorStoreProvider:
    name = "chroma"

    def __init__(
        self,
        root: str | Path,
        *,
        collection_name: str = "workspace_records",
        create: bool = True,
        mode: str = "read_write",
        similarity: VectorSimilarity = "cosine",
        client: Any | None = None,
    ):
        _ = create
        self.root = Path(root).expanduser().resolve()
        self.read_only = mode in {"read", "read_only", "readonly"}
        self.similarity: VectorSimilarity = similarity if similarity in {"cosine", "dot", "l2"} else "cosine"
        self.collection_name = collection_name
        try:
            chromadb = importlib.import_module("chromadb")
        except ImportError as error:
            raise WorkspaceConfigurationError("ChromaDB is not installed for Workspace vector store.") from error
        self.root.mkdir(parents=True, exist_ok=True)
        self._client = client or chromadb.PersistentClient(path=str(self.root))
        metadata = {"hnsw:space": "ip" if self.similarity == "dot" else self.similarity}
        self._collection = self._client.get_or_create_collection(name=collection_name, metadata=metadata)

    async def index_record(self, ref: WorkspaceRecordRef, embedding: list[float]) -> None:
        if self.read_only:
            raise WorkspacePolicyError("Workspace vector store is configured read-only.")
        vector = [float(value) for value in embedding]
        if not vector:
            return
        metadata = {
            "record_id": ref["id"],
            "collection": ref["collection"],
            "kind": ref.get("kind") or "",
            "path": ref.get("path") or "",
            "scope_json": json_dumps(ref.get("scope") or {}),
            "meta_json": json_dumps(ref.get("meta") or {}),
            "ref_json": json_dumps(ref),
        }
        self._collection.upsert(
            ids=[ref["id"]],
            embeddings=[vector],
            documents=[ref.get("summary") or ""],
            metadatas=[metadata],
        )

    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        filters = filters or {}
        safe_limit = max(1, int(limit or 10))
        n_results = max(safe_limit, 50 if filters else safe_limit)
        results = self._collection.query(query_embeddings=[[float(value) for value in embedding]], n_results=n_results)
        metadatas = (results.get("metadatas") or [[]])[0] if isinstance(results, dict) else []
        refs: list[WorkspaceRecordRef] = []
        for metadata in metadatas:
            if not isinstance(metadata, Mapping):
                continue
            ref = cast(WorkspaceRecordRef, json_loads(str(metadata.get("ref_json") or "{}"), {}))
            if not ref or not _record_matches_filters(ref, filters):
                continue
            refs.append(ref)
            if len(refs) >= safe_limit:
                break
        return refs

    async def delete_records(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        if self.read_only:
            raise WorkspacePolicyError("Workspace vector store is configured read-only.")
        self._collection.delete(ids=[str(record_id) for record_id in record_ids])


def _vector_score(left: Sequence[float], right: Sequence[float], similarity: VectorSimilarity) -> float:
    size = min(len(left), len(right))
    if size == 0:
        return float("-inf")
    left_values = [float(value) for value in left[:size]]
    right_values = [float(value) for value in right[:size]]
    if similarity == "dot":
        return sum(a * b for a, b in zip(left_values, right_values))
    if similarity == "l2":
        return -math.sqrt(sum((a - b) ** 2 for a, b in zip(left_values, right_values)))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0 or right_norm == 0:
        return float("-inf")
    return sum(a * b for a, b in zip(left_values, right_values)) / (left_norm * right_norm)


def _record_matches_filters(ref: WorkspaceRecordRef, filters: Mapping[str, Any]) -> bool:
    for key, expected in filters.items():
        actual = _resolve_ref_filter_value(ref, str(key))
        if isinstance(expected, (list, tuple, set, frozenset)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _resolve_ref_filter_value(ref: WorkspaceRecordRef, key: str) -> Any:
    if key.startswith("scope."):
        return _mapping_dot_get(ref.get("scope") or {}, key.removeprefix("scope."))
    if key.startswith("meta."):
        return _mapping_dot_get(ref.get("meta") or {}, key.removeprefix("meta."))
    return ref.get(key)


def _mapping_dot_get(mapping: Mapping[str, Any], key: str) -> Any:
    current: Any = mapping
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current
