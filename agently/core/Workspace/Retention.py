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
import json
from typing import Any, cast

from agently.types.data.workspace import (
    WorkspaceRetainedReference,
    WorkspaceRetentionCategory,
    WorkspaceRetentionLifecycle,
    WorkspaceRetentionPolicy,
    WorkspaceRetentionRepresentation,
    WorkspaceRetentionRule,
)


RETENTION_CATEGORIES: tuple[WorkspaceRetentionCategory, ...] = (
    "terminal_result",
    "artifacts",
    "runtime_events",
    "checkpoints",
    "records",
    "files",
    "scratch",
)
RETENTION_REPRESENTATIONS: frozenset[str] = frozenset({"discard", "summary", "hot", "cold"})
RETENTION_SELECTION_KEYS: tuple[str, ...] = (
    "record_ids",
    "runtime_event_ids",
    "checkpoint_ids",
    "link_ids",
    "retention_anchor_ids",
    "scratch_lease_ids",
    "content_paths",
    "file_paths",
    "scratch_paths",
)

_DEFAULT_REPRESENTATIONS: dict[WorkspaceRetentionCategory, WorkspaceRetentionRepresentation] = {
    "terminal_result": "summary",
    "artifacts": "summary",
    "runtime_events": "discard",
    "checkpoints": "discard",
    "records": "discard",
    "files": "discard",
    "scratch": "discard",
}


def serialized_size(value: Any) -> int:
    """Return the UTF-8 size of the stable JSON carrier used by retention."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def resolve_retention_policy(
    policy: WorkspaceRetentionPolicy | None,
    *,
    supports_cold: bool = False,
) -> WorkspaceRetentionPolicy:
    """Resolve a partial policy onto the terminal-minimal default."""

    source = dict(policy or {})
    raw_limit = source.get("inline_result_limit", 4096)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 0:
        raise ValueError("Workspace retention inline_result_limit must be a non-negative integer.")

    resolved = dict(_DEFAULT_REPRESENTATIONS)
    seen: set[str] = set()
    raw_rules = source.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError("Workspace retention policy rules must be a list.")
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise ValueError("Each Workspace retention policy rule must be an object.")
        category = str(raw_rule.get("category") or "")
        representation = str(raw_rule.get("representation") or "")
        if category not in RETENTION_CATEGORIES:
            raise ValueError(f"Unsupported Workspace retention category: { category or '<empty>' }.")
        if category in seen:
            raise ValueError(f"Workspace retention policy contains duplicate category rule: { category }.")
        if representation not in RETENTION_REPRESENTATIONS:
            raise ValueError(
                f"Unsupported Workspace retention representation for { category }: " f"{ representation or '<empty>' }."
            )
        if representation == "cold" and not supports_cold:
            raise ValueError(f"Workspace retention cold representation is unsupported for category: { category }.")
        seen.add(category)
        resolved[cast(WorkspaceRetentionCategory, category)] = cast(WorkspaceRetentionRepresentation, representation)

    rules: list[WorkspaceRetentionRule] = [
        {"category": category, "representation": resolved[category]} for category in RETENTION_CATEGORIES
    ]
    return {"rules": rules, "inline_result_limit": raw_limit}


def empty_retention_selection() -> dict[str, list[str]]:
    return {key: [] for key in RETENTION_SELECTION_KEYS}


def _retained_ref_identity(ref: WorkspaceRetainedReference) -> dict[str, Any]:
    if "workspace_id" in ref:
        return {
            "type": "envelope",
            "workspace_id": str(ref.get("workspace_id") or ""),
            "record_id": str(ref.get("record_id") or ""),
            "content_ref": ref.get("content_ref"),
            "digest": ref.get("digest"),
            "size": int(ref.get("size") or 0),
        }
    if "id" in ref:
        return {
            "type": "record",
            "record_id": str(ref.get("id") or ""),
            "path": ref.get("path"),
            "digest": ref.get("sha256"),
            "size": int(ref.get("size") or 0),
        }
    return {
        "type": "file",
        "path": str(ref.get("path") or ""),
        "digest": str(ref.get("sha256") or ""),
        "size": int(ref.get("bytes") or 0),
    }


def canonical_retention_fingerprint(
    scope: dict[str, Any],
    lifecycle: WorkspaceRetentionLifecycle,
    policy: WorkspaceRetentionPolicy,
    retained_refs: list[WorkspaceRetainedReference],
    selected: dict[str, list[str]],
) -> str:
    """Fingerprint only mutation-relevant facts, never estimates or prose."""

    normalized_refs = [_retained_ref_identity(ref) for ref in retained_refs]
    normalized_refs.sort(
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )
    normalized_rules = sorted(
        (
            {
                "category": str(rule.get("category") or ""),
                "representation": str(rule.get("representation") or ""),
            }
            for rule in policy.get("rules", [])
        ),
        key=lambda item: (item["category"], item["representation"]),
    )
    payload = {
        "scope": dict(scope),
        "lifecycle_state_version": lifecycle.get("state_version"),
        "rules": normalized_rules,
        "retained_refs": normalized_refs,
        "selected": {str(key): sorted(str(value) for value in values) for key, values in sorted(selected.items())},
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
