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
from typing import Any

from agently.types.data.workspace import WorkspaceRecordRef

from .Errors import WorkspacePolicyError


class LocalWorkspacePolicyEngine:
    def __init__(self, content_root: Path, *, read_only: bool = False):
        self.content_root = content_root
        self.read_only = read_only

    def ensure_writable(self) -> None:
        if self.read_only:
            raise WorkspacePolicyError("Workspace is configured read-only.")

    def resolve_content_path(self, path: str | Path):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.content_root / candidate
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(self.content_root)
        except ValueError as error:
            raise WorkspacePolicyError(f"Path is outside workspace content root: { path }") from error
        return resolved

    async def filter_records(
        self,
        records: list[WorkspaceRecordRef],
        *,
        purpose: str = "prompt",
    ) -> list[WorkspaceRecordRef]:
        _ = purpose
        return records


class LocalContentStore:
    def __init__(self, content_root: Path, policy: LocalWorkspacePolicyEngine):
        self.content_root = content_root
        self.policy = policy

    def ensure_collection(self, collection: str):
        collection_path = self.content_root / collection
        collection_path.mkdir(parents=True, exist_ok=True)
        return collection_path

    async def write_content(self, relative_path: str, content: bytes) -> str:
        self.policy.ensure_writable()
        target = self.policy.resolve_content_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return str(target.relative_to(self.content_root))

    async def read_content(self, path: str) -> Any:
        target = self.policy.resolve_content_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Workspace content not found: { path }")
        return target.read_text(encoding="utf-8", errors="replace")


class NoopVectorIndex:
    name = "noop"

    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
        _ = (ref, content)

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        _ = (query, filters, limit)
        return []
