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
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, TypeVar, cast

from agently.types.data.workspace import (
    WorkspaceRetainedReference,
    WorkspaceRetentionCategory,
    WorkspaceRetentionDiagnostic,
    WorkspaceRetentionLifecycle,
    WorkspaceRetentionPolicy,
    WorkspaceRetentionPreview,
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
    "checkpoint_row_ids",
    "link_ids",
    "retention_anchor_ids",
    "scratch_lease_ids",
    "record_scope_index_ids",
    "manifest_keys",
    "fts_record_ids",
    "vector_record_ids",
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

_T = TypeVar("_T")


@dataclass(frozen=True)
class NormalizedRetainedRoot:
    """Backend-verified identities reachable from one declared retained ref."""

    canonical_refs: tuple[WorkspaceRetainedReference, ...]
    record_ids: tuple[str, ...] = ()
    file_paths: tuple[str, ...] = ()
    content_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetentionSelectionResult:
    selected: dict[str, list[str]]
    record_content_sizes: dict[str, int]
    checkpoint_rows: tuple[Mapping[str, Any], ...]


def normalized_retained_root(
    canonical_refs: Sequence[WorkspaceRetainedReference],
    *,
    record_ids: Sequence[str] = (),
    file_paths: Sequence[str] = (),
    content_paths: Sequence[str] = (),
) -> NormalizedRetainedRoot:
    return NormalizedRetainedRoot(
        canonical_refs=tuple(canonical_refs),
        record_ids=tuple(sorted(set(record_ids))),
        file_paths=tuple(sorted(set(file_paths))),
        content_paths=tuple(sorted(set(content_paths))),
    )


def strict_retention_json(
    raw: Any,
    expected_type: type[_T],
    *,
    field: str,
    nullable: bool = False,
) -> _T | None:
    """Decode persisted retention authority without fallback defaults."""

    if raw is None:
        if nullable:
            return None
        raise ValueError(f"Persisted Workspace retention field '{field}' is null.")
    value = strict_retention_json_value(raw, field=field)
    if value is None and nullable:
        return None
    if not isinstance(value, expected_type):
        raise ValueError(
            f"Persisted Workspace retention field '{field}' must decode to "
            f"{expected_type.__name__}."
        )
    return value


def strict_retention_json_value(raw: Any, *, field: str) -> Any:
    if raw is None:
        raise ValueError(f"Persisted Workspace retention field '{field}' is null.")
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Persisted Workspace retention field '{field}' is invalid JSON: {error}"
        ) from error


def validate_retained_reference_shape(
    value: Any,
    *,
    field: str,
) -> WorkspaceRetainedReference:
    if not isinstance(value, dict):
        raise ValueError(f"Persisted Workspace retention field '{field}' must contain a reference object.")
    if "workspace_id" in value:
        if not isinstance(value.get("workspace_id"), str) or not value["workspace_id"]:
            raise ValueError(f"Persisted Workspace retention field '{field}' has no workspace identity.")
        record_id = value.get("record_id")
        content_ref = value.get("content_ref")
        if not isinstance(record_id, str) or not isinstance(content_ref, (str, type(None))):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid envelope identities.")
        if not record_id and not content_ref:
            raise ValueError(f"Persisted Workspace retention field '{field}' has no retained identity.")
        if isinstance(value.get("size"), bool) or not isinstance(value.get("size"), int):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid size.")
        if not isinstance(value.get("digest"), (str, type(None))):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid digest.")
        if not isinstance(value.get("policy_labels"), list) or not all(
            isinstance(label, str) for label in value["policy_labels"]
        ):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid policy labels.")
        if not isinstance(value.get("backend_capabilities"), dict):
            raise ValueError(
                f"Persisted Workspace retention field '{field}' has invalid backend capabilities."
            )
        return cast(WorkspaceRetainedReference, value)
    if "id" in value:
        if not isinstance(value.get("id"), str) or not value["id"]:
            raise ValueError(f"Persisted Workspace retention field '{field}' has no record identity.")
        if not isinstance(value.get("path"), (str, type(None))):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid record path.")
        if isinstance(value.get("size"), bool) or not isinstance(value.get("size"), int):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid record size.")
        if not isinstance(value.get("collection"), str) or not value["collection"]:
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid collection.")
        if not isinstance(value.get("sha256"), (str, type(None))):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid record digest.")
        if not isinstance(value.get("summary"), str) or not isinstance(value.get("created_at"), str):
            raise ValueError(f"Persisted Workspace retention field '{field}' has invalid record metadata.")
        for object_field in ("scope", "source", "meta"):
            if not isinstance(value.get(object_field), dict):
                raise ValueError(
                    f"Persisted Workspace retention field '{field}.{object_field}' must be an object."
                )
        return cast(WorkspaceRetainedReference, value)
    if not isinstance(value.get("path"), str) or not value["path"]:
        raise ValueError(f"Persisted Workspace retention field '{field}' has no file path.")
    if isinstance(value.get("bytes"), bool) or not isinstance(value.get("bytes"), int):
        raise ValueError(f"Persisted Workspace retention field '{field}' has invalid file size.")
    if not isinstance(value.get("sha256"), str) or not value["sha256"]:
        raise ValueError(f"Persisted Workspace retention field '{field}' has invalid file digest.")
    return cast(WorkspaceRetainedReference, value)


