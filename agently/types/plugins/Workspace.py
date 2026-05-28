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

from typing import Any, Protocol, runtime_checkable

from agently.types.data.workspace import WorkspaceLinkRef, WorkspaceRecordRef


@runtime_checkable
class IngestionProfile(Protocol):
    name: str

    async def ingest(
        self,
        *,
        workspace: Any,
        content: Any,
        collection: str,
        kind: str | None,
        scope: dict[str, Any],
        source: dict[str, Any],
        summary: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef: ...


@runtime_checkable
class WorkspaceBackend(Protocol):
    root: Any
    content_root: Any

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
    ) -> WorkspaceRecordRef: ...

    async def get(self, ref_or_path: WorkspaceRecordRef | str) -> Any: ...

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]: ...

    async def link(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef: ...

    async def checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> WorkspaceRecordRef: ...

