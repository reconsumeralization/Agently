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
from collections.abc import Mapping
from typing import Any

from agently.types.data import (
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
    ContextSourceRead,
)

from .TaskWorkspace import TaskWorkspace


class TaskWorkspaceContextSource:
    """Structural descriptor and exact-read port for TaskWorkspace files."""

    source_kind = "task_workspace"

    def __init__(self, task_workspace: TaskWorkspace) -> None:
        self.task_workspace = task_workspace
        root_digest = hashlib.sha256(str(task_workspace.root).encode("utf-8")).hexdigest()[:16]
        self.source_id = f"task-workspace:{root_digest}:{task_workspace.execution_id}"

    def _logical_paths(self) -> tuple[str, ...]:
        logical: set[str] = set()
        for relative in self.task_workspace.list_files():
            target = self.task_workspace.root / relative
            parts = self.task_workspace._logical_file_parts(target)
            if parts:
                logical.add("/".join(parts))
        return tuple(sorted(logical))

    @property
    def source_revision(self) -> str:
        digest = hashlib.sha256()
        for relative in self._logical_paths():
            path = self.task_workspace.resolve_file_path(relative)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    async def async_enumerate_descriptors(
        self,
        *,
        profile: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> ContextSourceDescriptorPage:
        page_size = int(limit)
        if page_size <= 0:
            raise ValueError("limit must be a positive integer.")
        try:
            offset = int(cursor or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("TaskWorkspace descriptor cursor is invalid.") from error
        if offset < 0:
            raise ValueError("TaskWorkspace descriptor cursor cannot be negative.")
        projection_max_chars = int(profile.get("projection_max_chars") or 2000)
        if projection_max_chars <= 0:
            raise ValueError("projection_max_chars must be positive.")
        revision = self.source_revision
        paths = self._logical_paths()
        page_paths = paths[offset : offset + page_size]
        descriptors: list[ContextSourceDescriptor] = []
        for relative in page_paths:
            info = self.task_workspace.inspect_file(relative)
            content_kind = str(info.get("content_kind") or "unknown")
            projection = ""
            readback = None
            if content_kind in {"text", "pdf", "office"}:
                readback = await self.task_workspace.read_file(
                    relative,
                    max_bytes=projection_max_chars,
                )
                projection = readback.content
            total_bytes = int(
                str(
                    info.get("bytes")
                    or info.get("size")
                    or (readback.total_bytes if readback is not None else 0)
                )
            )
            media_type = info.get("media_type") or (
                readback.media_type if readback is not None else None
            )
            sha256 = info.get("sha256") or (
                readback.sha256 if readback is not None else None
            )
            metadata_only = content_kind in {"image", "binary", "unknown"} or (
                content_kind in {"pdf", "office"}
                and (readback is None or not readback.readable)
            )
            descriptors.append(
                ContextSourceDescriptor(
                    descriptor_key=f"task-workspace:{relative}",
                    source_id=self.source_id,
                    source_revision=revision,
                    source_ref=relative,
                    role="information",
                    title=relative,
                    summary=(projection or relative)[:500],
                    estimated_chars=(len(relative) if metadata_only else total_bytes),
                    index_text=(
                        relative
                        if metadata_only or not projection
                        else f"{relative}\n{projection}"
                    ),
                    content_digest=str(sha256 or ""),
                    metadata={
                        "path": relative,
                        "sha256": sha256,
                        "total_bytes": total_bytes,
                        "media_type": media_type,
                        "content_kind": content_kind,
                        "context_representation": (
                            "image_attachment_or_metadata"
                            if content_kind == "image"
                            else "metadata_only"
                            if metadata_only
                            else "parsed_text"
                            if content_kind in {"pdf", "office"}
                            else "text"
                        ),
                    },
                )
            )
        next_offset = offset + len(page_paths)
        return ContextSourceDescriptorPage(
            source_id=self.source_id,
            source_revision=revision,
            descriptors=tuple(descriptors),
            next_cursor=(str(next_offset) if next_offset < len(paths) else None),
        )

    async def async_read_exact(
        self,
        source_ref: str,
        *,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        info = self.task_workspace.inspect_file(source_ref)
        content_kind = str(info.get("content_kind") or "unknown")
        if content_kind in {"binary", "unknown"} or (
            content_kind == "image" and representation != "image_attachment"
        ):
            return ContextSourceRead(
                source_id=self.source_id,
                source_revision=self.source_revision,
                source_ref=source_ref,
                content=None,
                completeness="ref_only",
                content_digest=str(info.get("sha256") or ""),
                refs=(source_ref,),
                metadata={
                    "path": source_ref,
                    "sha256": info.get("sha256"),
                    "total_bytes": int(str(info.get("bytes") or 0)),
                    "media_type": info.get("media_type"),
                    "content_kind": content_kind,
                    "context_representation": "metadata_only",
                },
            )
        readback = await self.task_workspace.read_file(
            source_ref,
            max_bytes=max_chars,
            offset=range_start,
        )
        content: Any = readback.content
        context_representation = (
            "parsed_text"
            if content_kind in {"pdf", "office"}
            else "text"
        )
        if content_kind == "image" and representation == "image_attachment":
            content = list(readback.attachments)
            context_representation = "image_attachment"
        next_range_start = range_start + len(readback.data)
        return ContextSourceRead(
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=source_ref,
            content=content,
            completeness="truncated" if readback.truncated else "complete",
            next_range_start=(next_range_start if readback.truncated else None),
            content_digest=readback.sha256,
            metadata={
                "path": source_ref,
                "sha256": readback.sha256,
                "total_bytes": readback.total_bytes,
                "media_type": readback.media_type,
                "content_kind": readback.content_kind,
                "context_representation": context_representation,
            },
        )


__all__ = ["TaskWorkspaceContextSource"]
