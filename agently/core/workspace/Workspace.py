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
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, cast

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
    WorkspaceFilePolicyMetadata,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetentionAnchor,
    WorkspaceRetrievalMethod,
    WorkspaceRetrievalPackage,
    WorkspaceRetrievalSelection,
    WorkspaceRuntimeEventRecord,
    WorkspaceScratchLease,
)
from agently.types.plugins import WorkspaceBackend
from .Retrieval import RerankHandler, retrieve_workspace
from ._defaults import (
    ScopeNode,
    WORKSPACE_FILE_AREAS,
    WORKSPACE_GUIDE_FILENAME,
    extend_lineage,
    extend_lineage_nodes,
    lineage_files_root,
    lineage_scratch_root,
    merge_scope,
    normalize_file_area,
    normalize_lineage,
    scope_from_lineage,
)
from ._utils import utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .Manager import WorkspaceManager


_MISSING = object()


class Workspace:
    """Workspace API bound to one backend."""

    def __init__(
        self,
        backend: WorkspaceBackend | str | Path | None = None,
        manager: "WorkspaceManager | None" = None,
        *,
        create: bool = True,
        mode: str = "read_write",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        files_root: str | Path | None = None,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
        scope_lineage: "Sequence[Mapping[str, Any]] | None" = None,
    ):
        if manager is None:
            from .Manager import WorkspaceManager

            workspace = WorkspaceManager().create(
                backend,
                create=create,
                mode=mode,
                provider=provider,
                provider_options=provider_options,
                files_root=files_root,
                default_scope=default_scope,
                default_search_scope=default_search_scope,
                scope_lineage=scope_lineage,
            )
            self.__dict__.update(workspace.__dict__)
            return
        if backend is None:
            raise ValueError("Workspace backend is required when manager is provided.")
        self.backend = cast(WorkspaceBackend, backend)
        self.manager = manager
        self.root = Path(str(getattr(self.backend, "root")))
        self.content_root = Path(str(getattr(self.backend, "content_root")))
        if files_root is None:
            self.files_root = Path(str(getattr(self.backend, "files_root", self.content_root)))
        else:
            self.files_root = Path(str(files_root)).expanduser().resolve()
            if mode not in {"read", "read_only", "readonly"}:
                self.files_root.mkdir(parents=True, exist_ok=True)
        self.scope_lineage: list[ScopeNode] = normalize_lineage(scope_lineage)
        self.default_scope = dict(default_scope or {})
        self.default_search_scope = dict(default_search_scope or self.default_scope)
        if not getattr(self.backend, "read_only", False):
            self.ensure_files_guide()

    def _bind_child(
        self,
        child_lineage: list[ScopeNode],
        *,
        scope: dict[str, Any] | None,
        search_scope: dict[str, Any] | None,
    ) -> "Workspace":
        lineage_scope = scope_from_lineage(child_lineage)
        files_root = lineage_files_root(self.root, child_lineage)
        return Workspace(
            self.backend,
            self.manager,
            files_root=files_root,
            mode="read_only" if self.capabilities().get("read_only") else "read_write",
            default_scope=merge_scope(merge_scope(self.default_scope, lineage_scope), scope),
            default_search_scope=merge_scope(
                merge_scope(self.default_search_scope, lineage_scope), search_scope
            ),
            scope_lineage=child_lineage,
        )

    def with_scope_node(
        self,
        kind: str,
        node_id: str | None,
        *,
        scope: dict[str, Any] | None = None,
        search_scope: dict[str, Any] | None = None,
    ) -> "Workspace":
        """Bind a child Workspace whose file root is contained under this scope.

        This is the lineage-aware replacement for the removed flat
        ``scoped_files_root(kind, id)`` helper: the child file root is derived
        from the full resolved scope chain, and the child ``default_scope``
        carries the same lineage so physical cleanup and record-index cleanup
        agree (spec section 8.2).
        """

        return self._bind_child(
            extend_lineage(self.scope_lineage, kind, node_id),
            scope=scope,
            search_scope=search_scope,
        )

    def with_scope_lineage(
        self,
        nodes: "Sequence[Mapping[str, Any]]",
        *,
        scope: dict[str, Any] | None = None,
        search_scope: dict[str, Any] | None = None,
    ) -> "Workspace":
        """Bind a child Workspace extended by several resolved lineage nodes."""

        return self._bind_child(
            extend_lineage_nodes(self.scope_lineage, nodes),
            scope=scope,
            search_scope=search_scope,
        )

    def with_files_root(
        self,
        files_root: str | Path,
        *,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
    ) -> "Workspace":
        # Internal materialization helper that preserves the resolved scope
        # lineage. It must not be used to invent flat, lineage-unaware roots;
        # use ``with_scope_node`` / ``with_scope_lineage`` for child binding.
        return Workspace(
            self.backend,
            self.manager,
            files_root=files_root,
            mode="read_only" if self.capabilities().get("read_only") else "read_write",
            default_scope=merge_scope(self.default_scope, default_scope),
            default_search_scope=merge_scope(self.default_search_scope, default_search_scope),
            scope_lineage=self.scope_lineage,
        )

    def ensure_files_guide(self) -> Path:
        """Write a small human-readable guide into the scoped editable file root.

        The guide is intentionally not named ``README.md`` so task deliverables
        and cloned repositories can keep their own README semantics.
        """

        self.files_root.mkdir(parents=True, exist_ok=True)
        guide_path = self.files_root / WORKSPACE_GUIDE_FILENAME
        if guide_path.exists():
            return guide_path
        lineage = " -> ".join(
            f"{ node.get('kind', '') }/{ node.get('id', '') }"
            for node in self.scope_lineage
            if node.get("kind")
        ) or "workspace root"
        scope_lines = [
            f"- { key }: { value }"
            for key, value in sorted(self.default_scope.items())
            if value is not None and key != "scope_lineage"
        ]
        if not scope_lines:
            scope_lines = ["- none"]
        area_lines = [
            f"- { name }/: { description }"
            for name, description in sorted(WORKSPACE_FILE_AREAS.items())
        ]
        guide_path.write_text(
            "\n".join(
                [
                    "# Agently Workspace Files",
                    "",
                    "This directory is the editable file working tree for the current Agently scope.",
                    "",
                    f"- Workspace root: { self.root }",
                    f"- Files root: { self.files_root }",
                    f"- Scope lineage: { lineage }",
                    "",
                    "Scope fields:",
                    *scope_lines,
                    "",
                    "Standard file areas:",
                    *area_lines,
                    "",
                    "Use this directory for task deliverables, downloaded source files, and files shared with Actions or external coding agents.",
                    "Use Workspace.open_scratch(...) or Workspace.scratch_root() for temporary scratch work; do not invent a scratch/ folder under this files root.",
                    "Do not assume sibling lineage directories are in scope. Do not edit workspace.db or content/ directly.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return guide_path

    @staticmethod
    def standard_file_areas() -> dict[str, str]:
        """Return the standard editable file-area names for scoped files roots."""

        return dict(WORKSPACE_FILE_AREAS)

    def file_area_path(
        self,
        area: str,
        *parts: str | Path,
        create: bool = False,
    ) -> Path:
        """Resolve a standard file-area path within this scoped ``files_root``.

        The returned path is always contained by ``files_root``. When ``create``
        is true, the area directory or the returned path's parent directory is
        created unless the Workspace is read-only.
        """

        normalized_area = normalize_file_area(area)
        relative = Path(normalized_area)
        for part in parts:
            candidate = Path(part)
            if candidate.is_absolute():
                raise ValueError(f"Workspace file area path parts must be relative: { part }")
            if any(segment == ".." for segment in candidate.parts):
                raise ValueError(f"Workspace file area path parts must not contain '..': { part }")
            relative = relative / candidate
        target = self.resolve_file_path(relative)
        if create:
            if self.capabilities().get("read_only"):
                raise PermissionError("Workspace is read-only; file_area_path(..., create=True) is blocked.")
            directory = target if not parts else target.parent
            directory.mkdir(parents=True, exist_ok=True)
        return target

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
        scoped_ref["scope"] = merge_scope(self.default_scope, existing_scope if isinstance(existing_scope, dict) else {})
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
                summary=summary_value if isinstance(summary_value, str) or summary_value is None else str(summary_value),
                meta=meta,
            )
        if self.default_scope:
            kwargs["scope"] = self._scoped_record_scope(kwargs.get("scope"))
        return await self.backend.put(record_or_content, collection=collection, kind=kind, meta=meta, **kwargs)

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

    async def checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
        ref = await self.backend.checkpoint(run_id, state, step_id=step_id)
        return await self._scope_record_ref(ref)

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
        return await self.backend.record_file_policy(
            action_file_root=action_file_root or str(self.files_root),
            allowed_roots=allowed_roots or [str(self.files_root)],
            root_source=root_source,
            path_normalization=path_normalization,
            symlink_policy=symlink_policy,
            case_policy=case_policy,
            policy_labels=policy_labels,
            links=links,
        )

    async def get_file_policy(self) -> WorkspaceFilePolicyMetadata:
        metadata = dict(await self.backend.get_file_policy())
        metadata["action_file_root"] = metadata.get("action_file_root") or str(self.files_root)
        metadata["allowed_roots"] = metadata.get("allowed_roots") or [str(self.files_root)]
        return cast(WorkspaceFilePolicyMetadata, metadata)

    def resolve_file_path(self, path: str | Path = ".") -> Path:
        """Resolve a Workspace-relative file path within this Workspace root."""

        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.files_root / candidate
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(self.files_root)
        except ValueError as error:
            raise ValueError(f"Path is outside workspace file root: { path }") from error
        return resolved

    def inspect_file(self, path: str | Path) -> WorkspaceFileInfo:
        target = self.resolve_file_path(path)
        return self.manager.inspect_file_path(
            target,
            relative_path=str(target.relative_to(self.files_root)),
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
            relative_path=str(target.relative_to(self.files_root)),
            max_bytes=max_bytes,
            offset=offset,
            handler=handler,
            options=options,
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
                relative = str(candidate.relative_to(self.files_root))
            except ValueError:
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
                relative = str(candidate.relative_to(self.files_root))
            except ValueError:
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
                relative = str(candidate.relative_to(self.files_root))
            except ValueError:
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
        file_ref = cast(
            WorkspaceFileRef,
            {
                "path": relative,
                "bytes": int(read_result.get("bytes", file_size)),
                "sha256": str(read_result.get("sha256", "")),
                "media_type": read_result.get("media_type"),
                "content_kind": str(read_result.get("content_kind", "unknown")),
                "role": "source",
            },
        )
        locator_ref = {
            "role": "locator_ref",
            "content_state": "ref_only",
            "source": "workspace.search_files",
            "query": query_text,
            "scope": search_scope,
            "path": relative,
            "bytes": file_ref["bytes"],
            "sha256": file_ref["sha256"],
            "media_type": file_ref["media_type"],
            "content_kind": file_ref["content_kind"],
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
                "bytes": file_ref["bytes"],
                "sha256": file_ref["sha256"],
                "media_type": file_ref["media_type"],
                "content_kind": file_ref["content_kind"],
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
        if self.capabilities().get("read_only"):
            raise PermissionError("Workspace is read-only; write_file(...) is blocked.")
        target = self.resolve_file_path(path)
        return await self.manager.write_file_path(
            target,
            relative_path=str(target.relative_to(self.files_root)),
            content=content,
            append=append,
            handler=handler,
            options=options,
        )

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
        if old_string == new_string:
            raise ValueError("old_string and new_string are identical; no edit was applied.")
        info = self.inspect_file(path)
        if expected_sha256 is not None and info.get("exists") and str(info.get("sha256") or "") != expected_sha256:
            raise ValueError("Workspace file has changed since the expected sha256.")
        if not info.get("exists"):
            if old_string != "":
                raise FileNotFoundError(f"Workspace file not found: { path }")
            return await self.write_file(path, new_string, append=False, handler=handler, options=options)
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
            new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        result = dict(await self.write_file(path, new_content, append=False, handler=handler, options=options))
        result["replacements"] = replacement_count
        return cast(WorkspaceFileWriteResult, result)

    async def apply_patch(
        self,
        patch: str,
        *,
        expected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.capabilities().get("read_only"):
            raise PermissionError("Workspace is read-only; apply_patch(...) is blocked.")
        patch_text = str(patch or "")
        paths = self._paths_from_unified_patch(patch_text)
        if not paths:
            raise ValueError("Patch did not declare any file paths.")
        normalized_expected: list[str] = []
        if expected_files is not None:
            for item in expected_files:
                target = self.resolve_file_path(item)
                normalized_expected.append(str(target.relative_to(self.files_root)))
            if sorted(paths) != sorted(dict.fromkeys(normalized_expected)):
                raise ValueError("Patch file set does not match expected_files.")
        git_path = shutil.which("git")
        if git_path is None:
            raise RuntimeError("git executable is required for Workspace.apply_patch(...).")
        completed = await asyncio.to_thread(
            subprocess.run,
            [git_path, "apply", "--whitespace=nowarn"],
            cwd=str(self.files_root),
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
            relative = str(target.relative_to(self.files_root))
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

    async def materialize_file(
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
        if self.capabilities().get("read_only"):
            raise PermissionError("Workspace is read-only; materialize_file(...) is blocked.")
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("Workspace.materialize_file(...) requires bytes content.")
        target = self.resolve_file_path(path)
        if target.exists() and not overwrite:
            raise FileExistsError(f"Workspace file already exists: { path }")
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = bytes(content)
        target.write_bytes(raw)
        relative_path = str(target.relative_to(self.files_root))
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
        return {
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
                {
                    "path": relative_path,
                    "bytes": int(file_info.get("bytes", len(raw))),
                    "sha256": str(file_info.get("sha256") or hashlib.sha256(raw).hexdigest()),
                    "media_type": file_info.get("media_type"),
                    "content_kind": str(file_info.get("content_kind", "unknown")),
                    "role": "download",
                }
            ],
        }

    async def export_file(
        self,
        source_path: str | Path,
        output_path: str | Path,
        *,
        export_kind: str,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> WorkspaceFileExportResult:
        if self.capabilities().get("read_only"):
            raise PermissionError("Workspace is read-only; export_file(...) is blocked.")
        source = self.resolve_file_path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"Workspace source file not found: { source_path }")
        output = self.resolve_file_path(output_path)
        return await self.manager.export_file_path(
            source,
            output,
            source_relative_path=str(source.relative_to(self.files_root)),
            output_relative_path=str(output.relative_to(self.files_root)),
            export_kind=export_kind,
            handler=handler,
            options=options,
        )

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
        return await self.backend.add_retention_anchor(
            execution_id,
            anchor_type=anchor_type,
            sequence=sequence,
            record_ref=record_ref,
            summary_ref=summary_ref,
            preserved_event_ids=preserved_event_ids,
            meta=meta,
        )

    async def retention_anchors(
        self,
        execution_id: str,
        *,
        anchor_type: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRetentionAnchor]:
        return await self.backend.retention_anchors(execution_id, anchor_type=anchor_type, limit=limit)

    def capabilities(self) -> WorkspaceBackendCapabilities:
        capabilities = dict(self.backend.capabilities())
        capabilities["files_root"] = str(self.files_root)
        return cast(WorkspaceBackendCapabilities, capabilities)

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
            root=self.files_root,
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

    async def prune_scope(
        self,
        scope: dict[str, Any],
        *,
        remove_files: bool = True,
    ) -> dict[str, Any]:
        prune = getattr(self.backend, "prune_scope", None)
        if not callable(prune):
            raise TypeError("Workspace backend does not support prune_scope(...).")
        prune_scope = cast(Callable[..., Awaitable[dict[str, Any]]], prune)
        # Physical cleanup is delegated to the backend so it removes only the
        # lineage subtree(s) matching the scope, not the entire files_root
        # (spec sections 8.2 / 9). The backend owns the lineage path layout.
        return await prune_scope(scope, remove_files=remove_files)

    def scratch_root(self) -> Path:
        """Local lineage scratch root for this scope (no lease).

        Local-only convenience for ephemeral, self-managed scratch. Durable
        scratch that must survive a crash should use ``open_scratch(...)`` so its
        lifecycle is tracked by a lease record and does not bypass the lease
        lifecycle (spec sections 8.5 / A.1).
        """

        return lineage_scratch_root(self.root, self.scope_lineage)

    async def open_scratch(
        self,
        *,
        scope: dict[str, Any] | None = None,
        purpose: str | None = None,
        ttl_seconds: float | None = None,
        cleanup_policy: Literal["on_close", "on_scope_prune", "ttl"] = "on_close",
        read_only: bool = False,
        policy_labels: list[str] | None = None,
    ) -> WorkspaceScratchLease:
        """Open a scratch working directory backed by a durable lease record.

        The lease is registered as a durable Workspace fact so crashed runs can
        be recovered by TTL/startup cleanup and scope prune, not only by on_close
        cleanup (spec sections 8.5 / 11.1).
        """

        register_attr = getattr(self.backend, "register_scratch_lease", None)
        if not callable(register_attr):
            raise TypeError("Workspace backend does not support scratch leases.")
        register = cast(
            Callable[[WorkspaceScratchLease], Awaitable[WorkspaceScratchLease]], register_attr
        )
        lease_id = uuid.uuid4().hex
        local_path = self.scratch_root() / lease_id
        if not read_only:
            local_path.mkdir(parents=True, exist_ok=True)
        expires_at: str | None = None
        if ttl_seconds is not None:
            expires = datetime.now(timezone.utc) + timedelta(seconds=float(ttl_seconds))
            expires_at = expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        lease: WorkspaceScratchLease = {
            "lease_id": lease_id,
            "scope": merge_scope(self.default_scope, scope),
            "local_path": str(local_path),
            "mount": None,
            "purpose": purpose,
            "cleanup_policy": cleanup_policy,
            "expires_at": expires_at,
            "read_only": read_only,
            "policy_labels": list(policy_labels or []),
            "created_at": utc_now(),
            "closed_at": None,
        }
        return await register(lease)

    async def close_scratch(
        self,
        lease_id: str,
        *,
        remove: bool | None = None,
    ) -> WorkspaceScratchLease | None:
        get_attr = getattr(self.backend, "get_scratch_lease", None)
        close_attr = getattr(self.backend, "close_scratch_lease", None)
        if not callable(get_attr) or not callable(close_attr):
            raise TypeError("Workspace backend does not support scratch leases.")
        get = cast(Callable[[str], Awaitable["WorkspaceScratchLease | None"]], get_attr)
        close = cast(Callable[..., Awaitable["WorkspaceScratchLease | None"]], close_attr)
        lease = await get(lease_id)
        if lease is None:
            return None
        should_remove = remove if remove is not None else lease.get("cleanup_policy") in {"on_close", "ttl"}
        local_path = lease.get("local_path")
        if should_remove and local_path:
            import shutil

            path = Path(str(local_path))
            if path.exists():
                shutil.rmtree(path)
        return await close(lease_id)

    async def cleanup_scratch_leases(self, *, now: str | None = None) -> dict[str, Any]:
        """Recover crashed scratch leases using durable lease facts.

        Removes the working directory of every expired lease and marks it closed,
        so TTL/startup recovery does not rely on filesystem mtime heuristics
        (spec section 8.5).
        """

        list_attr = getattr(self.backend, "list_scratch_leases", None)
        close_attr = getattr(self.backend, "close_scratch_lease", None)
        if not callable(list_attr) or not callable(close_attr):
            raise TypeError("Workspace backend does not support scratch leases.")
        list_leases = cast(Callable[..., Awaitable[list[WorkspaceScratchLease]]], list_attr)
        close = cast(Callable[..., Awaitable["WorkspaceScratchLease | None"]], close_attr)
        stamp = now or utc_now()
        import shutil

        removed_paths: list[str] = []
        recovered: list[str] = []
        expired = await list_leases(expired_before=stamp)
        for lease in expired:
            lease_id = cast(str, lease.get("lease_id"))
            local_path = lease.get("local_path")
            if local_path:
                path = Path(str(local_path))
                if path.exists():
                    shutil.rmtree(path)
                    removed_paths.append(str(path))
            await close(lease_id, closed_at=stamp)
            recovered.append(lease_id)
        return {"recovered_leases": recovered, "removed_paths": removed_paths}
