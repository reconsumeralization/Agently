from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from agently.core import LazyWorkspace, WorkspaceManager
from agently.core.Workspace.Retention import (
    canonical_retention_fingerprint,
    resolve_retention_policy,
    serialized_size,
)
from agently.types.data import (
    WorkspaceRetentionLifecycle,
    WorkspaceRetentionPolicy,
    WorkspaceRetentionPreview,
)


def _terminal_lifecycle(
    execution_id: str = "exec-1",
    *,
    recovery_active: bool = False,
    lease_active: bool = False,
) -> WorkspaceRetentionLifecycle:
    return {
        "execution_id": execution_id,
        "status": "completed",
        "terminal_at": "2026-07-11T00:00:00+00:00",
        "state_version": 3,
        "recovery_active": recovery_active,
        "lease_active": lease_active,
    }


def _assert_nothing_selected(preview: WorkspaceRetentionPreview) -> None:
    assert preview["status"] == "deferred"
    assert preview["selected"]
    assert all(values == [] for values in preview["selected"].values())


def _storage_snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_inspect_retention_default_policy_and_helpers_are_canonical():
    policy = resolve_retention_policy(None)
    assert policy == {
        "rules": [
            {"category": "terminal_result", "representation": "summary"},
            {"category": "artifacts", "representation": "summary"},
            {"category": "runtime_events", "representation": "discard"},
            {"category": "checkpoints", "representation": "discard"},
            {"category": "records", "representation": "discard"},
            {"category": "files", "representation": "discard"},
            {"category": "scratch", "representation": "discard"},
        ],
        "inline_result_limit": 4096,
    }
    assert serialized_size({"summary": "done"}) == len(b'{"summary":"done"}')

    with pytest.raises(ValueError, match="duplicate"):
        resolve_retention_policy(
            cast(
                WorkspaceRetentionPolicy,
                {
                    "rules": [
                        {"category": "records", "representation": "discard"},
                        {"category": "records", "representation": "hot"},
                    ]
                },
            )
        )
    with pytest.raises(ValueError, match="cold"):
        resolve_retention_policy(
            cast(
                WorkspaceRetentionPolicy,
                {"rules": [{"category": "runtime_events", "representation": "cold"}]},
            )
        )

    first = canonical_retention_fingerprint(
        {"execution_id": "exec-1", "project_id": "project-1"},
        _terminal_lifecycle(),
        policy,
        [
            {
                "path": "b.txt",
                "bytes": 1,
                "sha256": "b",
                "media_type": None,
                "content_kind": "text",
                "role": "artifact",
            },
            {
                "path": "a.txt",
                "bytes": 1,
                "sha256": "a",
                "media_type": None,
                "content_kind": "text",
                "role": "artifact",
            },
        ],
        {"record_ids": ["rec-b", "rec-a"], "runtime_event_ids": ["evt-b", "evt-a"]},
    )
    second = canonical_retention_fingerprint(
        {"project_id": "project-1", "execution_id": "exec-1"},
        _terminal_lifecycle(),
        policy,
        [
            {
                "path": "a.txt",
                "bytes": 1,
                "sha256": "a",
                "media_type": None,
                "content_kind": "text",
                "role": "artifact",
            },
            {
                "path": "b.txt",
                "bytes": 1,
                "sha256": "b",
                "media_type": None,
                "content_kind": "text",
                "role": "artifact",
            },
        ],
        {"runtime_event_ids": ["evt-a", "evt-b"], "record_ids": ["rec-a", "rec-b"]},
    )
    assert first == second
    assert len(first) == 64


@pytest.mark.asyncio
async def test_inspect_retention_preserves_declared_artifact_and_selects_runtime_events(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-ready")
    artifact_ref = await workspace.put(
        {"report": "ready"},
        collection="artifacts",
        kind="report",
        scope={"execution_id": "exec-1"},
    )
    await workspace.put(
        {"process": "discard"},
        collection="observations",
        kind="process",
        scope={"execution_id": "exec-1"},
    )
    event = await workspace.append_runtime_event(
        "exec-1",
        {"event_type": "execution.completed", "event_id": "evt-terminal"},
    )
    before = _storage_snapshot(workspace.root)

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-1"},
        lifecycle=_terminal_lifecycle(),
        retained_refs=[artifact_ref],
        inline_result={"summary": "done"},
    )

    assert preview["status"] == "ready"
    assert artifact_ref["id"] not in preview["selected"]["record_ids"]
    assert event["id"] in preview["selected"]["runtime_event_ids"]
    assert all(values == sorted(values) for values in preview["selected"].values())

    repeated = await workspace.inspect_retention(
        {"execution_id": "exec-1"},
        lifecycle=_terminal_lifecycle(),
        retained_refs=[artifact_ref],
        inline_result={"summary": "done"},
    )
    assert repeated == preview
    assert _storage_snapshot(workspace.root) == before


