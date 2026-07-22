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
from pathlib import Path
from typing import Any, cast

from agently.types.data import (
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
    ContextSourceRead,
    ContextRole,
)

from .RecordStore import RecordStore


_CONTEXT_ROLES = {
    "instruction",
    "information",
    "example",
    "state",
    "artifact",
    "capability",
    "index",
}
class RecordStoreContextSource:
    """Structural descriptor and exact-read port for one RecordStore view."""

    source_kind = "record_store"

    def __init__(self, record_store: RecordStore) -> None:
        self.record_store = record_store
        self.source_id = f"record-store:{record_store.record_store_id}"

    @property
    def source_revision(self) -> str:
        explicit = getattr(self.record_store, "source_revision", None)
        if explicit is not None:
            return str(explicit)
        digest = hashlib.sha256(self.record_store.record_store_id.encode("utf-8"))
        for path in self._local_state_paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(str(path.name).encode("utf-8"))
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
        return f"record-store-revision:{digest.hexdigest()}"

    def _local_state_paths(self) -> tuple[Path, ...]:
        root = Path(self.record_store.root)
        candidates = (
            root / "records.db",
            root / "records.db-wal",
            root / ".agently" / "records" / "records.db",
            root / ".agently" / "records" / "records.db-wal",
        )
        return tuple(path for path in candidates if path.exists())

    @staticmethod
    def _record_role(ref: Mapping[str, Any]) -> ContextRole:
        meta = ref.get("meta")
        role = str(meta.get("context_role") or "") if isinstance(meta, Mapping) else ""
        return cast(ContextRole, role if role in _CONTEXT_ROLES else "information")

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
            raise ValueError("RecordStore descriptor cursor is invalid.") from error
        if offset < 0:
            raise ValueError("RecordStore descriptor cursor cannot be negative.")
        projection_max_chars = int(profile.get("projection_max_chars") or 2000)
        if projection_max_chars <= 0:
            raise ValueError("projection_max_chars must be positive.")
        revision = self.source_revision
        refs = tuple(await self.record_store.search(query=None))
        page_refs = refs[offset : offset + page_size]
        descriptors: list[ContextSourceDescriptor] = []
        for ref in page_refs:
            record_id = str(ref.get("id") or "").strip()
            if not record_id:
                raise ValueError("RecordStore search returned a record without id.")
            projection = await self.record_store.read_bounded(
                record_id,
                offset=0,
                limit=projection_max_chars,
            )
            content = str(projection.get("content") or "")
            summary = str(ref.get("summary") or content[:500] or record_id)
            descriptors.append(
                ContextSourceDescriptor(
                    descriptor_key=f"record-store:{record_id}",
                    source_id=self.source_id,
                    source_revision=revision,
                    source_ref=record_id,
                    role=self._record_role(ref),
                    title=summary[:200] or record_id,
                    summary=summary[:500],
                    estimated_chars=max(
                        0,
                        int(ref.get("size") or projection.get("total_size") or len(content)),
                    ),
                    index_text=f"{summary}\n{content}",
                    content_digest=str(ref.get("sha256") or projection.get("digest") or "") or None,
                    metadata={
                        "record_id": record_id,
                        "collection": ref.get("collection"),
                        "kind": ref.get("kind"),
                        "sha256": ref.get("sha256"),
                        "scope": dict(ref.get("scope") or {}),
                        "source": dict(ref.get("source") or {}),
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


__all__ = ["RecordStoreContextSource"]
