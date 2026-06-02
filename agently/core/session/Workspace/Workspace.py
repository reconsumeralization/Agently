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

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agently.types.data.workspace import WorkspaceBackendCapabilities, WorkspaceLinkRef, WorkspaceRecordRef
from agently.types.plugins import WorkspaceBackend

if TYPE_CHECKING:
    from .Manager import WorkspaceManager


class Workspace:
    """Workspace API bound to one backend."""

    def __init__(self, backend: WorkspaceBackend, manager: "WorkspaceManager"):
        self.backend = backend
        self.manager = manager
        self.root = Path(str(getattr(backend, "root")))
        self.content_root = Path(str(getattr(backend, "content_root")))
        self.files_root = Path(str(getattr(backend, "files_root", self.content_root)))

    async def put(
        self,
        record_or_content: Any,
        *,
        collection: str,
        kind: str | None = None,
        meta: dict[str, Any] | None = None,
        **kwargs,
    ):
        return await self.backend.put(record_or_content, collection=collection, kind=kind, meta=meta, **kwargs)

    async def get(self, ref_or_path: WorkspaceRecordRef | str):
        return await self.backend.get(ref_or_path)

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str):
        return await self.backend.get_data(ref_or_path)

    async def search(self, query: str | None = None, filters: dict[str, Any] | None = None):
        return await self.backend.search(query, filters)

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

    async def checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
        return await self.backend.checkpoint(run_id, state, step_id=step_id)

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.backend.latest_checkpoint(run_id)

    async def checkpoint_history(self, run_id: str, *, step_id: str | None = None) -> list[WorkspaceRecordRef]:
        return await self.backend.checkpoint_history(run_id, step_id=step_id)

    def capabilities(self) -> WorkspaceBackendCapabilities:
        return self.backend.capabilities()

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
            scope=scope,
            budget=budget,
            profile=profile,
        )