@pytest.mark.asyncio
async def test_inspect_retention_defers_invalid_or_unverifiable_roots_without_selection(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-invalid")
    artifact_ref = await workspace.put(
        {"report": "ready"},
        collection="artifacts",
        kind="report",
        scope={"execution_id": "exec-1"},
    )
    outside = WorkspaceManager().create(tmp_path / "outside")
    outside_ref = await outside.put(
        {"report": "outside"},
        collection="artifacts",
        kind="report",
        scope={"execution_id": "exec-1"},
    )
    outside_envelope = await outside.ref_envelope(outside_ref)
    before = _storage_snapshot(workspace.root)

    missing_ref = dict(artifact_ref)
    missing_ref["id"] = "rec_missing"
    digest_mismatch = dict(artifact_ref)
    digest_mismatch["sha256"] = "0" * 64

    for retained_refs, expected_code in (
        ([cast(Any, missing_ref)], "workspace.retention.ref_missing"),
        ([cast(Any, digest_mismatch)], "workspace.retention.ref_digest_mismatch"),
        ([outside_envelope], "workspace.retention.ref_workspace_mismatch"),
    ):
        preview = await workspace.inspect_retention(
            {"execution_id": "exec-1"},
            lifecycle=_terminal_lifecycle(),
            retained_refs=retained_refs,
        )
        _assert_nothing_selected(preview)
        assert expected_code in {item.get("code") for item in preview["diagnostics"]}

    assert _storage_snapshot(workspace.root) == before


@pytest.mark.asyncio
async def test_inspect_retention_defers_active_lifecycle_unsupported_policy_and_large_inline_result(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-lifecycle")
    await workspace.put(
        {"process": "discard"},
        collection="observations",
        kind="process",
        scope={"execution_id": "exec-1"},
    )

    cases: tuple[tuple[dict[str, Any], str], ...] = (
        (
            {"lifecycle": _terminal_lifecycle(recovery_active=True)},
            "workspace.retention.recovery_active",
        ),
        (
            {"lifecycle": _terminal_lifecycle(lease_active=True)},
            "workspace.retention.lease_active",
        ),
        (
            {
                "lifecycle": _terminal_lifecycle(),
                "policy": cast(
                    WorkspaceRetentionPolicy,
                    {"rules": [{"category": "runtime_events", "representation": "cold"}]},
                ),
            },
            "workspace.retention.policy_unsupported",
        ),
        (
            {
                "lifecycle": _terminal_lifecycle(),
                "inline_result": "x" * 4097,
            },
            "workspace.retention.inline_result_too_large",
        ),
    )
    for kwargs, expected_code in cases:
        preview = await workspace.inspect_retention(
            {"execution_id": "exec-1"},
            **kwargs,
        )
        _assert_nothing_selected(preview)
        assert expected_code in {item.get("code") for item in preview["diagnostics"]}


@pytest.mark.asyncio
async def test_inspect_retention_defers_external_incoming_link(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-incoming")
    scoped_ref = await workspace.put(
        {"process": "discard"},
        collection="observations",
        kind="process",
        scope={"execution_id": "exec-1"},
    )
    external_ref = await workspace.put(
        {"owner": "other execution"},
        collection="records",
        kind="external",
        scope={"execution_id": "exec-2"},
    )
    link = await workspace.link(external_ref, scoped_ref, relation="depends_on")

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-1"},
        lifecycle=_terminal_lifecycle(),
    )

    _assert_nothing_selected(preview)
    diagnostic = next(
        item for item in preview["diagnostics"] if item.get("code") == "workspace.retention.incoming_reference"
    )
    assert diagnostic.get("entity") == link["id"]


@pytest.mark.asyncio
async def test_inspect_retention_normalizes_child_file_ref_without_mutating_caller(tmp_path):
    manager = WorkspaceManager()
    lazy = LazyWorkspace(manager, tmp_path / "retention-child")
    assert isinstance(lazy, LazyWorkspace)
    assert lazy.is_materialized is False

    child = cast(LazyWorkspace, lazy.with_scope_node("executions", "exec-file"))
    assert child.is_materialized is False
    written = await child.write_file("artifacts/final.txt", "deliverable")
    file_ref = written["file_refs"][0]
    caller_path = file_ref["path"]
    assert child.is_materialized is True

    preview = await child.inspect_retention(
        {},
        lifecycle=_terminal_lifecycle("exec-file"),
        retained_refs=[file_ref],
    )

    assert preview["status"] == "ready"
    assert file_ref["path"] == caller_path
    normalized = cast(dict[str, object], preview["retained_refs"][0])
    assert normalized["path"] == "lineage/executions/exec-file/files/artifacts/final.txt"
    assert normalized["sha256"] == hashlib.sha256(b"deliverable").hexdigest()
    assert normalized["path"] not in preview["selected"]["file_paths"]
