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
from typing import Any, AsyncIterator

from agently.types.data.workspace import WorkspaceContentSegment, WorkspaceRecordRef, WorkspaceReferenceEnvelope

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

    async def read_content_segment(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment:
        target = self.policy.resolve_content_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"Workspace content not found: { path }")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0.")
        if limit is not None and limit < 0:
            raise ValueError("limit must be greater than or equal to 0.")
        total_size = target.stat().st_size
        read_size = max(0, total_size - offset) if limit is None else limit
        with target.open("rb") as file:
            file.seek(offset)
            raw = file.read(read_size)
        placeholder_ref: WorkspaceReferenceEnvelope = {
            "workspace_id": "",
            "kind": "content",
            "collection": "",
            "record_id": "",
            "version": None,
            "content_ref": path,
            "digest": None,
            "size": total_size,
            "created_at": "",
            "policy_labels": [],
            "backend_capabilities": {},
        }
        segment: WorkspaceContentSegment = {
            "ref": placeholder_ref,
            "content": raw.decode("utf-8", errors="replace"),
            "offset": offset,
            "size": len(raw),
            "total_size": total_size,
            "eof": offset + len(raw) >= total_size,
            "digest": None,
            "content_type": "text/plain",
        }
        return segment

    async def stream_content(
        self,
        path: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0.")
        remaining = limit
        current_offset = offset
        while remaining is None or remaining > 0:
            next_limit = chunk_size if remaining is None else min(chunk_size, remaining)
            segment = await self.read_content_segment(path, offset=current_offset, limit=next_limit)
            if segment["size"] == 0:
                break
            yield segment
            current_offset += segment["size"]
            if remaining is not None:
                remaining -= segment["size"]
            if segment["eof"]:
                break


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
