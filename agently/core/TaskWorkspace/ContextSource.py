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
from collections.abc import Mapping, Sequence
from typing import Any

from agently.core.context._Cursor import decode_source_cursor, encode_source_cursor
from agently.types.data import ContextBlock, ContextCandidate, ContextReadIntent
from agently.types.plugins import ContextSourceCandidateWindow

from .TaskWorkspace import TaskWorkspace


def _source_kind_enabled(filters: Mapping[str, Any], kind: str) -> bool:
    raw = filters.get("source_kinds")
    if raw is None:
        return True
    if isinstance(raw, str):
        offered = {raw.strip()}
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        offered = {str(item).strip() for item in raw if str(item).strip()}
    else:
        return False
    return not offered or kind in offered


class TaskWorkspaceContextSource:
    """Source-local structural search and exact readback for TaskWorkspace files."""

    def __init__(self, task_workspace: TaskWorkspace) -> None:
        self.task_workspace = task_workspace
        root_digest = hashlib.sha256(str(task_workspace.root).encode("utf-8")).hexdigest()[:16]
        self.source_id = f"task-workspace:{root_digest}:{task_workspace.execution_id}"

    @property
    def source_revision(self) -> str:
        digest = hashlib.sha256()
        for relative in self.task_workspace.list_files():
            path = self.task_workspace.resolve_path(relative)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        cursor: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> ContextSourceCandidateWindow:
        page_size = int(limit)
        if page_size <= 0:
            raise ValueError("limit must be a positive integer.")
        resolved_filters = dict(filters or intent.filters)
        explicit = set(intent.explicit_refs)
        path = str(resolved_filters.get("path") or ".")
        pattern = str(resolved_filters.get("pattern") or "**/*")
        max_file_bytes = int(resolved_filters.get("max_file_bytes") or 200000)
        include_hidden = bool(resolved_filters.get("include_hidden", False))
        revision = self.source_revision
        scope = {
            "query": intent.query,
            "path": path,
            "pattern": pattern,
            "max_file_bytes": max_file_bytes,
            "include_hidden": include_hidden,
            "explicit_refs": sorted(explicit),
        }
        if not _source_kind_enabled(resolved_filters, "task_workspace"):
            return ContextSourceCandidateWindow(
                source_id=self.source_id,
                source_revision=revision,
                scope={**scope, "enabled": False},
                candidates=(),
                returned_candidates=0,
                exhaustive=True,
                cursor=cursor,
            )
        offset = decode_source_cursor(
            cursor,
            source_id=self.source_id,
            source_revision=revision,
            scope=scope,
        )
        results = await self.task_workspace.search_files(
            intent.query,
            path=path,
            pattern=pattern,
            offset=offset,
            max_results=page_size + 1,
            max_file_bytes=max_file_bytes,
            include_hidden=include_hidden,
        )
        has_more = len(results) > page_size
        page_results = results[:page_size]
        by_path = {str(item.get("path") or ""): item for item in page_results}
        for source_ref in sorted(explicit):
            if source_ref in by_path:
                continue
            try:
                info = self.task_workspace.inspect_file(source_ref)
            except (FileNotFoundError, IsADirectoryError, ValueError):
                continue
            by_path[source_ref] = {
                "path": source_ref,
                "line": 0,
                "text": str(info.get("path") or source_ref),
                "bytes": info.get("bytes", info.get("size", 0)),
                "sha256": info.get("sha256"),
                "media_type": info.get("media_type"),
            }
        ordered_paths = [ref for ref in sorted(explicit) if ref in by_path]
        ordered_paths.extend(
            relative for relative in by_path if relative not in explicit
        )
        candidates: list[ContextCandidate] = []
        for relative in ordered_paths:
            result = by_path[relative]
            matched_line = int(str(result.get("line") or 0))
            matched_text = str(result.get("text") or result.get("snippet") or relative)
            total_bytes = int(str(result.get("bytes") or 0))
            candidates.append(
                ContextCandidate(
                    block_key=f"task-workspace-source:{len(candidates) + 1}",
                    source_id=self.source_id,
                    source_revision=revision,
                    source_ref=relative,
                    binding_id=self.source_id,
                    role="information",
                    summary=(matched_text or relative)[:500],
                    estimated_chars=total_bytes,
                    completeness="truncated" if bool(result.get("truncated")) else "complete",
                    metadata={
                        "path": relative,
                        "line": matched_line,
                        "sha256": result.get("sha256"),
                        "total_bytes": total_bytes,
                        "media_type": result.get("media_type"),
                    },
                )
            )
        next_cursor = (
            encode_source_cursor(
                source_id=self.source_id,
                source_revision=revision,
                scope=scope,
                offset=offset + len(page_results),
            )
            if has_more
            else None
        )
        return ContextSourceCandidateWindow(
            source_id=self.source_id,
            source_revision=revision,
            scope=scope,
            candidates=tuple(candidates),
            returned_candidates=len(candidates),
            exhaustive=not has_more,
            cursor=cursor,
            next_cursor=next_cursor,
        )

    async def async_read(
        self,
        candidate: ContextCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
    ) -> ContextBlock:
        del representation
        if candidate.source_id != self.source_id:
            raise ValueError("TaskWorkspace candidate belongs to a different source.")
        readback = await self.task_workspace.read_file(
            candidate.source_ref,
            max_bytes=max_chars,
        )
        digest = hashlib.sha256(
            f"{self.source_revision}\0{candidate.source_ref}".encode("utf-8")
        ).hexdigest()
        return ContextBlock(
            block_id=f"task_workspace_block:{digest}",
            block_key=candidate.block_key,
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=candidate.source_ref,
            binding_id=candidate.binding_id,
            role=candidate.role,
            content=readback.content,
            completeness="truncated" if readback.truncated else "complete",
            content_chars=len(readback.content),
            required=candidate.required,
            refs=(candidate.source_ref,),
            metadata={
                **dict(candidate.metadata),
                "sha256": readback.sha256,
                "total_bytes": readback.total_bytes,
            },
        )


__all__ = ["TaskWorkspaceContextSource"]
