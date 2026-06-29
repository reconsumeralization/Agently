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

from .TaskShared import *


_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION = (
    "Apply source_ref_policy. A source ref with content_state='ref_only' proves only that a URL, path, "
    "download, snapshot, note, or artifact ref was discovered or materialized; it is not evidence that the "
    "source content has been read. Use it as content support only after a bounded readback/content preview is "
    "available. If the deliverable depends on unread source content, request readback with target_refs or call "
    "the available readback action; otherwise label the ref as discovered-only and do not claim facts from it. "
    "When target refs point at Workspace/repository/file evidence, prefer scoped search/readback that returns "
    "locator_ref or evidence_snippet before requesting broad content. "
)


class AgentTaskTaskBoardSourceRefsMixin(AgentTaskMixinBase):
    """TaskBoard source-ref policy, content-state tagging, and hot ref collection."""

    @classmethod
    def _taskboard_source_ref_policy(cls) -> dict[str, Any]:
        return {
            "schema_version": "agent_task_taskboard_source_refs/v1",
            "content_states": {
                "ref_only": (
                    "The URL/path/artifact was discovered or materialized, but the current input does not contain "
                    "a bounded content readback for it."
                ),
                "bounded_readback_available": (
                    "The current input contains a bounded readback or content preview for this ref. Use only the "
                    "visible preview unless a later block reads more."
                ),
            },
            "rules": [
                "Keep downloads, webpage snapshots, notes, generated code, and extracted text as cold refs unless "
                "a later block needs scoped content.",
                "Do not claim source contents from ref_only records.",
                "Use scoped retrieval query groups for Workspace/repository/file evidence before broad reads when it can reduce prompt input.",
                "Use search_surface='workspace_index' for Workspace SQLite/FTS records, 'workspace_files' for bounded file search, or 'workspace_index_and_files' when both bounded surfaces are justified; for workspace_index records, put collection names in filters.collection, do not put collection names in path, and use filters.kind only when the exact record kind is provided; never infer a generic kind such as note. For workspace_files, query is content text or an exact phrase, path is the directory/file scope, and pattern is one file glob such as *.md, * or **. Do not put list/read/search commands in query.",
                "Treat truncated evidence snippets as partial facts; downstream consumers decide whether to request wider scoped retrieval, readback, or continuation.",
                "Treat local search results as bounded facts, not as semantic acceptance.",
                "When unread source content is required, return next_board_action=readback with concrete "
                "target_refs or use an available readback action.",
                "If a ref remains unread but is still useful, label it as discovered-only in the deliverable or "
                "diagnostics.",
            ],
            "scoped_retrieval_policy": scoped_retrieval_policy(),
        }

    @classmethod
    def _taskboard_source_ref_content_state(cls, candidate: Mapping[str, Any]) -> str:
        raw_state = str(
            candidate.get("content_state")
            or candidate.get("readback_state")
            or candidate.get("materialization_state")
            or ""
        ).strip()
        if raw_state in {"bounded_readback_available", "bounded_preview_available", "content_read"}:
            return "bounded_readback_available"
        if raw_state in {"ref_only", "discovered_only", "unread"}:
            return "ref_only"

        readback_keys = (
            "content",
            "content_preview",
            "content_snippet",
            "evidence_snippet",
            "excerpt",
            "snippet",
            "text",
            "value_preview",
            "readback_preview",
            "bounded_preview",
            "file_readbacks",
            "readbacks",
            "workspace_readback",
            "artifact_readback",
        )
        for key in readback_keys:
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return "bounded_readback_available"
            if isinstance(value, Mapping) and value:
                return "bounded_readback_available"
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray) and value:
                return "bounded_readback_available"
        return "ref_only"

    @classmethod
    def _collect_taskboard_source_refs(
        cls,
        *values: Any,
        max_refs: int = _TASKBOARD_SOURCE_REFS_MAX,
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        url_keys = {
            "source_url",
            "selected_url",
            "requested_url",
            "canonical_url",
            "url",
            "href",
        }
        metadata_keys = {
            "role",
            "source",
            "record_id",
            "collection",
            "kind",
            "artifact_id",
            "action_call_id",
            "label",
            "title",
        }

        def normalize_url(raw: Any) -> str:
            text = str(raw or "").strip()
            if text.startswith("http://") or text.startswith("https://"):
                return text
            return ""

        def add(candidate: Mapping[str, Any]) -> None:
            if len(refs) >= max_refs:
                return
            record: dict[str, Any] = {}
            for key in url_keys:
                url = normalize_url(candidate.get(key))
                if url:
                    record[key] = url
            path = str(candidate.get("path") or "").strip()
            if path and len(path) <= 500:
                record["path"] = path
            for key in metadata_keys:
                if key in record or key == "path":
                    continue
                item = candidate.get(key)
                if item is None:
                    continue
                if isinstance(item, (str, int, float, bool)):
                    text = str(item).strip()
                    if text:
                        record[key] = text[:500]
            if not record:
                return
            if not any(key in record for key in url_keys) and not record.get("path"):
                return
            record["content_state"] = cls._taskboard_source_ref_content_state(candidate)
            dedupe_key = "|".join(
                str(record.get(field) or "")
                for field in (
                    "source_url",
                    "selected_url",
                    "requested_url",
                    "canonical_url",
                    "url",
                    "href",
                    "path",
                    "record_id",
                    "artifact_id",
                    "action_call_id",
                )
            )
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            refs.append(DataFormatter.sanitize(record))

        def visit(value: Any, *, depth: int = 0) -> None:
            if len(refs) >= max_refs or depth > 8:
                return
            if isinstance(value, Mapping):
                add(value)
                for item in value.values():
                    if isinstance(item, (Mapping, list, tuple)):
                        visit(item, depth=depth + 1)
                return
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    visit(item, depth=depth + 1)
                    if len(refs) >= max_refs:
                        break

        for value in values:
            visit(value)
            if len(refs) >= max_refs:
                break
        return refs


__all__ = ["AgentTaskTaskBoardSourceRefsMixin", "_TASKBOARD_SOURCE_REF_POLICY_INSTRUCTION"]