def stable_checkpoint_row_identities(
    rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Build rowid-independent logical identities with duplicate ordinals."""

    duplicate_counts: dict[str, int] = {}
    identities: list[str] = []
    for row in rows:
        payload = {
            "run_id": str(row.get("run_id") or ""),
            "step_id": row.get("step_id"),
            "record_id": str(row.get("record_id") or ""),
            "state": row.get("state"),
            "created_at": str(row.get("created_at") or ""),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        ordinal = duplicate_counts.get(digest, 0)
        duplicate_counts[digest] = ordinal + 1
        identities.append(f"checkpoint:{digest}:{ordinal}")
    return identities


def retention_lifecycle_diagnostics(
    *,
    scope: Mapping[str, Any],
    lifecycle: WorkspaceRetentionLifecycle,
    execution_id: str,
    lease_manifest_key: str,
    persisted_lease: Mapping[str, Any] | None,
    checkpoint_version: int | None,
    runtime_events: Sequence[Mapping[str, Any]],
    now: float,
) -> list[WorkspaceRetentionDiagnostic]:
    diagnostics: list[WorkspaceRetentionDiagnostic] = []
    if not execution_id or str(scope.get("execution_id") or "") != execution_id:
        diagnostics.append(
            retention_diagnostic(
                "workspace.retention.lifecycle_scope_mismatch",
                "Retention lifecycle execution_id does not match the cleanup scope.",
                entity=execution_id or "execution_id",
            )
        )
    if lifecycle.get("status") not in {"completed", "failed", "cancelled"}:
        diagnostics.append(
            retention_diagnostic(
                "workspace.retention.lifecycle_not_terminal",
                "Workspace retention requires a terminal lifecycle status.",
                entity=execution_id,
            )
        )
    if lifecycle.get("recovery_active"):
        diagnostics.append(
            retention_diagnostic(
                "workspace.retention.recovery_active",
                "Workspace recovery remains active for the cleanup scope.",
                entity=execution_id,
            )
        )
    if lifecycle.get("lease_active"):
        diagnostics.append(
            retention_diagnostic(
                "workspace.retention.lease_active",
                "Workspace execution lease remains active for the cleanup scope.",
                entity=execution_id,
            )
        )
    if (
        persisted_lease is not None
        and persisted_lease.get("released_at") is None
        and float(persisted_lease.get("lease_until") or 0) > now
    ):
        diagnostics.append(
            retention_diagnostic(
                "workspace.retention.lease_active",
                "Persisted Workspace execution lease remains active.",
                entity=lease_manifest_key,
            )
        )

    runtime_version = next(
        (
            event.get("state_version")
            for event in reversed(runtime_events)
            if event.get("state_version") is not None
        ),
        None,
    )
    authoritative_versions = {
        int(value) for value in (checkpoint_version, runtime_version) if value is not None
    }
    if len(authoritative_versions) > 1:
        diagnostics.append(
            retention_diagnostic(
                "workspace.retention.state_version_ambiguous",
                "Persisted checkpoint and RuntimeEvent state versions disagree.",
                entity=execution_id,
                detail={"versions": sorted(authoritative_versions)},
            )
        )
    elif authoritative_versions:
        authoritative_version = next(iter(authoritative_versions))
        if lifecycle.get("state_version") != authoritative_version:
            diagnostics.append(
                retention_diagnostic(
                    "workspace.retention.state_version_mismatch",
                    "Supplied lifecycle state version is stale against persisted Workspace facts.",
                    entity=execution_id,
                    detail={
                        "supplied": lifecycle.get("state_version"),
                        "persisted": authoritative_version,
                    },
                )
            )

    persisted_lifecycle: tuple[str, str | None] | None = None
    for event in reversed(runtime_events):
        event_type = str(event.get("event_type") or "").lower()
        if event_type.endswith(("execution_completed", "execution.completed")):
            persisted_lifecycle = ("terminal", "completed")
        elif event_type.endswith(("execution_failed", "execution.failed")):
            persisted_lifecycle = ("terminal", "failed")
        elif event_type.endswith(
            ("execution_cancelled", "execution.cancelled", "execution_canceled", "execution.canceled")
        ):
            persisted_lifecycle = ("terminal", "cancelled")
        elif event_type.endswith(("execution_closed", "execution.closed")):
            persisted_lifecycle = ("terminal", None)
        elif event_type.endswith(
            (
                "execution_started",
                "execution.started",
                "execution_resumed",
                "execution.resumed",
                "execution_sealed",
                "execution.sealed",
                "execution_unsealed",
                "execution.unsealed",
                "execution_waiting",
                "execution.waiting",
                "execution_paused",
                "execution.paused",
            )
        ):
            persisted_lifecycle = ("active", None)
        if persisted_lifecycle is not None:
            break
    if persisted_lifecycle is not None:
        persisted_state, persisted_status = persisted_lifecycle
        if persisted_state == "active":
            diagnostics.append(
                retention_diagnostic(
                    "workspace.retention.recovery_active",
                    "Persisted Workspace lifecycle facts remain nonterminal.",
                    entity=execution_id,
                )
            )
        elif persisted_status is not None and lifecycle.get("status") != persisted_status:
            diagnostics.append(
                retention_diagnostic(
                    "workspace.retention.lifecycle_state_mismatch",
                    "Supplied lifecycle status disagrees with persisted Workspace facts.",
                    entity=execution_id,
                )
            )
    return diagnostics


def build_retention_selection(
    *,
    owned_record_ids: set[str],
    records_by_id: Mapping[str, Mapping[str, Any]],
    retained_record_ids: set[str],
    retained_content_paths: set[str],
    checkpoint_record_ids: set[str],
    checkpoint_rows: Sequence[Mapping[str, Any]],
    runtime_events: Sequence[Mapping[str, Any]],
    retained_event_ids: set[str],
    links: Sequence[Mapping[str, Any]],
    anchors: Sequence[Mapping[str, Any]],
    scratch_leases: Sequence[Mapping[str, Any]],
    selected_scratch_paths: Sequence[str],
    selected_file_paths: Sequence[str],
    retained_file_paths: set[str],
    scope_index_rows: Sequence[Mapping[str, Any]],
    fts_rows: Sequence[Mapping[str, Any]],
    manifest_keys: set[str],
    checkpoint_manifest_key: str,
    lease_manifest_key: str,
    representation_by_category: Mapping[str, str],
) -> RetentionSelectionResult:
    selected = empty_retention_selection()
    record_content_sizes: dict[str, int] = {}
    for record_id in sorted(owned_record_ids):
        ref = records_by_id[record_id]
        if record_id in retained_record_ids:
            continue
        if record_id in checkpoint_record_ids or ref.get("collection") == "checkpoints":
            category = "checkpoints"
        elif ref.get("collection") == "artifacts":
            category = "artifacts"
        else:
            category = "records"
        if representation_by_category.get(category) == "hot":
            continue
        selected["record_ids"].append(record_id)
        record_content_sizes[record_id] = int(ref.get("size") or 0)
        content_path = str(ref.get("path") or "")
        if content_path and content_path not in retained_content_paths:
            selected["content_paths"].append(content_path)

    selected_record_ids = set(selected["record_ids"])
    selected_checkpoint_rows = tuple(
        row for row in checkpoint_rows if str(row.get("record_id") or "") in selected_record_ids
    )
    selected["checkpoint_ids"] = sorted(
        {str(row.get("record_id") or "") for row in selected_checkpoint_rows}
    )
    selected["checkpoint_row_ids"] = sorted(
        stable_checkpoint_row_identities(selected_checkpoint_rows)
    )
    selected["link_ids"] = sorted(
        str(link.get("id") or "")
        for link in links
        if str(link.get("source_id") or "") in selected_record_ids
        or str(link.get("target_id") or "") in selected_record_ids
    )
    if representation_by_category.get("runtime_events") != "hot":
        selected["runtime_event_ids"] = sorted(
            str(event.get("id") or "")
            for event in runtime_events
            if str(event.get("event_id") or "") not in retained_event_ids
        )
    selected["retention_anchor_ids"] = sorted(str(anchor.get("id") or "") for anchor in anchors)
    if representation_by_category.get("scratch") != "hot":
        selected["scratch_lease_ids"] = sorted(
            str(lease.get("lease_id") or "")
            for lease in scratch_leases
            if lease.get("closed_at") is not None
        )
        selected["scratch_paths"] = list(selected_scratch_paths)
    if representation_by_category.get("files") != "hot":
        selected["file_paths"] = [
            path for path in selected_file_paths if path not in retained_file_paths
        ]
    selected["record_scope_index_ids"] = sorted(
        f"{row.get('record_id')}:{row.get('scope_key')}"
        for row in scope_index_rows
        if str(row.get("record_id") or "") in selected_record_ids
    )
    selected["fts_record_ids"] = sorted(
        str(row.get("record_id") or "")
        for row in fts_rows
        if str(row.get("record_id") or "") in selected_record_ids
    )
    if (
        representation_by_category.get("checkpoints") != "hot"
        and checkpoint_manifest_key in manifest_keys
    ):
        selected["manifest_keys"].append(checkpoint_manifest_key)
    if lease_manifest_key in manifest_keys:
        selected["manifest_keys"].append(lease_manifest_key)
    for key in selected:
        selected[key] = sorted(set(selected[key]))
    return RetentionSelectionResult(selected, record_content_sizes, selected_checkpoint_rows)


def calculate_retention_logical_bytes(
    *,
    selected: Mapping[str, Sequence[str]],
    record_content_sizes: Mapping[str, int],
    record_rows: Sequence[Mapping[str, Any]],
    runtime_events: Sequence[Mapping[str, Any]],
    links: Sequence[Mapping[str, Any]],
    anchors: Sequence[Mapping[str, Any]],
    checkpoint_rows: Sequence[Mapping[str, Any]],
    scope_index_rows: Sequence[Mapping[str, Any]],
    scratch_rows: Sequence[Mapping[str, Any]],
    manifest_raw: Mapping[str, str],
    fts_rows: Sequence[Mapping[str, Any]],
    vector_rows: Sequence[Mapping[str, Any]],
    sqlite_vector_store: bool,
    selected_path_sizes: Sequence[int],
) -> int:
    selected_record_ids = set(selected["record_ids"])
    logical_bytes = sum(record_content_sizes.values())
    logical_bytes += sum(
        serialized_size(row)
        for row in record_rows
        if str(row.get("id") or "") in selected_record_ids
    )
    selected_event_ids = set(selected["runtime_event_ids"])
    logical_bytes += sum(
        serialized_size(event)
        for event in runtime_events
        if str(event.get("id") or "") in selected_event_ids
    )
    selected_link_ids = set(selected["link_ids"])
    logical_bytes += sum(
        serialized_size(link)
        for link in links
        if str(link.get("id") or "") in selected_link_ids
    )
    selected_anchor_ids = set(selected["retention_anchor_ids"])
    logical_bytes += sum(
        serialized_size(anchor)
        for anchor in anchors
        if str(anchor.get("id") or "") in selected_anchor_ids
    )
    logical_bytes += sum(serialized_size(row) for row in checkpoint_rows)
    selected_scope_ids = set(selected["record_scope_index_ids"])
    logical_bytes += sum(
        serialized_size(row)
        for row in scope_index_rows
        if f"{row.get('record_id')}:{row.get('scope_key')}" in selected_scope_ids
    )
    selected_scratch_ids = set(selected["scratch_lease_ids"])
    logical_bytes += sum(
        serialized_size(row)
        for row in scratch_rows
        if str(row.get("lease_id") or "") in selected_scratch_ids
    )
    logical_bytes += sum(
        len(key.encode("utf-8")) + len(manifest_raw[key].encode("utf-8"))
        for key in set(selected["manifest_keys"])
    )
    selected_fts_ids = set(selected["fts_record_ids"])
    logical_bytes += sum(
        len(str(row.get("summary") or "").encode("utf-8"))
        + len(str(row.get("content") or "").encode("utf-8"))
        for row in fts_rows
        if str(row.get("record_id") or "") in selected_fts_ids
    )
    selected_vector_ids = set(selected["vector_record_ids"])
    if sqlite_vector_store:
        logical_bytes += sum(
            len(str(row.get("ref_json") or "").encode("utf-8"))
            + len(str(row.get("embedding_json") or "").encode("utf-8"))
            for row in vector_rows
            if str(row.get("record_id") or "") in selected_vector_ids
        )
    else:
        logical_bytes += sum(
            serialized_size({"record_id": record_id}) for record_id in selected_vector_ids
        )
    return logical_bytes + sum(selected_path_sizes)


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


def retention_selection_nonempty(selected: Mapping[str, Sequence[str]]) -> bool:
    return any(bool(values) for values in selected.values())


def read_only_retention_components(components: Mapping[str, bool]) -> list[str]:
    return sorted(name for name, read_only in components.items() if read_only)


def retention_diagnostic(
    code: str,
    message: str,
    *,
    entity: str,
    detail: dict[str, Any] | None = None,
) -> WorkspaceRetentionDiagnostic:
    diagnostic: WorkspaceRetentionDiagnostic = {
        "code": code,
        "message": message,
        "retryable": True,
        "entity": entity,
    }
    if detail:
        diagnostic["detail"] = detail
    return diagnostic


def deduplicate_retained_refs(
    retained_refs: Sequence[WorkspaceRetainedReference],
) -> list[WorkspaceRetainedReference]:
    canonical = {
        json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str): ref
        for ref in retained_refs
    }
    return [canonical[key] for key in sorted(canonical)]


def build_retention_preview(
    *,
    status: str,
    scope: dict[str, Any],
    lifecycle: WorkspaceRetentionLifecycle,
    policy: WorkspaceRetentionPolicy,
    retained_refs: list[WorkspaceRetainedReference],
    inline_result: Any,
    diagnostics: list[WorkspaceRetentionDiagnostic] | None = None,
    selected: dict[str, list[str]] | None = None,
    logical_bytes: int = 0,
) -> WorkspaceRetentionPreview:
    resolved_selected = selected if status == "ready" and selected is not None else empty_retention_selection()
    for key in resolved_selected:
        resolved_selected[key] = sorted(set(resolved_selected[key]))
    resolved_diagnostics = sorted(
        diagnostics or [],
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("entity") or ""),
            str(item.get("message") or ""),
        ),
    )
    plan_fingerprint = canonical_retention_fingerprint(
        scope,
        lifecycle,
        policy,
        retained_refs,
        resolved_selected,
    )
    return cast(
        WorkspaceRetentionPreview,
        {
            "status": status,
            "plan_fingerprint": plan_fingerprint,
            "scope": scope,
            "lifecycle": lifecycle,
            "policy": policy,
            "retained_refs": retained_refs,
            "inline_result": inline_result,
            "selected": resolved_selected,
            "accounting": {
                "entities": {
                    key: len(values) for key, values in sorted(resolved_selected.items())
                },
                "logical_bytes_deleted": logical_bytes if status == "ready" else 0,
                "physical_bytes_reclaimed": 0,
                "physical_bytes_pending": 0,
            },
            "diagnostics": resolved_diagnostics,
        },
    )


def validate_retention_preview(
    value: Any,
    *,
    scope: Mapping[str, Any],
    lifecycle: WorkspaceRetentionLifecycle,
    policy: WorkspaceRetentionPolicy,
    declared_retained_refs: Sequence[WorkspaceRetainedReference],
    inline_result: Any,
) -> WorkspaceRetentionPreview:
    """Validate untrusted provider preview data against original request facts."""

    if not isinstance(value, dict):
        raise ValueError("Workspace retention provider preview must be an object.")
    required_keys = {
        "status",
        "plan_fingerprint",
        "scope",
        "lifecycle",
        "policy",
        "retained_refs",
        "inline_result",
        "selected",
        "accounting",
        "diagnostics",
    }
    missing = sorted(required_keys - set(value))
    if missing:
        raise ValueError(
            f"Workspace retention provider preview is missing fields: {', '.join(missing)}."
        )
    status = value["status"]
    if status not in {"ready", "deferred"}:
        raise ValueError("Workspace retention provider preview has an invalid status.")
    if not isinstance(value["scope"], dict) or value["scope"] != dict(scope):
        raise ValueError("Workspace retention provider preview scope does not match the request.")
    if not isinstance(value["lifecycle"], dict) or value["lifecycle"] != dict(lifecycle):
        raise ValueError("Workspace retention provider preview lifecycle does not match the request.")
    if not isinstance(value["policy"], dict) or value["policy"] != dict(policy):
        raise ValueError("Workspace retention provider preview policy does not match the request.")
    if value["inline_result"] != inline_result:
        raise ValueError("Workspace retention provider preview inline result does not match the request.")

    retained_values = value["retained_refs"]
    if not isinstance(retained_values, list):
        raise ValueError("Workspace retention provider preview retained refs must be a list.")
    canonical_refs = [
        validate_retained_reference_shape(ref, field=f"provider_preview.retained_refs[{index}]")
        for index, ref in enumerate(retained_values)
    ]
    declared_refs = [
        validate_retained_reference_shape(ref, field=f"request.retained_refs[{index}]")
        for index, ref in enumerate(declared_retained_refs)
    ]
    for declared_ref in declared_refs:
        if not any(
            _retained_ref_equivalent(declared_ref, canonical_ref)
            for canonical_ref in canonical_refs
        ):
            raise ValueError(
                "Workspace retention provider preview dropped a declared retained root."
            )

    selected_value = value["selected"]
    if not isinstance(selected_value, dict) or set(selected_value) != set(RETENTION_SELECTION_KEYS):
        raise ValueError("Workspace retention provider preview selection has invalid carrier keys.")
    selected: dict[str, list[str]] = {}
    for key in RETENTION_SELECTION_KEYS:
        carrier_values = selected_value[key]
        if not isinstance(carrier_values, list) or not all(
            isinstance(item, str) and item for item in carrier_values
        ):
            raise ValueError(
                f"Workspace retention provider preview selection '{key}' must contain strings."
            )
        if carrier_values != sorted(set(carrier_values)):
            raise ValueError(
                f"Workspace retention provider preview selection '{key}' is not canonical."
            )
        selected[key] = list(carrier_values)

    retained_facts = [_retained_ref_facts(ref) for ref in [*declared_refs, *canonical_refs]]
    retained_record_ids = {
        str(facts[0]) for facts in retained_facts if facts[0]
    }
    retained_content_paths = {
        str(facts[1]) for facts in retained_facts if facts[1]
    }
    retained_file_paths = {
        str(facts[2]) for facts in retained_facts if facts[2]
    }
    selected_record_ids = set().union(
        selected["record_ids"],
        selected["checkpoint_ids"],
        selected["fts_record_ids"],
        selected["vector_record_ids"],
    )
    retained_scope_selected = any(
        scope_identity.startswith(f"{record_id}:")
        for record_id in retained_record_ids
        for scope_identity in selected["record_scope_index_ids"]
    )
    if retained_record_ids & selected_record_ids or retained_scope_selected:
        raise ValueError(
            "Workspace retention provider preview selects a retained record identity."
        )
    if retained_content_paths & set(selected["content_paths"]):
        raise ValueError(
            "Workspace retention provider preview selects a retained managed-content path."
        )
    if retained_file_paths & set(selected["file_paths"]):
        raise ValueError(
            "Workspace retention provider preview selects a retained editable-file path."
        )

    accounting = value["accounting"]
    accounting_keys = {
        "entities",
        "logical_bytes_deleted",
        "physical_bytes_reclaimed",
        "physical_bytes_pending",
    }
    if not isinstance(accounting, dict) or set(accounting) != accounting_keys:
        raise ValueError("Workspace retention provider preview accounting has invalid fields.")
    entities = accounting["entities"]
    if not isinstance(entities, dict) or set(entities) != set(RETENTION_SELECTION_KEYS):
        raise ValueError("Workspace retention provider preview accounting entities are incomplete.")
    if any(
        isinstance(entities[key], bool)
        or not isinstance(entities[key], int)
        or entities[key] != len(selected[key])
        for key in RETENTION_SELECTION_KEYS
    ):
        raise ValueError("Workspace retention provider preview accounting counts do not match selection.")
    for byte_field in (
        "logical_bytes_deleted",
        "physical_bytes_reclaimed",
        "physical_bytes_pending",
    ):
        byte_value = accounting[byte_field]
        if isinstance(byte_value, bool) or not isinstance(byte_value, int) or byte_value < 0:
            raise ValueError(
                f"Workspace retention provider preview accounting '{byte_field}' is invalid."
            )

    diagnostics = value["diagnostics"]
    if not isinstance(diagnostics, list):
        raise ValueError("Workspace retention provider preview diagnostics must be a list.")
    for diagnostic in diagnostics:
        if (
            not isinstance(diagnostic, dict)
            or not isinstance(diagnostic.get("code"), str)
            or not isinstance(diagnostic.get("message"), str)
            or not isinstance(diagnostic.get("retryable"), bool)
            or not isinstance(diagnostic.get("entity"), str)
            or ("detail" in diagnostic and not isinstance(diagnostic["detail"], dict))
        ):
            raise ValueError("Workspace retention provider preview contains an invalid diagnostic.")
    if status == "deferred" and (
        retention_selection_nonempty(selected) or accounting["logical_bytes_deleted"] != 0
    ):
        raise ValueError("Workspace deferred retention preview must have an empty zero-byte selection.")

    fingerprint = value["plan_fingerprint"]
    expected_fingerprint = canonical_retention_fingerprint(
        dict(scope),
        lifecycle,
        policy,
        canonical_refs,
        selected,
    )
    if not isinstance(fingerprint, str) or fingerprint != expected_fingerprint:
        raise ValueError("Workspace retention provider preview fingerprint is invalid.")
    return cast(WorkspaceRetentionPreview, value)


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


def _retained_ref_facts(
    ref: WorkspaceRetainedReference,
) -> tuple[str | None, str | None, str | None, str | None, int]:
    if "workspace_id" in ref:
        return (
            str(ref.get("record_id") or "") or None,
            str(ref.get("content_ref") or "") or None,
            None,
            cast(str | None, ref.get("digest")),
            int(ref.get("size") or 0),
        )
    if "id" in ref:
        return (
            str(ref.get("id") or "") or None,
            str(ref.get("path") or "") or None,
            None,
            cast(str | None, ref.get("sha256")),
            int(ref.get("size") or 0),
        )
    return (
        None,
        None,
        str(ref.get("path") or "") or None,
        str(ref.get("sha256") or "") or None,
        int(ref.get("bytes") or 0),
    )


def _retained_ref_equivalent(
    declared: WorkspaceRetainedReference,
    canonical: WorkspaceRetainedReference,
) -> bool:
    declared_record, declared_content, declared_file, declared_digest, declared_size = (
        _retained_ref_facts(declared)
    )
    canonical_record, canonical_content, canonical_file, canonical_digest, canonical_size = (
        _retained_ref_facts(canonical)
    )
    if declared_digest != canonical_digest or declared_size != canonical_size:
        return False
    if "workspace_id" in declared:
        if "workspace_id" not in canonical:
            return False
        if declared.get("workspace_id") != canonical.get("workspace_id"):
            return False
        if declared_record is not None:
            return (
                canonical_record == declared_record
                and (declared_content is None or canonical_content == declared_content)
            )
        return declared_content is not None and canonical_content == declared_content
    if "id" in declared:
        if "workspace_id" not in canonical and "id" not in canonical:
            return False
        return (
            canonical_record == declared_record
            and (declared_content is None or canonical_content == declared_content)
        )
    if "workspace_id" in canonical or "id" in canonical:
        return False
    return declared_file is not None and canonical_file == declared_file


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
