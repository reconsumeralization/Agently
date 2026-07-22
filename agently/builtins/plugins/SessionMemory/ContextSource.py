# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from agently.core.storage import RecordStoreContextSource
from agently.types.data import (
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
    ContextSourceRead,
)


class AgentlyMemoryContextSource:
    """Fixed-scope Session memory adapter; ContextIndex owns recall."""

    source_kind = "session_memory"

    def __init__(self, record_store: Any, *, session_id: str) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id cannot be empty.")
        self.record_store = record_store
        self.session_id = normalized_session_id
        self._record_source = RecordStoreContextSource(record_store)
        scope_digest = hashlib.sha256(
            normalized_session_id.encode("utf-8")
        ).hexdigest()[:16]
        self.source_id = f"session-memory:{record_store.record_store_id}:{scope_digest}"

    @property
    def source_revision(self) -> str:
        return self._record_source.source_revision

    def _authorized_filter_sets(self) -> tuple[dict[str, Any], ...]:
        return (
            {
                "collection": "memory",
                "kind": "global_memory",
                "scope.memory_scope": "GLOBAL_MEMORY",
            },
            {
                "collection": "memory",
                "kind": "session_memory",
                "scope.memory_scope": "SESSION_MEMORY",
                "scope.session_id": self.session_id,
            },
        )

    async def _authorized_refs(self) -> tuple[Mapping[str, Any], ...]:
        by_id: dict[str, Mapping[str, Any]] = {}
        for filters in self._authorized_filter_sets():
            for ref in await self.record_store.search(query=None, filters=filters):
                record_id = str(ref.get("id") or "").strip()
                if not record_id:
                    raise ValueError("Session memory search returned a record without id.")
                by_id[record_id] = ref
        return tuple(by_id[key] for key in sorted(by_id))

    async def _authorize_ref(self, source_ref: str) -> Mapping[str, Any]:
        record_id = str(source_ref or "").strip()
        if not record_id:
            raise ValueError("source_ref cannot be empty.")
        for filters in self._authorized_filter_sets():
            matches = await self.record_store.search(
                query=None,
                filters={**filters, "id": record_id},
            )
            if matches:
                return matches[0]
        raise PermissionError(
            "Session memory source_ref is not authorized for global or active-session recall."
        )

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
            raise ValueError("Session memory descriptor cursor is invalid.") from error
        if offset < 0:
            raise ValueError("Session memory descriptor cursor cannot be negative.")
        projection_max_chars = int(profile.get("projection_max_chars") or 2000)
        if projection_max_chars <= 0:
            raise ValueError("projection_max_chars must be positive.")
        revision = self.source_revision
        refs = await self._authorized_refs()
        page_refs = refs[offset : offset + page_size]
        descriptors: list[ContextSourceDescriptor] = []
        for ref in page_refs:
            record_id = str(ref["id"])
            projection = await self.record_store.read_bounded(
                record_id,
                offset=0,
                limit=projection_max_chars,
            )
            content = str(projection.get("content") or "")
            summary = str(ref.get("summary") or content[:500] or record_id)
            meta = ref.get("meta")
            descriptors.append(
                ContextSourceDescriptor(
                    descriptor_key=f"session-memory:{record_id}",
                    source_id=self.source_id,
                    source_revision=revision,
                    source_ref=record_id,
                    role="state",
                    title=summary[:200] or record_id,
                    summary=summary[:500],
                    estimated_chars=max(
                        0,
                        int(
                            ref.get("size")
                            or projection.get("total_size")
                            or len(content)
                        ),
                    ),
                    index_text=f"{summary}\n{content}",
                    content_digest=(
                        str(ref.get("sha256") or projection.get("digest") or "")
                        or None
                    ),
                    metadata={
                        "record_id": record_id,
                        "collection": ref.get("collection"),
                        "kind": ref.get("kind"),
                        "scope": dict(ref.get("scope") or {}),
                        "tags": (
                            tuple(meta.get("tags") or ())
                            if isinstance(meta, Mapping)
                            else ()
                        ),
                    },
                )
            )
        next_offset = offset + len(page_refs)
        return ContextSourceDescriptorPage(
            source_id=self.source_id,
            source_revision=revision,
            descriptors=tuple(descriptors),
            next_cursor=(str(next_offset) if next_offset < len(refs) else None),
        )

    async def async_read_exact(
        self,
        source_ref: str,
        *,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        del representation
        await self._authorize_ref(source_ref)
        segment = await self.record_store.read_bounded(
            source_ref,
            offset=range_start,
            limit=max_chars,
        )
        content = str(segment.get("content") or "")
        eof = bool(segment.get("eof"))
        size = int(segment.get("size") or len(content))
        return ContextSourceRead(
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=source_ref,
            content=content,
            completeness="complete" if eof else "truncated",
            next_range_start=(range_start + size if not eof else None),
            content_digest=str(segment.get("digest") or "") or None,
            metadata={
                "offset": segment.get("offset"),
                "size": segment.get("size"),
                "total_size": segment.get("total_size"),
                "digest": segment.get("digest"),
            },
        )


__all__ = ["AgentlyMemoryContextSource"]
