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

from collections.abc import Callable
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agently.types.data import (
    WorkspaceRetentionLifecycle,
    WorkspaceRetentionPreview,
    WorkspaceRetentionResult,
    WorkspaceRetentionTerminalStatus,
)
from agently.types.plugins import WorkspaceBackend

from ._defaults import (
    ScopeNode,
    extend_lineage,
    extend_lineage_nodes,
    lineage_files_root,
    merge_scope,
    normalize_lineage,
    scope_from_lineage,
)
from .Workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .Manager import WorkspaceManager


class LazyWorkspace:
    """Agent-facing Workspace facade that materializes the backend on first use."""

    def __init__(
        self,
        manager: "WorkspaceManager",
        default_root: str | Path | WorkspaceBackend,
        *,
        create: bool = True,
        mode: str = "read_write",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        db_store_provider: Any | None = None,
        db_store_options: dict[str, Any] | None = None,
        embedding_provider: Any | None = None,
        embedding_options: dict[str, Any] | None = None,
        vector_store_provider: Any | None = None,
        vector_store_options: dict[str, Any] | None = None,
        files_root: str | Path | None = None,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
        scope_lineage: "Sequence[Mapping[str, Any]] | None" = None,
        on_materialize: Callable[[Workspace], None] | None = None,
    ):
        self.manager = manager
        self._default_root = default_root
        self._create = create
        self._mode = mode
        self._provider = provider
        self._provider_options = dict(provider_options or {})
        self._db_store_provider = db_store_provider
        self._db_store_options = dict(db_store_options or {})
        self._embedding_provider = embedding_provider
        self._embedding_options = dict(embedding_options or {})
        self._vector_store_provider = vector_store_provider
        self._vector_store_options = dict(vector_store_options or {})
        self._files_root = Path(str(files_root)).expanduser().resolve() if files_root is not None else None
        self._default_scope = dict(default_scope or {})
        self._default_search_scope = dict(default_search_scope or self._default_scope)
        self._scope_lineage: list[ScopeNode] = normalize_lineage(scope_lineage)
        self._workspace: Workspace | None = None
        self._on_materialize = on_materialize

    @property
    def scope_lineage(self) -> list[ScopeNode]:
        workspace = self._workspace
        if workspace is not None:
            return workspace.scope_lineage
        return list(self._scope_lineage)

    @property
    def is_materialized(self) -> bool:
        return self._workspace is not None

    @property
    def root(self) -> Path:
        workspace = self._workspace
        if workspace is not None:
            return workspace.root
        return Path(str(self._default_root)).expanduser().resolve()

    @property
    def content_root(self) -> Path:
        workspace = self._workspace
        if workspace is not None:
            return workspace.content_root
        return self.root / "content"

    @property
    def files_root(self) -> Path:
        workspace = self._workspace
        if workspace is not None:
            return workspace.files_root
        if self._files_root is not None:
            return self._files_root
        return self.root / "files"

    @property
    def backend(self) -> WorkspaceBackend:
        return self._materialize().backend

    def materialize(self) -> Workspace:
        return self._materialize()

    def capabilities(self):
        return self._materialize().capabilities()

    async def put_checkpoint(self, *args: Any, **kwargs: Any):
        return await self._materialize().put_checkpoint(*args, **kwargs)

    async def put_snapshot(self, *args: Any, **kwargs: Any):
        return await self._materialize().put_snapshot(*args, **kwargs)

    async def get_snapshot(self, *args: Any, **kwargs: Any):
        return await self._materialize().get_snapshot(*args, **kwargs)

    async def append_runtime_event(self, *args: Any, **kwargs: Any):
        return await self._materialize().append_runtime_event(*args, **kwargs)

    async def inspect_retention(self, *args: Any, **kwargs: Any) -> WorkspaceRetentionPreview:
        return await self._materialize().inspect_retention(*args, **kwargs)

    async def get_retention_lifecycle(
        self,
        execution_id: str,
        *,
        status: WorkspaceRetentionTerminalStatus,
        terminal_at: str | None = None,
    ) -> WorkspaceRetentionLifecycle:
        return await self._materialize().get_retention_lifecycle(
            execution_id,
            status=status,
            terminal_at=terminal_at,
        )

    async def apply_retention(
        self,
        preview: WorkspaceRetentionPreview,
    ) -> WorkspaceRetentionResult:
        return await self._materialize().apply_retention(preview)

    def _bind_child_lazy(
        self,
        child_lineage: list[ScopeNode],
        *,
        scope: dict[str, Any] | None,
        search_scope: dict[str, Any] | None,
    ) -> "LazyWorkspace":
        lineage_scope = scope_from_lineage(child_lineage)
        files_root = lineage_files_root(self.root, child_lineage)
        return LazyWorkspace(
            self.manager,
            self._default_root,
            create=self._create,
            mode=self._mode,
            provider=self._provider,
            provider_options=self._provider_options,
            db_store_provider=self._db_store_provider,
            db_store_options=self._db_store_options,
            embedding_provider=self._embedding_provider,
            embedding_options=self._embedding_options,
            vector_store_provider=self._vector_store_provider,
            vector_store_options=self._vector_store_options,
            files_root=files_root,
            default_scope=merge_scope(merge_scope(self._default_scope, lineage_scope), scope),
            default_search_scope=merge_scope(
                merge_scope(self._default_search_scope, lineage_scope), search_scope
            ),
            scope_lineage=child_lineage,
            on_materialize=None,
        )

    def with_scope_node(
        self,
        kind: str,
        node_id: str | None,
        *,
        scope: dict[str, Any] | None = None,
        search_scope: dict[str, Any] | None = None,
    ):
        if self._workspace is not None:
            return self._workspace.with_scope_node(
                kind, node_id, scope=scope, search_scope=search_scope
            )
        return self._bind_child_lazy(
            extend_lineage(self._scope_lineage, kind, node_id),
            scope=scope,
            search_scope=search_scope,
        )

    def with_scope_lineage(
        self,
        nodes: "Sequence[Mapping[str, Any]]",
        *,
        scope: dict[str, Any] | None = None,
        search_scope: dict[str, Any] | None = None,
    ):
        if self._workspace is not None:
            return self._workspace.with_scope_lineage(nodes, scope=scope, search_scope=search_scope)
        return self._bind_child_lazy(
            extend_lineage_nodes(self._scope_lineage, nodes),
            scope=scope,
            search_scope=search_scope,
        )

    def with_files_root(
        self,
        files_root: str | Path,
        *,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
    ):
        if self._workspace is not None:
            return self._workspace.with_files_root(
                files_root,
                default_scope=default_scope,
                default_search_scope=default_search_scope,
            )
        return LazyWorkspace(
            self.manager,
            self._default_root,
            create=self._create,
            mode=self._mode,
            provider=self._provider,
            provider_options=self._provider_options,
            db_store_provider=self._db_store_provider,
            db_store_options=self._db_store_options,
            embedding_provider=self._embedding_provider,
            embedding_options=self._embedding_options,
            vector_store_provider=self._vector_store_provider,
            vector_store_options=self._vector_store_options,
            files_root=files_root,
            default_scope=merge_scope(self._default_scope, default_scope),
            default_search_scope=merge_scope(self._default_search_scope, default_search_scope),
            scope_lineage=self._scope_lineage,
            on_materialize=None,
        )

    def _materialize(self) -> Workspace:
        if self._workspace is None:
            self._workspace = self.manager.create(
                self._default_root,
                create=self._create,
                mode=self._mode,
                provider=self._provider,
                provider_options=self._provider_options,
                db_store_provider=self._db_store_provider,
                db_store_options=self._db_store_options,
                embedding_provider=self._embedding_provider,
                embedding_options=self._embedding_options,
                vector_store_provider=self._vector_store_provider,
                vector_store_options=self._vector_store_options,
                files_root=self._files_root,
                default_scope=self._default_scope,
                default_search_scope=self._default_search_scope,
                scope_lineage=self._scope_lineage,
            )
            if self._on_materialize is not None:
                self._on_materialize(self._workspace)
        return self._workspace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._materialize(), name)

    def __repr__(self) -> str:
        state = "materialized" if self.is_materialized else "lazy"
        return f"<LazyWorkspace state={state} root={self.root!s}>"
