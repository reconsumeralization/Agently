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
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from agently.types.data.workspace import WorkspaceBackendCapabilities, WorkspaceLinkRef, WorkspaceRecordRef

from .Errors import WorkspaceConfigurationError, WorkspacePolicyError
from .Stores import LocalContentStore, LocalWorkspacePolicyEngine, NoopVectorIndex
from ._utils import json_dumps, json_loads, slug, utc_now


class LocalWorkspaceBackend:
    """Local filesystem content plus SQLite metadata and FTS index."""

    DEFAULT_COLLECTIONS = ("dialogue", "observations", "decisions", "artifacts", "checkpoints")

    def __init__(
        self,
        root: str | Path,
        *,
        create: bool = True,
        mode: str = "read_write",
    ):
        self.root = Path(root).expanduser().resolve()
        self.content_root = self.root / "content"
        self.db_path = self.root / "workspace.db"
        self.mode = mode
        self.read_only = mode in {"read", "read_only", "readonly"}
        self.policy = LocalWorkspacePolicyEngine(self.content_root, read_only=self.read_only)
        self.content = LocalContentStore(self.content_root, self.policy)
        self.metadata = self
        self.checkpoint_store = self
        self.text_index = self
        self.vector_index = NoopVectorIndex()
        if create:
            self._initialize()
        elif not self.root.exists():
            raise WorkspaceConfigurationError(f"Workspace root does not exist: { self.root }")

    def _initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.content_root.mkdir(parents=True, exist_ok=True)
        for collection in self.DEFAULT_COLLECTIONS:
            self._ensure_collection(collection)
        meta_path = self.root / "workspace.meta.json"
        if not meta_path.exists():
            meta_path.write_text(
                json_dumps(
                    {
                        "schema_version": "agently.workspace.local.v1",
                        "created_at": utc_now(),
                        "backend": "local",
                        "content_root": str(self.content_root),
                    }
                ),
                encoding="utf-8",
            )
        with self._connect() as conn:
            self._create_schema(conn)

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
                sql = (
                    "SELECT r.* FROM records r JOIN records_fts f ON r.id = f.record_id "
                    f"{ where + ' AND' if where else 'WHERE' } records_fts MATCH ? "
                    "ORDER BY bm25(records_fts)"
                )
                rows = conn.execute(sql, [*params, query]).fetchall()
                if not rows:
                    like = f"%{ query }%"
                    sql = f"SELECT r.* FROM records r { where }"
                    rows = [
                        row
                        for row in conn.execute(sql, params).fetchall()
                        if like.strip("%").lower() in str(row["summary"]).lower()
                    ]
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
    ) -> list[WorkspaceRecordRef]:
        params: list[Any] = [run_id]
        step_clause = ""
        if step_id is not None:
            step_clause = "AND c.step_id = ?"
            params.append(step_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.* FROM checkpoints c
                JOIN records r ON r.id = c.record_id
                WHERE c.run_id = ? { step_clause }
                ORDER BY c.created_at DESC, c.rowid DESC
                """,
                params,
            ).fetchall()
        return [self._row_to_ref(row) for row in rows]

    def capabilities(self) -> WorkspaceBackendCapabilities:
        vector_index = self.vector_index
        return {
            "backend": "local",
            "root": str(self.root),
            "content_root": str(self.content_root),
            "read_only": self.read_only,
            "components": {
                "content": type(self.content).__name__,
                "metadata": type(self.metadata).__name__,
                "checkpoint_store": type(self.checkpoint_store).__name__,
                "text_index": type(self.text_index).__name__,
                "policy": type(self.policy).__name__,
                "vector_index": type(vector_index).__name__ if vector_index is not None else None,
            },
            "features": {
                "structured_get_data": True,
                "links_query": True,
                "checkpoint_lookup": True,
                "metadata_filters": True,
                "text_search": True,
                "vector_search": vector_index is not None and getattr(vector_index, "name", None) != "noop",
            },
        }
