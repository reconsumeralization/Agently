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
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

from agently.types.data.workspace import WorkspaceLinkRef, WorkspaceRecordRef
from agently.types.plugins import IngestionProfile, WorkspaceBackend


class WorkspaceError(RuntimeError):
    """Base Workspace error."""


class WorkspaceConfigurationError(WorkspaceError):
    """Raised when Workspace is missing required configuration."""


class WorkspacePolicyError(WorkspaceError):
    """Raised when Workspace policy blocks an operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _slug(value: str, fallback: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in str(value))
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized or fallback


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
                _json_dumps(
                    {
                        "schema_version": "agently.workspace.local.v1",
                        "created_at": _utc_now(),
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
        if self.read_only:
            raise WorkspacePolicyError("Workspace is configured read-only.")

    def _ensure_collection(self, collection: str):
        collection_path = self.content_root / collection
        collection_path.mkdir(parents=True, exist_ok=True)
        descriptor = collection_path / "_collection.meta.json"
        if not descriptor.exists():
            descriptor.write_text(
                _json_dumps(
                    {
                        "schema_version": "agently.workspace.collection.v1",
                        "collection": collection,
                        "created_at": _utc_now(),
                    }
                ),
                encoding="utf-8",
            )

    def _resolve_content_path(self, path: str | Path):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.content_root / candidate
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(self.content_root)
        except ValueError as error:
            raise WorkspacePolicyError(f"Path is outside workspace content root: { path }") from error
        return resolved

    @staticmethod
    def _content_to_bytes(content: Any) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        return _json_dumps(content).encode("utf-8")

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            return content
        return _json_dumps(content)

    def _row_to_ref(self, row: sqlite3.Row) -> WorkspaceRecordRef:
        return {
            "id": str(row["id"]),
            "collection": str(row["collection"]),
            "kind": row["kind"],
            "path": row["path"],
            "sha256": row["sha256"],
            "size": int(row["size"] or 0),
            "summary": str(row["summary"] or ""),
            "scope": _json_loads(row["scope_json"], {}),
            "source": _json_loads(row["source_json"], {}),
            "created_at": str(row["created_at"]),
            "meta": _json_loads(row["meta_json"], {}),
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
        collection = _slug(collection, "artifacts")
        self._ensure_collection(collection)
        record_id = f"rec_{ uuid.uuid4().hex }"
        content_bytes = self._content_to_bytes(content)
        content_text = self._content_to_text(content)
        digest = hashlib.sha256(content_bytes).hexdigest()
        suffix = ".json" if not isinstance(content, (str, bytes)) else ".txt"
        file_name = f"{ record_id }-{ _slug(kind or collection, 'record') }{ suffix }"
        relative_path = f"{ collection }/{ file_name }"
        target = self._resolve_content_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content_bytes)
        created_at = _utc_now()
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
                    _json_dumps(scope or {}),
                    _json_dumps(source or {}),
                    _json_dumps(meta or {}),
                    created_at,
                    1 if collection == "checkpoints" else 0,
                ),
            )
            conn.execute(
                "INSERT INTO records_fts(record_id, summary, content) VALUES (?, ?, ?)",
                (record_id, record_summary, content_text),
            )
            conn.commit()
        return {
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
        target = self._resolve_content_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Workspace content not found: { path }")
        return target.read_text(encoding="utf-8", errors="replace")

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
        created_at = _utc_now()
        source_id = self._record_id(source)
        target_id = self._record_id(target)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO links(id, source_id, target_id, relation, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, source_id, target_id, relation, _json_dumps(meta or {}), created_at),
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
                (run_id, step_id, ref["id"], _json_dumps(state), ref["created_at"]),
            )
            conn.execute(
                "INSERT OR REPLACE INTO manifests(key, value_json) VALUES (?, ?)",
                (f"checkpoint.latest.{ run_id }", _json_dumps(ref)),
            )
            conn.commit()
        return ref


class FastIngestionProfile:
    name = "fast"

    async def ingest(self, *, workspace, content, collection, kind, scope, source, summary=None, meta=None):
        return await workspace.put(
            content,
            collection=collection,
            kind=kind,
            summary=summary,
            scope=scope,
            source=source,
            meta=meta,
        )


class CheckpointIngestionProfile:
    name = "checkpoint"

    async def ingest(self, *, workspace, content, collection, kind, scope, source, summary=None, meta=None):
        run_id = str(scope.get("run_id") or source.get("run_id") or "default")
        step_id = scope.get("step_id")
        state = content if isinstance(content, dict) else {"value": content}
        return await workspace.checkpoint(run_id, state, step_id=str(step_id) if step_id is not None else None)


class Workspace:
    """Workspace facade bound to one backend."""

    def __init__(self, backend: WorkspaceBackend, manager: "WorkspaceManager"):
        self.backend = backend
        self.manager = manager
        self.root = Path(str(getattr(backend, "root")))
        self.content_root = Path(str(getattr(backend, "content_root")))

    async def put(self, record_or_content: Any, *, collection: str, kind: str | None = None, meta: dict[str, Any] | None = None, **kwargs):
        return await self.backend.put(record_or_content, collection=collection, kind=kind, meta=meta, **kwargs)

    async def get(self, ref_or_path: WorkspaceRecordRef | str):
        return await self.backend.get(ref_or_path)

    async def search(self, query: str | None = None, filters: dict[str, Any] | None = None):
        return await self.backend.search(query, filters)

    async def link(self, source: WorkspaceRecordRef | str, target: WorkspaceRecordRef | str, relation: str, meta: dict[str, Any] | None = None):
        return await self.backend.link(source, target, relation, meta)

    async def checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
        return await self.backend.checkpoint(run_id, state, step_id=step_id)

    async def ingest(
        self,
        *,
        content: Any,
        collection: str,
        kind: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        summary: str | None = None,
        meta: dict[str, Any] | None = None,
        profile: str = "fast",
    ):
        handler = self.manager.get_profile(profile)
        return await handler.ingest(
            workspace=self,
            content=content,
            collection=collection,
            kind=kind,
            scope=scope or {},
            source=source or {},
            summary=summary,
            meta=meta,
        )


class WorkspaceManager:
    """Factory and registry for Workspace foundation capabilities."""

    def __init__(self):
        self._profiles: dict[str, IngestionProfile] = {}
        self.register_profile("fast", FastIngestionProfile())
        self.register_profile("checkpoint", CheckpointIngestionProfile())

    def create(
        self,
        path_or_backend: str | Path | WorkspaceBackend,
        *,
        create: bool = True,
        mode: str = "read_write",
    ) -> Workspace:
        if hasattr(path_or_backend, "put") and hasattr(path_or_backend, "search"):
            backend = cast(WorkspaceBackend, path_or_backend)
        else:
            backend = LocalWorkspaceBackend(path_or_backend, create=create, mode=mode)  # type: ignore[arg-type]
        return Workspace(backend, self)

    def register_profile(self, name: str, handler: IngestionProfile | Callable[..., Any]):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace profile name must be non-empty.")
        if not hasattr(handler, "ingest"):
            raise TypeError("Workspace profile handler must provide async ingest(...).")
        self._profiles[normalized] = handler  # type: ignore[assignment]
        return self

    def get_profile(self, name: str) -> IngestionProfile:
        normalized = str(name or "fast").strip() or "fast"
        if normalized not in self._profiles:
            raise WorkspaceConfigurationError(f"Workspace ingestion profile is not registered: { normalized }")
        return self._profiles[normalized]

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())
