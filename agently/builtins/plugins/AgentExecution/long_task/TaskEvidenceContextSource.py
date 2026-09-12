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

from .TaskReferences import TaskReferenceCatalog


class TaskEvidenceContextSource:
    """TaskContext retrieval port over canonical body-bearing task evidence."""

    source_kind = "task_evidence"

    def __init__(self, catalog: TaskReferenceCatalog) -> None:
        self.catalog = catalog
        task_digest = hashlib.sha256(catalog.task_id.encode("utf-8")).hexdigest()[:16]
        self.source_id = f"task-evidence:{task_digest}"

    @property
    def source_revision(self) -> str:
        return self.catalog.context_source_revision()

    def _record(self, source_ref: str) -> dict[str, Any]:
        normalized_ref = str(source_ref or "").strip()
        for record in self.catalog.context_source_records():
            if str(record.get("reference_id") or "") == normalized_ref:
                return record
        raise ValueError("Task evidence reference is unknown, stale, or has no readable body.")

    @staticmethod
    def _record_title(record: Mapping[str, Any]) -> str:
        target = record.get("target")
        target_mapping = target if isinstance(target, Mapping) else {}
        for value in (
            target_mapping.get("title"),
            target_mapping.get("label"),
            target_mapping.get("path"),
            record.get("kind"),
            record.get("reference_id"),
        ):
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return "task evidence"

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
            raise ValueError("Task evidence descriptor cursor is invalid.") from error
        if offset < 0:
            raise ValueError("Task evidence descriptor cursor cannot be negative.")
        projection_max_chars = int(profile.get("projection_max_chars") or 2000)
        if projection_max_chars <= 0:
            raise ValueError("projection_max_chars must be positive.")

        revision = self.source_revision
        records = self.catalog.context_source_records()
        page_records = records[offset : offset + page_size]
        descriptors: list[ContextSourceDescriptor] = []
        for record in page_records:
            reference_id = str(record.get("reference_id") or "")
            evidence_id = str(record.get("evidence_id") or "")
            body = str(record.get("body") or "")
            title = self._record_title(record)
            content_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            descriptors.append(
                ContextSourceDescriptor(
                    descriptor_key=f"task-evidence:{reference_id}",
                    source_id=self.source_id,
                    source_revision=revision,
                    source_ref=reference_id,
                    role="information",
                    title=title[:200],
                    summary=body[:500],
                    estimated_chars=len(body),
                    index_text=(
                        f"{reference_id}\n{evidence_id}\n{record.get('kind') or ''}\n"
                        f"{title}\n{body[:projection_max_chars]}"
                    ),
                    content_digest=content_digest,
                    metadata={
                        "reference_id": reference_id,
                        "evidence_id": evidence_id,
                        "source_role": record.get("source_role"),
                        "kind": record.get("kind"),
                        "status": record.get("status"),
                        "body_state": record.get("body_state"),
                        "target": record.get("target") or {},
                    },
                )
            )
        next_offset = offset + len(page_records)
        return ContextSourceDescriptorPage(
            source_id=self.source_id,
            source_revision=revision,
            descriptors=tuple(descriptors),
            next_cursor=(str(next_offset) if next_offset < len(records) else None),
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
        if max_chars <= 0:
            raise ValueError("max_chars must be positive.")
        if range_start < 0:
            raise ValueError("range_start cannot be negative.")
        record = self._record(source_ref)
        body = str(record.get("body") or "")
        content = body[range_start : range_start + max_chars]
        next_range_start = range_start + len(content)
        complete = next_range_start >= len(body)
        return ContextSourceRead(
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=source_ref,
            content=content,
            completeness="complete" if complete else "truncated",
            next_range_start=None if complete else next_range_start,
            content_digest=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            refs=(source_ref,),
            metadata={
                "reference_id": record.get("reference_id"),
                "evidence_id": record.get("evidence_id"),
                "source_role": record.get("source_role"),
                "kind": record.get("kind"),
                "total_chars": len(body),
                "range_start": range_start,
                "context_representation": "text",
            },
        )

    async def async_read_scoped(
        self,
        source_ref: str,
        *,
        query: str,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        del representation
        record = self._record(source_ref)
        body = str(record.get("body") or "")
        normalized_query = str(query or "").strip()
        match_start = body.casefold().find(normalized_query.casefold()) if normalized_query else -1
        start = range_start
        if match_start >= 0:
            context_before = max(0, min(max_chars // 4, match_start))
            start = match_start - context_before
        return await self.async_read_exact(
            source_ref,
            max_chars=max_chars,
            range_start=start,
        )


__all__ = ["TaskEvidenceContextSource"]
