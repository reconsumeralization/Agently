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
from pathlib import Path
from typing import Any, cast

from agently.types.data import (
    ContextBlock,
    ContextCandidate,
    ContextReadIntent,
    RecordRetrievalMethod,
    RecordRetrievalSelection,
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
_ADAPTER_FILTERS = {
    "context_binding_scope",
    "path",
    "pattern",
    "source_kinds",
    "include_hidden",
    "max_file_bytes",
    "context_lines",
    "tags",
    "method",
    "selection",
    "top_n",
    "rerank",
    "max_candidates",
}


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


class RecordStoreContextSource:
    """Structural candidate and exact-read adapter for one RecordStore view."""

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
    def _record_role(ref: Mapping[str, Any]) -> str:
        meta = ref.get("meta")
        role = str(meta.get("context_role") or "") if isinstance(meta, Mapping) else ""
        return role if role in _CONTEXT_ROLES else "information"

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ContextCandidate]:
        resolved_filters = {
            str(key): value
            for key, value in dict(filters or intent.filters).items()
            if str(key) not in _ADAPTER_FILTERS
        }
        raw_filters = filters or intent.filters
        if not _source_kind_enabled(raw_filters, "record_store"):
            return ()
        tags_value = raw_filters.get("tags")
        tags = (
            tuple(str(item) for item in tags_value if str(item).strip())
            if isinstance(tags_value, Sequence) and not isinstance(tags_value, (str, bytes, bytearray))
            else None
        )
        selection = str(raw_filters.get("selection") or "top_n")
        if selection not in {"length", "top_n"}:
            selection = "top_n"
        method = str(raw_filters.get("method") or "auto")
        if method not in {"auto", "keyword", "vector", "hybrid"}:
            method = "auto"
        package = await self.record_store.retrieve(
            intent.query,
            tags=tags,
            filters=resolved_filters,
            selection=cast(RecordRetrievalSelection, selection),
            top_n=max(0, int(limit)),
            method=cast(RecordRetrievalMethod, method),
            rerank=bool(raw_filters.get("rerank", False)),
            max_candidates=raw_filters.get("max_candidates"),
            budget={"max_items": max(0, int(limit)), "max_chars": max(4000, int(limit) * 2000)},
        )
        candidates: list[ContextCandidate] = []
        seen_content: set[tuple[str, str, str, str]] = set()
        for item in package.get("items", ()):
            ref = item.get("ref")
            if item.get("source") != "record" or not isinstance(ref, Mapping):
                continue
            record_id = str(ref.get("id") or "").strip()
            if not record_id:
                continue
            content_identity = (
                str(ref.get("sha256") or record_id),
                str(ref.get("collection") or ""),
                str(ref.get("kind") or ""),
                repr(sorted(dict(ref.get("source") or {}).items())),
            )
            if content_identity in seen_content:
                continue
            seen_content.add(content_identity)
            summary = str(item.get("summary") or ref.get("summary") or record_id)
            candidates.append(
                ContextCandidate(
                    block_key=f"record-store-source:{record_id}",
                    source_id=self.source_id,
                    source_revision=self.source_revision,
                    source_ref=record_id,
                    binding_id=self.source_id,
                    role=self._record_role(ref),  # type: ignore[arg-type]
                    summary=summary[:500],
                    estimated_chars=max(0, int(ref.get("size") or item.get("raw_chars") or 0)),
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
            if len(candidates) >= max(0, int(limit)):
                break
        return tuple(candidates)

    async def async_read(
        self,
        candidate: ContextCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
    ) -> ContextBlock:
        del representation
        if candidate.source_id != self.source_id:
            raise ValueError("RecordStore candidate belongs to a different source.")
        segment = await self.record_store.read_bounded(
            candidate.source_ref,
            offset=0,
            limit=max_chars,
        )
        content = str(segment.get("content") or "")
        return ContextBlock(
            block_id=f"record_store_block:{candidate.source_ref}:{segment.get('digest')}",
            block_key=candidate.block_key,
            source_id=self.source_id,
            source_revision=candidate.source_revision,
            source_ref=candidate.source_ref,
            binding_id=candidate.binding_id,
            role=candidate.role,
            content=content,
            completeness="complete" if bool(segment.get("eof")) else "truncated",
            content_chars=len(content),
            required=candidate.required,
            refs=(candidate.source_ref,),
            metadata={
                **dict(candidate.metadata),
                "offset": segment.get("offset"),
                "size": segment.get("size"),
                "total_size": segment.get("total_size"),
                "digest": segment.get("digest"),
            },
        )


__all__ = ["RecordStoreContextSource"]
