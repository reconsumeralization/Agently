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
import copy
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Concatenate, Literal, ParamSpec, TypeVar, cast

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.workspace import (
    WorkspaceBackendCapabilities,
    WorkspaceContentSegment,
    WorkspaceFileExportResult,
    WorkspaceFileDiagnostic,
    WorkspaceFileRef,
    WorkspaceFileReadResult,
    WorkspaceFileSearchResult,
    WorkspaceFileWriteResult,
    WorkspaceFileInfo,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetrievalMethod,
    WorkspaceRetrievalPackage,
    WorkspaceRetrievalSelection,
    WorkspaceRuntimeEventRecord,
)
from agently.types.plugins import WorkspaceBackend
from .Errors import WorkspacePolicyError
from .Retrieval import RerankHandler, retrieve_workspace
from ._defaults import default_workspace_root, merge_scope

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .Manager import WorkspaceManager


_MISSING = object()
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _guard_workspace_mutation(
    method: Callable[Concatenate["Workspace", _P], Coroutine[Any, Any, _R]],
) -> Callable[Concatenate["Workspace", _P], Coroutine[Any, Any, _R]]:
    @wraps(method)
    async def guarded(
        self: "Workspace",
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        async with self._backend_mutation_guard():
            return await method(self, *args, **kwargs)

    return cast(
        Callable[Concatenate["Workspace", _P], Coroutine[Any, Any, _R]],
        guarded,
    )


class Workspace:
    """Workspace API bound to one backend."""

    def __init__(
        self,
        backend: WorkspaceBackend | str | Path | None = None,
        manager: "WorkspaceManager | None" = None,
        *,
        create: bool = True,
        mode: str = "read_only",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        db_store_provider: Any | None = None,
        db_store_options: dict[str, Any] | None = None,
        embedding_provider: Any | None = None,
        embedding_options: dict[str, Any] | None = None,
        vector_store_provider: Any | None = None,
        vector_store_options: dict[str, Any] | None = None,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
    ):
        if mode not in {"read_only", "read_write"}:
            raise ValueError("Workspace mode must be 'read_only' or 'read_write'.")
        if manager is None:
            from .Manager import WorkspaceManager

            manager = WorkspaceManager()
        self.manager = manager
        self._create = bool(create)
        self._provider = provider
        self._provider_options = dict(provider_options or {})
        self._db_store_provider = db_store_provider
        self._db_store_options = dict(db_store_options or {})
        self._embedding_provider = embedding_provider
        self._embedding_options = dict(embedding_options or {})
        self._vector_store_provider = vector_store_provider
        self._vector_store_options = dict(vector_store_options or {})
        self._file_mutation_lock = asyncio.Lock()
        self._execution_id = uuid.uuid4().hex
        if backend is None or isinstance(backend, (str, Path)):
            self._backend: WorkspaceBackend | None = None
            selected_root = default_workspace_root() if backend is None else Path(backend)
            self.root = selected_root.expanduser().resolve()
            self.mode = mode
        else:
            self._backend = cast(WorkspaceBackend, backend)
            self.root = Path(str(getattr(self._backend, "root"))).expanduser().resolve()
            self.mode = "read_only" if bool(getattr(self._backend, "read_only", False)) else mode
        self._workspace_id = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
        self.default_scope = dict(default_scope or {})
        self.default_search_scope = dict(default_search_scope or self.default_scope)

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def execution_id(self) -> str:
        return self._execution_id

    def _bind_execution(
        self,
        execution_id: str,
        *,
        scope: dict[str, Any] | None = None,
        search_scope: dict[str, Any] | None = None,
    ) -> "Workspace":
        if not str(execution_id).strip():
            raise ValueError("Workspace execution_id cannot be empty.")
        # Execution binding is a view, not a new storage/provider binding. A
        # shallow copy preserves the caller's backend and instance-level
        # adapters while keeping execution identity and scopes view-local.
        bound = copy.copy(self)
        bound.default_scope = merge_scope(self.default_scope, scope)
        bound.default_search_scope = merge_scope(self.default_search_scope, search_scope)
        bound._execution_id = str(execution_id)
        return bound

    @property
    def backend(self) -> Any:
        if self._backend is None:
            materialization_root = self.root if self._provider is not None else self.root / ".agently"
            materialized = self.manager._materialize_workspace(
                materialization_root,
                create=self._create,
                mode="read_write",
                provider=self._provider,
                provider_options=self._provider_options,
                db_store_provider=self._db_store_provider,
                db_store_options=self._db_store_options,
                embedding_provider=self._embedding_provider,
                embedding_options=self._embedding_options,
                vector_store_provider=self._vector_store_provider,
                vector_store_options=self._vector_store_options,
                default_scope=self.default_scope,
                default_search_scope=self.default_search_scope,
            )
            self._backend = materialized._backend
        if self._backend is None:
            raise RuntimeError("Workspace backend materialization did not produce a backend.")
        return self._backend

    @asynccontextmanager
    async def _backend_mutation_guard(self) -> AsyncIterator[None]:
        guard = getattr(self._backend, "_mutation_guard", None) if self._backend is not None else None
        if callable(guard):
            typed_guard = cast(
                Callable[[], AbstractAsyncContextManager[None]],
                guard,
            )
            async with typed_guard():
                yield
            return
        async with self._file_mutation_lock:
            yield

    def _try_backend_sync_mutation_guard(self) -> AbstractContextManager[bool] | None:
        guard = getattr(self._backend, "_try_sync_mutation_guard", None) if self._backend is not None else None
        if not callable(guard):
            return None
        typed_guard = cast(
            Callable[[], AbstractContextManager[bool]],
            guard,
        )
        return typed_guard()

    def _scoped_record_scope(self, scope: dict[str, Any] | None) -> dict[str, Any]:
        return merge_scope(self.default_scope, scope)

    def _scoped_filters(self, filters: dict[str, Any] | None) -> dict[str, Any]:
        scoped = dict(filters or {})
        for key, value in self.default_search_scope.items():
            filter_key = f"scope.{key}"
            scoped.setdefault(filter_key, value)
        return scoped

    def _matches_default_search_scope(self, ref: WorkspaceRecordRef) -> bool:
        if not self.default_search_scope:
            return True
        scope = ref.get("scope")
        if not isinstance(scope, dict):
            return False
        for key, value in self.default_search_scope.items():
            if value is None:
                continue
            if scope.get(key) != value:
                return False
        return True

    async def _scope_record_ref(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef:
        if not self.default_scope:
            return ref
        scoped_ref = dict(ref)
        existing_scope = scoped_ref.get("scope")
        scoped_ref["scope"] = merge_scope(
            self.default_scope, existing_scope if isinstance(existing_scope, dict) else {}
        )
        put_record = getattr(self.backend, "put_record", None)
        if not callable(put_record):
            return cast(WorkspaceRecordRef, scoped_ref)
        put_record_callable = cast(Callable[[WorkspaceRecordRef], Awaitable[WorkspaceRecordRef]], put_record)
        return await put_record_callable(cast(WorkspaceRecordRef, scoped_ref))

    async def put(
        self,
        record_or_content: Any = _MISSING,
        *,
        content: Any = _MISSING,
        collection: str,
        kind: str | None = None,
        meta: dict[str, Any] | None = None,
        profile: str | None = None,
        indexed: bool = False,
        vector: bool = False,
        **kwargs,
    ):
        if record_or_content is _MISSING:
            if content is _MISSING:
                raise TypeError("Workspace.put(...) requires record_or_content or content.")
            record_or_content = content
        elif content is not _MISSING:
            raise TypeError("Workspace.put(...) accepts either record_or_content or content, not both.")
        profile_name = str(profile or "").strip()
        if profile_name:
            handler = self.manager.get_profile(profile_name)
            scope_value = kwargs.get("scope")
            source_value = kwargs.get("source")
            summary_value = kwargs.get("summary")
            return await handler.ingest(
                workspace=self,
                content=record_or_content,
                collection=collection,
                kind=kind,
                scope=self._scoped_record_scope(scope_value if isinstance(scope_value, dict) else None),
                source=source_value if isinstance(source_value, dict) else {},
                summary=(
                    summary_value if isinstance(summary_value, str) or summary_value is None else str(summary_value)
                ),
                meta=meta,
            )
        if self.default_scope:
            kwargs["scope"] = self._scoped_record_scope(kwargs.get("scope"))
        return await self.backend.put(
            record_or_content,
            collection=collection,
            kind=kind,
            meta=meta,
            indexed=indexed,
            vector=vector,
            **kwargs,
        )

    async def get(self, ref_or_path: WorkspaceRecordRef | str):
        return await self.backend.get(ref_or_path)

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str):
        return await self.backend.get_data(ref_or_path)

    async def ref_envelope(self, ref_or_id: WorkspaceRecordRef | str) -> WorkspaceReferenceEnvelope:
        return await self.backend.ref_envelope(ref_or_id)

    async def read_bounded(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment:
        return await self.backend.read_bounded(ref_or_path, offset=offset, limit=limit)

    def stream_read(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]:
        return self.backend.stream_read(
            ref_or_path,
            offset=offset,
            limit=limit,
            chunk_size=chunk_size,
        )

    async def grep(self, query: str | None = None, filters: dict[str, Any] | None = None):
        return await self.backend.search(query, self._scoped_filters(filters))

    async def search(self, query: str | None = None, filters: dict[str, Any] | None = None):
        scoped_filters = self._scoped_filters(filters)
        deterministic = await self.backend.search(query, scoped_filters)
        if not self._search_should_use_retrieve(
            query=query,
            filters=scoped_filters,
            candidate_count=len(deterministic),
        ):
            return deterministic
        package = await self.retrieve(
            query,
            filters=scoped_filters,
            sources=["records"],
            budget={
                "chars": 6000,
                "item_chars": 1200,
                "max_candidates": max(50, len(deterministic)),
            },
            selection="length",
            rerank=False,
            max_candidates=max(50, len(deterministic)),
            profile="search_auto",
        )
        refs = self._record_refs_from_retrieval_package(package)
        return refs or deterministic

    async def retrieve(
        self,
        query: str | None = None,
        *,
        tags: "Sequence[str] | None" = None,
        filters: dict[str, Any] | None = None,
        scope: dict[str, Any] | None = None,
        sources: "Sequence[str] | None" = None,
        budget: dict[str, Any] | None = None,
        selection: WorkspaceRetrievalSelection = "length",
        top_n: int | None = None,
        method: WorkspaceRetrievalMethod = "auto",
        rerank: bool | None = None,
        rerank_handler: RerankHandler | None = None,
        max_rerank_retries: int = 1,
        file_options: dict[str, Any] | None = None,
        max_candidates: int | None = None,
        profile: str = "auto",
        plugin_manager: Any = None,
        settings: Any = None,
    ) -> WorkspaceRetrievalPackage:
        return await retrieve_workspace(
            self,
            query,
            tags=tags,
            filters=filters,
            scope=scope,
            sources=sources,
            budget=budget,
            selection=selection,
            top_n=top_n,
            method=method,
            rerank=rerank,
            rerank_handler=rerank_handler,
            max_rerank_retries=max_rerank_retries,
            file_options=file_options,
            max_candidates=max_candidates,
            profile=profile,
            plugin_manager=plugin_manager,
            settings=settings,
        )

    async def link(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ):
        return await self.backend.link(source, target, relation, meta)

    async def links(
        self,
        ref_or_id: WorkspaceRecordRef | str | None = None,
        *,
        source: WorkspaceRecordRef | str | None = None,
        target: WorkspaceRecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[WorkspaceLinkRef]:
        return await self.backend.links(ref_or_id, source=source, target=target, relation=relation)

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
        return await self.backend.link_evidence(
            source,
            target,
            relation,
            execution_id=execution_id,
            operation_id=operation_id,
            runtime_event_id=runtime_event_id,
            checkpoint_id=checkpoint_id,
            exchange_id=exchange_id,
            artifact_refs=artifact_refs,
            meta=meta,
        )

    @_guard_workspace_mutation
    async def checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
        ref = await self.backend.checkpoint(run_id, state, step_id=step_id)
        return await self._scope_record_ref(ref)

    @_guard_workspace_mutation
    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        ref = await self.backend.put_checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )
        return await self._scope_record_ref(ref)

    async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

    @_guard_workspace_mutation
    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        ref = await self.backend.put_snapshot(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )
        return await self._scope_record_ref(ref)

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        ref = await self.latest_snapshot(run_id)
        if ref is None:
            return None
        state = await self.get_data(ref)
        return state if isinstance(state, dict) else None

    async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

    @_guard_workspace_mutation
    async def delete_snapshot(self, run_id: str) -> dict[str, Any]:
        delete_snapshot = getattr(self.backend, "delete_snapshot", None)
        if not callable(delete_snapshot):
            raise TypeError("Workspace snapshot provider must expose async delete_snapshot(run_id).")
        delete_snapshot_async = cast(
            Callable[[str], Awaitable[dict[str, Any]]],
            delete_snapshot,
        )
        return await delete_snapshot_async(run_id)

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        if not self.default_search_scope:
            return await self.backend.latest_checkpoint(run_id)
        history = await self.checkpoint_history(run_id)
        return history[0] if history else None

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        backend_limit = None if self.default_search_scope else limit
        history = await self.backend.checkpoint_history(run_id, step_id=step_id, limit=backend_limit)
        if self.default_search_scope:
            history = [ref for ref in history if self._matches_default_search_scope(ref)]
            if limit is not None:
                if limit < 0:
                    raise ValueError("limit must be greater than or equal to 0.")
                history = history[:limit]
        return history

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> WorkspaceLeaseRef:
        return await self.backend.claim_lease(
            run_id,
            owner_id,
            ttl=ttl,
            expected_state_version=expected_state_version,
        )

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        return await self.backend.heartbeat_lease(run_id, owner_id, lease_token)

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        return await self.backend.release_lease(run_id, owner_id, lease_token)

    @_guard_workspace_mutation
    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef:
        scoped_metadata = dict(metadata or {})
        if self.default_scope:
            raw_scope = scoped_metadata.get("scope")
            scoped_metadata["scope"] = self._scoped_record_scope(raw_scope if isinstance(raw_scope, dict) else {})
        ref = await self.backend.put_artifact_ref(run_id, artifact, metadata=scoped_metadata)
        return await self._scope_record_ref(ref)

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
        return await self.backend.append_runtime_event(
            execution_id,
            event,
            sequence=sequence,
            expected_sequence=expected_sequence,
            idempotency_key=idempotency_key,
            snapshot_ref=snapshot_ref,
            artifact_refs=artifact_refs,
            exchange_id=exchange_id,
            state_version=state_version,
            parent_id=parent_id,
            causation_id=causation_id,
            parent_signal_id=parent_signal_id,
            node_id=node_id,
            operator_id=operator_id,
            interrupt_id=interrupt_id,
            resume_request_id=resume_request_id,
            actor_id=actor_id,
            lease_owner_id=lease_owner_id,
            aggregation_scope=aggregation_scope,
        )

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRuntimeEventRecord]:
        return await self.backend.query_runtime_events(
            execution_id,
            sequence_from=sequence_from,
            sequence_to=sequence_to,
            event_id=event_id,
            limit=limit,
        )

    def resolve_file_path(self, path: str | Path = ".") -> Path:
        """Resolve a Workspace-relative file path within this Workspace root."""

        candidate = Path(path)
        was_relative = not candidate.is_absolute()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.expanduser().resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"Path is outside Workspace root: { path }") from error
        if relative.parts and relative.parts[0] == ".agently" and not self._is_current_execution_file(relative):
            raise WorkspacePolicyError("Agently-private Workspace state is not available through ordinary file access.")
        if was_relative and not relative.parts[:1] == (".agently",) and not resolved.exists():
            fallback = self._resolve_fallback_file_path(resolved)
            if fallback.exists():
                return fallback
        return resolved

    def _is_current_execution_file(self, path: str | Path) -> bool:
        parts = Path(path).parts
        return len(parts) >= 4 and parts[:3] == (".agently", "files", self._execution_id)

    def _resolve_external_file_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.expanduser().resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as error:
            raise WorkspacePolicyError(f"Path is outside Workspace root: {path}") from error
        if relative.parts and relative.parts[0] == ".agently":
            raise WorkspacePolicyError("External Workspace writes cannot target Agently-private state.")
        return resolved

    def _resolve_fallback_file_path(self, requested_external_path: Path) -> Path:
        relative = requested_external_path.relative_to(self.root)
        fallback_root = (self.root / ".agently" / "files" / self._execution_id).resolve()
        target = (fallback_root / relative).resolve()
        try:
            target.relative_to(fallback_root)
        except ValueError as error:
            raise WorkspacePolicyError("Fallback file path escaped the current execution area.") from error
        return target

    def _ordinary_file_relative_path(self, target: Path) -> str:
        """Project the current fallback carrier into the ordinary file view."""

        relative = target.resolve().relative_to(self.root)
        parts = relative.parts
        if len(parts) >= 3 and parts[:3] == (".agently", "files", self._execution_id):
            logical = Path(*parts[3:])
            return logical.as_posix() if logical.parts else "."
        return relative.as_posix()

    def _with_trusted_file_ref(
        self,
        result: WorkspaceFileWriteResult,
        *,
        target: Path,
    ) -> WorkspaceFileWriteResult:
        relative = str(target.relative_to(self.root))
        result["path"] = relative
        refs = result.get("file_refs", [])
        for ref in refs:
            ref.update(
                {
                    "type": "file",
                    "path": relative,
                    "workspace_id": self._workspace_id,
                    "execution_id": self._execution_id,
                    "size": int(result.get("bytes", 0)),
                    "sha256": str(result.get("sha256", "")),
                    "available": target.is_file(),
                }
            )
        return result

    @staticmethod
    def _is_private_relative_path(path: str | Path) -> bool:
        parts = Path(path).parts
        return bool(parts) and parts[0] == ".agently"

    def inspect_file(self, path: str | Path) -> WorkspaceFileInfo:
        target = self.resolve_file_path(path)
        return self.manager.inspect_file_path(
            target,
            relative_path=str(target.relative_to(self.root)),
        )

    async def read_file(
        self,
        path: str | Path,
        *,
        max_bytes: int = 20000,
        offset: int = 0,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileReadResult:
        target = self.resolve_file_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Workspace file not found: { path }")
        return await self.manager.read_file_path(
            target,
            relative_path=str(target.relative_to(self.root)),
            max_bytes=max_bytes,
            offset=offset,
            handler=handler,
            options=options,
        )

    async def _promote_file_identity(
        self,
        path: str | Path,
        *,
        role: str,
    ) -> WorkspaceFileRef:
        """Promote one physically verified file into the private identity graph."""

        normalized_role = str(role or "").strip()
        if not normalized_role:
            raise ValueError("Workspace file identity promotion requires a role.")
        target = self.resolve_file_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Workspace file not found: {path}")
        relative = target.relative_to(self.root).as_posix()

        def fingerprint() -> tuple[str, int]:
            digest = hashlib.sha256()
            size = 0
            with target.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            return digest.hexdigest(), size

        digest, size = await asyncio.to_thread(fingerprint)
        catalog = getattr(self.backend, "_identity_catalog", None)
        observe_content = getattr(catalog, "observe_content", None)
        if not callable(observe_content):
            raise WorkspacePolicyError("Workspace backend cannot persist the private content identity graph.")
        typed_observe_content = cast(Callable[..., Awaitable[Any]], observe_content)
        observation = await typed_observe_content(
            locator_kind="path",
            normalized_locator=relative,
            digest=digest,
            size=size,
            payload_pointer={"type": "workspace_file", "path": relative},
        )
        info = self.manager.inspect_file_path(target, relative_path=relative)
        return cast(
            WorkspaceFileRef,
            {
                "type": "file",
                "path": relative,
                "workspace_id": self._workspace_id,
                "execution_id": self._execution_id,
                "size": size,
                "bytes": size,
                "sha256": digest,
                "available": True,
                "media_type": info.get("media_type"),
                "content_kind": str(info.get("content_kind") or "unknown"),
                "role": normalized_role,
                "locator_id": observation.locator_id,
                "content_version_id": observation.content_version_id,
            },
        )

    async def glob_files(
        self,
        pattern: str,
        *,
        path: str | Path = ".",
        max_results: int = 200,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        requested_pattern = str(pattern or "*").strip() or "*"
        effective_pattern = "**/*" if requested_pattern in {"**", "**/"} else requested_pattern
        safe_max_results = max(1, min(int(max_results), 5000))
        base = self.resolve_file_path(path)
        if base.is_file():
            candidates = [base] if base.match(requested_pattern) or requested_pattern in {"*", "**", "**/*"} else []
        elif base.exists():
            candidates = base.rglob(effective_pattern)
        else:
            candidates = []
        matches: list[str] = []
        truncated = False
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                relative = self._ordinary_file_relative_path(candidate)
            except ValueError:
                continue
            if self._is_private_relative_path(relative):
                continue
            if not include_hidden and any(part.startswith(".") for part in Path(relative).parts):
                continue
            if len(matches) >= safe_max_results:
                truncated = True
                break
            matches.append(relative)
        matches = sorted(dict.fromkeys(matches))
        return {
            "pattern": requested_pattern,
            "path": str(path),
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
            "max_results": safe_max_results,
        }

    async def grep_files(
        self,
        query: str,
        *,
        path: str | Path = ".",
        pattern: str = "*",
        max_results: int = 50,
        include_hidden: bool = False,
        max_file_bytes: int = 200000,
        context_lines: int = 0,
        max_snippet_bytes: int = 1200,
        regex: bool | None = None,
        glob: str | None = None,
    ) -> Any:
        if regex is not None or glob is not None:
            return await self._grep_file_lines(
                query,
                path=path,
                regex=True if regex is None else regex,
                glob=glob,
                context_lines=context_lines,
                max_results=max_results,
                include_hidden=include_hidden,
                max_file_bytes=max_file_bytes,
                max_snippet_bytes=max_snippet_bytes,
            )
        return await self._search_file_refs(
            query,
            path=path,
            pattern=pattern,
            max_results=max_results,
            include_hidden=include_hidden,
            max_file_bytes=max_file_bytes,
            context_lines=context_lines,
            max_snippet_bytes=max_snippet_bytes,
        )

    async def _grep_file_lines(
        self,
        pattern: str,
        *,
        path: str | Path = ".",
        regex: bool = True,
        glob: str | None = None,
        context_lines: int = 0,
        max_results: int = 50,
        include_hidden: bool = False,
        max_file_bytes: int = 200000,
        max_snippet_bytes: int = 1200,
    ) -> dict[str, Any]:
        query_text = str(pattern or "")
        if not query_text:
            return {"pattern": query_text, "matches": [], "count": 0, "truncated": False}
        compiled = re.compile(query_text) if regex else None
        file_pattern = str(glob or "**/*")
        safe_context_lines = max(0, min(int(context_lines), 20))
        safe_max_results = max(1, min(int(max_results), 1000))
        candidates = await self.glob_files(
            file_pattern,
            path=path,
            max_results=5000,
            include_hidden=include_hidden,
        )
        matches: list[dict[str, Any]] = []
        truncated = bool(candidates.get("truncated"))
        for relative in candidates.get("matches", []):
            if len(matches) >= safe_max_results:
                truncated = True
                break
            if not isinstance(relative, str):
                continue
            info = self.inspect_file(relative)
            if int(info.get("bytes") or 0) > max_file_bytes:
                continue
            read_result = await self.read_file(relative, max_bytes=max_file_bytes)
            if not read_result.get("readable") or read_result.get("content_kind") != "text":
                continue
            lines = str(read_result.get("content") or "").splitlines()
            for line_index, line in enumerate(lines):
                matched = bool(compiled.search(line)) if compiled is not None else query_text in line
                if not matched:
                    continue
                snippet_start = max(0, line_index - safe_context_lines)
                snippet_end = min(len(lines), line_index + safe_context_lines + 1)
                snippet = "\n".join(lines[snippet_start:snippet_end])
                snippet_raw = snippet.encode("utf-8")
                snippet_truncated = False
                if len(snippet_raw) > max_snippet_bytes:
                    snippet = snippet_raw[:max_snippet_bytes].decode("utf-8", errors="ignore")
                    snippet_truncated = True
                matches.append(
                    {
                        "path": relative,
                        "line": line_index + 1,
                        "text": line,
                        "snippet": snippet,
                        "line_start": snippet_start + 1,
                        "line_end": snippet_end,
                        "truncated": snippet_truncated,
                    }
                )
                if len(matches) >= safe_max_results:
                    truncated = True
                    break
        return {
            "pattern": query_text,
            "regex": regex,
            "glob": file_pattern,
            "path": str(path),
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
            "max_results": safe_max_results,
        }

    async def search_files(
        self,
        query: str,
        *,
        path: str | Path = ".",
        pattern: str = "*",
        max_results: int = 50,
        include_hidden: bool = False,
        max_file_bytes: int = 200000,
        context_lines: int = 0,
        max_snippet_bytes: int = 1200,
    ) -> list[WorkspaceFileSearchResult]:
        deterministic = await self.grep_files(
            query,
            path=path,
            pattern=pattern,
            max_results=max_results,
            include_hidden=include_hidden,
            max_file_bytes=max_file_bytes,
            context_lines=context_lines,
            max_snippet_bytes=max_snippet_bytes,
        )
        if not self._search_files_should_use_retrieve(
            query=query,
            path=path,
            pattern=pattern,
            candidate_count=len(deterministic),
            max_results=max_results,
        ):
            return deterministic
        package = await self.retrieve(
            query,
            sources=["files"],
            budget={
                "chars": 6000,
                "item_chars": max(400, min(int(max_snippet_bytes), 1200)),
                "max_candidates": max(50, len(deterministic)),
            },
            selection="length",
            rerank=False,
            file_options={
                "path": path,
                "pattern": pattern,
                "max_results": max_results,
                "include_hidden": include_hidden,
                "max_file_bytes": max_file_bytes,
                "context_lines": context_lines,
                "max_snippet_bytes": max_snippet_bytes,
            },
            max_candidates=max(50, len(deterministic)),
            profile="search_files_auto",
        )
        results = self._file_results_from_retrieval_package(package)
        return results or deterministic

    @staticmethod
    def _search_should_use_retrieve(
        *,
        query: str | None,
        filters: dict[str, Any],
        candidate_count: int,
    ) -> bool:
        if not query or not str(query).strip():
            return False
        if candidate_count <= 8:
            return False
        if filters.get("id") is not None or filters.get("path") is not None:
            return False
        return True

    @staticmethod
    def _search_files_should_use_retrieve(
        *,
        query: str,
        path: str | Path,
        pattern: str,
        candidate_count: int,
        max_results: int,
    ) -> bool:
        _ = path, pattern, max_results
        if not str(query or "").strip():
            return False
        if candidate_count <= 8:
            return False
        return True

    @staticmethod
    def _record_refs_from_retrieval_package(package: Any) -> list[WorkspaceRecordRef]:
        if not isinstance(package, dict):
            return []
        items = package.get("items")
        if not isinstance(items, list):
            return []
        refs: list[WorkspaceRecordRef] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            ref = item.get("ref")
            if not isinstance(ref, dict):
                continue
            record_id = str(ref.get("id") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            refs.append(cast(WorkspaceRecordRef, ref))
        return refs

    @staticmethod
    def _file_results_from_retrieval_package(package: Any) -> list[WorkspaceFileSearchResult]:
        if not isinstance(package, dict):
            return []
        items = package.get("items")
        if not isinstance(items, list):
            return []
        results: list[WorkspaceFileSearchResult] = []
        seen: set[tuple[str, int]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            result = item.get("file")
            if not isinstance(result, dict):
                continue
            key = (str(result.get("path") or ""), int(result.get("line") or 0))
            if key in seen:
                continue
            seen.add(key)
            results.append(cast(WorkspaceFileSearchResult, result))
        return results

    async def _search_file_refs(
        self,
        query: str,
        *,
        path: str | Path = ".",
        pattern: str = "*",
        max_results: int = 50,
        include_hidden: bool = False,
        max_file_bytes: int = 200000,
        context_lines: int = 0,
        max_snippet_bytes: int = 1200,
    ) -> list[WorkspaceFileSearchResult]:
        query_text = str(query)
        if not query_text:
            return []
        requested_pattern = str(pattern or "*")
        effective_pattern = "**/*" if requested_pattern.strip() in {"**", "**/"} else requested_pattern
        safe_max_results = max(1, min(int(max_results), 1000))
        safe_max_file_bytes = max(1, min(int(max_file_bytes), 5_000_000))
        safe_context_lines = max(0, min(int(context_lines), 20))
        safe_max_snippet_bytes = max(1, min(int(max_snippet_bytes), 12000))
        base = self.resolve_file_path(path)
        results: list[WorkspaceFileSearchResult] = []
        rg_matches = await asyncio.to_thread(
            self._search_file_matches_with_rg,
            query_text,
            base,
            requested_pattern,
            effective_pattern,
            safe_max_results,
            include_hidden,
        )
        if rg_matches is not None:
            for candidate, relative, line_no in rg_matches:
                if len(results) >= safe_max_results:
                    break
                result = await self._build_file_search_result(
                    candidate=candidate,
                    relative=relative,
                    line_no=line_no,
                    query_text=query_text,
                    path=path,
                    requested_pattern=requested_pattern,
                    effective_pattern=effective_pattern,
                    include_hidden=include_hidden,
                    max_results=safe_max_results,
                    max_file_bytes=safe_max_file_bytes,
                    context_lines=safe_context_lines,
                    max_snippet_bytes=safe_max_snippet_bytes,
                    search_engine="workspace_file_grep",
                    grep_tool="rg",
                )
                if result is not None:
                    results.append(result)
            return results

        if base.is_file():
            candidates = [base]
        elif base.exists():
            candidates = base.rglob(effective_pattern)
        else:
            candidates = []

        for candidate in candidates:
            if len(results) >= safe_max_results:
                break
            if not candidate.is_file():
                continue
            try:
                relative = self._ordinary_file_relative_path(candidate)
            except ValueError:
                continue
            if self._is_private_relative_path(relative):
                continue
            if not include_hidden and any(part.startswith(".") for part in Path(relative).parts):
                continue
            file_size = candidate.stat().st_size
            if file_size > safe_max_file_bytes:
                continue
            read_result = await self.read_file(relative, max_bytes=safe_max_file_bytes)
            if not read_result.get("readable") or read_result.get("content_kind") != "text":
                continue
            line_no = self._first_matching_line(str(read_result.get("content", "")).splitlines(), query_text)
            if line_no <= 0:
                continue
            result = await self._build_file_search_result(
                candidate=candidate,
                relative=relative,
                line_no=line_no,
                query_text=query_text,
                path=path,
                requested_pattern=requested_pattern,
                effective_pattern=effective_pattern,
                include_hidden=include_hidden,
                max_results=safe_max_results,
                max_file_bytes=safe_max_file_bytes,
                context_lines=safe_context_lines,
                max_snippet_bytes=safe_max_snippet_bytes,
                search_engine="workspace_file_scan",
                grep_tool=None,
            )
            if result is not None:
                results.append(result)
        return results

    def _search_file_matches_with_rg(
        self,
        query_text: str,
        base: Path,
        requested_pattern: str,
        effective_pattern: str,
        max_results: int,
        include_hidden: bool,
    ) -> list[tuple[Path, str, int]] | None:
        rg_path = shutil.which("rg")
        if rg_path is None:
            return None
        if not base.exists():
            return []
        command = [
            rg_path,
            "--json",
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--no-ignore",
            "--max-count",
            "1",
            "--glob",
            effective_pattern,
            query_text,
        ]
        if include_hidden:
            command.insert(1, "--hidden")
        search_root = base if base.is_dir() else base.parent
        command.append(str(base.name if base.is_file() else "."))
        try:
            completed = subprocess.run(
                command,
                cwd=str(search_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except Exception:
            return None
        if completed.returncode == 1:
            return []
        if completed.returncode != 0:
            return None

        matches: list[tuple[Path, str, int]] = []
        seen_paths: set[str] = set()
        for raw_line in completed.stdout.splitlines():
            if len(matches) >= max_results:
                break
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            path_info = data.get("path")
            if not isinstance(path_info, dict):
                continue
            raw_candidate_path = path_info.get("text")
            if not isinstance(raw_candidate_path, str):
                continue
            candidate = (search_root / raw_candidate_path).resolve()
            try:
                relative = self._ordinary_file_relative_path(candidate)
            except ValueError:
                continue
            if self._is_private_relative_path(relative):
                continue
            if relative in seen_paths:
                continue
            if not include_hidden and any(part.startswith(".") for part in Path(relative).parts):
                continue
            line_no = int(data.get("line_number") or 0)
            matches.append((candidate, relative, line_no))
            seen_paths.add(relative)
        return matches

    async def _build_file_search_result(
        self,
        *,
        candidate: Path,
        relative: str,
        line_no: int,
        query_text: str,
        path: str | Path,
        requested_pattern: str,
        effective_pattern: str,
        include_hidden: bool,
        max_results: int,
        max_file_bytes: int,
        context_lines: int,
        max_snippet_bytes: int,
        search_engine: str,
        grep_tool: str | None,
    ) -> WorkspaceFileSearchResult | None:
        if not candidate.is_file():
            return None
        file_size = candidate.stat().st_size
        if file_size > max_file_bytes:
            return None
        read_result = await self.read_file(relative, max_bytes=max_file_bytes)
        if not read_result.get("readable") or read_result.get("content_kind") != "text":
            return None
        text = str(read_result.get("content", ""))
        lines = text.splitlines()
        if line_no <= 0 or line_no > len(lines):
            line_no = self._first_matching_line(lines, query_text)
        if line_no <= 0:
            return None
        line_index = line_no - 1
        snippet_start = max(0, line_index - context_lines)
        snippet_end = min(len(lines), line_index + context_lines + 1)
        snippet = "\n".join(lines[snippet_start:snippet_end])
        snippet_raw = snippet.encode("utf-8")
        snippet_truncated = False
        if len(snippet_raw) > max_snippet_bytes:
            snippet = snippet_raw[:max_snippet_bytes].decode("utf-8", errors="ignore")
            snippet_raw = snippet.encode("utf-8")
            snippet_truncated = True
        search_scope = {
            "path": str(path),
            "pattern": requested_pattern,
            "effective_pattern": effective_pattern,
            "include_hidden": include_hidden,
            "max_results": max_results,
            "max_file_bytes": max_file_bytes,
            "context_lines": context_lines,
            "max_snippet_bytes": max_snippet_bytes,
            "search_engine": search_engine,
            "grep_tool": grep_tool,
        }
        file_bytes = int(read_result.get("bytes", file_size))
        file_sha256 = str(read_result.get("sha256", ""))
        file_media_type = read_result.get("media_type")
        file_content_kind = str(read_result.get("content_kind", "unknown"))
        file_ref: WorkspaceFileRef = {
            "type": "file",
            "path": relative,
            "workspace_id": self._workspace_id,
            "execution_id": self._execution_id,
            "size": file_bytes,
            "available": True,
            "bytes": file_bytes,
            "sha256": file_sha256,
            "media_type": file_media_type,
            "content_kind": file_content_kind,
            "role": "source",
        }
        locator_ref = {
            "role": "locator_ref",
            "content_state": "ref_only",
            "source": "workspace.search_files",
            "query": query_text,
            "scope": search_scope,
            "path": relative,
            "bytes": file_bytes,
            "sha256": file_sha256,
            "media_type": file_media_type,
            "content_kind": file_content_kind,
            "search_engine": search_engine,
            "grep_tool": grep_tool,
        }
        return cast(
            WorkspaceFileSearchResult,
            {
                "path": relative,
                "line": line_no,
                "text": lines[line_index],
                "role": "evidence_snippet",
                "content_state": "bounded_readback_available",
                "source": "workspace.search_files",
                "query": query_text,
                "scope": search_scope,
                "locator_ref": locator_ref,
                "snippet": snippet,
                "snippet_chars": len(snippet),
                "snippet_bytes": len(snippet_raw),
                "truncated": snippet_truncated,
                "line_start": snippet_start + 1,
                "line_end": snippet_end,
                "bytes": file_bytes,
                "sha256": file_sha256,
                "media_type": file_media_type,
                "content_kind": file_content_kind,
                "search_engine": search_engine,
                "grep_tool": grep_tool,
                "file_ref": file_ref,
            },
        )

    @staticmethod
    def _first_matching_line(lines: list[str], query_text: str) -> int:
        for line_no, line in enumerate(lines, start=1):
            if query_text in line:
                return line_no
        return 0

    async def write_file(
        self,
        path: str | Path,
        content: str,
        *,
        append: bool = False,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileWriteResult:
        async with self._backend_mutation_guard():
            return await self._write_file_unlocked(
                path,
                content,
                append=append,
                handler=handler,
                options=options,
            )

    async def _write_file_unlocked(
        self,
        path: str | Path,
        content: str,
        *,
        append: bool = False,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileWriteResult:
        candidate_target = self.resolve_file_path(path)
        candidate_relative = candidate_target.relative_to(self.root)
        current_execution_private = self._is_current_execution_file(candidate_relative)
        external_target = candidate_target if current_execution_private else self._resolve_external_file_path(path)
        if current_execution_private:
            target = candidate_target
        elif self.mode == "read_only":
            if external_target.exists() or append:
                raise WorkspacePolicyError("External Workspace mutation requires an explicit write grant or approval.")
            target = self._resolve_fallback_file_path(external_target)
        else:
            target = external_target
        result = await self.manager.write_file_path(
            target,
            relative_path=str(target.relative_to(self.root)),
            content=content,
            append=append,
            handler=handler,
            options=options,
        )
        return self._with_trusted_file_ref(result, target=target)

    async def edit_file(
        self,
        path: str | Path,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
        expected_sha256: str | None = None,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileWriteResult:
        async with self._backend_mutation_guard():
            return await self._edit_file_unlocked(
                path,
                old_string,
                new_string,
                replace_all=replace_all,
                expected_sha256=expected_sha256,
                handler=handler,
                options=options,
            )

    async def _edit_file_unlocked(
        self,
        path: str | Path,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
        expected_sha256: str | None = None,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileWriteResult:
        target = self.resolve_file_path(path)
        target_relative = target.relative_to(self.root)
        if self.mode == "read_only" and not self._is_current_execution_file(target_relative):
            raise WorkspacePolicyError(
                "Editing an external Workspace file requires an explicit write grant or approval."
            )
        if old_string == new_string:
            raise ValueError("old_string and new_string are identical; no edit was applied.")
        info = self.inspect_file(path)
        if expected_sha256 is not None and info.get("exists") and str(info.get("sha256") or "") != expected_sha256:
            raise ValueError("Workspace file has changed since the expected sha256.")
        if not info.get("exists"):
            if old_string != "":
                raise FileNotFoundError(f"Workspace file not found: { path }")
            return await self._write_file_unlocked(
                path,
                new_string,
                append=False,
                handler=handler,
                options=options,
            )
        read_result = await self.read_file(path, max_bytes=int(info.get("bytes") or 0) + 1)
        if not read_result.get("readable") or read_result.get("content_kind") != "text":
            raise ValueError(f"Workspace file is not editable text: { path }")
        if read_result.get("truncated"):
            raise ValueError(f"Workspace file must be fully read before edit_file can edit it: { path }")
        content = str(read_result.get("content") or "")
        if old_string == "":
            if content:
                raise ValueError("Cannot create a file with edit_file because the target already exists.")
            replacement_count = 1
            new_content = new_string
        else:
            replacement_count = content.count(old_string)
            if replacement_count <= 0:
                raise ValueError("old_string was not found in the Workspace file.")
            if replacement_count > 1 and not replace_all:
                raise ValueError("old_string matched multiple locations; set replace_all=True or provide more context.")
            new_content = (
                content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
            )
        result = dict(
            await self._write_file_unlocked(
                path,
                new_content,
                append=False,
                handler=handler,
                options=options,
            )
        )
        result["replacements"] = replacement_count
        return cast(WorkspaceFileWriteResult, result)

    async def apply_patch(
        self,
        patch: str,
        *,
        expected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._backend_mutation_guard():
            return await self._apply_patch_unlocked(
                patch,
                expected_files=expected_files,
            )

    async def _apply_patch_unlocked(
        self,
        patch: str,
        *,
        expected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.mode == "read_only":
            raise WorkspacePolicyError(
                "Applying a patch to external Workspace files requires an explicit write grant or approval."
            )
        patch_text = str(patch or "")
        paths = self._paths_from_unified_patch(patch_text)
        if not paths:
            raise ValueError("Patch did not declare any file paths.")
        normalized_expected: list[str] = []
        if expected_files is not None:
            for item in expected_files:
                target = self.resolve_file_path(item)
                normalized_expected.append(str(target.relative_to(self.root)))
            if sorted(paths) != sorted(dict.fromkeys(normalized_expected)):
                raise ValueError("Patch file set does not match expected_files.")
        git_path = shutil.which("git")
        if git_path is None:
            raise RuntimeError("git executable is required for Workspace.apply_patch(...).")
        completed = await asyncio.to_thread(
            subprocess.run,
            [git_path, "apply", "--whitespace=nowarn"],
            cwd=str(self.root),
            input=patch_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            message = str(completed.stderr or completed.stdout or "git apply failed").strip()
            raise ValueError(message)
        return {
            "ok": True,
            "status": "success",
            "paths": paths,
            "file_infos": [self.inspect_file(path) for path in paths],
        }

    def _paths_from_unified_patch(self, patch: str) -> list[str]:
        paths: list[str] = []

        def add(raw_path: str) -> None:
            text = raw_path.strip()
            if not text or text == "/dev/null":
                return
            if text.startswith("a/") or text.startswith("b/"):
                text = text[2:]
            if "\t" in text:
                text = text.split("\t", 1)[0]
            target = self.resolve_file_path(text)
            relative = str(target.relative_to(self.root))
            if relative not in paths:
                paths.append(relative)

        for line in patch.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    add(parts[2])
                    add(parts[3])
                continue
            if line.startswith("+++ ") or line.startswith("--- "):
                add(line[4:])
                continue
        return paths

    async def _close_execution_files(
        self,
        *,
        retained_refs: list[WorkspaceFileRef | dict[str, Any]],
        status: str,
    ) -> dict[str, Any]:
        """Close the current fallback carrier without touching external files."""
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Workspace execution status must be completed, failed, or cancelled.")
        execution_root = (self.root / ".agently" / "files" / self._execution_id).resolve()
        if not execution_root.exists():
            return {
                "status": "noop",
                "execution_id": self._execution_id,
                "retained_refs": [],
                "retained_bytes": 0,
                "deleted_bytes": 0,
                "diagnostics": [],
            }

        verified_paths: set[Path] = set()
        verified_refs: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []

        for raw_ref in retained_refs:
            ref = dict(raw_ref) if isinstance(raw_ref, dict) else {}
            diagnostic_code: str | None = None
            diagnostic_message = ""
            path_text = str(ref.get("path") or "")
            target: Path | None = None
            if ref.get("type") != "file":
                diagnostic_code = "workspace.file_ref.invalid_type"
                diagnostic_message = "Retained Workspace reference is not a file ref."
            elif str(ref.get("workspace_id") or "") != self._workspace_id:
                diagnostic_code = "workspace.file_ref.workspace_mismatch"
                diagnostic_message = "Retained file ref belongs to another Workspace."
            elif str(ref.get("execution_id") or "") != self._execution_id:
                diagnostic_code = "workspace.file_ref.execution_mismatch"
                diagnostic_message = "Retained file ref belongs to another execution."
            else:
                try:
                    target = (self.root / path_text).resolve()
                    relative = target.relative_to(self.root)
                    if relative.parts and relative.parts[0] == ".agently":
                        target.relative_to(execution_root)
                except (OSError, ValueError):
                    target = None
                    diagnostic_code = "workspace.file_ref.path_outside_workspace"
                    diagnostic_message = "Retained file ref is outside the current Workspace."
            if diagnostic_code is None and target is not None:
                if not target.is_file():
                    diagnostic_code = "workspace.file_ref.unavailable"
                    diagnostic_message = "Retained file ref has no readable physical file."
                else:
                    raw = target.read_bytes()
                    actual_size = len(raw)
                    actual_digest = hashlib.sha256(raw).hexdigest()
                    claimed_size = ref.get("size")
                    if not isinstance(claimed_size, int) or claimed_size != actual_size:
                        diagnostic_code = "workspace.file_ref.size_mismatch"
                        diagnostic_message = "Retained file ref size does not match physical readback."
                    elif str(ref.get("sha256") or "") != actual_digest:
                        diagnostic_code = "workspace.file_ref.digest_mismatch"
                        diagnostic_message = "Retained file ref digest does not match physical readback."
                    else:
                        try:
                            target.relative_to(execution_root)
                        except ValueError:
                            # External files are caller-owned. Verify their refs, but
                            # never include them in execution-owned cleanup accounting.
                            verified_refs.append(ref)
                        else:
                            verified_paths.add(target)
                            verified_refs.append(ref)
            if diagnostic_code is not None:
                diagnostics.append(
                    {
                        "code": diagnostic_code,
                        "message": diagnostic_message,
                        "retryable": True,
                        "path": path_text,
                    }
                )

        # Retained refs are one closure: cleanup may start only after every
        # declared product has passed physical identity and integrity checks.
        # Returning before mutation keeps every possible product and
        # intermediate available for a retry or an explicit recovery decision.
        if diagnostics:
            return {
                "status": "deferred",
                "execution_id": self._execution_id,
                "retained_refs": [],
                "retained_bytes": 0,
                "deleted_bytes": 0,
                "diagnostics": diagnostics,
            }

        deleted_bytes = 0
        retained_bytes = sum(path.stat().st_size for path in verified_paths)
        for candidate in sorted(execution_root.rglob("*"), reverse=True):
            if not candidate.is_file() and not candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if resolved in verified_paths:
                continue
            try:
                size = candidate.lstat().st_size
                candidate.unlink()
                deleted_bytes += int(size)
            except OSError as error:
                diagnostics.append(
                    {
                        "code": "workspace.execution_file.delete_failed",
                        "message": str(error),
                        "retryable": True,
                        "path": str(candidate.relative_to(self.root)),
                    }
                )

        for directory in sorted(
            (path for path in execution_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        for directory in (
            execution_root,
            execution_root.parent,
            execution_root.parent.parent,
            self.root / ".agently",
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

        return {
            "status": "deferred" if diagnostics else "applied",
            "execution_id": self._execution_id,
            "retained_refs": verified_refs,
            "retained_bytes": retained_bytes,
            "deleted_bytes": deleted_bytes,
            "diagnostics": diagnostics,
        }

    async def materialize_file(
        self,
        path: str | Path,
        content: bytes,
        *,
        source: dict[str, Any] | None = None,
        media_type: str | None = None,
        overwrite: bool = False,
    ) -> WorkspaceFileWriteResult:
        async with self._backend_mutation_guard():
            return await self._materialize_file_unlocked(
                path,
                content,
                source=source,
                media_type=media_type,
                overwrite=overwrite,
            )

    async def _materialize_file_unlocked(
        self,
        path: str | Path,
        content: bytes,
        *,
        source: dict[str, Any] | None = None,
        media_type: str | None = None,
        overwrite: bool = False,
    ) -> WorkspaceFileWriteResult:
        """Materialize trusted bytes into the Workspace file boundary.

        This is intentionally separate from write_file(...), whose public
        contract stays plain-text handler-backed writes. Materialization is for
        framework-owned remote file downloads and binary evidence refs.
        """
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("Workspace.materialize_file(...) requires bytes content.")
        candidate_target = self.resolve_file_path(path)
        candidate_relative = candidate_target.relative_to(self.root)
        current_execution_private = self._is_current_execution_file(candidate_relative)
        external_target = candidate_target if current_execution_private else self._resolve_external_file_path(path)
        if current_execution_private:
            target = candidate_target
        elif self.mode == "read_only":
            if external_target.exists() or overwrite:
                raise WorkspacePolicyError("External Workspace mutation requires an explicit write grant or approval.")
            target = self._resolve_fallback_file_path(external_target)
        else:
            target = external_target
        if target.exists() and not overwrite:
            raise FileExistsError(f"Workspace file already exists: { path }")
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = bytes(content)
        target.write_bytes(raw)
        relative_path = str(target.relative_to(self.root))
        file_info: WorkspaceFileInfo = self.manager.inspect_file_path(target, relative_path=relative_path)
        if media_type and not file_info.get("media_type"):
            file_info = cast(WorkspaceFileInfo, dict(file_info))
            file_info["media_type"] = str(media_type)
        diagnostics: list[WorkspaceFileDiagnostic] = []
        if source:
            diagnostics.append(
                {
                    "code": "workspace.file.materialized",
                    "message": "File bytes were materialized into the Workspace file boundary.",
                    "handler_id": "workspace.materialize_file",
                    "detail": {"source": dict(source)},
                }
            )
        result: WorkspaceFileWriteResult = {
            "ok": True,
            "writable": True,
            "path": relative_path,
            "bytes": int(file_info.get("bytes", len(raw))),
            "sha256": str(file_info.get("sha256") or hashlib.sha256(raw).hexdigest()),
            "media_type": file_info.get("media_type"),
            "content_kind": str(file_info.get("content_kind", "unknown")),
            "encoding": None,
            "mode": "materialize",
            "handler_id": "workspace.materialize_file",
            "diagnostics": diagnostics,
            "file_refs": [
                cast(
                    WorkspaceFileRef,
                    {
                        "path": relative_path,
                        "bytes": int(file_info.get("bytes", len(raw))),
                        "sha256": str(file_info.get("sha256") or hashlib.sha256(raw).hexdigest()),
                        "media_type": file_info.get("media_type"),
                        "content_kind": str(file_info.get("content_kind", "unknown")),
                        "role": "download",
                    },
                )
            ],
        }
        return self._with_trusted_file_ref(result, target=target)

    async def export_file(
        self,
        source_path: str | Path,
        output_path: str | Path,
        *,
        export_kind: str,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileExportResult:
        async with self._backend_mutation_guard():
            return await self._export_file_unlocked(
                source_path,
                output_path,
                export_kind=export_kind,
                handler=handler,
                options=options,
            )

    async def _export_file_unlocked(
        self,
        source_path: str | Path,
        output_path: str | Path,
        *,
        export_kind: str,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileExportResult:
        source = self.resolve_file_path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"Workspace source file not found: { source_path }")
        candidate_output = self.resolve_file_path(output_path)
        candidate_relative = candidate_output.relative_to(self.root)
        current_execution_private = self._is_current_execution_file(candidate_relative)
        external_output = (
            candidate_output if current_execution_private else self._resolve_external_file_path(output_path)
        )
        if current_execution_private:
            output = candidate_output
        elif self.mode == "read_only":
            if external_output.exists():
                raise WorkspacePolicyError("External Workspace mutation requires an explicit write grant or approval.")
            output = self._resolve_fallback_file_path(external_output)
        else:
            output = external_output
        result = await self.manager.export_file_path(
            source,
            output,
            source_relative_path=str(source.relative_to(self.root)),
            output_relative_path=str(output.relative_to(self.root)),
            export_kind=export_kind,
            handler=handler,
            options=options,
        )
        result["output_path"] = str(output.relative_to(self.root))
        for ref in result.get("file_refs", []):
            ref.update(
                {
                    "type": "file",
                    "path": result["output_path"],
                    "workspace_id": self._workspace_id,
                    "execution_id": self._execution_id,
                    "size": int(result.get("bytes", 0)),
                    "sha256": str(result.get("sha256", "")),
                    "available": output.is_file(),
                }
            )
        return result

    def capabilities(self) -> WorkspaceBackendCapabilities:
        materialized_components: list[str] = []
        private_write = True
        if self._backend is not None:
            backend_capabilities = dict(self._backend.capabilities())
            private_write = bool(backend_capabilities.get("private_write", False))
            components = backend_capabilities.get("materialized_components", [])
            if isinstance(components, list):
                materialized_components = sorted(str(name) for name in components)
        return cast(
            WorkspaceBackendCapabilities,
            {
                "root": str(self.root),
                "mode": self.mode,
                "external_read": True,
                "external_write": self.mode == "read_write",
                "private_write": private_write,
                "materialized_components": materialized_components,
            },
        )

    def enable_file_actions(
        self,
        agent: Any,
        *,
        write: bool = False,
        read: bool = True,
        search: bool = True,
        list_files: bool = True,
        export: bool = False,
        action_prefix: str = "",
        expose_to_model: bool = True,
        **kwargs: Any,
    ):
        """Expose this Workspace's file area through an Agent's Action surface.

        Workspace owns the file root and path boundary; ActionRuntime only makes
        the scoped operations callable by the model.
        """
        enable = getattr(agent, "enable_workspace_file_actions", None)
        if not callable(enable):
            raise TypeError("Workspace file actions require an Agent with enable_workspace_file_actions(...).")
        return enable(
            root=self.root,
            read=read,
            write=write,
            search=search,
            list_files=list_files,
            export=export,
            action_prefix=action_prefix,
            expose_to_model=expose_to_model,
            **kwargs,
        )

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
        return await self.put(
            content=content,
            collection=collection,
            kind=kind,
            scope=scope,
            source=source or {},
            summary=summary,
            meta=meta,
            profile=profile or "fast",
        )

    async def build_context(
        self,
        *,
        goal: str,
        scope: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        profile: str = "auto",
    ):
        return await self.manager.build_context(
            self,
            goal=goal,
            scope=merge_scope(self.default_search_scope, scope),
            budget=budget,
            profile=profile,
        )
