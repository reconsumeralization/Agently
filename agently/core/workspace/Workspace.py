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
    WorkspaceFileReadResult,
    WorkspaceFileWriteResult,
    WorkspaceFilePolicyMetadata,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetentionAnchor,
    WorkspaceRuntimeEventRecord,
    WorkspaceScratchLease,
)
from agently.types.plugins import WorkspaceBackend
from ._defaults import (
    ScopeNode,
    extend_lineage,
    extend_lineage_nodes,
    lineage_files_root,
    lineage_scratch_root,
    merge_scope,
    normalize_lineage,
    scope_from_lineage,
)
from ._utils import utc_now

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .Manager import WorkspaceManager


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

    def _scoped_record_scope(self, scope: dict[str, Any] | None) -> dict[str, Any]:
        return merge_scope(self.default_scope, scope)

    def _scoped_filters(self, filters: dict[str, Any] | None) -> dict[str, Any]:
        scoped = dict(filters or {})
        for key, value in self.default_search_scope.items():
            filter_key = f"scope.{key}"
            scoped.setdefault(filter_key, value)
        return scoped

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
        record_or_content: Any,
        *,
        collection: str,
        kind: str | None = None,
        meta: dict[str, Any] | None = None,
        **kwargs,
    ):
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

    async def search(self, query: str | None = None, filters: dict[str, Any] | None = None):
        return await self.backend.search(query, self._scoped_filters(filters))

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
        return await self.backend.get_checkpoint(run_id)

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
        return await self.backend.get_snapshot(run_id)

    async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.backend.latest_snapshot(run_id)

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.backend.latest_checkpoint(run_id)

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        return await self.backend.checkpoint_history(run_id, step_id=step_id, limit=limit)

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
        return await self.backend.put_artifact_ref(run_id, artifact, metadata=metadata)

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
        handler = self.manager.get_profile(profile)
        return await handler.ingest(
            workspace=self,
            content=content,
            collection=collection,
            kind=kind,
            scope=self._scoped_record_scope(scope),
            source=source or {},
            summary=summary,
            meta=meta,
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
