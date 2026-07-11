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
import shutil
import sqlite3
import stat
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, AsyncIterator, cast

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.workspace import (
    WorkspaceBackendCapabilities,
    WorkspaceContentSegment,
    WorkspaceFileRef,
    WorkspaceFilePolicyMetadata,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetainedReference,
    WorkspaceRetentionAnchor,
    WorkspaceRetentionDiagnostic,
    WorkspaceRetentionLifecycle,
    WorkspaceRetentionPolicy,
    WorkspaceRetentionPreview,
    WorkspaceRetentionResult,
    WorkspaceRuntimeEventRecord,
    WorkspaceScratchLease,
)
from agently.utils import DataFormatter

from .Errors import WorkspaceConfigurationError, WorkspacePolicyError
from .Stores import (
    ChromaVectorStoreProvider,
    EmbeddingProviderUnavailableError,
    LocalContentStore,
    LocalWorkspacePolicyEngine,
    NoopVectorIndex,
    SQLiteVectorStoreProvider,
    VectorIndexPipeline,
    VectorStoreProviderUnavailableError,
)
from .Retention import (
    canonical_retention_fingerprint,
    empty_retention_selection,
    resolve_retention_policy,
    serialized_size,
)
from ._defaults import (
    SCOPE_LINEAGE_KINDS,
    WORKSPACE_FILE_AREAS,
    WORKSPACE_GUIDE_FILENAME,
    normalize_lineage,
    scope_filter_path_nodes,
)
from ._utils import json_dumps, json_loads, slug, utc_now


