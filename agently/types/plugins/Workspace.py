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

from agently.types.data.workspace import WorkspaceBackendCapabilities, WorkspaceLinkRef, WorkspaceRecordRef


@runtime_checkable
class ContentStore(Protocol):
    async def write_content(self, relative_path: str, content: bytes) -> str: ...

    async def read_content(self, path: str) -> Any: ...


@runtime_checkable
class MetadataStore(Protocol):
    async def put_record(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef: ...

    async def get_record(self, record_id: str) -> WorkspaceRecordRef | None: ...


@runtime_checkable
class CheckpointStore(Protocol):
    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> WorkspaceRecordRef: ...


@runtime_checkable
class TextIndex(Protocol):
    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]: ...


@runtime_checkable
class VectorIndex(Protocol):
    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]: ...


@runtime_checkable
class PolicyEngine(Protocol):
    def ensure_writable(self) -> None: ...

    def resolve_content_path(self, path: str) -> Any: ...

    async def filter_records(
        self,
        records: list[WorkspaceRecordRef],
        *,
        purpose: str = "prompt",
    ) -> list[WorkspaceRecordRef]: ...


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
    @property
    def root(self) -> Any: ...

    @property
    def content_root(self) -> Any: ...

    @property
    def content(self) -> ContentStore: ...

    @property
    def metadata(self) -> MetadataStore: ...

    @property
    def checkpoint_store(self) -> CheckpointStore: ...

    @property
    def text_index(self) -> TextIndex: ...

    @property
    def policy(self) -> PolicyEngine: ...

    @property
    def vector_index(self) -> VectorIndex | None: ...

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

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str) -> Any: ...

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

    async def links(
        self,
        ref_or_id: WorkspaceRecordRef | str | None = None,
        *,
        source: WorkspaceRecordRef | str | None = None,
        target: WorkspaceRecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[WorkspaceLinkRef]: ...

    async def checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> WorkspaceRecordRef: ...

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None: ...

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
    ) -> list[WorkspaceRecordRef]: ...

    def capabilities(self) -> WorkspaceBackendCapabilities: ...
