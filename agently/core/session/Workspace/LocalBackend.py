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

import hashlib
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.workspace import (
    WorkspaceBackendCapabilities,
    WorkspaceContentSegment,
    WorkspaceFilePolicyMetadata,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetentionAnchor,
    WorkspaceRuntimeEventRecord,
)

from .Errors import WorkspaceConfigurationError, WorkspacePolicyError
from .Stores import LocalContentStore, LocalWorkspacePolicyEngine, NoopVectorIndex
from ._utils import json_dumps, json_loads, slug, utc_now


class LocalWorkspaceBackend:
    """Local filesystem content plus SQLite metadata and FTS index."""

    DEFAULT_COLLECTIONS = ("dialogue", "observations", "decisions", "artifacts", "checkpoints", "runtime_events")

    def __init__(
        self,
        root: str | Path,
        *,
        create: bool = True,
        mode: str = "read_write",
    ):
        self.root = Path(root).expanduser().resolve()
        self.content_root = self.root / "content"
        self.files_root = self.root / "files"
        self.db_path = self.root / "workspace.db"
        self.mode = mode
        self.read_only = mode in {"read", "read_only", "readonly"}
        self.workspace_id = self._default_workspace_id()
        self.policy = LocalWorkspacePolicyEngine(self.content_root, read_only=self.read_only)
        self.content = LocalContentStore(self.content_root, self.policy)
        self.metadata = self
        self.checkpoint_store = self
        self.runtime_event_store = self
        self.ref_resolver = self
        self.retention_policy = self
        self.evidence_linker = self
        self.text_index = self
        self.vector_index = NoopVectorIndex()
        if create:
            self._initialize()
        elif not self.root.exists():
            raise WorkspaceConfigurationError(f"Workspace root does not exist: { self.root }")
        else:
            self.workspace_id = self._load_workspace_meta().get("workspace_id", self.workspace_id)

    def _initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.content_root.mkdir(parents=True, exist_ok=True)
        self.files_root.mkdir(parents=True, exist_ok=True)
        for collection in self.DEFAULT_COLLECTIONS:
            self._ensure_collection(collection)
        meta_path = self.root / "workspace.meta.json"
        meta = self._load_workspace_meta()
        if "workspace_id" not in meta:
            meta["workspace_id"] = self.workspace_id
        self.workspace_id = str(meta["workspace_id"])
        meta.update(
            {
                "schema_version": "agently.workspace.local.v1",
                "backend": "local",
                "content_root": str(self.content_root),
                "files_root": str(self.files_root),
            }
        )
        meta.setdefault("created_at", utc_now())
        meta_path.write_text(json_dumps(meta), encoding="utf-8")
        with self._connect() as conn:
            self._create_schema(conn)

    def _default_workspace_id(self):
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:24]
        return f"ws_{ digest }"

    def _load_workspace_meta(self):
        meta_path = self.root / "workspace.meta.json"
        if not meta_path.exists():
            return {}
        return json_loads(meta_path.read_text(encoding="utf-8"), {})

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _create_schema(self, conn: sqlite3.Connection):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                kind TEXT,
                path TEXT,
                sha256 TEXT,
                size INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '',
                scope_json TEXT NOT NULL DEFAULT '{}',
                source_json TEXT NOT NULL DEFAULT '{}',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                is_checkpoint INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL,
                step_id TEXT,
                record_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manifests (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS records_fts
            USING fts5(record_id UNINDEXED, summary, content)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                idempotency_key TEXT,
                parent_id TEXT,
                causation_id TEXT,
                node_id TEXT,
                aggregation_scope TEXT,
                checkpoint_ref_json TEXT,
                exchange_id TEXT,
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS runtime_events_execution_sequence_idx
            ON runtime_events(execution_id, sequence)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS runtime_events_idempotency_idx
            ON runtime_events(execution_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS retention_anchors (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                anchor_type TEXT NOT NULL,
                sequence INTEGER,
                record_ref_json TEXT,
                summary_ref_json TEXT,
                preserved_event_ids_json TEXT NOT NULL DEFAULT '[]',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def _ensure_writable(self):
        self.policy.ensure_writable()

    def _ensure_collection(self, collection: str):
        collection_path = self.content.ensure_collection(collection)
        descriptor = collection_path / "_collection.meta.json"
        if not descriptor.exists():
            descriptor.write_text(
                json_dumps(
                    {
                        "schema_version": "agently.workspace.collection.v1",
                        "collection": collection,
                        "created_at": utc_now(),
                    }
                ),
                encoding="utf-8",
            )

    def _resolve_content_path(self, path: str | Path):
        try:
            return self.policy.resolve_content_path(path)
        except WorkspacePolicyError:
            raise

    @staticmethod
    def _content_to_bytes(content: Any) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        return json_dumps(content).encode("utf-8")

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            return content
        return json_dumps(content)

    def _row_to_ref(self, row: sqlite3.Row) -> WorkspaceRecordRef:
        return {
            "id": str(row["id"]),
            "collection": str(row["collection"]),
            "kind": row["kind"],
            "path": row["path"],
            "sha256": row["sha256"],
            "size": int(row["size"] or 0),
            "summary": str(row["summary"] or ""),
            "scope": json_loads(row["scope_json"], {}),
            "source": json_loads(row["source_json"], {}),
            "created_at": str(row["created_at"]),
            "meta": json_loads(row["meta_json"], {}),
        }

    def _row_to_link(self, row: sqlite3.Row) -> WorkspaceLinkRef:
        return {
            "id": str(row["id"]),
            "source_id": str(row["source_id"]),
            "target_id": str(row["target_id"]),
            "relation": str(row["relation"]),
            "created_at": str(row["created_at"]),
            "meta": json_loads(row["meta_json"], {}),
        }

    def _features(self) -> dict[str, bool]:
        vector_index = self.vector_index
        return {
            "structured_get_data": True,
            "links_query": True,
            "checkpoint_lookup": True,
            "metadata_filters": True,
            "text_search": True,
            "vector_search": vector_index is not None and getattr(vector_index, "name", None) != "noop",
            "workspace_reference_envelopes": True,
            "bounded_read": True,
            "stream_read": True,
            "runtime_event_store": True,
            "runtime_event_idempotency": True,
            "evidence_links": True,
            "file_policy_metadata": True,
            "retention_anchors": True,
            "supports_cas": False,
            "supports_lease": False,
            "supports_event_sequence": True,
            "supports_range_read": True,
            "supports_stream_read": True,
            "supports_retention": True,
            "supports_compaction_anchor": True,
            "supports_remote_backend": False,
        }

    @staticmethod
    def _policy_labels(ref: WorkspaceRecordRef) -> list[str]:
        labels = ref.get("meta", {}).get("policy_labels", [])
        if isinstance(labels, list):
            return [str(label) for label in labels]
        if isinstance(labels, str):
            return [labels]
        return []

    def _record_ref_envelope(self, ref: WorkspaceRecordRef) -> WorkspaceReferenceEnvelope:
        return {
            "workspace_id": self.workspace_id,
            "kind": str(ref.get("kind") or ref.get("collection") or "record"),
            "collection": str(ref.get("collection") or ""),
            "record_id": str(ref.get("id") or ""),
            "version": ref.get("meta", {}).get("version"),
            "content_ref": ref.get("path"),
            "digest": ref.get("sha256"),
            "size": int(ref.get("size") or 0),
            "created_at": str(ref.get("created_at") or ""),
            "policy_labels": self._policy_labels(ref),
            "backend_capabilities": self._features(),
        }

    @staticmethod
    def _is_reference_envelope(value: Any) -> bool:
        return isinstance(value, dict) and "workspace_id" in value and (
            "record_id" in value or "content_ref" in value
        )

    async def _coerce_ref_envelope(
        self,
        value: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None,
    ) -> WorkspaceReferenceEnvelope | None:
        if value is None:
            return None
        if self._is_reference_envelope(value):
            return value  # type: ignore[return-value]
        return await self.ref_envelope(value)  # type: ignore[arg-type]

    async def ref_envelope(self, ref_or_id: WorkspaceRecordRef | str) -> WorkspaceReferenceEnvelope:
        if isinstance(ref_or_id, dict):
            return self._record_ref_envelope(ref_or_id)
        if str(ref_or_id).startswith("rec_"):
            ref = await self.get_record(str(ref_or_id))
            if ref is None:
                raise FileNotFoundError(f"Workspace record not found: { ref_or_id }")
            return self._record_ref_envelope(ref)
        path = str(ref_or_id)
        target = self.policy.resolve_content_path(path)
        size = target.stat().st_size if target.exists() else 0
        digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        return {
            "workspace_id": self.workspace_id,
            "kind": "content",
            "collection": "",
            "record_id": "",
            "version": None,
            "content_ref": path,
            "digest": digest,
            "size": size,
            "created_at": "",
            "policy_labels": [],
            "backend_capabilities": self._features(),
        }

    def _content_type_for_path(self, path: str | None):
        if path and path.endswith(".json"):
            return "application/json"
        if path and path.endswith(".md"):
            return "text/markdown"
        return "text/plain"

    async def _resolve_read_target(
        self,
        ref_or_path: WorkspaceRecordRef | str,
    ) -> tuple[str, WorkspaceReferenceEnvelope, str | None, str | None]:
        path: str | None = None
        ref: WorkspaceRecordRef | None = None
        if isinstance(ref_or_path, dict):
            ref = ref_or_path
            path = ref_or_path.get("path")
        elif isinstance(ref_or_path, str) and ref_or_path.startswith("rec_"):
            ref = await self.get_record(ref_or_path)
            if ref is not None:
                path = ref.get("path")
        else:
            path = str(ref_or_path)
        if not path:
            raise FileNotFoundError(f"Workspace record content not found: { ref_or_path }")
        envelope = self._record_ref_envelope(ref) if ref is not None else await self.ref_envelope(path)
        digest = ref.get("sha256") if ref is not None else envelope.get("digest")
        return path, envelope, digest, self._content_type_for_path(path)

    @staticmethod
    def _normalize_runtime_event(event: RuntimeEvent | RuntimeEventDict | dict[str, Any]) -> dict[str, Any]:
        if hasattr(event, "model_dump"):
            return event.model_dump(mode="json")  # type: ignore[union-attr]
        return dict(event)

    def _row_to_runtime_event_record(self, row: sqlite3.Row) -> WorkspaceRuntimeEventRecord:
        checkpoint_ref = json_loads(row["checkpoint_ref_json"], None)
        return {
            "id": str(row["id"]),
            "execution_id": str(row["execution_id"]),
            "sequence": int(row["sequence"]),
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "idempotency_key": row["idempotency_key"],
            "parent_id": row["parent_id"],
            "causation_id": row["causation_id"],
            "node_id": row["node_id"],
            "aggregation_scope": row["aggregation_scope"],
            "checkpoint_ref": checkpoint_ref,
            "exchange_id": row["exchange_id"],
            "artifact_refs": json_loads(row["artifact_refs_json"], []),
            "event": json_loads(row["event_json"], {}),
            "created_at": str(row["created_at"]),
        }

    def _row_to_retention_anchor(self, row: sqlite3.Row) -> WorkspaceRetentionAnchor:
        return {
            "id": str(row["id"]),
            "execution_id": str(row["execution_id"]),
            "anchor_type": str(row["anchor_type"]),
            "sequence": row["sequence"],
            "record_ref": json_loads(row["record_ref_json"], None),
            "summary_ref": json_loads(row["summary_ref_json"], None),
            "preserved_event_ids": json_loads(row["preserved_event_ids_json"], []),
            "created_at": str(row["created_at"]),
            "meta": json_loads(row["meta_json"], {}),
        }

    def _get_manifest(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM manifests WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json_loads(row["value_json"], default)

    def _set_manifest(self, key: str, value: Any) -> None:
        self._ensure_writable()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO manifests(key, value_json) VALUES (?, ?)",
                (key, json_dumps(value)),
            )
            conn.commit()

    async def put(
        self,
        content: Any,
        *,
        collection: str,
        kind: str | None = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef:
        self._ensure_writable()
        collection = slug(collection, "artifacts")
        self._ensure_collection(collection)
        record_id = f"rec_{ uuid.uuid4().hex }"
        content_bytes = self._content_to_bytes(content)
        content_text = self._content_to_text(content)
        digest = hashlib.sha256(content_bytes).hexdigest()
        suffix = ".json" if not isinstance(content, (str, bytes)) else ".txt"
        file_name = f"{ record_id }-{ slug(kind or collection, 'record') }{ suffix }"
        relative_path = f"{ collection }/{ file_name }"
        relative_path = await self.content.write_content(relative_path, content_bytes)
        created_at = utc_now()
        record_summary = summary or content_text[:240].replace("\n", " ").strip()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO records (
                    id, collection, kind, path, sha256, size, summary,
                    scope_json, source_json, meta_json, created_at, is_checkpoint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    collection,
                    kind,
                    relative_path,
                    digest,
                    len(content_bytes),
                    record_summary,
                    json_dumps(scope or {}),
                    json_dumps(source or {}),
                    json_dumps(meta or {}),
                    created_at,
                    1 if collection == "checkpoints" else 0,
                ),
            )
            conn.execute(
                "INSERT INTO records_fts(record_id, summary, content) VALUES (?, ?, ?)",
                (record_id, record_summary, content_text),
            )
            conn.commit()
        ref: WorkspaceRecordRef = {
            "id": record_id,
            "collection": collection,
            "kind": kind,
            "path": relative_path,
            "sha256": digest,
            "size": len(content_bytes),
            "summary": record_summary,
            "scope": scope or {},
            "source": source or {},
            "created_at": created_at,
            "meta": meta or {},
        }
        await self.vector_index.index_record(ref, content_text)
        return ref

    async def put_record(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef:
        self._ensure_writable()
        self._ensure_collection(ref["collection"])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO records (
                    id, collection, kind, path, sha256, size, summary,
                    scope_json, source_json, meta_json, created_at, is_checkpoint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref["id"],
                    ref["collection"],
                    ref["kind"],
                    ref["path"],
                    ref["sha256"],
                    ref["size"],
                    ref["summary"],
                    json_dumps(ref["scope"]),
                    json_dumps(ref["source"]),
                    json_dumps(ref["meta"]),
                    ref["created_at"],
                    1 if ref["collection"] == "checkpoints" or ref["meta"].get("checkpoint") else 0,
                ),
            )
            conn.commit()
        return ref

    async def get_record(self, record_id: str) -> WorkspaceRecordRef | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_ref(row)

    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
        self._ensure_writable()
        with self._connect() as conn:
            conn.execute("DELETE FROM records_fts WHERE record_id = ?", (ref["id"],))
            conn.execute(
                "INSERT INTO records_fts(record_id, summary, content) VALUES (?, ?, ?)",
                (ref["id"], ref["summary"], content),
            )
            conn.commit()

    async def get(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
        path: str | None = None
        if isinstance(ref_or_path, dict):
            path = ref_or_path.get("path")
        elif isinstance(ref_or_path, str) and ref_or_path.startswith("rec_"):
            with self._connect() as conn:
                row = conn.execute("SELECT path FROM records WHERE id = ?", (ref_or_path,)).fetchone()
            if row is not None:
                path = row["path"]
        else:
            path = str(ref_or_path)
        if not path:
            raise FileNotFoundError(f"Workspace record content not found: { ref_or_path }")
        return await self.content.read_content(path)

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
        content = await self.get(ref_or_path)
        path: str | None = None
        if isinstance(ref_or_path, dict):
            path = ref_or_path.get("path")
        elif isinstance(ref_or_path, str) and ref_or_path.startswith("rec_"):
            record = await self.get_record(ref_or_path)
            path = record.get("path") if record is not None else None
        else:
            path = str(ref_or_path)
        if path and path.endswith(".json") and isinstance(content, str):
            return json_loads(content, content)
        return content

    async def read_bounded(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment:
        path, envelope, digest, content_type = await self._resolve_read_target(ref_or_path)
        segment = await self.content.read_content_segment(path, offset=offset, limit=limit)
        segment["ref"] = envelope
        segment["digest"] = digest
        segment["content_type"] = content_type
        return segment

    def stream_read(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]:
        async def _stream():
            path, envelope, digest, content_type = await self._resolve_read_target(ref_or_path)
            async for segment in self.content.stream_content(
                path,
                offset=offset,
                limit=limit,
                chunk_size=chunk_size,
            ):
                segment["ref"] = envelope
                segment["digest"] = digest
                segment["content_type"] = content_type
                yield segment

        return _stream()

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]:
        filters = filters or {}
        params: list[Any] = []
        clauses: list[str] = []
        if filters.get("collection") is not None:
            clauses.append("r.collection = ?")
            params.append(str(filters["collection"]))
        if filters.get("kind") is not None:
            clauses.append("r.kind = ?")
            params.append(str(filters["kind"]))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            if query:
                fts_query = self._safe_fts_query(query)
                rows = []
                if fts_query:
                    sql = (
                        "SELECT r.* FROM records r JOIN records_fts f ON r.id = f.record_id "
                        f"{ where + ' AND' if where else 'WHERE' } records_fts MATCH ? "
                        "ORDER BY bm25(records_fts)"
                    )
                    try:
                        rows = conn.execute(sql, [*params, fts_query]).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                if not rows:
                    rows = self._like_search_rows(conn, where=where, params=params, query=query)
            else:
                rows = conn.execute(f"SELECT r.* FROM records r { where } ORDER BY created_at DESC", params).fetchall()
        refs = [self._row_to_ref(row) for row in rows]
        for key, value in filters.items():
            if key in {"collection", "kind"}:
                continue
            if key.startswith("scope."):
                path = key.split(".", 1)[1]
                refs = [ref for ref in refs if ref.get("scope", {}).get(path) == value]
            elif key.startswith("meta."):
                path = key.split(".", 1)[1]
                refs = [ref for ref in refs if ref.get("meta", {}).get(path) == value]
        return refs

    @staticmethod
    def _safe_fts_query(query: str) -> str:
        tokens = re.findall(r"[\w][\w.\-:/]*", str(query), flags=re.UNICODE)
        phrases = []
        for token in tokens[:16]:
            normalized = token.strip().strip(".:-/")
            if not normalized:
                continue
            escaped = normalized.replace('"', '""')
            phrases.append(f'"{ escaped }"')
        return " OR ".join(phrases)

    @staticmethod
    def _like_search_rows(
        conn: sqlite3.Connection,
        *,
        where: str,
        params: list[Any],
        query: str,
    ) -> list[sqlite3.Row]:
        like = f"%{ query }%"
        like_clauses = "(r.summary LIKE ? OR f.summary LIKE ? OR f.content LIKE ?)"
        sql = (
            "SELECT DISTINCT r.* FROM records r LEFT JOIN records_fts f ON r.id = f.record_id "
            f"{ where + ' AND ' if where else 'WHERE ' }{ like_clauses } "
            "ORDER BY r.created_at DESC"
        )
        return conn.execute(sql, [*params, like, like, like]).fetchall()

    @staticmethod
    def _record_id(value: WorkspaceRecordRef | str) -> str:
        if isinstance(value, dict):
            return str(value.get("id", ""))
        return str(value)

    async def link(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef:
        self._ensure_writable()
        link_id = f"link_{ uuid.uuid4().hex }"
        created_at = utc_now()
        source_id = self._record_id(source)
        target_id = self._record_id(target)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO links(id, source_id, target_id, relation, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, source_id, target_id, relation, json_dumps(meta or {}), created_at),
            )
            conn.commit()
        return {
            "id": link_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "created_at": created_at,
            "meta": meta or {},
        }

    async def link_evidence(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        *,
        execution_id: str | None = None,
        operation_id: str | None = None,
        runtime_event_id: str | None = None,
        checkpoint_id: str | None = None,
        exchange_id: str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef:
        evidence_meta = dict(meta or {})
        evidence_meta["evidence"] = {
            key: value
            for key, value in {
                "execution_id": execution_id,
                "operation_id": operation_id,
                "runtime_event_id": runtime_event_id,
                "checkpoint_id": checkpoint_id,
                "exchange_id": exchange_id,
                "artifact_refs": [
                    await self._coerce_ref_envelope(ref)
                    for ref in (artifact_refs or [])
                ],
            }.items()
            if value is not None
        }
        return await self.link(source, target, relation, evidence_meta)

    async def links(
        self,
        ref_or_id: WorkspaceRecordRef | str | None = None,
        *,
        source: WorkspaceRecordRef | str | None = None,
        target: WorkspaceRecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[WorkspaceLinkRef]:
        params: list[Any] = []
        clauses: list[str] = []
        if ref_or_id is not None:
            record_id = self._record_id(ref_or_id)
            clauses.append("(source_id = ? OR target_id = ?)")
            params.extend([record_id, record_id])
        if source is not None:
            clauses.append("source_id = ?")
            params.append(self._record_id(source))
        if target is not None:
            clauses.append("target_id = ?")
            params.append(self._record_id(target))
        if relation is not None:
            clauses.append("relation = ?")
            params.append(relation)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM links { where } ORDER BY created_at ASC", params).fetchall()
        return [self._row_to_link(row) for row in rows]

    async def checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> WorkspaceRecordRef:
        ref = await self.put(
            state,
            collection="checkpoints",
            kind="checkpoint",
            summary=f"Checkpoint for { run_id }" + (f" step { step_id }" if step_id else ""),
            scope={"run_id": run_id, **({"step_id": step_id} if step_id else {})},
            source={"type": "workspace", "name": "checkpoint"},
            meta={"checkpoint": True},
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(run_id, step_id, record_id, state_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, step_id, ref["id"], json_dumps(state), ref["created_at"]),
            )
            conn.execute(
                "INSERT OR REPLACE INTO manifests(key, value_json) VALUES (?, ?)",
                (f"checkpoint.latest.{ run_id }", json_dumps(ref)),
            )
            conn.commit()
        return ref

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> WorkspaceRecordRef:
        return await self.checkpoint(run_id, state, step_id=step_id)

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.* FROM checkpoints c
                JOIN records r ON r.id = c.record_id
                WHERE c.run_id = ?
                ORDER BY c.created_at DESC, c.rowid DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_ref(row)

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        params: list[Any] = [run_id]
        step_clause = ""
        if step_id is not None:
            step_clause = "AND c.step_id = ?"
            params.append(step_id)
        limit_clause = ""
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be greater than or equal to 0.")
            limit_clause = "LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.* FROM checkpoints c
                JOIN records r ON r.id = c.record_id
                WHERE c.run_id = ? { step_clause }
                ORDER BY c.created_at DESC, c.rowid DESC
                { limit_clause }
                """,
                params,
            ).fetchall()
        return [self._row_to_ref(row) for row in rows]

    async def append_runtime_event(
        self,
        execution_id: str,
        event: RuntimeEvent | RuntimeEventDict | dict[str, Any],
        *,
        sequence: int | None = None,
        idempotency_key: str | None = None,
        checkpoint_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        exchange_id: str | None = None,
        node_id: str | None = None,
        aggregation_scope: str | None = None,
    ) -> WorkspaceRuntimeEventRecord:
        self._ensure_writable()
        if not execution_id:
            raise ValueError("execution_id must be non-empty.")
        event_dict = self._normalize_runtime_event(event)
        event_id = str(event_dict.get("event_id") or f"evt_{ uuid.uuid4().hex }")
        event_dict["event_id"] = event_id
        event_type = str(event_dict.get("event_type") or "runtime.event")
        raw_meta = event_dict.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        parent_id = meta.get("parent_event_id") or meta.get("parent_id")
        causation_id = meta.get("causation_id")
        resolved_checkpoint_ref = await self._coerce_ref_envelope(checkpoint_ref)
        resolved_artifact_refs = [
            envelope
            for envelope in [
                await self._coerce_ref_envelope(ref)
                for ref in (artifact_refs or [])
            ]
            if envelope is not None
        ]
        created_at = utc_now()
        with self._connect() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    """
                    SELECT * FROM runtime_events
                    WHERE execution_id = ? AND idempotency_key = ?
                    """,
                    (execution_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._row_to_runtime_event_record(existing)
            if sequence is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM runtime_events WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                sequence = int(row["max_sequence"] or 0) + 1
            record_id = f"rtevt_{ uuid.uuid4().hex }"
            conn.execute(
                """
                INSERT INTO runtime_events (
                    id, execution_id, sequence, event_id, event_type, idempotency_key,
                    parent_id, causation_id, node_id, aggregation_scope, checkpoint_ref_json,
                    exchange_id, artifact_refs_json, event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    execution_id,
                    sequence,
                    event_id,
                    event_type,
                    idempotency_key,
                    parent_id,
                    causation_id,
                    node_id or meta.get("node_id"),
                    aggregation_scope or meta.get("aggregation_scope"),
                    json_dumps(resolved_checkpoint_ref) if resolved_checkpoint_ref is not None else None,
                    exchange_id or meta.get("exchange_id"),
                    json_dumps(resolved_artifact_refs),
                    json_dumps(event_dict),
                    created_at,
                ),
            )
            row = conn.execute("SELECT * FROM runtime_events WHERE id = ?", (record_id,)).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError(f"Workspace runtime event insert failed: { record_id }")
        return self._row_to_runtime_event_record(row)

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRuntimeEventRecord]:
        params: list[Any] = [execution_id]
        clauses = ["execution_id = ?"]
        if sequence_from is not None:
            clauses.append("sequence >= ?")
            params.append(sequence_from)
        if sequence_to is not None:
            clauses.append("sequence <= ?")
            params.append(sequence_to)
        if event_id is not None:
            clauses.append("event_id = ?")
            params.append(event_id)
        limit_clause = ""
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be greater than or equal to 0.")
            limit_clause = "LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM runtime_events
                WHERE {' AND '.join(clauses)}
                ORDER BY sequence ASC
                { limit_clause }
                """,
                params,
            ).fetchall()
        return [self._row_to_runtime_event_record(row) for row in rows]

    async def record_file_policy(
        self,
        *,
        action_file_root: str | None = None,
        allowed_roots: list[str] | None = None,
        root_source: str = "workspace",
        path_normalization: str = "resolve",
        symlink_policy: str = "resolved_within_root",
        case_policy: str = "platform_default",
        policy_labels: list[str] | None = None,
        links: dict[str, str] | None = None,
    ) -> WorkspaceFilePolicyMetadata:
        metadata: WorkspaceFilePolicyMetadata = {
            "content_root": str(self.content_root),
            "files_root": str(self.files_root),
            "action_file_root": action_file_root,
            "allowed_roots": allowed_roots or [str(self.files_root)],
            "root_source": root_source,
            "path_normalization": path_normalization,
            "symlink_policy": symlink_policy,
            "case_policy": case_policy,
            "policy_labels": policy_labels or [],
            "links": links or {},
        }
        self._set_manifest("file_policy", metadata)
        return metadata

    async def get_file_policy(self) -> WorkspaceFilePolicyMetadata:
        existing = self._get_manifest("file_policy", None)
        if existing is not None:
            return existing
        return {
            "content_root": str(self.content_root),
            "files_root": str(self.files_root),
            "action_file_root": None,
            "allowed_roots": [str(self.files_root)],
            "root_source": "workspace",
            "path_normalization": "resolve",
            "symlink_policy": "resolved_within_root",
            "case_policy": "platform_default",
            "policy_labels": [],
            "links": {},
        }

    async def add_retention_anchor(
        self,
        execution_id: str,
        *,
        anchor_type: str,
        sequence: int | None = None,
        record_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        summary_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        preserved_event_ids: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRetentionAnchor:
        self._ensure_writable()
        if not execution_id:
            raise ValueError("execution_id must be non-empty.")
        if not anchor_type:
            raise ValueError("anchor_type must be non-empty.")
        anchor_id = f"ret_{ uuid.uuid4().hex }"
        created_at = utc_now()
        resolved_record_ref = await self._coerce_ref_envelope(record_ref)
        resolved_summary_ref = await self._coerce_ref_envelope(summary_ref)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO retention_anchors (
                    id, execution_id, anchor_type, sequence, record_ref_json,
                    summary_ref_json, preserved_event_ids_json, meta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor_id,
                    execution_id,
                    anchor_type,
                    sequence,
                    json_dumps(resolved_record_ref) if resolved_record_ref is not None else None,
                    json_dumps(resolved_summary_ref) if resolved_summary_ref is not None else None,
                    json_dumps(preserved_event_ids or []),
                    json_dumps(meta or {}),
                    created_at,
                ),
            )
            row = conn.execute("SELECT * FROM retention_anchors WHERE id = ?", (anchor_id,)).fetchone()
            conn.commit()
        return self._row_to_retention_anchor(row)

    async def retention_anchors(
        self,
        execution_id: str,
        *,
        anchor_type: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRetentionAnchor]:
        params: list[Any] = [execution_id]
        clauses = ["execution_id = ?"]
        if anchor_type is not None:
            clauses.append("anchor_type = ?")
            params.append(anchor_type)
        limit_clause = ""
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be greater than or equal to 0.")
            limit_clause = "LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM retention_anchors
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC
                { limit_clause }
                """,
                params,
            ).fetchall()
        return [self._row_to_retention_anchor(row) for row in rows]

    def capabilities(self) -> WorkspaceBackendCapabilities:
        vector_index = self.vector_index
        return {
            "backend": "local",
            "root": str(self.root),
            "content_root": str(self.content_root),
            "files_root": str(self.files_root),
            "read_only": self.read_only,
            "components": {
                "content": type(self.content).__name__,
                "metadata": type(self.metadata).__name__,
                "checkpoint_store": type(self.checkpoint_store).__name__,
                "text_index": type(self.text_index).__name__,
                "policy": type(self.policy).__name__,
                "vector_index": type(vector_index).__name__ if vector_index is not None else None,
                "runtime_event_store": type(self.runtime_event_store).__name__,
                "ref_resolver": type(self.ref_resolver).__name__,
                "retention_policy": type(self.retention_policy).__name__,
                "evidence_linker": type(self.evidence_linker).__name__,
            },
            "features": self._features(),
        }