class LocalWorkspaceBackend:
    """Local filesystem content plus SQLite metadata and FTS index."""

    DEFAULT_COLLECTIONS = ("dialogue", "observations", "decisions", "artifacts", "checkpoints", "runtime_events")
    DB_STORE_PROVIDER_METHODS = frozenset(
        {
            "put_record",
            "get_record",
            "index_record",
            "search",
            "link",
            "link_evidence",
            "links",
            "checkpoint",
            "put_checkpoint",
            "get_checkpoint",
            "put_artifact_ref",
            "claim_lease",
            "heartbeat_lease",
            "release_lease",
            "put_snapshot",
            "get_snapshot",
            "latest_snapshot",
            "latest_checkpoint",
            "checkpoint_history",
            "append_runtime_event",
            "query_runtime_events",
            "record_file_policy",
            "get_file_policy",
            "add_retention_anchor",
            "retention_anchors",
            "inspect_retention",
            "apply_retention",
            "prune_scope",
            "register_scratch_lease",
            "get_scratch_lease",
            "list_scratch_leases",
            "close_scratch_lease",
        }
    )

    def __getattribute__(self, name: str) -> Any:
        if name in object.__getattribute__(self, "DB_STORE_PROVIDER_METHODS"):
            try:
                provider = object.__getattribute__(self, "db_store_provider")
            except AttributeError:
                provider = None
            if provider is not None and provider is not self:
                return getattr(provider, name)
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        create: bool = True,
        mode: str = "read_write",
        initialize_default_vector_store_provider: bool = True,
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
        self.db_store_provider = self
        self.db_store_provider_name = "sqlite"
        self.embedding_provider = None
        self.vector_store_fallback_reason: str | None = None
        self.vector_store_provider = None
        self.vector_store_provider_name = None
        self.metadata = self
        self.checkpoint_store = self
        self.runtime_event_store = self
        self.ref_resolver = self
        self.retention_policy = self
        self.evidence_linker = self
        self.text_index = self
        self.vector_index = self._default_vector_index()
        if create:
            self._initialize()
        elif not self.root.exists():
            raise WorkspaceConfigurationError(f"Workspace root does not exist: { self.root }")
        else:
            self.workspace_id = self._load_workspace_meta().get("workspace_id", self.workspace_id)
        if initialize_default_vector_store_provider:
            self.vector_store_provider = self._default_vector_store_provider(create=create)
        self.vector_store_provider_name = getattr(self.vector_store_provider, "name", None)
        self.vector_index = self._default_vector_index()

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
        self._ensure_root_guide()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema(conn)

    def _ensure_root_guide(self) -> None:
        guide_path = self.root / WORKSPACE_GUIDE_FILENAME
        if guide_path.exists():
            return
        area_lines = [
            f"- { name }/: { description }"
            for name, description in sorted(WORKSPACE_FILE_AREAS.items())
        ]
        guide_path.write_text(
            "\n".join(
                [
                    "# Agently Workspace",
                    "",
                    "This directory is managed by Agently.",
                    "",
                    "Directory roles:",
                    "",
                    "- workspace.db: local metadata, search index, links, checkpoints, and runtime events.",
                    "- workspace.meta.json: machine-readable Workspace metadata.",
                    "- content/: managed record payloads owned by Workspace.",
                    "- files/: editable file working trees scoped by lineage.",
                    "",
                    "Standard file areas inside each scoped files root:",
                    *area_lines,
                    "",
                    "Use files/lineage/.../files for task artifacts, downloads, and files shared with Actions or external coding agents.",
                    "Use scratch/lineage/.../scratch only through Workspace scratch APIs; do not mix scratch files into files/.",
                    "Do not edit workspace.db or content/ directly unless you are debugging Workspace internals.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _default_workspace_id(self):
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:24]
        return f"ws_{ digest }"

    def _default_vector_store_provider(self, *, create: bool):
        try:
            store = ChromaVectorStoreProvider(
                self.root / "vectors" / "chroma",
                create=create,
                mode=self.mode,
            )
            self.vector_store_fallback_reason = None
            return store
        except Exception as error:
            self.vector_store_fallback_reason = f"chroma_unavailable:{type(error).__name__}"
            return SQLiteVectorStoreProvider(
                self.db_path,
                read_only=self.read_only,
                create=create,
            )

    def _default_vector_index(self):
        if self.vector_store_provider is None:
            return NoopVectorIndex()
        return VectorIndexPipeline(
            embedding_provider=self.embedding_provider,
            vector_store_provider=self.vector_store_provider,
        )

    def configure_components(
        self,
        *,
        db_store_provider: Any | None = None,
        db_store_provider_name: str | None = None,
        embedding_provider: Any | None = None,
        vector_store_provider: Any | None = None,
        vector_store_provider_name: str | None = None,
        vector_store_fallback_reason: str | None = None,
    ) -> None:
        if db_store_provider is not None:
            self.db_store_provider = db_store_provider
        if db_store_provider_name is not None:
            self.db_store_provider_name = db_store_provider_name
        self.embedding_provider = embedding_provider
        if vector_store_provider is not None:
            self.vector_store_provider = vector_store_provider
        self.vector_store_provider_name = vector_store_provider_name or getattr(self.vector_store_provider, "name", None)
        self.vector_store_fallback_reason = vector_store_fallback_reason
        self.vector_index = self._default_vector_index()

    def _load_workspace_meta(self):
        meta_path = self.root / "workspace.meta.json"
        if not meta_path.exists():
            return {}
        return json_loads(meta_path.read_text(encoding="utf-8"), {})

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
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
            CREATE TABLE IF NOT EXISTS record_scope_index (
                record_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                scope_value TEXT NOT NULL,
                PRIMARY KEY(record_id, scope_key)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS record_scope_index_lookup_idx
            ON record_scope_index(scope_key, scope_value, record_id)
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
                state_version INTEGER,
                idempotency_key TEXT,
                parent_id TEXT,
                causation_id TEXT,
                parent_signal_id TEXT,
                node_id TEXT,
                operator_id TEXT,
                interrupt_id TEXT,
                resume_request_id TEXT,
                actor_id TEXT,
                lease_owner_id TEXT,
                aggregation_scope TEXT,
                snapshot_ref_json TEXT,
                exchange_id TEXT,
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                persisted_at TEXT
            )
            """
        )
        self._ensure_runtime_event_schema(conn)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch_leases (
                lease_id TEXT PRIMARY KEY,
                scope_json TEXT NOT NULL DEFAULT '{}',
                local_path TEXT,
                mount_json TEXT,
                purpose TEXT,
                cleanup_policy TEXT NOT NULL DEFAULT 'on_close',
                expires_at TEXT,
                read_only INTEGER NOT NULL DEFAULT 0,
                policy_labels_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS scratch_leases_open_idx
            ON scratch_leases(closed_at, expires_at)
            """
        )
        self._backfill_scope_index(conn)
        conn.commit()

    def _ensure_runtime_event_schema(self, conn: sqlite3.Connection):
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(runtime_events)").fetchall()
        }
        for column, column_type in {
            "state_version": "INTEGER",
            "parent_signal_id": "TEXT",
            "operator_id": "TEXT",
            "interrupt_id": "TEXT",
            "resume_request_id": "TEXT",
            "actor_id": "TEXT",
            "lease_owner_id": "TEXT",
            "snapshot_ref_json": "TEXT",
            "persisted_at": "TEXT",
        }.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE runtime_events ADD COLUMN { column } { column_type }")

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
        vector_search = vector_index is not None and getattr(vector_index, "name", None) != "noop"
        if isinstance(vector_index, VectorIndexPipeline):
            vector_search = self.embedding_provider is not None and self.vector_store_provider is not None
        return {
            "structured_get_data": True,
            "links_query": True,
            "checkpoint_lookup": True,
            "metadata_filters": True,
            "text_search": True,
            "vector_search": vector_search,
            "workspace_reference_envelopes": True,
            "bounded_read": True,
            "stream_read": True,
            "runtime_event_store": True,
            "runtime_event_idempotency": True,
            "snapshot_store": True,
            "evidence_links": True,
            "file_policy_metadata": True,
            "retention_anchors": True,
            "supports_cas": True,
            "supports_lease": True,
            "supports_artifact_refs": True,
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
            try:
                return event.model_dump(mode="json")  # type: ignore[union-attr]
            except Exception:
                sanitized = DataFormatter.sanitize(event.model_dump(mode="python"))  # type: ignore[union-attr]
                return sanitized if isinstance(sanitized, dict) else {"value": sanitized}
        sanitized = DataFormatter.sanitize(dict(event))
        return sanitized if isinstance(sanitized, dict) else {"value": sanitized}

    def _row_to_runtime_event_record(self, row: sqlite3.Row) -> WorkspaceRuntimeEventRecord:
        snapshot_ref = json_loads(row["snapshot_ref_json"], None)
        return {
            "id": str(row["id"]),
            "execution_id": str(row["execution_id"]),
            "sequence": int(row["sequence"]),
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "state_version": row["state_version"],
            "idempotency_key": row["idempotency_key"],
            "parent_id": row["parent_id"],
            "causation_id": row["causation_id"],
            "parent_signal_id": row["parent_signal_id"],
            "node_id": row["node_id"],
            "operator_id": row["operator_id"],
            "interrupt_id": row["interrupt_id"],
            "resume_request_id": row["resume_request_id"],
            "actor_id": row["actor_id"],
            "lease_owner_id": row["lease_owner_id"],
            "aggregation_scope": row["aggregation_scope"],
            "snapshot_ref": snapshot_ref,
            "exchange_id": row["exchange_id"],
            "artifact_refs": json_loads(row["artifact_refs_json"], []),
            "event": json_loads(row["event_json"], {}),
            "created_at": str(row["created_at"]),
            "persisted_at": row["persisted_at"],
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

    @staticmethod
    def _scope_index_value(value: Any) -> str:
        return json_dumps(value)

    @staticmethod
    def _replace_scope_index_on_conn(conn: sqlite3.Connection, record_id: str, scope: dict[str, Any]) -> None:
        conn.execute("DELETE FROM record_scope_index WHERE record_id = ?", (record_id,))
        for key, value in scope.items():
            if value is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO record_scope_index(record_id, scope_key, scope_value)
                VALUES (?, ?, ?)
                """,
                (record_id, str(key), LocalWorkspaceBackend._scope_index_value(value)),
            )

    def _backfill_scope_index(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) AS count FROM record_scope_index").fetchone()
        if row is not None and int(row["count"] or 0) > 0:
            return
        rows = conn.execute("SELECT id, scope_json FROM records").fetchall()
        for record in rows:
            scope = json_loads(record["scope_json"], {})
            if isinstance(scope, dict):
                self._replace_scope_index_on_conn(conn, str(record["id"]), scope)

    def _get_manifest(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM manifests WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json_loads(row["value_json"], default)

    @staticmethod
    def _manifest_from_conn(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value_json FROM manifests WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json_loads(row["value_json"], default)

    @staticmethod
    def _set_manifest_on_conn(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO manifests(key, value_json) VALUES (?, ?)",
            (key, json_dumps(value)),
        )

    def _set_manifest(self, key: str, value: Any) -> None:
        self._ensure_writable()
        with self._connect() as conn:
            self._set_manifest_on_conn(conn, key, value)
            conn.commit()

    @staticmethod
    def _checkpoint_state_version(state: Any) -> int | None:
        if not isinstance(state, dict):
            return None
        value = state.get("state_version")
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    def _latest_checkpoint_state_version(self, conn: sqlite3.Connection, run_id: str) -> int | None:
        row = conn.execute(
            """
            SELECT state_json FROM checkpoints
            WHERE run_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return 0
        return self._checkpoint_state_version(json_loads(row["state_json"], {}))

    def _ensure_expected_checkpoint_state_version(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        expected_state_version: int | None,
    ) -> None:
        if expected_state_version is None:
            return
        current_state_version = self._latest_checkpoint_state_version(conn, run_id)
        if current_state_version != expected_state_version:
            raise RuntimeError(
                f"Workspace checkpoint state version conflict for run '{ run_id }': "
                f"expected { expected_state_version }, current state version is { current_state_version }."
            )

    @staticmethod
    def _lease_manifest_key(run_id: str) -> str:
        return f"lease.{ run_id }"

    def _require_active_lease(
        self,
        lease: Any,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        now: float,
    ) -> WorkspaceLeaseRef:
        if not isinstance(lease, dict) or lease.get("released_at") is not None:
            raise RuntimeError(f"Workspace lease for run '{ run_id }' is not active.")
        if float(lease.get("lease_until") or 0) <= now:
            raise RuntimeError(f"Workspace lease for run '{ run_id }' has expired.")
        if lease.get("owner_id") != owner_id or lease.get("lease_token") != lease_token:
            raise RuntimeError(f"Workspace lease conflict for run '{ run_id }'.")
        return cast(WorkspaceLeaseRef, lease)

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
        await self.put_record(ref)
        await self.index_record(ref, content_text)
        try:
            await self.vector_index.index_record(ref, content_text)
        except (EmbeddingProviderUnavailableError, VectorStoreProviderUnavailableError):
            pass
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
            self._replace_scope_index_on_conn(conn, ref["id"], ref["scope"])
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
        if filters.get("id") is not None:
            clauses.append("r.id = ?")
            params.append(str(filters["id"]))
        if filters.get("path") is not None:
            clauses.append("r.path = ?")
            params.append(str(filters["path"]))
        if filters.get("collection") is not None:
            clauses.append("r.collection = ?")
            params.append(str(filters["collection"]))
        if filters.get("kind") is not None:
            clauses.append("r.kind = ?")
            params.append(str(filters["kind"]))
        scope_filter_keys: set[str] = set()
        scope_index = 0
        for key, value in filters.items():
            if not key.startswith("scope."):
                continue
            scope_key = key.split(".", 1)[1]
            scope_filter_keys.add(key)
            alias = f"s{scope_index}"
            scope_index += 1
            clauses.append(
                f"""
                EXISTS (
                    SELECT 1 FROM record_scope_index {alias}
                    WHERE {alias}.record_id = r.id
                    AND {alias}.scope_key = ?
                    AND {alias}.scope_value = ?
                )
                """
            )
            params.extend([scope_key, self._scope_index_value(value)])
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
            if key in {"id", "path", "collection", "kind"} or key in scope_filter_keys:
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
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        self._ensure_writable()
        with self._connect() as conn:
            self._ensure_expected_checkpoint_state_version(
                conn,
                run_id=run_id,
                expected_state_version=expected_state_version,
            )
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
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        return await self.checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )

    async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        return await self.put_checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        ref = await self.latest_snapshot(run_id)
        if ref is None:
            return None
        state = await self.get_data(ref)
        return state if isinstance(state, dict) else None

    async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

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

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef:
        metadata = dict(metadata or {})
        kind = str(metadata.pop("kind", "runtime_artifact"))
        summary = metadata.pop("summary", f"Artifact for { run_id }")
        scope = metadata.pop("scope", {})
        if not isinstance(scope, dict):
            scope = {}
        source = metadata.pop("source", {})
        if not isinstance(source, dict):
            source = {}
        source = {"type": "workspace", "name": "artifact_ref", **source}
        return await self.put(
            artifact,
            collection="artifacts",
            kind=kind,
            summary=str(summary),
            scope={"run_id": run_id, **scope},
            source=source,
            meta={"artifact_ref": True, **metadata},
        )

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> WorkspaceLeaseRef:
        self._ensure_writable()
        if not owner_id:
            raise ValueError("owner_id must be non-empty.")
        if ttl <= 0:
            raise ValueError("ttl must be greater than 0.")
        now = time.time()
        lease_key = self._lease_manifest_key(run_id)
        with self._connect() as conn:
            self._ensure_expected_checkpoint_state_version(
                conn,
                run_id=run_id,
                expected_state_version=expected_state_version,
            )
            current = self._manifest_from_conn(conn, lease_key, None)
            if (
                isinstance(current, dict)
                and current.get("released_at") is None
                and float(current.get("lease_until") or 0) > now
                and current.get("owner_id") != owner_id
            ):
                raise RuntimeError(f"Workspace lease conflict for run '{ run_id }'.")
            timestamp = utc_now()
            lease: WorkspaceLeaseRef = {
                "run_id": run_id,
                "owner_id": owner_id,
                "lease_token": uuid.uuid4().hex,
                "lease_ttl": float(ttl),
                "lease_until": now + float(ttl),
                "claimed_at": timestamp,
                "heartbeat_at": timestamp,
                "released_at": None,
                "state_version": self._latest_checkpoint_state_version(conn, run_id),
            }
            self._set_manifest_on_conn(conn, lease_key, lease)
            conn.commit()
        return lease

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        self._ensure_writable()
        now = time.time()
        lease_key = self._lease_manifest_key(run_id)
        with self._connect() as conn:
            active_lease = self._require_active_lease(
                self._manifest_from_conn(conn, lease_key, None),
                run_id=run_id,
                owner_id=owner_id,
                lease_token=lease_token,
                now=now,
            )
            lease: dict[str, Any] = dict(active_lease)
            lease["heartbeat_at"] = utc_now()
            lease_ttl = lease.get("lease_ttl")
            lease["lease_until"] = now + float(lease_ttl if isinstance(lease_ttl, (int, float, str)) else 0)
            self._set_manifest_on_conn(conn, lease_key, lease)
            conn.commit()
        return cast(WorkspaceLeaseRef, lease)

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        self._ensure_writable()
        now = time.time()
        lease_key = self._lease_manifest_key(run_id)
        with self._connect() as conn:
            active_lease = self._require_active_lease(
                self._manifest_from_conn(conn, lease_key, None),
                run_id=run_id,
                owner_id=owner_id,
                lease_token=lease_token,
                now=now,
            )
            lease: dict[str, Any] = dict(active_lease)
            lease["released_at"] = utc_now()
            lease["lease_until"] = now
            self._set_manifest_on_conn(conn, lease_key, lease)
            conn.commit()
        return cast(WorkspaceLeaseRef, lease)

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
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        snapshot_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        exchange_id: str | None = None,
        state_version: int | None = None,
        parent_id: str | None = None,
        causation_id: str | None = None,
        parent_signal_id: str | None = None,
        node_id: str | None = None,
        operator_id: str | None = None,
        interrupt_id: str | None = None,
        resume_request_id: str | None = None,
        actor_id: str | None = None,
        lease_owner_id: str | None = None,
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
        resolved_parent_id = parent_id or meta.get("parent_event_id") or meta.get("parent_id")
        resolved_causation_id = causation_id or meta.get("causation_id")
        resolved_snapshot_ref = await self._coerce_ref_envelope(snapshot_ref)
        resolved_artifact_refs = [
            envelope
            for envelope in [
                await self._coerce_ref_envelope(ref)
                for ref in (artifact_refs or [])
            ]
            if envelope is not None
        ]
        created_at = utc_now()
        persisted_at = created_at
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
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM runtime_events WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            next_sequence = int(row["max_sequence"] or 0) + 1
            if expected_sequence is not None and int(expected_sequence) != next_sequence:
                raise RuntimeError(
                    f"Workspace runtime event sequence conflict for execution '{ execution_id }': "
                    f"expected { expected_sequence }, next sequence is { next_sequence }."
                )
            if sequence is None:
                sequence = next_sequence
            record_id = f"rtevt_{ uuid.uuid4().hex }"
            conn.execute(
                """
                INSERT INTO runtime_events (
                    id, execution_id, sequence, event_id, event_type, idempotency_key,
                    parent_id, causation_id, parent_signal_id, node_id, operator_id,
                    interrupt_id, resume_request_id, actor_id, lease_owner_id, state_version,
                    aggregation_scope, snapshot_ref_json,
                    exchange_id, artifact_refs_json, event_json, created_at, persisted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    execution_id,
                    sequence,
                    event_id,
                    event_type,
                    idempotency_key,
                    resolved_parent_id,
                    resolved_causation_id,
                    parent_signal_id or meta.get("parent_signal_id"),
                    node_id or meta.get("node_id"),
                    operator_id or meta.get("operator_id"),
                    interrupt_id or meta.get("interrupt_id"),
                    resume_request_id or meta.get("resume_request_id"),
                    actor_id or meta.get("actor_id"),
                    lease_owner_id or meta.get("lease_owner_id"),
                    state_version,
                    aggregation_scope or meta.get("aggregation_scope"),
                    json_dumps(resolved_snapshot_ref) if resolved_snapshot_ref is not None else None,
                    exchange_id or meta.get("exchange_id"),
                    json_dumps(resolved_artifact_refs),
                    json_dumps(event_dict),
                    created_at,
                    persisted_at,
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

    @staticmethod
    def _retention_diagnostic(
        code: str,
        message: str,
        *,
        entity: str,
        detail: dict[str, Any] | None = None,
    ) -> WorkspaceRetentionDiagnostic:
        diagnostic: WorkspaceRetentionDiagnostic = {
            "code": code,
            "message": message,
            "retryable": True,
            "entity": entity,
        }
        if detail:
            diagnostic["detail"] = detail
        return diagnostic

    def _safe_resolve_path(
        self,
        path: Path,
        *,
        entity: str,
        operation: str,
        code: str = "workspace.retention.ref_readback_failed",
    ) -> tuple[Path | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return path.expanduser().resolve(), None
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    def _safe_walk_paths(
        self,
        root: Path,
        pattern: str,
        *,
        entity: str,
        operation: str,
        code: str = "workspace.retention.ref_readback_failed",
    ) -> tuple[list[Path] | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return list(root.rglob(pattern)), None
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    def _safe_stat_path(
        self,
        path: Path,
        *,
        entity: str,
        operation: str,
        follow_symlinks: bool = True,
        code: str = "workspace.retention.ref_readback_failed",
        missing_message: str | None = None,
        missing_ok: bool = False,
    ) -> tuple[Any | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return (path.stat() if follow_symlinks else path.lstat()), None
        except FileNotFoundError as error:
            if missing_ok:
                return None, None
            if missing_message is not None:
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    missing_message,
                    entity=entity,
                )
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    def _safe_read_path(
        self,
        path: Path,
        *,
        entity: str,
        operation: str,
    ) -> tuple[bytes | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return path.read_bytes(), None
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    @staticmethod
    def _deduplicate_retained_refs(
        retained_refs: Sequence[WorkspaceRetainedReference],
    ) -> list[WorkspaceRetainedReference]:
        canonical = {json_dumps(ref): ref for ref in retained_refs}
        return [canonical[key] for key in sorted(canonical)]

    def _retention_preview(
        self,
        *,
        status: str,
        scope: dict[str, Any],
        lifecycle: WorkspaceRetentionLifecycle,
        policy: WorkspaceRetentionPolicy,
        retained_refs: list[WorkspaceRetainedReference],
        inline_result: Any,
        diagnostics: list[WorkspaceRetentionDiagnostic] | None = None,
        selected: dict[str, list[str]] | None = None,
        logical_bytes: int = 0,
    ) -> WorkspaceRetentionPreview:
        resolved_selected = selected if status == "ready" and selected is not None else empty_retention_selection()
        for key in resolved_selected:
            resolved_selected[key] = sorted(set(resolved_selected[key]))
        resolved_diagnostics = sorted(
            diagnostics or [],
            key=lambda item: (
                str(item.get("code") or ""),
                str(item.get("entity") or ""),
                str(item.get("message") or ""),
            ),
        )
        plan_fingerprint = canonical_retention_fingerprint(
            scope,
            lifecycle,
            policy,
            retained_refs,
            resolved_selected,
        )
        return cast(
            WorkspaceRetentionPreview,
            {
                "status": status,
                "plan_fingerprint": plan_fingerprint,
                "scope": scope,
                "lifecycle": lifecycle,
                "policy": policy,
                "retained_refs": retained_refs,
                "inline_result": inline_result,
                "selected": resolved_selected,
                "accounting": {
                    "entities": {
                        key: len(values) for key, values in sorted(resolved_selected.items())
                    },
                    "logical_bytes_deleted": logical_bytes if status == "ready" else 0,
                    "physical_bytes_reclaimed": 0,
                    "physical_bytes_pending": 0,
                },
                "diagnostics": resolved_diagnostics,
            },
        )

    async def _verified_record_envelope_unchecked(
        self,
        ref: WorkspaceRecordRef,
    ) -> tuple[WorkspaceReferenceEnvelope, WorkspaceRetentionDiagnostic | None]:
        envelope = await self.ref_envelope(ref)
        path = ref.get("path")
        if not path:
            if int(ref.get("size") or 0) != 0 or ref.get("sha256") is not None:
                return envelope, self._retention_diagnostic(
                    "workspace.retention.ref_readback_failed",
                    "Workspace record has content facts but no contained content path.",
                    entity=ref["id"],
                )
            return envelope, None
        target, diagnostic = self._safe_resolve_path(
            self.content_root / str(path),
            entity=ref["id"],
            operation="record content resolution",
        )
        if diagnostic is not None or target is None:
            return envelope, diagnostic
        try:
            target.relative_to(self.content_root)
        except ValueError:
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                "Workspace record content path is outside the Workspace content root.",
                entity=ref["id"],
            )
        target_stat, diagnostic = self._safe_stat_path(
            target,
            entity=ref["id"],
            operation="record content stat",
            missing_message="Workspace record content is missing.",
        )
        if diagnostic is not None:
            return envelope, diagnostic
        if target_stat is None or not stat.S_ISREG(target_stat.st_mode):
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_missing",
                "Workspace record content is missing.",
                entity=ref["id"],
            )
        raw, diagnostic = self._safe_read_path(
            target,
            entity=ref["id"],
            operation="record content readback",
        )
        if diagnostic is not None or raw is None:
            return envelope, diagnostic
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != int(ref.get("size") or 0):
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_size_mismatch",
                "Workspace record content size does not match its persisted ref.",
                entity=ref["id"],
            )
        if ref.get("sha256") != digest:
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_digest_mismatch",
                "Workspace record content digest does not match its persisted ref.",
                entity=ref["id"],
            )
        return envelope, None

    async def _verified_record_envelope(
        self,
        ref: WorkspaceRecordRef,
    ) -> tuple[WorkspaceReferenceEnvelope, WorkspaceRetentionDiagnostic | None]:
        try:
            return await self._verified_record_envelope_unchecked(ref)
        except (OSError, RuntimeError, WorkspaceConfigurationError, WorkspacePolicyError) as error:
            return self._record_ref_envelope(ref), self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                f"Workspace record readback failed: { error }",
                entity=str(ref.get("id") or ""),
            )

    async def _verify_retained_ref_unchecked(
        self,
        ref: WorkspaceRetainedReference,
        *,
        records_by_id: Mapping[str, WorkspaceRecordRef] | None = None,
    ) -> tuple[
        WorkspaceRetainedReference | None,
        str | None,
        str | None,
        WorkspaceRetentionDiagnostic | None,
    ]:
        if "workspace_id" in ref:
            envelope_ref = cast(WorkspaceReferenceEnvelope, ref)
            if str(envelope_ref.get("workspace_id") or "") != self.workspace_id:
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_workspace_mismatch",
                    "Retained ref belongs to another Workspace.",
                    entity=str(envelope_ref.get("record_id") or envelope_ref.get("content_ref") or ""),
                )
            record_id = str(envelope_ref.get("record_id") or "")
            if record_id:
                actual = (
                    records_by_id.get(record_id)
                    if records_by_id is not None
                    else await self.get_record(record_id)
                )
                if actual is None:
                    return None, None, None, self._retention_diagnostic(
                        "workspace.retention.ref_missing",
                        "Retained Workspace record does not exist.",
                        entity=record_id,
                    )
                actual_envelope, diagnostic = await self._verified_record_envelope(actual)
                if diagnostic is not None:
                    return None, None, None, diagnostic
                if envelope_ref.get("digest") != actual_envelope.get("digest"):
                    return None, None, None, self._retention_diagnostic(
                        "workspace.retention.ref_digest_mismatch",
                        "Retained Workspace envelope digest does not match readback.",
                        entity=record_id,
                    )
                if int(envelope_ref.get("size") or 0) != int(actual_envelope.get("size") or 0):
                    return None, None, None, self._retention_diagnostic(
                        "workspace.retention.ref_size_mismatch",
                        "Retained Workspace envelope size does not match readback.",
                        entity=record_id,
                    )
                if envelope_ref.get("content_ref") != actual_envelope.get("content_ref"):
                    return None, None, None, self._retention_diagnostic(
                        "workspace.retention.ref_path_mismatch",
                        "Retained Workspace envelope content path does not match readback.",
                        entity=record_id,
                    )
                return actual_envelope, record_id, None, None
            content_ref = str(envelope_ref.get("content_ref") or "")
            if not content_ref:
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "Retained Workspace envelope has no record or content identity.",
                    entity="retained_ref",
                )
            target, diagnostic = self._safe_resolve_path(
                self.content_root / content_ref,
                entity=content_ref,
                operation="retained content resolution",
            )
            if diagnostic is not None or target is None:
                return None, None, None, diagnostic
            try:
                target.relative_to(self.content_root)
            except ValueError:
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_readback_failed",
                    "Retained content ref is outside the Workspace content root.",
                    entity=content_ref,
                )
            target_stat, diagnostic = self._safe_stat_path(
                target,
                entity=content_ref,
                operation="retained content stat",
                missing_message="Retained Workspace content does not exist.",
            )
            if diagnostic is not None:
                return None, None, None, diagnostic
            if target_stat is None or not stat.S_ISREG(target_stat.st_mode):
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "Retained Workspace content does not exist.",
                    entity=content_ref,
                )
            actual_envelope = await self.ref_envelope(content_ref)
            if envelope_ref.get("digest") != actual_envelope.get("digest"):
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_digest_mismatch",
                    "Retained content envelope digest does not match readback.",
                    entity=content_ref,
                )
            if int(envelope_ref.get("size") or 0) != int(actual_envelope.get("size") or 0):
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_size_mismatch",
                    "Retained content envelope size does not match readback.",
                    entity=content_ref,
                )
            return actual_envelope, None, None, None

        if "id" in ref:
            record_ref = cast(WorkspaceRecordRef, ref)
            record_id = str(record_ref.get("id") or "")
            actual = (
                records_by_id.get(record_id)
                if records_by_id is not None
                else await self.get_record(record_id)
            )
            if actual is None:
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "Retained Workspace record does not exist.",
                    entity=record_id,
                )
            _, diagnostic = await self._verified_record_envelope(actual)
            if diagnostic is not None:
                return None, None, None, diagnostic
            if record_ref.get("sha256") != actual.get("sha256"):
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_digest_mismatch",
                    "Retained Workspace record digest does not match readback.",
                    entity=record_id,
                )
            if int(record_ref.get("size") or 0) != int(actual.get("size") or 0):
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_size_mismatch",
                    "Retained Workspace record size does not match readback.",
                    entity=record_id,
                )
            if record_ref.get("path") != actual.get("path"):
                return None, None, None, self._retention_diagnostic(
                    "workspace.retention.ref_path_mismatch",
                    "Retained Workspace record path does not match readback.",
                    entity=record_id,
                )
            return actual, record_id, None, None

        file_ref = cast(WorkspaceFileRef, ref)
        relative_path = str(file_ref.get("path") or "")
        candidate = Path(relative_path)
        target = candidate if candidate.is_absolute() else self.files_root / candidate
        target, diagnostic = self._safe_resolve_path(
            target,
            entity=relative_path,
            operation="retained file resolution",
        )
        if diagnostic is not None or target is None:
            return None, None, None, diagnostic
        try:
            normalized_path = target.relative_to(self.files_root).as_posix()
        except ValueError:
            return None, None, None, self._retention_diagnostic(
                "workspace.retention.file_ref_invalid",
                "Retained file ref is outside the Workspace file root.",
                entity=relative_path,
            )
        target_stat, diagnostic = self._safe_stat_path(
            target,
            entity=normalized_path,
            operation="retained file stat",
            missing_message="Retained Workspace file does not exist.",
        )
        if diagnostic is not None:
            return None, None, None, diagnostic
        if target_stat is None or not stat.S_ISREG(target_stat.st_mode):
            return None, None, None, self._retention_diagnostic(
                "workspace.retention.ref_missing",
                "Retained Workspace file does not exist.",
                entity=normalized_path,
            )
        raw, diagnostic = self._safe_read_path(
            target,
            entity=normalized_path,
            operation="retained file readback",
        )
        if diagnostic is not None or raw is None:
            return None, None, None, diagnostic
        if int(file_ref.get("bytes") or 0) != len(raw):
            return None, None, None, self._retention_diagnostic(
                "workspace.retention.ref_size_mismatch",
                "Retained Workspace file size does not match readback.",
                entity=normalized_path,
            )
        digest = hashlib.sha256(raw).hexdigest()
        if str(file_ref.get("sha256") or "") != digest:
            return None, None, None, self._retention_diagnostic(
                "workspace.retention.ref_digest_mismatch",
                "Retained Workspace file digest does not match readback.",
                entity=normalized_path,
            )
        canonical_file_ref = dict(file_ref)
        canonical_file_ref["path"] = normalized_path
        return cast(WorkspaceFileRef, canonical_file_ref), None, normalized_path, None

    async def _verify_retained_ref(
        self,
        ref: WorkspaceRetainedReference,
        *,
        records_by_id: Mapping[str, WorkspaceRecordRef] | None = None,
    ) -> tuple[
        WorkspaceRetainedReference | None,
        str | None,
        str | None,
        WorkspaceRetentionDiagnostic | None,
    ]:
        try:
            return await self._verify_retained_ref_unchecked(
                ref,
                records_by_id=records_by_id,
            )
        except (OSError, RuntimeError, WorkspaceConfigurationError, WorkspacePolicyError) as error:
            entity = str(
                ref.get("id")
                or ref.get("record_id")
                or ref.get("content_ref")
                or ref.get("path")
                or "retained_ref"
            )
            return None, None, None, self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                f"Workspace retained-ref readback failed: { error }",
                entity=entity,
            )

    def _scoped_area_files(
        self,
        scope: dict[str, Any],
        area: str,
    ) -> tuple[list[str], WorkspaceRetentionDiagnostic | None]:
        area_root, diagnostic = self._safe_resolve_path(
            self.root / area,
            entity=area,
            operation="lineage area resolution",
        )
        if diagnostic is not None or area_root is None:
            return [], diagnostic
        lineage_root = area_root / "lineage"
        lineage_stat, diagnostic = self._safe_stat_path(
            lineage_root,
            entity=str(lineage_root),
            operation="lineage root stat",
            follow_symlinks=False,
            code="workspace.retention.lineage_ambiguous",
            missing_ok=True,
        )
        if diagnostic is not None:
            return [], diagnostic
        if lineage_stat is None:
            return [], None
        if stat.S_ISLNK(lineage_stat.st_mode) or not stat.S_ISDIR(lineage_stat.st_mode):
            return [], self._retention_diagnostic(
                "workspace.retention.lineage_ambiguous",
                "Workspace lineage root must be a real directory, not a symlink.",
                entity=str(lineage_root),
            )
        candidate_roots: list[Path] = []
        raw_lineage = scope.get("scope_lineage")
        if raw_lineage is not None:
            if not isinstance(raw_lineage, list) or not raw_lineage:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope_lineage must be a non-empty ordered list.",
                    entity=area,
                )
            lineage = normalize_lineage(raw_lineage)
            if len(lineage) != len(raw_lineage) or any(
                not isinstance(item, Mapping) or item.get("id") is None for item in raw_lineage
            ):
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope_lineage contains an incomplete node.",
                    entity=area,
                )
            candidate = lineage_root
            for node in lineage:
                scope_key = SCOPE_LINEAGE_KINDS.get(node["kind"])
                if scope_key is not None and scope_key in scope and str(scope[scope_key]) != node["id"]:
                    return [], self._retention_diagnostic(
                        "workspace.retention.lineage_ambiguous",
                        "Workspace cleanup scope conflicts with its ordered scope_lineage.",
                        entity=scope_key,
                    )
                candidate = candidate / slug(node["kind"], "scope") / slug(node["id"], "default")
            candidate_roots = [candidate]
        else:
            nodes = scope_filter_path_nodes(scope)
            if not nodes:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope cannot identify a contained lineage subtree.",
                    entity=area,
                )
            matched_candidates: set[Path] = set()
            required_pairs = {
                (slug(node["kind"], "scope"), slug(node["id"], "default"))
                for node in nodes
            }
            for leaf in nodes:
                leaf_kind = slug(leaf["kind"], "scope")
                leaf_id = slug(leaf["id"], "default")
                candidates, diagnostic = self._safe_walk_paths(
                    lineage_root,
                    leaf_id,
                    entity=str(lineage_root),
                    operation="lineage discovery",
                )
                if diagnostic is not None or candidates is None:
                    return [], diagnostic
                for candidate in candidates:
                    candidate_stat, diagnostic = self._safe_stat_path(
                        candidate,
                        entity=str(candidate),
                        operation="lineage candidate stat",
                        follow_symlinks=False,
                        code="workspace.retention.lineage_ambiguous",
                        missing_ok=True,
                    )
                    if diagnostic is not None:
                        return [], diagnostic
                    if (
                        candidate_stat is None
                        or stat.S_ISLNK(candidate_stat.st_mode)
                        or not stat.S_ISDIR(candidate_stat.st_mode)
                        or candidate.parent.name != leaf_kind
                    ):
                        continue
                    parts = candidate.relative_to(lineage_root).parts
                    lineage_pairs = set(zip(parts[0::2], parts[1::2]))
                    if required_pairs.issubset(lineage_pairs):
                        matched_candidates.add(candidate)
            candidate_roots = sorted(matched_candidates)
            if not candidate_roots:
                return [], None
            if len(candidate_roots) != 1:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope matches multiple lineage subtrees.",
                    entity=area,
                )

        paths: set[str] = set()
        for candidate in candidate_roots:
            lexical_parts = (lineage_root,) + tuple(
                lineage_root.joinpath(*candidate.relative_to(lineage_root).parts[:index])
                for index in range(1, len(candidate.relative_to(lineage_root).parts) + 1)
            )
            candidate_missing = False
            for lexical_path in lexical_parts:
                lexical_stat, diagnostic = self._safe_stat_path(
                    lexical_path,
                    entity=str(lexical_path),
                    operation="lineage lexical-component stat",
                    follow_symlinks=False,
                    code="workspace.retention.lineage_ambiguous",
                    missing_ok=True,
                )
                if diagnostic is not None:
                    return [], diagnostic
                if lexical_stat is None:
                    candidate_missing = True
                    break
                if stat.S_ISLNK(lexical_stat.st_mode):
                    return [], self._retention_diagnostic(
                        "workspace.retention.lineage_ambiguous",
                        "Workspace cleanup does not traverse symlinks in a lineage path.",
                        entity=str(lexical_path),
                    )
            if candidate_missing:
                continue
            resolved_candidate, diagnostic = self._safe_resolve_path(
                candidate,
                entity=str(candidate),
                operation="lineage subtree resolution",
                code="workspace.retention.lineage_ambiguous",
            )
            if diagnostic is not None or resolved_candidate is None:
                return [], diagnostic
            try:
                resolved_candidate.relative_to(lineage_root)
            except ValueError as error:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    f"Workspace lineage subtree is not contained: { error }",
                    entity=str(candidate),
                )
            descendants, diagnostic = self._safe_walk_paths(
                candidate,
                "*",
                entity=str(candidate),
                operation="lineage readback",
            )
            if diagnostic is not None or descendants is None:
                return [], diagnostic
            for path in descendants:
                path_stat, diagnostic = self._safe_stat_path(
                    path,
                    entity=str(path),
                    operation="lineage descendant stat",
                    follow_symlinks=False,
                    missing_ok=True,
                )
                if diagnostic is not None:
                    return [], diagnostic
                if path_stat is None:
                    continue
                if stat.S_ISLNK(path_stat.st_mode):
                    return [], self._retention_diagnostic(
                        "workspace.retention.lineage_ambiguous",
                        "Workspace cleanup does not traverse symlinks in a lineage subtree.",
                        entity=str(path),
                    )
                if not stat.S_ISREG(path_stat.st_mode):
                    continue
                resolved, diagnostic = self._safe_resolve_path(
                    path,
                    entity=str(path),
                    operation="lineage file resolution",
                )
                if diagnostic is not None or resolved is None:
                    return [], diagnostic
                try:
                    paths.add(resolved.relative_to(area_root).as_posix())
                except ValueError as error:
                    return [], self._retention_diagnostic(
                        "workspace.retention.ref_readback_failed",
                        f"Workspace lineage readback failed: { error }",
                        entity=str(path),
                    )
        return sorted(paths), None

    async def inspect_retention(
        self,
        scope: dict[str, Any],
        *,
        lifecycle: WorkspaceRetentionLifecycle,
        retained_refs: Sequence[WorkspaceRetainedReference] = (),
        inline_result: Any = None,
        policy: WorkspaceRetentionPolicy | None = None,
    ) -> WorkspaceRetentionPreview:
        normalized_scope = {str(key): value for key, value in dict(scope or {}).items() if value is not None}
        if not normalized_scope:
            raise ValueError("Workspace inspect_retention requires at least one scope value.")

        requested_policy = resolve_retention_policy(policy, supports_cold=True)
        supports_cold = bool(self._features().get("supports_cold_retention", False))
        try:
            resolved_policy = resolve_retention_policy(requested_policy, supports_cold=supports_cold)
        except ValueError as error:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=requested_policy,
                retained_refs=list(retained_refs),
                inline_result=inline_result,
                diagnostics=[
                    self._retention_diagnostic(
                        "workspace.retention.policy_unsupported",
                        str(error),
                        entity="policy",
                    )
                ],
            )

        representation_by_category = {
            rule["category"]: rule["representation"] for rule in resolved_policy.get("rules", [])
        }
        execution_id = str(lifecycle.get("execution_id") or "")
        checkpoint_manifest_key = f"checkpoint.latest.{ execution_id }"
        lease_manifest_key = self._lease_manifest_key(execution_id)

        with self._connect() as conn:
            conn.execute("BEGIN")
            all_record_rows = conn.execute("SELECT * FROM records ORDER BY id").fetchall()
            clauses: list[str] = []
            scope_params: list[Any] = []
            for index, (key, value) in enumerate(normalized_scope.items()):
                alias = f"ret_scope_{ index }"
                clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1 FROM record_scope_index { alias }
                        WHERE { alias }.record_id = r.id
                        AND { alias }.scope_key = ?
                        AND { alias }.scope_value = ?
                    )
                    """
                )
                scope_params.extend([key, self._scope_index_value(value)])
            scoped_rows = conn.execute(
                f"SELECT r.id FROM records r WHERE {' AND '.join(clauses)} ORDER BY r.id",
                scope_params,
            ).fetchall()
            checkpoint_rows = conn.execute(
                """
                SELECT rowid AS checkpoint_rowid, * FROM checkpoints
                WHERE run_id = ? ORDER BY created_at ASC, checkpoint_rowid ASC
                """,
                (execution_id,),
            ).fetchall()
            runtime_event_rows = conn.execute(
                "SELECT * FROM runtime_events WHERE execution_id = ? ORDER BY sequence ASC, id ASC",
                (execution_id,),
            ).fetchall()
            anchor_rows = conn.execute(
                "SELECT * FROM retention_anchors WHERE execution_id = ? ORDER BY id",
                (execution_id,),
            ).fetchall()
            scratch_rows = conn.execute("SELECT * FROM scratch_leases ORDER BY lease_id").fetchall()
            link_rows = conn.execute("SELECT * FROM links ORDER BY id").fetchall()
            scope_index_rows = conn.execute(
                "SELECT record_id, scope_key, scope_value FROM record_scope_index ORDER BY record_id, scope_key"
            ).fetchall()
            fts_rows = conn.execute(
                "SELECT record_id, summary, content FROM records_fts ORDER BY record_id"
            ).fetchall()
            manifest_rows = conn.execute(
                "SELECT key, value_json FROM manifests WHERE key IN (?, ?) ORDER BY key",
                (checkpoint_manifest_key, lease_manifest_key),
            ).fetchall()
            vector_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspace_vectors'"
            ).fetchone()
            vector_rows = (
                conn.execute(
                    "SELECT record_id, ref_json, embedding_json FROM workspace_vectors ORDER BY record_id"
                ).fetchall()
                if vector_table is not None
                else []
            )
            conn.rollback()

        all_records = {str(row["id"]): self._row_to_ref(row) for row in all_record_rows}
        scoped_record_ids = {str(row["id"]) for row in scoped_rows}
        checkpoint_record_ids = {str(row["record_id"]) for row in checkpoint_rows}
        owned_record_ids = scoped_record_ids | checkpoint_record_ids
        runtime_events = [self._row_to_runtime_event_record(row) for row in runtime_event_rows]
        anchors = [self._row_to_retention_anchor(row) for row in anchor_rows]
        links = [self._row_to_link(row) for row in link_rows]
        manifest_values = {
            str(row["key"]): json_loads(row["value_json"], None) for row in manifest_rows
        }
        manifest_raw = {str(row["key"]): str(row["value_json"]) for row in manifest_rows}

        diagnostics: list[WorkspaceRetentionDiagnostic] = []
        scope_execution_id = normalized_scope.get("execution_id")
        if not execution_id or (scope_execution_id is not None and str(scope_execution_id) != execution_id):
            diagnostics.append(
                self._retention_diagnostic(
                    "workspace.retention.lifecycle_scope_mismatch",
                    "Retention lifecycle execution_id does not match the cleanup scope.",
                    entity=execution_id or "execution_id",
                )
            )
        if lifecycle.get("status") not in {"completed", "failed", "cancelled"}:
            diagnostics.append(
                self._retention_diagnostic(
                    "workspace.retention.lifecycle_not_terminal",
                    "Workspace retention requires a terminal lifecycle status.",
                    entity=execution_id,
                )
            )
        if lifecycle.get("recovery_active"):
            diagnostics.append(
                self._retention_diagnostic(
                    "workspace.retention.recovery_active",
                    "Workspace recovery remains active for the cleanup scope.",
                    entity=execution_id,
                )
            )
        if lifecycle.get("lease_active"):
            diagnostics.append(
                self._retention_diagnostic(
                    "workspace.retention.lease_active",
                    "Workspace execution lease remains active for the cleanup scope.",
                    entity=execution_id,
                )
            )

        persisted_lease = manifest_values.get(lease_manifest_key)
        if lease_manifest_key in manifest_values and not isinstance(persisted_lease, dict):
            diagnostics.append(
                self._retention_diagnostic(
                    "workspace.retention.ref_readback_failed",
                    "Persisted Workspace execution lease is unreadable.",
                    entity=lease_manifest_key,
                )
            )
        elif isinstance(persisted_lease, dict):
            try:
                persisted_lease_active = (
                    persisted_lease.get("released_at") is None
                    and float(persisted_lease.get("lease_until") or 0) > time.time()
                )
            except (TypeError, ValueError) as error:
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.ref_readback_failed",
                        f"Persisted Workspace execution lease is invalid: { error }",
                        entity=lease_manifest_key,
                    )
                )
            else:
                if persisted_lease_active:
                    diagnostics.append(
                        self._retention_diagnostic(
                            "workspace.retention.lease_active",
                            "Persisted Workspace execution lease remains active.",
                            entity=lease_manifest_key,
                        )
                    )

        checkpoint_version = None
        for row in reversed(checkpoint_rows):
            checkpoint_version = self._checkpoint_state_version(json_loads(row["state_json"], {}))
            if checkpoint_version is not None:
                break
        runtime_version = next(
            (
                event["state_version"]
                for event in reversed(runtime_events)
                if event["state_version"] is not None
            ),
            None,
        )
        authoritative_versions = {
            int(value) for value in (checkpoint_version, runtime_version) if value is not None
        }
        if len(authoritative_versions) > 1:
            diagnostics.append(
                self._retention_diagnostic(
                    "workspace.retention.state_version_ambiguous",
                    "Persisted checkpoint and RuntimeEvent state versions disagree.",
                    entity=execution_id,
                    detail={"versions": sorted(authoritative_versions)},
                )
            )
        elif authoritative_versions:
            authoritative_version = next(iter(authoritative_versions))
            if lifecycle.get("state_version") != authoritative_version:
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.state_version_mismatch",
                        "Supplied lifecycle state version is stale against persisted Workspace facts.",
                        entity=execution_id,
                        detail={
                            "supplied": lifecycle.get("state_version"),
                            "persisted": authoritative_version,
                        },
                    )
                )

        persisted_lifecycle: tuple[str, str | None] | None = None
        for event in reversed(runtime_events):
            event_type = event["event_type"].lower()
            if event_type.endswith(("execution_completed", "execution.completed")):
                persisted_lifecycle = ("terminal", "completed")
            elif event_type.endswith(("execution_failed", "execution.failed")):
                persisted_lifecycle = ("terminal", "failed")
            elif event_type.endswith(
                ("execution_cancelled", "execution.cancelled", "execution_canceled", "execution.canceled")
            ):
                persisted_lifecycle = ("terminal", "cancelled")
            elif event_type.endswith(("execution_closed", "execution.closed")):
                persisted_lifecycle = ("terminal", None)
            elif event_type.endswith(
                (
                    "execution_started",
                    "execution.started",
                    "execution_resumed",
                    "execution.resumed",
                    "execution_sealed",
                    "execution.sealed",
                    "execution_unsealed",
                    "execution.unsealed",
                    "execution_waiting",
                    "execution.waiting",
                    "execution_paused",
                    "execution.paused",
                )
            ):
                persisted_lifecycle = ("active", None)
            if persisted_lifecycle is not None:
                break
        if persisted_lifecycle is not None:
            persisted_state, persisted_status = persisted_lifecycle
            if persisted_state == "active":
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.recovery_active",
                        "Persisted Workspace lifecycle facts remain nonterminal.",
                        entity=execution_id,
                    )
                )
            elif persisted_status is not None and lifecycle.get("status") != persisted_status:
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.lifecycle_state_mismatch",
                        "Supplied lifecycle status disagrees with persisted Workspace facts.",
                        entity=execution_id,
                    )
                )

        scoped_scratch_leases: list[WorkspaceScratchLease] = []
        for row in scratch_rows:
            lease = self._row_to_scratch_lease(row)
            lease_scope = lease.get("scope")
            if not isinstance(lease_scope, dict) or not all(
                lease_scope.get(key) == value for key, value in normalized_scope.items()
            ):
                continue
            scoped_scratch_leases.append(lease)
            if lease.get("closed_at") is None:
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.lease_active",
                        "A scratch lease remains active for the cleanup scope.",
                        entity=str(lease.get("lease_id") or ""),
                    )
                )

        canonical_records: dict[str, WorkspaceRecordRef] = {}
        for record_id in sorted(owned_record_ids):
            ref = all_records.get(record_id)
            if ref is None:
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.ref_missing",
                        "A Workspace checkpoint row references a missing record.",
                        entity=record_id,
                    )
                )
                continue
            _, diagnostic = await self._verified_record_envelope(ref)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                continue
            canonical_records[record_id] = ref

        canonical_retained_refs: list[WorkspaceRetainedReference] = []
        retained_record_ids: set[str] = set()
        retained_file_paths: set[str] = set()

        async def retain_ref(ref: WorkspaceRetainedReference) -> None:
            canonical, record_id, file_path, diagnostic = await self._verify_retained_ref(
                ref,
                records_by_id=all_records,
            )
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                canonical_retained_refs.append(ref)
                return
            if canonical is not None:
                canonical_retained_refs.append(canonical)
            if record_id:
                retained_record_ids.add(record_id)
            if file_path:
                retained_file_paths.add(file_path)

        for ref in retained_refs:
            await retain_ref(ref)

        checkpoint_manifest = manifest_values.get(checkpoint_manifest_key)
        if checkpoint_manifest_key in manifest_values:
            if not isinstance(checkpoint_manifest, dict):
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.ref_readback_failed",
                        "Persisted Workspace checkpoint manifest is unreadable.",
                        entity=checkpoint_manifest_key,
                    )
                )
            elif representation_by_category["checkpoints"] == "hot":
                await retain_ref(cast(WorkspaceRetainedReference, checkpoint_manifest))

        preserved_event_ids: set[str] = set()
        for anchor in anchors:
            for anchor_ref in (anchor.get("record_ref"), anchor.get("summary_ref")):
                if anchor_ref is not None:
                    await retain_ref(anchor_ref)
            preserved_event_ids.update(str(value) for value in anchor["preserved_event_ids"])

        known_runtime_event_ids = {event["event_id"] for event in runtime_events}
        for preserved_event_id in sorted(preserved_event_ids - known_runtime_event_ids):
            diagnostics.append(
                self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "A retention anchor references a RuntimeEvent that does not exist.",
                    entity=preserved_event_id,
                )
            )
        retained_event_ids = (
            known_runtime_event_ids
            if representation_by_category["runtime_events"] == "hot"
            else preserved_event_ids
        )
        for event in runtime_events:
            if event["event_id"] not in retained_event_ids:
                continue
            event_refs: list[WorkspaceReferenceEnvelope] = list(event["artifact_refs"])
            if event["snapshot_ref"] is not None:
                event_refs.append(event["snapshot_ref"])
            for event_ref in event_refs:
                await retain_ref(event_ref)

        adjacency: dict[str, list[tuple[str, str]]] = {}
        for link in links:
            adjacency.setdefault(link["source_id"], []).append((link["target_id"], link["id"]))
            adjacency.setdefault(link["target_id"], []).append((link["source_id"], link["id"]))
            if link["target_id"] in owned_record_ids and link["source_id"] not in owned_record_ids:
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.incoming_reference",
                        "A record in the cleanup scope has an incoming link from outside the scope.",
                        entity=link["id"],
                        detail={"source_id": link["source_id"], "target_id": link["target_id"]},
                    )
                )
        closure_queue = sorted(retained_record_ids)
        closure_seen = set(retained_record_ids)
        while closure_queue:
            current = closure_queue.pop(0)
            for neighbor, _ in sorted(adjacency.get(current, [])):
                if neighbor in closure_seen:
                    continue
                closure_seen.add(neighbor)
                neighbor_ref = all_records.get(neighbor)
                if neighbor_ref is None:
                    diagnostics.append(
                        self._retention_diagnostic(
                            "workspace.retention.ref_missing",
                            "A retained Workspace link reaches a missing record.",
                            entity=neighbor,
                        )
                    )
                    continue
                _, diagnostic = await self._verified_record_envelope(neighbor_ref)
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                    continue
                retained_record_ids.add(neighbor)
                canonical_retained_refs.append(neighbor_ref)
                closure_queue.append(neighbor)

        selected_file_paths: list[str] = []
        selected_scratch_paths: list[str] = []
        if representation_by_category["files"] != "hot":
            selected_file_paths, diagnostic = self._scoped_area_files(normalized_scope, "files")
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        if representation_by_category["scratch"] != "hot":
            selected_scratch_paths, diagnostic = self._scoped_area_files(normalized_scope, "scratch")
            if diagnostic is not None:
                diagnostics.append(diagnostic)

        if diagnostics:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=self._deduplicate_retained_refs(
                    canonical_retained_refs or list(retained_refs)
                ),
                inline_result=inline_result,
                diagnostics=diagnostics,
            )

        selected = empty_retention_selection()
        selected_record_sizes: dict[str, int] = {}
        for record_id in sorted(owned_record_ids):
            ref = canonical_records[record_id]
            if record_id in retained_record_ids:
                continue
            if record_id in checkpoint_record_ids or ref.get("collection") == "checkpoints":
                category = "checkpoints"
            elif ref.get("collection") == "artifacts":
                category = "artifacts"
            else:
                category = "records"
            if representation_by_category[category] == "hot":
                continue
            selected["record_ids"].append(record_id)
            selected_record_sizes[record_id] = int(ref.get("size") or 0)
            if ref.get("path"):
                selected["content_paths"].append(str(ref["path"]))

        selected_record_id_set = set(selected["record_ids"])
        selected_checkpoint_rows = [
            row for row in checkpoint_rows if str(row["record_id"]) in selected_record_id_set
        ]
        selected["checkpoint_ids"] = sorted(
            {str(row["record_id"]) for row in selected_checkpoint_rows}
        )
        selected["checkpoint_row_ids"] = sorted(
            f"{ row['run_id'] }:{ row['checkpoint_rowid'] }:{ row['record_id'] }"
            for row in selected_checkpoint_rows
        )
        selected["link_ids"] = sorted(
            link["id"]
            for link in links
            if link["source_id"] in selected_record_id_set
            or link["target_id"] in selected_record_id_set
        )
        if representation_by_category["runtime_events"] != "hot":
            selected["runtime_event_ids"] = sorted(
                event["id"] for event in runtime_events if event["event_id"] not in retained_event_ids
            )
        selected["retention_anchor_ids"] = sorted(anchor["id"] for anchor in anchors)
        if representation_by_category["scratch"] != "hot":
            selected["scratch_lease_ids"] = sorted(
                str(lease.get("lease_id") or "")
                for lease in scoped_scratch_leases
                if lease.get("closed_at") is not None
            )
            selected["scratch_paths"] = selected_scratch_paths
        if representation_by_category["files"] != "hot":
            selected["file_paths"] = [
                path for path in selected_file_paths if path not in retained_file_paths
            ]
        selected["record_scope_index_ids"] = sorted(
            f"{ row['record_id'] }:{ row['scope_key'] }"
            for row in scope_index_rows
            if str(row["record_id"]) in selected_record_id_set
        )
        selected["fts_record_ids"] = sorted(
            str(row["record_id"])
            for row in fts_rows
            if str(row["record_id"]) in selected_record_id_set
        )
        if (
            representation_by_category["checkpoints"] != "hot"
            and checkpoint_manifest_key in manifest_values
        ):
            selected["manifest_keys"].append(checkpoint_manifest_key)
        if lease_manifest_key in manifest_values:
            selected["manifest_keys"].append(lease_manifest_key)

        for key in selected:
            selected[key] = sorted(set(selected[key]))

        if any(selected.values()):
            read_only_components = [
                name
                for name, component in (
                    ("backend", self),
                    ("db_store", self.db_store_provider),
                    ("vector_store", self.vector_store_provider),
                )
                if component is not None and bool(getattr(component, "read_only", False))
            ]
            if read_only_components:
                return self._retention_preview(
                    status="deferred",
                    scope=normalized_scope,
                    lifecycle=lifecycle,
                    policy=resolved_policy,
                    retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
                    inline_result=inline_result,
                    diagnostics=[
                        self._retention_diagnostic(
                            "workspace.retention.provider_capability_missing",
                            "Workspace retention cannot apply a non-empty plan through read-only components.",
                            entity=type(self).__name__,
                            detail={"read_only_components": read_only_components},
                        )
                    ],
                )

        vector_provider = self.vector_store_provider
        if vector_provider is not None and selected_record_id_set:
            delete_vectors = getattr(vector_provider, "delete_records", None)
            if not callable(delete_vectors):
                return self._retention_preview(
                    status="deferred",
                    scope=normalized_scope,
                    lifecycle=lifecycle,
                    policy=resolved_policy,
                    retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
                    inline_result=inline_result,
                    diagnostics=[
                        self._retention_diagnostic(
                            "workspace.retention.provider_capability_missing",
                            "Configured vector provider cannot delete derived record entries.",
                            entity=type(vector_provider).__name__,
                            detail={"missing_method": "delete_records"},
                        )
                    ],
                )
            if isinstance(vector_provider, SQLiteVectorStoreProvider):
                selected["vector_record_ids"] = sorted(
                    str(row["record_id"])
                    for row in vector_rows
                    if str(row["record_id"]) in selected_record_id_set
                )
            else:
                selected["vector_record_ids"] = sorted(selected_record_id_set)

        for key in selected:
            selected[key] = sorted(set(selected[key]))

        logical_bytes = sum(selected_record_sizes.values())
        logical_bytes += sum(
            serialized_size(dict(row))
            for row in all_record_rows
            if str(row["id"]) in selected_record_id_set
        )
        selected_event_ids = set(selected["runtime_event_ids"])
        logical_bytes += sum(
            serialized_size(event) for event in runtime_events if event["id"] in selected_event_ids
        )
        selected_link_ids = set(selected["link_ids"])
        logical_bytes += sum(
            serialized_size(link) for link in links if link["id"] in selected_link_ids
        )
        selected_anchor_ids = set(selected["retention_anchor_ids"])
        logical_bytes += sum(
            serialized_size(anchor) for anchor in anchors if anchor["id"] in selected_anchor_ids
        )
        selected_checkpoint_ids = set(selected["checkpoint_row_ids"])
        logical_bytes += sum(
            serialized_size(
                {
                    "run_id": row["run_id"],
                    "step_id": row["step_id"],
                    "record_id": row["record_id"],
                    "state": json_loads(row["state_json"], {}),
                    "created_at": row["created_at"],
                }
            )
            for row in selected_checkpoint_rows
            if (
                f"{ row['run_id'] }:{ row['checkpoint_rowid'] }:{ row['record_id'] }"
                in selected_checkpoint_ids
            )
        )
        selected_scope_index_ids = set(selected["record_scope_index_ids"])
        logical_bytes += sum(
            serialized_size(dict(row))
            for row in scope_index_rows
            if f"{ row['record_id'] }:{ row['scope_key'] }" in selected_scope_index_ids
        )
        selected_scratch_lease_ids = set(selected["scratch_lease_ids"])
        logical_bytes += sum(
            serialized_size(dict(row))
            for row in scratch_rows
            if str(row["lease_id"]) in selected_scratch_lease_ids
        )
        selected_manifest_keys = set(selected["manifest_keys"])
        logical_bytes += sum(
            len(key.encode("utf-8")) + len(manifest_raw[key].encode("utf-8"))
            for key in selected_manifest_keys
        )
        selected_fts_ids = set(selected["fts_record_ids"])
        logical_bytes += sum(
            len(str(row["summary"] or "").encode("utf-8"))
            + len(str(row["content"] or "").encode("utf-8"))
            for row in fts_rows
            if str(row["record_id"]) in selected_fts_ids
        )
        selected_vector_ids = set(selected["vector_record_ids"])
        if isinstance(vector_provider, SQLiteVectorStoreProvider):
            logical_bytes += sum(
                len(str(row["ref_json"] or "").encode("utf-8"))
                + len(str(row["embedding_json"] or "").encode("utf-8"))
                for row in vector_rows
                if str(row["record_id"]) in selected_vector_ids
            )
        else:
            logical_bytes += sum(
                serialized_size({"record_id": record_id}) for record_id in selected_vector_ids
            )
        for area, key in (("files", "file_paths"), ("scratch", "scratch_paths")):
            area_root = self.root / area
            for relative_path in selected[key]:
                target, diagnostic = self._safe_resolve_path(
                    area_root / relative_path,
                    entity=relative_path,
                    operation="selected-path resolution",
                )
                if diagnostic is not None or target is None:
                    return self._retention_preview(
                        status="deferred",
                        scope=normalized_scope,
                        lifecycle=lifecycle,
                        policy=resolved_policy,
                        retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
                        inline_result=inline_result,
                        diagnostics=[cast(WorkspaceRetentionDiagnostic, diagnostic)],
                    )
                target_stat, diagnostic = self._safe_stat_path(
                    target,
                    entity=str(target),
                    operation="selected-path stat",
                )
                if diagnostic is not None or target_stat is None:
                    return self._retention_preview(
                        status="deferred",
                        scope=normalized_scope,
                        lifecycle=lifecycle,
                        policy=resolved_policy,
                        retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
                        inline_result=inline_result,
                        diagnostics=[cast(WorkspaceRetentionDiagnostic, diagnostic)],
                    )
                logical_bytes += target_stat.st_size

        return self._retention_preview(
            status="ready",
            scope=normalized_scope,
            lifecycle=lifecycle,
            policy=resolved_policy,
            retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
            inline_result=inline_result,
            selected=selected,
            logical_bytes=logical_bytes,
        )

    async def apply_retention(
        self,
        preview: WorkspaceRetentionPreview,
    ) -> WorkspaceRetentionResult:
        raise NotImplementedError

    async def prune_scope(
        self,
        scope: dict[str, Any],
        *,
        remove_files: bool = True,
    ) -> dict[str, Any]:
        self._ensure_writable()
        normalized_scope = {str(key): value for key, value in dict(scope or {}).items() if value is not None}
        if not normalized_scope:
            raise ValueError("Workspace prune_scope requires at least one scope value.")
        with self._connect() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            for index, (key, value) in enumerate(normalized_scope.items()):
                alias = f"s{index}"
                clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1 FROM record_scope_index {alias}
                        WHERE {alias}.record_id = r.id
                        AND {alias}.scope_key = ?
                        AND {alias}.scope_value = ?
                    )
                    """
                )
                params.extend([key, self._scope_index_value(value)])
            rows = conn.execute(
                f"SELECT r.id, r.path FROM records r WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
            record_ids = [str(row["id"]) for row in rows]
            content_paths = [str(row["path"]) for row in rows if row["path"]]
            placeholders = ",".join("?" for _ in record_ids)
            if record_ids:
                conn.execute(
                    f"DELETE FROM links WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                    [*record_ids, *record_ids],
                )
                conn.execute(f"DELETE FROM checkpoints WHERE record_id IN ({placeholders})", record_ids)
                conn.execute(f"DELETE FROM records_fts WHERE record_id IN ({placeholders})", record_ids)
                conn.execute(f"DELETE FROM record_scope_index WHERE record_id IN ({placeholders})", record_ids)
                conn.execute(f"DELETE FROM records WHERE id IN ({placeholders})", record_ids)
            runtime_events_deleted = 0
            retention_anchors_deleted = 0
            execution_id = normalized_scope.get("execution_id")
            if isinstance(execution_id, str) and execution_id:
                runtime_events_deleted = conn.execute(
                    "DELETE FROM runtime_events WHERE execution_id = ?",
                    (execution_id,),
                ).rowcount
                retention_anchors_deleted = conn.execute(
                    "DELETE FROM retention_anchors WHERE execution_id = ?",
                    (execution_id,),
                ).rowcount
            conn.commit()
        delete_vectors = getattr(self.vector_store_provider, "delete_records", None)
        if callable(delete_vectors):
            await cast(Callable[[list[str]], Awaitable[None]], delete_vectors)(record_ids)
        content_files_deleted = 0
        for path in content_paths:
            target = self.content_root / path
            if target.exists() and target.is_file():
                target.unlink()
                content_files_deleted += 1
        removed_paths: list[str] = []
        if remove_files:
            removed_paths = self._prune_scope_subtrees(normalized_scope)
        return {
            "scope": normalized_scope,
            "records_deleted": len(record_ids),
            "content_files_deleted": content_files_deleted,
            "runtime_events_deleted": runtime_events_deleted,
            "retention_anchors_deleted": retention_anchors_deleted,
            "removed_paths": removed_paths,
            "removed_files": bool(removed_paths),
        }

    def _prune_scope_subtrees(self, scope: dict[str, Any]) -> list[str]:
        """Remove only the lineage subtree(s) matching the prune scope.

        Each prunable scope value maps to a ``<kind>/<id>`` lineage node; the
        matching directories under ``files/lineage`` and ``scratch/lineage`` are
        removed as contained subtrees, leaving unrelated siblings intact. This
        replaces the previous whole-``files_root`` deletion (spec sections 8.2 / 9).
        """

        from ._defaults import scope_filter_path_nodes

        nodes = scope_filter_path_nodes(scope)
        if not nodes:
            return []
        removed: list[str] = []
        for area in ("files", "scratch"):
            lineage_root = self.root / area / "lineage"
            if not lineage_root.exists():
                continue
            for node in nodes:
                kind = slug(node["kind"], "scope")
                node_id = slug(node["id"], "default")
                for candidate in list(lineage_root.rglob(node_id)):
                    if not candidate.is_dir() or candidate.parent.name != kind:
                        continue
                    if candidate.exists():
                        shutil.rmtree(candidate)
                        removed.append(str(candidate))
        if removed:
            self._delete_scratch_leases_under(removed)
        return removed

    @staticmethod
    def _row_to_scratch_lease(row: sqlite3.Row) -> WorkspaceScratchLease:
        return cast(
            WorkspaceScratchLease,
            {
                "lease_id": row["lease_id"],
                "scope": json_loads(row["scope_json"], {}),
                "local_path": row["local_path"],
                "mount": json_loads(row["mount_json"], None),
                "purpose": row["purpose"],
                "cleanup_policy": row["cleanup_policy"],
                "expires_at": row["expires_at"],
                "read_only": bool(row["read_only"]),
                "policy_labels": json_loads(row["policy_labels_json"], []),
                "created_at": row["created_at"],
                "closed_at": row["closed_at"],
            },
        )

    async def register_scratch_lease(self, lease: WorkspaceScratchLease) -> WorkspaceScratchLease:
        self._ensure_writable()
        lease_id = str(lease.get("lease_id") or uuid.uuid4().hex)
        record: WorkspaceScratchLease = {
            "lease_id": lease_id,
            "scope": dict(lease.get("scope") or {}),
            "local_path": lease.get("local_path"),
            "mount": lease.get("mount"),
            "purpose": lease.get("purpose"),
            "cleanup_policy": lease.get("cleanup_policy") or "on_close",
            "expires_at": lease.get("expires_at"),
            "read_only": bool(lease.get("read_only", False)),
            "policy_labels": list(lease.get("policy_labels") or []),
            "created_at": lease.get("created_at") or utc_now(),
            "closed_at": lease.get("closed_at"),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scratch_leases(
                    lease_id, scope_json, local_path, mount_json, purpose,
                    cleanup_policy, expires_at, read_only, policy_labels_json,
                    created_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    json_dumps(record["scope"]),
                    record["local_path"],
                    json_dumps(record["mount"]) if record["mount"] is not None else None,
                    record["purpose"],
                    record["cleanup_policy"],
                    record["expires_at"],
                    1 if record["read_only"] else 0,
                    json_dumps(record["policy_labels"]),
                    record["created_at"],
                    record["closed_at"],
                ),
            )
        return record

    async def get_scratch_lease(self, lease_id: str) -> WorkspaceScratchLease | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scratch_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._row_to_scratch_lease(row) if row is not None else None

    async def list_scratch_leases(
        self,
        *,
        include_closed: bool = False,
        expired_before: str | None = None,
    ) -> list[WorkspaceScratchLease]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_closed:
            clauses.append("closed_at IS NULL")
        if expired_before is not None:
            clauses.append("expires_at IS NOT NULL AND expires_at <= ?")
            params.append(expired_before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM scratch_leases {where} ORDER BY created_at",
                params,
            ).fetchall()
        return [self._row_to_scratch_lease(row) for row in rows]

    async def close_scratch_lease(
        self,
        lease_id: str,
        *,
        closed_at: str | None = None,
    ) -> WorkspaceScratchLease | None:
        self._ensure_writable()
        stamp = closed_at or utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE scratch_leases SET closed_at = ? WHERE lease_id = ? AND closed_at IS NULL",
                (stamp, lease_id),
            )
            row = conn.execute(
                "SELECT * FROM scratch_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._row_to_scratch_lease(row) if row is not None else None

    def _delete_scratch_leases_under(self, removed_paths: list[str]) -> None:
        if not removed_paths:
            return
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT lease_id, local_path FROM scratch_leases WHERE local_path IS NOT NULL"
            ).fetchall()
            stale = [
                str(row["lease_id"])
                for row in rows
                if any(str(row["local_path"]).startswith(prefix) for prefix in removed_paths)
            ]
            if stale:
                placeholders = ",".join("?" for _ in stale)
                conn.execute(
                    f"DELETE FROM scratch_leases WHERE lease_id IN ({placeholders})",
                    stale,
                )

    def capabilities(self) -> WorkspaceBackendCapabilities:
        vector_index = self.vector_index
        return {
            "backend": "local",
            "root": str(self.root),
            "content_root": str(self.content_root),
            "files_root": str(self.files_root),
            "read_only": self.read_only,
            "components": {
                "db_store_provider": self.db_store_provider_name,
                "content": type(self.content).__name__,
                "metadata": type(self.metadata).__name__,
                "checkpoint_store": type(self.checkpoint_store).__name__,
                "text_index": type(self.text_index).__name__,
                "policy": type(self.policy).__name__,
                "embedding_provider": (
                    getattr(self.embedding_provider, "name", None) or type(self.embedding_provider).__name__
                    if self.embedding_provider is not None
                    else None
                ),
                "vector_store_provider": (
                    self.vector_store_provider_name
                    or getattr(self.vector_store_provider, "name", None)
                    or type(self.vector_store_provider).__name__
                    if self.vector_store_provider is not None
                    else None
                ),
                "vector_index": type(vector_index).__name__ if vector_index is not None else None,
                "runtime_event_store": type(self.runtime_event_store).__name__,
                "ref_resolver": type(self.ref_resolver).__name__,
                "retention_policy": type(self.retention_policy).__name__,
                "evidence_linker": type(self.evidence_linker).__name__,
            },
            "features": self._features(),
        }
