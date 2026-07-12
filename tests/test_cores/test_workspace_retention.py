from __future__ import annotations

import asyncio
import errno
import gc
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently.core import LazyWorkspace, WorkspaceManager
from agently.core.Workspace.Errors import WorkspacePolicyError
from agently.core.Workspace.LocalBackend import LocalWorkspaceBackend
from agently.core.Workspace.Retention import (
    canonical_retention_fingerprint,
    resolve_retention_policy,
    serialized_size,
    stable_checkpoint_row_identities,
)
from agently.core.Workspace.Stores import delete_owned_file_descriptor_relative
from agently.types.data import (
    WorkspaceRecordRef,
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


def _assert_zero_actual_accounting(result: dict[str, Any]) -> None:
    accounting = cast(dict[str, Any], result["accounting"])
    assert accounting["logical_bytes_deleted"] == 0
    assert accounting["physical_bytes_reclaimed"] == 0
    assert accounting["physical_bytes_pending"] == 0
    assert all(count == 0 for count in accounting["entities"].values())


def _sqlite_allocated_bytes(db_path: Path) -> int:
    return sum(
        path.stat().st_blocks * 512
        for path in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        )
        if path.exists()
    )


async def _seed_large_sqlite_retention_scope(target, *, count: int = 48) -> None:
    padding = "x" * 32768
    for index in range(count):
        await target.put(
            {"row": index},
            collection="observations",
            kind="large_sqlite_row",
            meta={"padding": padding, "index": index},
        )


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


@pytest.mark.asyncio
async def test_inspect_retention_defers_child_file_ref_resolve_runtimeerror(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-child-resolve-error")
    child = root.with_scope_node("executions", "exec-child-resolve-error")
    written = await child.write_file("artifacts/final.txt", "deliverable")
    file_ref = written["file_refs"][0]
    target = child.files_root / str(file_ref["path"])
    original_resolve = Path.resolve

    def fail_target_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path == target:
            raise RuntimeError("Symlink loop from pathlib on Python 3.10")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_target_resolve)
    preview = await child.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-child-resolve-error"),
            "state_version": None,
        },
        retained_refs=[file_ref],
    )

    _assert_nothing_selected(preview)
    assert file_ref["path"] == "artifacts/final.txt"
    assert preview["diagnostics"] == [
        {
            "code": "workspace.retention.ref_readback_failed",
            "message": "Symlink loop from pathlib on Python 3.10",
            "retryable": True,
            "entity": "artifacts/final.txt",
        }
    ]


@pytest.mark.asyncio
async def test_inspect_retention_uses_one_exact_full_lineage_subtree(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-exact-lineage")
    target = root.with_scope_lineage(
        [
            {"kind": "projects", "id": "project-1"},
            {"kind": "tasks", "id": "task-1"},
            {"kind": "executions", "id": "exec-target"},
        ]
    )
    sibling = root.with_scope_lineage(
        [
            {"kind": "projects", "id": "project-1"},
            {"kind": "tasks", "id": "task-1"},
            {"kind": "executions", "id": "exec-sibling"},
        ]
    )
    await target.write_file("reports/target.txt", "target file")
    await sibling.write_file("reports/sibling.txt", "sibling file")
    target_scratch = target.scratch_root() / "target.tmp"
    sibling_scratch = sibling.scratch_root() / "sibling.tmp"
    target_scratch.parent.mkdir(parents=True, exist_ok=True)
    sibling_scratch.parent.mkdir(parents=True, exist_ok=True)
    target_scratch.write_text("target scratch", encoding="utf-8")
    sibling_scratch.write_text("sibling scratch", encoding="utf-8")

    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-target"), "state_version": None},
    )

    assert preview["status"] == "ready"
    assert any(path.endswith("target.txt") for path in preview["selected"]["file_paths"])
    assert not any(path.endswith("sibling.txt") for path in preview["selected"]["file_paths"])
    assert any(path.endswith("target.tmp") for path in preview["selected"]["scratch_paths"])
    assert not any(path.endswith("sibling.tmp") for path in preview["selected"]["scratch_paths"])


@pytest.mark.asyncio
async def test_inspect_retention_compatibility_scope_is_independent_of_key_order(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-ordered-scope")
    target = root.with_scope_lineage(
        [
            {"kind": "projects", "id": "project-order"},
            {"kind": "tasks", "id": "task-order"},
            {"kind": "executions", "id": "exec-order"},
        ]
    )
    await target.write_file("reports/ordered.txt", "ordered target")

    preview = await root.inspect_retention(
        {"execution_id": "exec-order", "project_id": "project-order"},
        lifecycle={**_terminal_lifecycle("exec-order"), "state_version": None},
    )

    assert preview["status"] == "ready"
    assert any(path.endswith("ordered.txt") for path in preview["selected"]["file_paths"])


@pytest.mark.asyncio
async def test_inspect_retention_defers_authoritative_active_lease(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-persisted-lease")
    await workspace.put_checkpoint("exec-lease", {"state_version": 4})
    await workspace.claim_lease("exec-lease", "worker-1", ttl=60, expected_state_version=4)

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-lease"},
        lifecycle={**_terminal_lifecycle("exec-lease"), "state_version": 4},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.lease_active" in {diagnostic.get("code") for diagnostic in preview["diagnostics"]}


@pytest.mark.asyncio
async def test_inspect_retention_defers_stale_or_ambiguous_persisted_state_version(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-persisted-version")
    await workspace.put_checkpoint("exec-version", {"state_version": 4})

    stale = await workspace.inspect_retention(
        {"execution_id": "exec-version"},
        lifecycle={**_terminal_lifecycle("exec-version"), "state_version": 3},
    )
    _assert_nothing_selected(stale)
    assert "workspace.retention.state_version_mismatch" in {
        diagnostic.get("code") for diagnostic in stale["diagnostics"]
    }

    await workspace.append_runtime_event(
        "exec-version",
        {"event_type": "execution.completed", "event_id": "evt-version"},
        state_version=5,
    )
    ambiguous = await workspace.inspect_retention(
        {"execution_id": "exec-version"},
        lifecycle={**_terminal_lifecycle("exec-version"), "state_version": 4},
    )
    _assert_nothing_selected(ambiguous)
    assert "workspace.retention.state_version_ambiguous" in {
        diagnostic.get("code") for diagnostic in ambiguous["diagnostics"]
    }


@pytest.mark.asyncio
async def test_inspect_retention_defers_persisted_nonterminal_recovery_fact(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-persisted-recovery")
    await workspace.append_runtime_event(
        "exec-recovery",
        {"event_type": "triggerflow.execution_started", "event_id": "evt-started"},
        state_version=3,
    )

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-recovery"},
        lifecycle=_terminal_lifecycle("exec-recovery"),
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.recovery_active" in {diagnostic.get("code") for diagnostic in preview["diagnostics"]}


@pytest.mark.asyncio
async def test_inspect_retention_hot_runtime_event_validates_every_reachable_ref(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-hot-event")
    dangling = await workspace.put(
        {"payload": "will disappear"},
        collection="artifacts",
        kind="event_artifact",
        scope={"execution_id": "other-execution"},
    )
    await workspace.append_runtime_event(
        "exec-hot",
        {"event_type": "execution.completed", "event_id": "evt-hot"},
        artifact_refs=[dangling],
        state_version=3,
    )
    backend = cast(Any, workspace.backend)
    with backend._connect() as conn:
        conn.execute("DELETE FROM record_scope_index WHERE record_id = ?", (dangling["id"],))
        conn.execute("DELETE FROM records_fts WHERE record_id = ?", (dangling["id"],))
        conn.execute("DELETE FROM records WHERE id = ?", (dangling["id"],))
        conn.commit()

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-hot"},
        lifecycle=_terminal_lifecycle("exec-hot"),
        policy=cast(
            WorkspaceRetentionPolicy,
            {"rules": [{"category": "runtime_events", "representation": "hot"}]},
        ),
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_missing" in {diagnostic.get("code") for diagnostic in preview["diagnostics"]}


@pytest.mark.asyncio
async def test_inspect_retention_preserves_transitive_workspace_link_closure(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-link-closure")
    retained = await workspace.put(
        {"root": True},
        collection="artifacts",
        kind="retained_root",
        scope={"execution_id": "exec-links"},
    )
    linked = await workspace.put(
        {"source": True},
        collection="records",
        kind="linked_source",
        scope={"execution_id": "exec-links"},
    )
    await workspace.link(retained, linked, relation="depends_on")

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-links"},
        lifecycle={**_terminal_lifecycle("exec-links"), "state_version": None},
        retained_refs=[retained],
    )

    assert preview["status"] == "ready"
    assert linked["id"] not in preview["selected"]["record_ids"]
    retained_ids = {str(ref.get("id") or ref.get("record_id") or "") for ref in preview["retained_refs"]}
    assert linked["id"] in retained_ids


@pytest.mark.asyncio
async def test_inspect_retention_selects_every_mutation_relevant_carrier(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-carriers")
    workspace = root.with_scope_lineage(
        [
            {"kind": "projects", "id": "project-carriers"},
            {"kind": "tasks", "id": "task-carriers"},
            {"kind": "executions", "id": "exec-carriers"},
        ]
    )
    ordinary = await workspace.put(
        {"record": "discard"},
        collection="observations",
        kind="process",
    )
    first_checkpoint = await root.put_checkpoint(
        "exec-carriers",
        {"state_version": 1, "step": "first"},
        expected_state_version=0,
    )
    second_checkpoint = await root.put_checkpoint(
        "exec-carriers",
        {"state_version": 2, "step": "second"},
        expected_state_version=1,
    )
    await root.append_runtime_event(
        "exec-carriers",
        {"event_type": "execution.completed", "event_id": "evt-carriers"},
        state_version=2,
    )
    lease = await root.claim_lease(
        "exec-carriers",
        "worker-carriers",
        ttl=60,
        expected_state_version=2,
    )
    await root.release_lease(
        "exec-carriers",
        cast(str, lease.get("owner_id")),
        cast(str, lease.get("lease_token")),
    )
    await root.record_file_policy()
    backend = cast(Any, root.backend)
    await backend.vector_store_provider.index_record(ordinary, [1.0, 0.0])

    preview = await workspace.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-carriers"), "state_version": 2},
    )

    with backend._connect() as conn:
        checkpoint_facts = [
            {
                "run_id": str(row["run_id"]),
                "step_id": row["step_id"],
                "record_id": str(row["record_id"]),
                "state": json.loads(str(row["state_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in conn.execute(
                """
                SELECT run_id, step_id, record_id, state_json, created_at FROM checkpoints
                WHERE run_id = ? ORDER BY created_at, rowid
                """,
                ("exec-carriers",),
            ).fetchall()
        ]
        checkpoint_identities = set(stable_checkpoint_row_identities(checkpoint_facts))

    assert preview["status"] == "ready"
    required_keys = {
        "record_ids",
        "runtime_event_ids",
        "checkpoint_row_ids",
        "link_ids",
        "retention_anchor_ids",
        "scratch_lease_ids",
        "content_paths",
        "file_paths",
        "scratch_paths",
        "record_scope_index_ids",
        "manifest_keys",
        "fts_record_ids",
        "vector_record_ids",
    }
    assert required_keys.issubset(preview["selected"])
    assert set(preview["selected"]["checkpoint_row_ids"]) == checkpoint_identities
    assert {first_checkpoint["id"], second_checkpoint["id"]}.issubset(preview["selected"]["record_ids"])
    assert {
        "checkpoint.latest.exec-carriers",
        "lease.exec-carriers",
    }.issubset(preview["selected"]["manifest_keys"])
    assert "file_policy" not in preview["selected"]["manifest_keys"]
    assert ordinary["id"] in preview["selected"]["fts_record_ids"]
    assert ordinary["id"] in preview["selected"]["vector_record_ids"]
    for key in required_keys:
        assert preview["accounting"]["entities"][key] == len(preview["selected"][key])
    assert preview["accounting"]["logical_bytes_deleted"] > sum(
        ref["size"] for ref in (ordinary, first_checkpoint, second_checkpoint)
    )


@pytest.mark.asyncio
async def test_task_scope_retention_deletes_checkpoint_rows_and_manifests_across_run_ids(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-task-checkpoint-runs")
    parent_execution_id = "parent-execution"
    task_id = "routed-task"
    target = root.with_scope_lineage(
        [
            {"kind": "executions", "id": parent_execution_id},
            {"kind": "tasks", "id": task_id},
        ]
    )
    sibling = root.with_scope_lineage(
        [
            {"kind": "executions", "id": parent_execution_id},
            {"kind": "tasks", "id": "sibling-task"},
        ]
    )
    task_checkpoint = await target.put_checkpoint(
        task_id,
        {"state_version": 1, "kind": "task-process"},
        expected_state_version=0,
    )
    resume_checkpoint = await target.put_checkpoint(
        f"{task_id}::resume",
        {"state_version": 1, "kind": "resume-process"},
        expected_state_version=0,
    )
    sibling_checkpoint = await sibling.put_checkpoint(
        "sibling-task",
        {"state_version": 1, "kind": "sibling-process"},
        expected_state_version=0,
    )

    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(parent_execution_id), "state_version": None},
    )
    assert preview["status"] == "ready", preview["diagnostics"]
    result = await target.apply_retention(preview)
    assert result["status"] == "applied"

    backend = cast(Any, root.backend)
    with backend._connect() as conn:
        target_rows = conn.execute(
            "SELECT run_id, record_id FROM checkpoints WHERE run_id IN (?, ?)",
            (task_id, f"{task_id}::resume"),
        ).fetchall()
        target_manifests = conn.execute(
            "SELECT key, value_json FROM manifests WHERE key IN (?, ?)",
            (f"checkpoint.latest.{task_id}", f"checkpoint.latest.{task_id}::resume"),
        ).fetchall()
        dangling_checkpoint_rows = conn.execute(
            """
            SELECT c.run_id, c.record_id FROM checkpoints c
            LEFT JOIN records r ON r.id = c.record_id
            WHERE r.id IS NULL
            """
        ).fetchall()
        sibling_rows = conn.execute(
            "SELECT run_id, record_id FROM checkpoints WHERE run_id = ?",
            ("sibling-task",),
        ).fetchall()
        sibling_manifest = conn.execute(
            "SELECT value_json FROM manifests WHERE key = ?",
            ("checkpoint.latest.sibling-task",),
        ).fetchone()

    assert target_rows == [], [dict(row) for row in target_rows]
    assert target_manifests == [], [dict(row) for row in target_manifests]
    assert dangling_checkpoint_rows == [], [dict(row) for row in dangling_checkpoint_rows]
    assert await backend.get_record(task_checkpoint["id"]) is None
    assert await backend.get_record(resume_checkpoint["id"]) is None
    assert [str(row["record_id"]) for row in sibling_rows] == [sibling_checkpoint["id"]]
    assert json.loads(str(sibling_manifest["value_json"]))["id"] == sibling_checkpoint["id"]


@pytest.mark.asyncio
async def test_failed_task_scope_retention_keeps_only_recovery_anchored_resume_checkpoint(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-task-recovery-anchor")
    parent_execution_id = "parent-failed-execution"
    task_id = "failed-routed-task"
    target = root.with_scope_lineage(
        [
            {"kind": "executions", "id": parent_execution_id},
            {"kind": "tasks", "id": task_id},
        ]
    )
    ordinary = await target.put_checkpoint(
        task_id,
        {"state_version": 1, "kind": "ordinary-process"},
        expected_state_version=0,
    )
    first_resume = await target.put_checkpoint(
        f"{task_id}::resume",
        {"state_version": 1, "kind": "old-resume"},
        expected_state_version=0,
    )
    latest_resume = await target.put_checkpoint(
        f"{task_id}::resume",
        {"state_version": 2, "kind": "compact-resume"},
        expected_state_version=1,
    )
    await root.add_retention_anchor(
        parent_execution_id,
        anchor_type="recovery",
        record_ref=latest_resume,
    )

    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(parent_execution_id), "status": "failed", "state_version": None},
    )
    assert preview["status"] == "ready", preview["diagnostics"]
    result = await target.apply_retention(preview)
    assert result["status"] == "applied"

    backend = cast(Any, root.backend)
    with backend._connect() as conn:
        checkpoint_rows = conn.execute(
            """
            SELECT run_id, record_id FROM checkpoints
            WHERE run_id IN (?, ?) ORDER BY run_id, rowid
            """,
            (task_id, f"{task_id}::resume"),
        ).fetchall()
        checkpoint_manifests = conn.execute(
            """
            SELECT key, value_json FROM manifests
            WHERE key IN (?, ?) ORDER BY key
            """,
            (f"checkpoint.latest.{task_id}", f"checkpoint.latest.{task_id}::resume"),
        ).fetchall()
        dangling_checkpoint_rows = conn.execute(
            """
            SELECT c.run_id, c.record_id FROM checkpoints c
            LEFT JOIN records r ON r.id = c.record_id
            WHERE r.id IS NULL
            """
        ).fetchall()

    assert [dict(row) for row in checkpoint_rows] == [
        {"run_id": f"{task_id}::resume", "record_id": latest_resume["id"]}
    ]
    assert [str(row["key"]) for row in checkpoint_manifests] == [
        f"checkpoint.latest.{task_id}::resume"
    ]
    assert json.loads(str(checkpoint_manifests[0]["value_json"]))["id"] == latest_resume["id"]
    assert dangling_checkpoint_rows == []
    assert await backend.get_record(ordinary["id"]) is None
    assert await backend.get_record(first_resume["id"]) is None
    assert await backend.get_record(latest_resume["id"]) == latest_resume


@pytest.mark.asyncio
async def test_inspect_retention_converts_readback_oserror_to_deferred(tmp_path, monkeypatch):
    workspace = WorkspaceManager().create(tmp_path / "retention-readback-error")
    ref = await workspace.put(
        {"record": "unreadable"},
        collection="observations",
        kind="process",
        scope={"execution_id": "exec-readback"},
    )
    target = (workspace.content_root / cast(str, ref["path"])).resolve()
    original_read_bytes = Path.read_bytes

    def fail_target_read(path: Path) -> bytes:
        if path.resolve() == target:
            raise OSError("simulated readback failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_target_read)
    preview = await workspace.inspect_retention(
        {"execution_id": "exec-readback"},
        lifecycle={**_terminal_lifecycle("exec-readback"), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_readback_failed" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink_component", ["project", "task", "execution"])
async def test_inspect_retention_rejects_symlink_in_every_candidate_ancestor(
    tmp_path,
    symlink_component,
):
    root = WorkspaceManager().create(tmp_path / f"retention-symlink-{symlink_component}")
    workspace = root.with_scope_lineage(
        [
            {"kind": "projects", "id": "project-symlink"},
            {"kind": "tasks", "id": "task-symlink"},
            {"kind": "executions", "id": "exec-symlink"},
        ]
    )
    await workspace.write_file("reports/result.txt", "result")
    lineage_root = root.root / "files" / "lineage"
    components = {
        "project": lineage_root / "projects" / "project-symlink",
        "task": lineage_root / "projects" / "project-symlink" / "tasks" / "task-symlink",
        "execution": (
            lineage_root
            / "projects"
            / "project-symlink"
            / "tasks"
            / "task-symlink"
            / "executions"
            / "exec-symlink"
        ),
    }
    lexical = components[symlink_component]
    actual = lexical.with_name(f"{lexical.name}-actual")
    lexical.rename(actual)
    lexical.symlink_to(actual, target_is_directory=True)

    preview = await workspace.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-symlink"), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.lineage_ambiguous" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only_component", ["workspace", "db_store", "vector_store"])
async def test_inspect_retention_defers_nonempty_read_only_plan_without_record_ids(
    tmp_path,
    read_only_component,
):
    root = tmp_path / "retention-read-only-plan"
    writable = WorkspaceManager().create(root)
    await writable.append_runtime_event(
        "exec-read-only",
        {"event_type": "execution.completed", "event_id": "evt-read-only"},
    )
    if read_only_component == "workspace":
        inspected = WorkspaceManager().create(root, mode="read_only")
    else:
        inspected = writable
        backend = cast(Any, inspected.backend)
        setattr(backend, f"{read_only_component}_provider", SimpleNamespace(read_only=True))

    inspect = inspected.inspect_retention
    if read_only_component == "db_store":
        backend = cast(Any, inspected.backend)
        inspect = type(backend).inspect_retention.__get__(backend, type(backend))
    preview = await inspect(
        {"execution_id": "exec-read-only"},
        lifecycle={**_terminal_lifecycle("exec-read-only"), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.provider_capability_missing" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
async def test_inspect_retention_selects_orphan_checkpoint_manifest(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-orphan-manifest")
    backend = cast(Any, workspace.backend)
    backend._set_manifest(
        "checkpoint.latest.exec-orphan",
        {
            "id": "rec_orphan",
            "collection": "checkpoints",
            "kind": "checkpoint",
            "path": "checkpoints/missing.json",
            "sha256": "0" * 64,
            "size": 1,
            "summary": "orphan",
            "scope": {"run_id": "exec-orphan"},
            "source": {},
            "created_at": "2026-07-12T00:00:00Z",
            "meta": {"checkpoint": True},
        },
    )

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-orphan"},
        lifecycle={**_terminal_lifecycle("exec-orphan"), "state_version": None},
    )

    assert preview["status"] == "ready"
    assert "checkpoint.latest.exec-orphan" in preview["selected"]["manifest_keys"]


@pytest.mark.asyncio
async def test_inspect_retention_hot_checkpoint_validates_manifest_ref(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-hot-manifest")
    checkpoint = await workspace.put_checkpoint(
        "exec-hot-manifest",
        {"state_version": 2},
        expected_state_version=0,
    )
    backend = cast(Any, workspace.backend)
    with backend._connect() as conn:
        conn.execute("DELETE FROM checkpoints WHERE run_id = ?", ("exec-hot-manifest",))
        conn.execute("DELETE FROM record_scope_index WHERE record_id = ?", (checkpoint["id"],))
        conn.execute("DELETE FROM records_fts WHERE record_id = ?", (checkpoint["id"],))
        conn.execute("DELETE FROM records WHERE id = ?", (checkpoint["id"],))
        conn.commit()

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-hot-manifest"},
        lifecycle={**_terminal_lifecycle("exec-hot-manifest"), "state_version": None},
        policy=cast(
            WorkspaceRetentionPolicy,
            {"rules": [{"category": "checkpoints", "representation": "hot"}]},
        ),
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_missing" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
async def test_inspect_retention_accounts_record_metadata_and_scratch_lease_rows(tmp_path):
    workspace = WorkspaceManager().create(
        tmp_path / "retention-row-bytes",
        vector_store_provider="sqlite",
    )
    record = await workspace.put(
        "",
        collection="observations",
        kind="empty_process",
        summary="",
        scope={"execution_id": "exec-row-bytes"},
    )
    lease = await workspace.open_scratch(
        scope={"execution_id": "exec-row-bytes"},
        cleanup_policy="on_scope_prune",
    )
    lease_id = cast(str, lease.get("lease_id"))
    await workspace.close_scratch(lease_id, remove=False)
    backend = cast(Any, workspace.backend)
    with backend._connect() as conn:
        record_row = conn.execute("SELECT * FROM records WHERE id = ?", (record["id"],)).fetchone()
        scope_rows = conn.execute(
            "SELECT record_id, scope_key, scope_value FROM record_scope_index WHERE record_id = ?",
            (record["id"],),
        ).fetchall()
        fts_row = conn.execute(
            "SELECT summary, content FROM records_fts WHERE record_id = ?",
            (record["id"],),
        ).fetchone()
        scratch_row = conn.execute(
            "SELECT * FROM scratch_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
    assert record_row is not None
    assert fts_row is not None
    assert scratch_row is not None
    expected_bytes = (
        record["size"]
        + serialized_size(dict(record_row))
        + sum(serialized_size(dict(row)) for row in scope_rows)
        + len(str(fts_row["summary"] or "").encode("utf-8"))
        + len(str(fts_row["content"] or "").encode("utf-8"))
        + serialized_size(dict(scratch_row))
    )

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-row-bytes"},
        lifecycle={**_terminal_lifecycle("exec-row-bytes"), "state_version": None},
    )

    assert preview["status"] == "ready"
    assert preview["accounting"]["logical_bytes_deleted"] == expected_bytes


@pytest.mark.asyncio
async def test_inspect_retention_catches_directory_iterator_oserror(tmp_path, monkeypatch):
    root = WorkspaceManager().create(tmp_path / "retention-walk-error")
    workspace = root.with_scope_lineage(
        [{"kind": "executions", "id": "exec-walk-error"}]
    )
    await workspace.write_file("reports/result.txt", "result")
    candidate = root.root / "files" / "lineage" / "executions" / "exec-walk-error"
    original_rglob = Path.rglob

    def broken_rglob(path: Path, pattern: str):
        if path == candidate and pattern == "*":
            def broken_iterator():
                yield path / "files" / "reports" / "result.txt"
                raise OSError("simulated iterator advancement failure")

            return broken_iterator()
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", broken_rglob)
    preview = await workspace.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-walk-error"), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_readback_failed" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
async def test_inspect_retention_catches_resolve_runtimeerror(tmp_path, monkeypatch):
    workspace = WorkspaceManager().create(tmp_path / "retention-resolve-error")
    record = await workspace.put(
        {"record": "resolve failure"},
        collection="observations",
        kind="process",
        scope={"execution_id": "exec-resolve-error"},
    )
    target = workspace.content_root / cast(str, record["path"])
    original_resolve = Path.resolve

    def broken_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if str(path) == str(target):
            raise RuntimeError("Symlink loop from pathlib on Python 3.10")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", broken_resolve)
    preview = await workspace.inspect_retention(
        {"execution_id": "exec-resolve-error"},
        lifecycle={**_terminal_lifecycle("exec-resolve-error"), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_readback_failed" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
async def test_inspect_retention_preserves_content_only_root_and_owning_record(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-content-root")
    owner = await workspace.put(
        {"artifact": "managed content"},
        collection="artifacts",
        kind="managed_content",
        scope={"execution_id": "exec-content-root"},
    )
    content_root = await workspace.ref_envelope(cast(str, owner["path"]))
    assert content_root["record_id"] == ""
    assert content_root["content_ref"] == owner["path"]

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-content-root"},
        lifecycle={**_terminal_lifecycle("exec-content-root"), "state_version": None},
        retained_refs=[content_root],
    )

    assert preview["status"] == "ready"
    assert owner["id"] not in preview["selected"]["record_ids"]
    assert owner["path"] not in preview["selected"]["content_paths"]
    retained_owner_ids = {
        str(ref.get("id") or ref.get("record_id") or "")
        for ref in preview["retained_refs"]
    }
    assert owner["id"] in retained_owner_ids


@pytest.mark.asyncio
async def test_inspect_retention_rejects_broad_scope_without_execution_id(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-execution-exact")
    for execution_id in ("exec-target", "exec-sibling"):
        await workspace.put(
            {"execution": execution_id},
            collection="observations",
            kind="process",
            scope={"project_id": "project-shared", "execution_id": execution_id},
        )

    preview = await workspace.inspect_retention(
        {"project_id": "project-shared"},
        lifecycle={**_terminal_lifecycle("exec-target"), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.lifecycle_scope_mismatch" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("area", ["files", "scratch"])
async def test_inspect_retention_rejects_symlinked_area_root(tmp_path, area):
    root = WorkspaceManager().create(tmp_path / f"retention-area-root-{area}")
    workspace = root.with_scope_node("executions", "exec-area-root")
    await workspace.write_file("reports/result.txt", "result")
    lease = await workspace.open_scratch(cleanup_policy="on_scope_prune")
    await workspace.close_scratch(cast(str, lease.get("lease_id")), remove=False)
    lexical_root = root.root / area
    actual_root = lexical_root.with_name(f"{area}-actual")
    lexical_root.rename(actual_root)
    lexical_root.symlink_to(actual_root, target_is_directory=True)

    preview = await workspace.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-area-root"), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.lineage_ambiguous" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corruption_case", "corrupt_json"),
    [
        ("record_scope", "[]"),
        ("record_meta", "[]"),
        ("runtime_snapshot", "[]"),
        ("runtime_artifacts", "{}"),
        ("anchor_ref", "[]"),
        ("anchor_preserved_events", "{}"),
        ("checkpoint_state", "[]"),
        ("scratch_scope", "[]"),
        ("checkpoint_manifest", "{}"),
        ("scope_index_value", "{"),
        ("vector_ref", "[]"),
        ("vector_embedding", "{}"),
    ],
)
async def test_inspect_retention_strictly_validates_persisted_json(
    tmp_path,
    corruption_case,
    corrupt_json,
):
    execution_id = "exec-strict-json"
    workspace = WorkspaceManager().create(
        tmp_path / f"retention-json-{corruption_case}",
        vector_store_provider="sqlite",
    )
    record = await workspace.put(
        {"record": "strict JSON"},
        collection="observations",
        kind="process",
        scope={"execution_id": execution_id},
        meta={"source": "test"},
    )
    checkpoint = await workspace.put_checkpoint(
        execution_id,
        {"state_version": 1},
        expected_state_version=0,
    )
    event = await workspace.append_runtime_event(
        execution_id,
        {"event_type": "execution.completed", "event_id": "evt-strict-json"},
        snapshot_ref=record,
        artifact_refs=[record],
        state_version=1,
    )
    anchor = await workspace.add_retention_anchor(
        execution_id,
        anchor_type="final",
        record_ref=record,
        preserved_event_ids=[event["event_id"]],
    )
    lease = await workspace.open_scratch(
        scope={"execution_id": execution_id},
        cleanup_policy="on_scope_prune",
    )
    await workspace.close_scratch(cast(str, lease.get("lease_id")), remove=False)
    backend = cast(Any, workspace.backend)
    await backend.vector_store_provider.index_record(record, [1.0, 0.0])
    statements = {
        "record_scope": ("UPDATE records SET scope_json = ? WHERE id = ?", (record["id"],)),
        "record_meta": ("UPDATE records SET meta_json = ? WHERE id = ?", (record["id"],)),
        "runtime_snapshot": (
            "UPDATE runtime_events SET snapshot_ref_json = ? WHERE id = ?",
            (event["id"],),
        ),
        "runtime_artifacts": (
            "UPDATE runtime_events SET artifact_refs_json = ? WHERE id = ?",
            (event["id"],),
        ),
        "anchor_ref": (
            "UPDATE retention_anchors SET record_ref_json = ? WHERE id = ?",
            (anchor["id"],),
        ),
        "anchor_preserved_events": (
            "UPDATE retention_anchors SET preserved_event_ids_json = ? WHERE id = ?",
            (anchor["id"],),
        ),
        "checkpoint_state": (
            "UPDATE checkpoints SET state_json = ? WHERE run_id = ?",
            (execution_id,),
        ),
        "scratch_scope": (
            "UPDATE scratch_leases SET scope_json = ? WHERE lease_id = ?",
            (lease.get("lease_id"),),
        ),
        "checkpoint_manifest": (
            "UPDATE manifests SET value_json = ? WHERE key = ?",
            (f"checkpoint.latest.{execution_id}",),
        ),
        "scope_index_value": (
            "UPDATE record_scope_index SET scope_value = ? WHERE record_id = ? AND scope_key = 'execution_id'",
            (record["id"],),
        ),
        "vector_ref": (
            "UPDATE workspace_vectors SET ref_json = ? WHERE record_id = ?",
            (record["id"],),
        ),
        "vector_embedding": (
            "UPDATE workspace_vectors SET embedding_json = ? WHERE record_id = ?",
            (record["id"],),
        ),
    }
    statement, tail_params = statements[corruption_case]
    with backend._connect() as conn:
        conn.execute(statement, (corrupt_json, *tail_params))
        conn.commit()

    preview = await workspace.inspect_retention(
        {"execution_id": execution_id},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": 1},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_readback_failed" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }
    assert checkpoint["id"]


@pytest.mark.asyncio
async def test_inspect_retention_checkpoint_identity_survives_rowid_rebuild(tmp_path):
    execution_id = "exec-stable-checkpoint"
    workspace = WorkspaceManager().create(tmp_path / "retention-stable-checkpoint")
    await workspace.put_checkpoint(
        execution_id,
        {"state_version": 1},
        expected_state_version=0,
    )
    lifecycle = cast(
        WorkspaceRetentionLifecycle,
        {**_terminal_lifecycle(execution_id), "state_version": 1},
    )
    first = await workspace.inspect_retention(
        {"execution_id": execution_id},
        lifecycle=lifecycle,
    )
    backend = cast(Any, workspace.backend)
    with backend._connect() as conn:
        rows = conn.execute(
            "SELECT run_id, step_id, record_id, state_json, created_at FROM checkpoints WHERE run_id = ?",
            (execution_id,),
        ).fetchall()
        conn.execute("DELETE FROM checkpoints WHERE run_id = ?", (execution_id,))
        conn.execute(
            "INSERT INTO checkpoints(run_id, step_id, record_id, state_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("other-run", None, "other-record", "{}", "2026-07-12T00:00:00Z"),
        )
        conn.executemany(
            "INSERT INTO checkpoints(run_id, step_id, record_id, state_json, created_at) VALUES (?, ?, ?, ?, ?)",
            [tuple(row) for row in rows],
        )
        conn.commit()

    second = await workspace.inspect_retention(
        {"execution_id": execution_id},
        lifecycle=lifecycle,
    )

    assert first["status"] == second["status"] == "ready"
    assert first["selected"]["checkpoint_row_ids"] == second["selected"]["checkpoint_row_ids"]
    assert first["plan_fingerprint"] == second["plan_fingerprint"]


@pytest.mark.asyncio
async def test_inspect_retention_treats_missing_lazy_scratch_area_as_empty(tmp_path):
    workspace = WorkspaceManager().create(tmp_path / "retention-no-scratch")
    await workspace.put(
        {"record": "no scratch required"},
        collection="observations",
        kind="process",
        scope={"execution_id": "exec-no-scratch"},
    )
    assert not (workspace.root / "scratch").exists()

    preview = await workspace.inspect_retention(
        {"execution_id": "exec-no-scratch"},
        lifecycle={**_terminal_lifecycle("exec-no-scratch"), "state_version": None},
    )

    assert preview["status"] == "ready"
    assert preview["selected"]["scratch_paths"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["missing", "extra", "contradictory"])
async def test_inspect_retention_requires_scope_index_to_match_record_scope(tmp_path, mismatch):
    execution_id = "exec-scope-index-match"
    workspace = WorkspaceManager().create(tmp_path / f"retention-scope-index-{mismatch}")
    record = await workspace.put(
        {"record": "scope index authority"},
        collection="observations",
        kind="process",
        scope={"execution_id": execution_id, "project_id": "project-authoritative"},
    )
    backend = cast(Any, workspace.backend)
    with backend._connect() as conn:
        if mismatch == "missing":
            conn.execute(
                "DELETE FROM record_scope_index WHERE record_id = ? AND scope_key = 'project_id'",
                (record["id"],),
            )
        elif mismatch == "extra":
            conn.execute(
                "INSERT INTO record_scope_index(record_id, scope_key, scope_value) VALUES (?, ?, ?)",
                (record["id"], "task_id", json.dumps("task-extra")),
            )
        else:
            conn.execute(
                "UPDATE record_scope_index SET scope_value = ? WHERE record_id = ? AND scope_key = 'execution_id'",
                (json.dumps("exec-contradictory"), record["id"]),
            )
        conn.commit()

    preview = await workspace.inspect_retention(
        {"execution_id": execution_id},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_readback_failed" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("files_representation", ["discard", "hot"])
async def test_inspect_retention_requires_files_area_root(tmp_path, files_representation):
    execution_id = "exec-required-files"
    workspace = WorkspaceManager().create(tmp_path / "retention-required-files")
    await workspace.put(
        {"record": "files root required"},
        collection="observations",
        kind="process",
        scope={"execution_id": execution_id},
    )
    shutil.rmtree(workspace.root / "files")

    preview = await workspace.inspect_retention(
        {"execution_id": execution_id},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
        policy=cast(
            WorkspaceRetentionPolicy,
            {"rules": [{"category": "files", "representation": files_representation}]},
        ),
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.lineage_ambiguous" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("area", ["files", "scratch"])
async def test_inspect_retention_defers_area_disappearance_after_observation(
    tmp_path,
    monkeypatch,
    area,
):
    execution_id = f"exec-{area}-race"
    root = WorkspaceManager().create(tmp_path / f"retention-{area}-race")
    workspace = root.with_scope_node("executions", execution_id)
    await workspace.write_file("reports/result.txt", "result")
    if area == "scratch":
        lease = await workspace.open_scratch(cleanup_policy="on_scope_prune")
        await workspace.close_scratch(cast(str, lease.get("lease_id")), remove=False)
    area_root = root.root / area
    original_resolve = Path.resolve
    removed = False

    def remove_after_observation(path: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal removed
        if path == area_root and not removed:
            removed = True
            shutil.rmtree(area_root)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", remove_after_observation)
    preview = await workspace.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )

    _assert_nothing_selected(preview)
    assert "workspace.retention.ref_readback_failed" in {
        diagnostic.get("code") for diagnostic in preview["diagnostics"]
    }


@pytest.mark.asyncio
async def test_apply_retention_atomically_cleans_all_selected_carriers_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-apply-carriers",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_lineage(
        [
            {"kind": "projects", "id": "project-apply"},
            {"kind": "tasks", "id": "task-apply"},
            {"kind": "executions", "id": "exec-apply"},
        ]
    )
    sibling = root.with_scope_lineage(
        [
            {"kind": "projects", "id": "project-apply"},
            {"kind": "tasks", "id": "task-apply"},
            {"kind": "executions", "id": "exec-sibling"},
        ]
    )
    retained_content = '{"deliverable":"retained"}'
    retained_ref = await target.put(
        retained_content,
        collection="artifacts",
        kind="report",
    )
    discarded_left = await target.put(
        {"process": "left"},
        collection="observations",
        kind="process",
    )
    discarded_right = await target.put(
        {"process": "right"},
        collection="observations",
        kind="process",
    )
    sibling_ref = await sibling.put(
        {"process": "sibling"},
        collection="observations",
        kind="process",
    )
    target_link = await root.link(discarded_left, discarded_right, relation="next")
    await root.put_checkpoint(
        "exec-apply",
        {"state_version": 1, "step": "terminal"},
        expected_state_version=0,
    )
    target_event = await root.append_runtime_event(
        "exec-apply",
        {"event_type": "execution.completed", "event_id": "evt-apply"},
        state_version=1,
    )
    sibling_event = await root.append_runtime_event(
        "exec-sibling",
        {"event_type": "execution.completed", "event_id": "evt-sibling"},
    )
    backend = cast(Any, root.backend)
    await backend.vector_store_provider.index_record(discarded_left, [1.0, 0.0])
    await backend.vector_store_provider.index_record(sibling_ref, [0.0, 1.0])

    discarded_write = await target.write_file("reports/process.txt", "discard file")
    retained_write = await target.write_file("reports/final.txt", "retained file")
    retained_file_ref = retained_write["file_refs"][0]
    sibling_write = await sibling.write_file("reports/sibling.txt", "sibling file")
    discarded_file = target.files_root / str(discarded_write["file_refs"][0]["path"])
    retained_file = target.files_root / str(retained_file_ref["path"])
    sibling_file = sibling.files_root / str(sibling_write["file_refs"][0]["path"])
    sibling_bytes = sibling_file.read_bytes()

    target_lease = await target.open_scratch(cleanup_policy="on_scope_prune")
    target_scratch = Path(cast(str, target_lease.get("local_path"))) / "process.tmp"
    target_scratch.write_text("discard scratch", encoding="utf-8")
    await target.close_scratch(cast(str, target_lease.get("lease_id")), remove=False)
    sibling_scratch = sibling.scratch_root() / "sibling.tmp"
    sibling_scratch.parent.mkdir(parents=True, exist_ok=True)
    sibling_scratch.write_text("sibling scratch", encoding="utf-8")

    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-apply"), "state_version": 1},
        retained_refs=[retained_ref, retained_file_ref],
        inline_result={"summary": "done"},
    )
    assert preview["status"] == "ready"

    observed_locked_snapshot: list[bool] = []
    original_read_snapshot = backend._read_retention_snapshot

    def observe_locked_snapshot(*args: Any, **kwargs: Any):
        connection = kwargs.get("connection")
        if connection is not None:
            observed_locked_snapshot.append(bool(connection.in_transaction))
        return original_read_snapshot(*args, **kwargs)

    monkeypatch.setattr(backend, "_read_retention_snapshot", observe_locked_snapshot)
    result = await target.apply_retention(preview)

    assert result["status"] == "applied"
    assert observed_locked_snapshot == [True]
    assert await root.get(retained_ref) == retained_content
    assert retained_file.read_bytes() == b"retained file"
    assert discarded_file.exists() is False
    assert target_scratch.exists() is False
    assert sibling_file.read_bytes() == sibling_bytes
    assert sibling_scratch.read_text(encoding="utf-8") == "sibling scratch"
    assert await root.query_runtime_events("exec-apply") == []
    assert await root.query_runtime_events("exec-sibling") == [sibling_event]
    assert await backend.get_record(discarded_left["id"]) is None
    assert await backend.get_record(discarded_right["id"]) is None
    assert await backend.get_record(sibling_ref["id"]) == sibling_ref
    assert await root.latest_checkpoint("exec-apply") is None
    assert target_link not in await root.links()
    assert await backend.get_scratch_lease(cast(str, target_lease.get("lease_id"))) is None
    assert target_event["id"] in preview["selected"]["runtime_event_ids"]

    manifest_ref = cast(dict[str, Any], result["manifest_ref"])
    expected_manifest_id = "rec_workspace_terminal_" + hashlib.sha256(
        f"{backend.workspace_id}:exec-apply".encode("utf-8")
    ).hexdigest()[:24]
    assert manifest_ref["id"] == expected_manifest_id
    assert manifest_ref["collection"] == "artifacts"
    assert manifest_ref["kind"] == "workspace_terminal_manifest"
    assert manifest_ref["path"] is None
    assert manifest_ref["sha256"] is None
    assert manifest_ref["size"] == 0
    assert manifest_ref["scope"] == preview["scope"]
    assert manifest_ref["source"] == {"type": "workspace", "name": "terminal_retention"}
    assert set(manifest_ref["meta"]) == {
        "schema_version",
        "plan_fingerprint",
        "state",
        "lifecycle",
        "retained_refs",
        "inline_result",
        "accounting",
        "derived_cleanup",
    }
    assert manifest_ref["meta"]["schema_version"] == "agently.workspace.terminal_manifest.v1"
    assert manifest_ref["meta"]["plan_fingerprint"] == preview["plan_fingerprint"]
    assert manifest_ref["meta"]["state"] == "applied"
    assert set(manifest_ref["meta"]["derived_cleanup"]) == {
        "pending",
        "attempts",
        "last_error",
    }
    assert manifest_ref["meta"]["derived_cleanup"] == {
        "pending": {
            "vector_record_ids": [],
            "content_paths": [],
            "file_paths": [],
            "scratch_paths": [],
        },
        "attempts": 1,
        "last_error": None,
    }
    assert result["accounting"]["entities"] == preview["accounting"]["entities"]
    assert result["accounting"]["logical_bytes_deleted"] == preview["accounting"]["logical_bytes_deleted"]
    assert manifest_ref["meta"]["accounting"] == result["accounting"]

    with backend._connect() as conn:
        manifest_rows = conn.execute(
            "SELECT id, path, meta_json FROM records WHERE kind = 'workspace_terminal_manifest'"
        ).fetchall()
        target_vector = conn.execute(
            "SELECT record_id FROM workspace_vectors WHERE record_id = ?",
            (discarded_left["id"],),
        ).fetchone()
        sibling_vector = conn.execute(
            "SELECT record_id FROM workspace_vectors WHERE record_id = ?",
            (sibling_ref["id"],),
        ).fetchone()
    assert len(manifest_rows) == 1
    assert manifest_rows[0]["path"] is None
    assert target_vector is None
    assert sibling_vector is not None

    repeated = await target.apply_retention(preview)
    assert repeated["status"] == "noop"
    assert repeated["manifest_ref"] == manifest_ref
    assert repeated["diagnostics"] == []
    _assert_zero_actual_accounting(cast(dict[str, Any], repeated))

    successor_preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-apply"), "state_version": None},
        retained_refs=[retained_ref, retained_file_ref],
        inline_result={"summary": "done"},
    )
    assert successor_preview["status"] == "ready"
    assert successor_preview["plan_fingerprint"] != preview["plan_fingerprint"]
    successor = await target.apply_retention(successor_preview)
    assert successor["status"] == "applied"
    assert successor["manifest_ref"] is not None
    assert successor["manifest_ref"]["id"] == manifest_ref["id"]


@pytest.mark.asyncio
async def test_apply_retention_stale_plan_defers_without_partial_deletion(tmp_path):
    root = WorkspaceManager().create(
        tmp_path / "retention-apply-stale",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_node("executions", "exec-stale")
    original = await target.put(
        {"process": "original"},
        collection="observations",
        kind="process",
    )
    event = await root.append_runtime_event(
        "exec-stale",
        {"event_type": "execution.completed", "event_id": "evt-stale"},
    )
    written = await target.write_file("reports/process.txt", "process file")
    target_file = target.files_root / str(written["file_refs"][0]["path"])
    backend = cast(Any, root.backend)
    await backend.vector_store_provider.index_record(original, [1.0])
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-stale"), "state_version": None},
    )
    assert preview["status"] == "ready"

    added_after_inspection = await target.put(
        {"process": "late mutation"},
        collection="observations",
        kind="process",
    )
    result = await target.apply_retention(preview)

    assert result["status"] == "deferred"
    assert result["manifest_ref"] is None
    assert [diagnostic.get("code") for diagnostic in result["diagnostics"]] == [
        "workspace.retention.plan_stale"
    ]
    _assert_zero_actual_accounting(cast(dict[str, Any], result))
    assert await backend.get_record(original["id"]) == original
    assert await backend.get_record(added_after_inspection["id"]) == added_after_inspection
    assert await root.query_runtime_events("exec-stale") == [event]
    assert target_file.read_bytes() == b"process file"
    with backend._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM records_fts WHERE record_id = ?", (original["id"],)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM workspace_vectors WHERE record_id = ?", (original["id"],)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM records WHERE kind = 'workspace_terminal_manifest'"
        ).fetchone() is None


@pytest.mark.asyncio
async def test_apply_retention_rolls_back_manifest_and_all_logical_deletes_on_db_failure(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-apply-rollback")
    target = root.with_scope_node("executions", "exec-rollback")
    discarded = await target.put(
        {"process": "rollback"},
        collection="observations",
        kind="process",
    )
    event = await root.append_runtime_event(
        "exec-rollback",
        {"event_type": "execution.completed", "event_id": "evt-rollback"},
    )
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-rollback"), "state_version": None},
    )
    assert preview["status"] == "ready"
    backend = cast(Any, root.backend)

    def fail_after_first_delete(connection, selected):
        backend._delete_ids_on_conn(
            connection,
            "runtime_events",
            "id",
            selected["runtime_event_ids"],
        )
        raise sqlite3.OperationalError("injected logical delete failure")

    monkeypatch.setattr(
        backend,
        "_delete_retention_logical_selection_on_conn",
        fail_after_first_delete,
    )
    result = await target.apply_retention(preview)

    assert result["status"] == "deferred"
    assert [diagnostic.get("code") for diagnostic in result["diagnostics"]] == [
        "workspace.retention.apply_failed"
    ]
    _assert_zero_actual_accounting(cast(dict[str, Any], result))
    assert await backend.get_record(discarded["id"]) == discarded
    assert await root.query_runtime_events("exec-rollback") == [event]
    with backend._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM records_fts WHERE record_id = ?", (discarded["id"],)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM records WHERE kind = 'workspace_terminal_manifest'"
        ).fetchone() is None


@pytest.mark.asyncio
async def test_apply_retention_resumes_only_pending_derived_cleanup_after_partial_failure(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-apply-resume",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_node("executions", "exec-resume")
    discarded = await target.put(
        {"process": "resume"},
        collection="observations",
        kind="process",
    )
    written = await target.write_file("reports/process.txt", "process file")
    target_file = target.files_root / str(written["file_refs"][0]["path"])
    lease = await target.open_scratch(cleanup_policy="on_scope_prune")
    scratch_file = Path(cast(str, lease.get("local_path"))) / "process.tmp"
    scratch_file.write_text("process scratch", encoding="utf-8")
    await target.close_scratch(cast(str, lease.get("lease_id")), remove=False)
    backend = cast(Any, root.backend)
    provider = backend.vector_store_provider
    await provider.index_record(discarded, [1.0])
    vector_delete_calls: list[list[str]] = []
    original_delete_records = provider.delete_records

    async def observe_vector_delete(record_ids):
        vector_delete_calls.append(list(record_ids))
        await original_delete_records(record_ids)

    monkeypatch.setattr(provider, "delete_records", observe_vector_delete)
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-resume"), "state_version": None},
    )
    assert preview["status"] == "ready"
    conflicting_preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-resume"), "state_version": None},
        retained_refs=[discarded],
    )
    assert conflicting_preview["status"] == "ready"
    assert conflicting_preview["plan_fingerprint"] != preview["plan_fingerprint"]
    content_target = root.content_root / cast(str, discarded["path"])
    original_delete_content = backend.content.delete_content
    failed_once = False

    async def fail_content_once(relative_path: str):
        nonlocal failed_once
        if relative_path == discarded["path"] and not failed_once:
            failed_once = True
            raise OSError("injected content delete failure")
        return await original_delete_content(relative_path)

    monkeypatch.setattr(backend.content, "delete_content", fail_content_once)
    first = await target.apply_retention(preview)

    assert first["status"] == "deferred"
    assert [diagnostic.get("code") for diagnostic in first["diagnostics"]] == [
        "workspace.retention.derived_cleanup_failed"
    ]
    assert await backend.get_record(discarded["id"]) is None
    assert content_target.exists()
    assert target_file.exists()
    assert scratch_file.exists()
    assert vector_delete_calls == [[discarded["id"]]]
    first_manifest = cast(dict[str, Any], first["manifest_ref"])
    first_derived = first_manifest["meta"]["derived_cleanup"]
    assert first_manifest["meta"]["state"] == "derived_pending"
    assert first_derived["pending"] == {
        "vector_record_ids": [],
        "content_paths": [cast(str, discarded["path"])],
        "file_paths": preview["selected"]["file_paths"],
        "scratch_paths": preview["selected"]["scratch_paths"],
    }
    assert first_derived["attempts"] == 1
    assert first_derived["last_error"]["code"] == "workspace.retention.derived_cleanup_failed"

    conflict = await target.apply_retention(conflicting_preview)
    assert conflict["status"] == "deferred"
    assert [diagnostic.get("code") for diagnostic in conflict["diagnostics"]] == [
        "workspace.retention.plan_conflict"
    ]
    _assert_zero_actual_accounting(cast(dict[str, Any], conflict))
    assert vector_delete_calls == [[discarded["id"]]]

    resumed = await target.apply_retention(preview)

    assert resumed["status"] == "applied"
    assert vector_delete_calls == [[discarded["id"]]]
    assert content_target.exists() is False
    assert target_file.exists() is False
    assert scratch_file.exists() is False
    resumed_manifest = cast(dict[str, Any], resumed["manifest_ref"])
    assert resumed_manifest["id"] == first_manifest["id"]
    assert resumed_manifest["meta"]["state"] == "applied"
    assert resumed_manifest["meta"]["derived_cleanup"] == {
        "pending": {
            "vector_record_ids": [],
            "content_paths": [],
            "file_paths": [],
            "scratch_paths": [],
        },
        "attempts": 2,
        "last_error": None,
    }


@pytest.mark.asyncio
async def test_apply_retention_descriptor_delete_cannot_follow_swapped_content_parent(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-parent-swap")
    target = root.with_scope_node("executions", "exec-parent-swap")
    discarded = await target.put(
        {"process": "parent swap"},
        collection="observations",
        kind="process",
    )
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-parent-swap"), "state_version": None},
    )
    assert preview["status"] == "ready"
    content_target = root.content_root / cast(str, discarded["path"])
    original_parent = content_target.parent
    displaced_parent = root.content_root / "observations-displaced"
    outside_parent = tmp_path / "outside-content"
    outside_parent.mkdir()
    outside_file = outside_parent / content_target.name
    outside_file.write_text("must survive", encoding="utf-8")
    original_unlink = os.unlink
    original_path_unlink = Path.unlink
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        original_parent.rename(displaced_parent)
        original_parent.symlink_to(outside_parent, target_is_directory=True)
        swapped = True

    def swap_parent_before_unlink(path: os.PathLike[str] | str, *, dir_fd: int | None = None) -> None:
        path_text = str(path)
        targets_selected_name = (
            (dir_fd is None and Path(path_text).name == content_target.name)
            or (dir_fd is not None and path_text == content_target.name)
        )
        if targets_selected_name and not swapped:
            swap_parent()
        if dir_fd is None:
            original_unlink(path)
        else:
            original_unlink(path, dir_fd=dir_fd)

    def swap_parent_before_path_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name == content_target.name and not swapped:
            swap_parent()
        original_path_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", swap_parent_before_unlink)
    monkeypatch.setattr(Path, "unlink", swap_parent_before_path_unlink)
    result = await target.apply_retention(preview)

    assert result["status"] == "applied", result["diagnostics"]
    assert swapped is True
    assert outside_file.read_text(encoding="utf-8") == "must survive"
    assert (displaced_parent / content_target.name).exists() is False


def test_descriptor_delete_pruning_does_not_remove_replacement_directory(
    tmp_path,
    monkeypatch,
):
    owned_root = tmp_path / "owned"
    original_parent = owned_root / "parent"
    original_parent.mkdir(parents=True)
    (original_parent / "selected.txt").write_text("selected", encoding="utf-8")
    displaced_parent = owned_root / "parent-displaced"
    original_stat = os.stat
    swapped = False

    def replace_before_prune_stat(
        path: os.PathLike[str] | str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ):
        nonlocal swapped
        if str(path) == "parent" and dir_fd is not None and not swapped:
            original_parent.rename(displaced_parent)
            original_parent.mkdir()
            swapped = True
        return original_stat(
            path,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "stat", replace_before_prune_stat)
    deleted = delete_owned_file_descriptor_relative(
        owned_root,
        "parent/selected.txt",
    )

    assert deleted is True
    assert swapped is True
    assert original_parent.is_dir()
    assert displaced_parent.is_dir()
    assert (displaced_parent / "selected.txt").exists() is False


@pytest.mark.asyncio
async def test_inspect_retention_defers_when_descriptor_relative_delete_is_unsupported(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-dirfd-unsupported")
    target = root.with_scope_node("executions", "exec-dirfd-unsupported")
    await target.put(
        {"process": "requires safe delete"},
        collection="observations",
        kind="process",
    )
    backend = cast(Any, root.backend)
    monkeypatch.setattr(
        backend,
        "_supports_descriptor_relative_delete",
        lambda: False,
        raising=False,
    )

    preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-dirfd-unsupported"),
            "state_version": None,
        },
    )

    _assert_nothing_selected(preview)
    assert [diagnostic.get("code") for diagnostic in preview["diagnostics"]] == [
        "workspace.retention.derived_delete_unsupported"
    ]
    assert preview["diagnostics"][0].get("retryable") is False


@pytest.mark.asyncio
async def test_backend_mutation_lock_serializes_content_and_file_writes_with_retention(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-mutation-lock",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_node("executions", "exec-mutation-lock")
    discarded = await target.put(
        {"process": "lock holder"},
        collection="observations",
        kind="process",
    )
    backend = cast(Any, root.backend)
    provider = backend.vector_store_provider
    await provider.index_record(discarded, [1.0])
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle("exec-mutation-lock"), "state_version": None},
    )
    entered_delete = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete_records = provider.delete_records

    async def pause_vector_delete(record_ids):
        entered_delete.set()
        await release_delete.wait()
        await original_delete_records(record_ids)

    monkeypatch.setattr(provider, "delete_records", pause_vector_delete)
    apply_task = asyncio.create_task(target.apply_retention(preview))
    await asyncio.wait_for(entered_delete.wait(), timeout=2)
    guide_path = target.files_root / "AGENTLY_WORKSPACE.md"
    guide_path.unlink()
    rebound = root.with_scope_node("executions", "exec-mutation-lock")
    guide_recreated_while_locked = (
        rebound.files_root / "AGENTLY_WORKSPACE.md"
    ).exists()
    content_write = asyncio.create_task(
        target.put(
            {"process": "late content"},
            collection="observations",
            kind="process",
        )
    )
    file_write = asyncio.create_task(
        target.write_file("reports/late.txt", "late file")
    )
    await asyncio.sleep(0.05)
    writes_waited = not content_write.done() and not file_write.done()
    release_delete.set()
    applied, late_record, late_file = await asyncio.gather(
        apply_task,
        content_write,
        file_write,
    )

    assert writes_waited is True
    assert guide_recreated_while_locked is False
    assert applied["status"] == "applied"
    assert await backend.get_record(late_record["id"]) == late_record
    assert (target.files_root / late_file["file_refs"][0]["path"]).read_text(
        encoding="utf-8"
    ) == "late file"


@pytest.mark.asyncio
async def test_root_mutation_guard_serializes_same_backend_scratch_and_runtime_event(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-same-backend-mutations",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_node("executions", "exec-same-backend-mutations")
    discarded = await target.put(
        {"process": "lock holder"},
        collection="observations",
        kind="process",
    )
    backend = cast(Any, root.backend)
    provider = backend.vector_store_provider
    await provider.index_record(discarded, [1.0])
    preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-same-backend-mutations"),
            "state_version": None,
        },
    )
    entered_delete = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete_records = provider.delete_records

    async def pause_vector_delete(record_ids):
        entered_delete.set()
        await release_delete.wait()
        await original_delete_records(record_ids)

    monkeypatch.setattr(provider, "delete_records", pause_vector_delete)
    apply_task = asyncio.create_task(target.apply_retention(preview))
    await asyncio.wait_for(entered_delete.wait(), timeout=2)
    event_task = asyncio.create_task(
        target.append_runtime_event(
            "exec-same-backend-mutations",
            {"event_type": "late.event", "event_id": "evt-late-mutation"},
        )
    )
    scratch_task = asyncio.create_task(
        target.open_scratch(cleanup_policy="on_scope_prune")
    )
    await asyncio.sleep(0.05)
    mutations_waited = not event_task.done() and not scratch_task.done()
    release_delete.set()
    applied, event, lease = await asyncio.gather(
        apply_task,
        event_task,
        scratch_task,
    )

    assert mutations_waited is True
    assert applied["status"] == "applied"
    assert event in await root.query_runtime_events("exec-same-backend-mutations")
    assert await backend.get_scratch_lease(cast(str, lease.get("lease_id"))) == lease
    assert Path(cast(str, lease.get("local_path"))).is_dir()


@pytest.mark.asyncio
async def test_root_mutation_guard_is_shared_by_two_local_backends(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-shared-root-mutations",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_node("executions", "exec-shared-root-mutations")
    second_root = WorkspaceManager().create(
        root.root,
        create=False,
        vector_store_provider="sqlite",
    )
    second_target = second_root.with_scope_node(
        "executions",
        "exec-shared-root-mutations",
    )
    discarded = await target.put(
        {"process": "root lock holder"},
        collection="observations",
        kind="process",
    )
    backend = cast(Any, root.backend)
    provider = backend.vector_store_provider
    await provider.index_record(discarded, [1.0])
    preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-shared-root-mutations"),
            "state_version": None,
        },
    )
    entered_delete = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete_records = provider.delete_records

    async def pause_vector_delete(record_ids):
        entered_delete.set()
        await release_delete.wait()
        await original_delete_records(record_ids)

    monkeypatch.setattr(provider, "delete_records", pause_vector_delete)
    apply_task = asyncio.create_task(target.apply_retention(preview))
    await asyncio.wait_for(entered_delete.wait(), timeout=2)
    content_task = asyncio.create_task(
        second_target.put(
            {"process": "late second backend"},
            collection="observations",
            kind="process",
        )
    )
    file_task = asyncio.create_task(
        second_target.write_file("reports/late-second.txt", "late second")
    )
    await asyncio.sleep(0.05)
    mutations_waited = not content_task.done() and not file_task.done()
    release_delete.set()
    applied, late_record, late_file = await asyncio.gather(
        apply_task,
        content_task,
        file_task,
    )

    assert mutations_waited is True
    assert applied["status"] == "applied"
    assert await cast(Any, second_root.backend).get_record(late_record["id"]) == late_record
    assert (
        second_target.files_root / late_file["file_refs"][0]["path"]
    ).read_text(encoding="utf-8") == "late second"


@pytest.mark.asyncio
async def test_root_mutation_guard_holds_compound_checkpoint_as_one_operation(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-compound-checkpoint",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_node("executions", "exec-compound-checkpoint")
    discarded = await target.put(
        {"process": "checkpoint race holder"},
        collection="observations",
        kind="process",
    )
    backend = cast(Any, root.backend)
    provider = backend.vector_store_provider
    await provider.index_record(discarded, [1.0])
    preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-compound-checkpoint"),
            "state_version": None,
        },
    )
    entered_delete = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete_records = provider.delete_records

    async def pause_vector_delete(record_ids):
        entered_delete.set()
        await release_delete.wait()
        await original_delete_records(record_ids)

    checkpoint_record_committed = asyncio.Event()
    release_checkpoint = asyncio.Event()
    original_put = backend.put

    async def pause_after_checkpoint_record(*args: Any, **kwargs: Any):
        ref = await original_put(*args, **kwargs)
        checkpoint_record_committed.set()
        await release_checkpoint.wait()
        return ref

    monkeypatch.setattr(provider, "delete_records", pause_vector_delete)
    monkeypatch.setattr(backend, "put", pause_after_checkpoint_record)
    checkpoint_task = asyncio.create_task(
        target.put_checkpoint(
            "exec-compound-checkpoint",
            {"state_version": 1, "step": "late"},
            expected_state_version=0,
        )
    )
    await asyncio.wait_for(checkpoint_record_committed.wait(), timeout=2)
    apply_task = asyncio.create_task(target.apply_retention(preview))
    await asyncio.sleep(0.05)
    apply_entered_derived_before_checkpoint_commit = entered_delete.is_set()
    apply_waited_for_checkpoint = not apply_task.done()
    release_checkpoint.set()
    release_delete.set()
    applied, checkpoint = await asyncio.gather(apply_task, checkpoint_task)

    assert apply_entered_derived_before_checkpoint_commit is False
    assert apply_waited_for_checkpoint is True
    assert applied["status"] == "deferred"
    assert await root.latest_checkpoint("exec-compound-checkpoint") == checkpoint
    assert await backend.get_record(checkpoint["id"]) == checkpoint
    with backend._connect() as conn:
        checkpoint_row = conn.execute(
            "SELECT record_id FROM checkpoints WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
            ("exec-compound-checkpoint",),
        ).fetchone()
        latest_manifest = backend._manifest_from_conn(
            conn,
            "checkpoint.latest.exec-compound-checkpoint",
            None,
        )
    assert checkpoint_row["record_id"] == checkpoint["id"]
    assert latest_manifest["id"] == checkpoint["id"]


@pytest.mark.asyncio
async def test_root_mutation_guard_coordinates_public_mutations_across_processes(
    tmp_path,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-subprocess-guard",
        vector_store_provider="sqlite",
    )
    execution_id = "exec-subprocess-guard"
    target = root.with_scope_node("executions", execution_id)
    discarded = await target.put(
        {"process": "subprocess lock holder"},
        collection="observations",
        kind="process",
    )
    backend = cast(Any, root.backend)
    await backend.vector_store_provider.index_record(discarded, [1.0])
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(json.dumps(preview), encoding="utf-8")
    worker_path = tmp_path / "workspace_guard_worker.py"
    worker_path.write_text(
        textwrap.dedent(
            """
            import asyncio
            import json
            import sys
            from pathlib import Path

            from agently.core import WorkspaceManager


            async def main():
                role = sys.argv[1]
                root_path = Path(sys.argv[2])
                execution_id = sys.argv[3]
                signal_a = Path(sys.argv[4])
                signal_b = Path(sys.argv[5])
                result_path = Path(sys.argv[6])
                root = WorkspaceManager().create(
                    root_path,
                    create=False,
                    vector_store_provider="sqlite",
                )
                target = root.with_scope_node("executions", execution_id)
                if role == "apply":
                    preview = json.loads(Path(sys.argv[7]).read_text(encoding="utf-8"))
                    provider = root.backend.vector_store_provider
                    original_delete = provider.delete_records

                    async def pause_after_commit(record_ids):
                        signal_a.write_text("ready", encoding="utf-8")
                        while not signal_b.exists():
                            await asyncio.sleep(0.01)
                        await original_delete(record_ids)

                    provider.delete_records = pause_after_commit
                    result = await target.apply_retention(preview)
                    result_path.write_text(json.dumps(result, default=str), encoding="utf-8")
                    return

                signal_a.write_text("started", encoding="utf-8")
                if role == "file":
                    result = await target.write_file(
                        "reports/subprocess-late.txt",
                        "late subprocess file",
                    )
                elif role == "content":
                    record = await target.put(
                        {"process": "late subprocess content"},
                        collection="observations",
                        kind="process",
                        summary="subprocess late content",
                    )
                    result = {"record_id": record["id"]}
                elif role == "event":
                    event = await target.append_runtime_event(
                        execution_id,
                        {"event_type": "late.event", "event_id": "evt-subprocess-late"},
                    )
                    result = {"event_id": event["event_id"]}
                elif role == "scratch":
                    lease = await target.open_scratch(
                        purpose="subprocess-late-scratch",
                        cleanup_policy="on_scope_prune",
                    )
                    result = {
                        "lease_id": lease.get("lease_id"),
                        "scratch_path": lease.get("local_path"),
                    }
                else:
                    raise ValueError(f"unknown worker role: {role}")
                result_path.write_text(
                    json.dumps(result, default=str),
                    encoding="utf-8",
                )


            asyncio.run(main())
            """
        ),
        encoding="utf-8",
    )
    apply_ready = tmp_path / "apply.ready"
    release_apply = tmp_path / "apply.release"
    apply_result = tmp_path / "apply.result.json"
    unused_signal = tmp_path / "mutation.unused"
    mutation_roles = ("file", "content", "event", "scratch")
    mutation_started = {
        role: tmp_path / f"mutation-{role}.started" for role in mutation_roles
    }
    mutation_results = {
        role: tmp_path / f"mutation-{role}.result.json" for role in mutation_roles
    }

    async def wait_for_path(
        path: Path,
        process: asyncio.subprocess.Process,
        *,
        role: str,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 5
        while not path.exists():
            if process.returncode is not None:
                stdout, stderr = await process.communicate()
                pytest.fail(
                    f"{role} worker exited before {path.name}: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            if asyncio.get_running_loop().time() >= deadline:
                process.kill()
                stdout, stderr = await process.communicate()
                pytest.fail(
                    f"{role} worker timed out before {path.name}: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            await asyncio.sleep(0.01)

    async def communicate_worker(
        process: asyncio.subprocess.Process,
        *,
        role: str,
    ) -> tuple[bytes, bytes]:
        try:
            return await asyncio.wait_for(process.communicate(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            pytest.fail(
                f"{role} worker timed out during completion: "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )

    repository_root = Path(__file__).resolve().parents[2]
    subprocess_env = os.environ.copy()
    subprocess_env["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (str(repository_root), subprocess_env.get("PYTHONPATH")),
        )
    )

    apply_process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(worker_path),
        "apply",
        str(root.root),
        execution_id,
        str(apply_ready),
        str(release_apply),
        str(apply_result),
        str(preview_path),
        cwd=str(repository_root),
        env=subprocess_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    mutation_processes: dict[str, asyncio.subprocess.Process] = {}
    try:
        await wait_for_path(apply_ready, apply_process, role="apply")
        for role in mutation_roles:
            mutation_processes[role] = await asyncio.create_subprocess_exec(
                sys.executable,
                str(worker_path),
                role,
                str(root.root),
                execution_id,
                str(mutation_started[role]),
                str(unused_signal),
                str(mutation_results[role]),
                cwd=str(repository_root),
                env=subprocess_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        for role, process in mutation_processes.items():
            await wait_for_path(mutation_started[role], process, role=role)
        await asyncio.sleep(0.3)
        mutations_blocked = {
            role: process.returncode is None and not mutation_results[role].exists()
            for role, process in mutation_processes.items()
        }
        late_file = target.files_root / "reports" / "subprocess-late.txt"
        late_file_exists_before = late_file.exists()
        with backend._connect() as conn:
            late_record_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM records WHERE summary = ?",
                    ("subprocess late content",),
                ).fetchone()["count"]
            )
        late_events_before = await root.query_runtime_events(
            execution_id,
            event_id="evt-subprocess-late",
        )
        late_leases_before = [
            lease
            for lease in await backend.list_scratch_leases()
            if lease.get("purpose") == "subprocess-late-scratch"
        ]
        release_apply.write_text("release", encoding="utf-8")
        apply_stdout, apply_stderr = await communicate_worker(
            apply_process,
            role="apply",
        )
        mutation_outputs = {
            role: await communicate_worker(process, role=role)
            for role, process in mutation_processes.items()
        }
    finally:
        release_apply.touch(exist_ok=True)
        for process in (apply_process, *mutation_processes.values()):
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()

    assert apply_process.returncode == 0, (apply_stdout, apply_stderr)
    for role, process in mutation_processes.items():
        assert process.returncode == 0, mutation_outputs[role]
    assert mutations_blocked == {role: True for role in mutation_roles}
    assert late_file_exists_before is False
    assert late_file.exists()
    assert late_record_count == 0
    assert late_events_before == []
    assert late_leases_before == []
    applied = json.loads(apply_result.read_text(encoding="utf-8"))
    assert applied["status"] == "applied", applied["diagnostics"]
    mutations = {
        role: json.loads(path.read_text(encoding="utf-8"))
        for role, path in mutation_results.items()
    }
    assert await backend.get_record(mutations["content"]["record_id"]) is not None
    assert await root.query_runtime_events(
        execution_id,
        event_id="evt-subprocess-late",
    )
    lease = await backend.get_scratch_lease(mutations["scratch"]["lease_id"])
    assert lease is not None
    assert Path(mutations["scratch"]["scratch_path"]).is_dir()
    lock_path = root.root / ".workspace.mutation.lock"
    assert lock_path.is_file()
    successor_preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    assert lock_path.name not in json.dumps(successor_preview["selected"])


@pytest.mark.asyncio
async def test_retention_inspection_defers_without_advisory_lock_support(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    monkeypatch.setattr(local_backend_module, "_fcntl", None, raising=False)
    monkeypatch.setattr(
        local_backend_module,
        "_msvcrt",
        SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2),
        raising=False,
    )

    root = WorkspaceManager().create(tmp_path / "retention-no-advisory-lock")
    target = root.with_scope_node("executions", "exec-no-advisory-lock")
    await target.put(
        {"process": "ordinary mutation remains available"},
        collection="observations",
        kind="process",
    )

    preview = await target.inspect_retention(
        {},
        lifecycle=_terminal_lifecycle("exec-no-advisory-lock"),
    )

    assert preview["status"] == "deferred"
    assert [diagnostic.get("code") for diagnostic in preview["diagnostics"]] == [
        "workspace.retention.advisory_lock_unsupported"
    ]
    assert preview["diagnostics"][0].get("retryable") is False
    assert cast(Any, root.backend).capabilities()["features"]["supports_retention"] is False
    assert cast(Any, root.backend).capabilities()["features"]["supports_physical_reclamation"] is False
    result = await target.apply_retention(preview)
    assert result["status"] == "deferred"
    assert [diagnostic.get("code") for diagnostic in result["diagnostics"]] == [
        "workspace.retention.advisory_lock_unsupported"
    ]
    _assert_zero_actual_accounting(cast(dict[str, Any], result))


@pytest.mark.asyncio
async def test_advisory_lock_symlink_carrier_defers_without_touching_target(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-lock-symlink")
    execution_id = "exec-lock-symlink"
    target = root.with_scope_node("executions", execution_id)
    await target.put(
        {"process": "unsafe lock carrier"},
        collection="observations",
        kind="process",
    )
    ready = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    assert ready["status"] == "ready"
    lock_path = root.root / ".workspace.mutation.lock"
    lock_path.unlink()
    outside = tmp_path / "outside-lock-target.txt"
    outside.write_bytes(b"outside lock target must stay unchanged")
    lock_path.symlink_to(outside)

    inspected = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    applied = await target.apply_retention(ready)

    assert inspected["status"] == "deferred"
    assert [item.get("code") for item in inspected["diagnostics"]] == [
        "workspace.retention.advisory_lock_invalid"
    ]
    assert inspected["diagnostics"][0].get("retryable") is False
    assert applied["status"] == "deferred"
    assert [item.get("code") for item in applied["diagnostics"]] == [
        "workspace.retention.advisory_lock_invalid"
    ]
    _assert_zero_actual_accounting(cast(dict[str, Any], applied))
    assert outside.read_bytes() == b"outside lock target must stay unchanged"
    assert lock_path.is_symlink()


@pytest.mark.asyncio
async def test_advisory_lock_directory_carrier_returns_typed_apply_deferral(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-lock-directory")
    execution_id = "exec-lock-directory"
    target = root.with_scope_node("executions", execution_id)
    await target.put(
        {"process": "directory lock carrier"},
        collection="observations",
        kind="process",
    )
    ready = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    assert ready["status"] == "ready"
    lock_path = root.root / ".workspace.mutation.lock"
    lock_path.unlink()
    lock_path.mkdir()

    inspected = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    applied = await target.apply_retention(ready)

    assert inspected["status"] == "deferred"
    assert [item.get("code") for item in inspected["diagnostics"]] == [
        "workspace.retention.advisory_lock_invalid"
    ]
    assert applied["status"] == "deferred"
    assert [item.get("code") for item in applied["diagnostics"]] == [
        "workspace.retention.advisory_lock_invalid"
    ]
    _assert_zero_actual_accounting(cast(dict[str, Any], applied))
    assert lock_path.is_dir()


@pytest.mark.asyncio
async def test_advisory_lock_identity_race_returns_typed_apply_deferral(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    root = WorkspaceManager().create(tmp_path / "retention-lock-identity-race")
    execution_id = "exec-lock-identity-race"
    target = root.with_scope_node("executions", execution_id)
    await target.put(
        {"process": "lock identity race"},
        collection="observations",
        kind="process",
    )
    ready = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    original_verify = local_backend_module._PosixAdvisoryLockWaiter._verify_named_identity
    verification_count = 0

    def fail_post_lock_verification(waiter):
        nonlocal verification_count
        verification_count += 1
        if verification_count == 2:
            raise OSError(errno.EIO, "injected post-lock identity read failure")
        return original_verify(waiter)

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "_verify_named_identity",
        fail_post_lock_verification,
    )

    applied = await target.apply_retention(ready)

    assert applied["status"] == "deferred"
    assert [item.get("code") for item in applied["diagnostics"]] == [
        "workspace.retention.advisory_lock_failed"
    ]
    assert applied["diagnostics"][0].get("retryable") is False
    _assert_zero_actual_accounting(cast(dict[str, Any], applied))


@pytest.mark.asyncio
async def test_advisory_lock_waiter_reuses_one_descriptor_with_bounded_backoff(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    if not local_backend_module._NativeAdvisoryLock.supported():
        pytest.skip("native advisory locking is unavailable")
    lock_path = tmp_path / "retention-lock-waiter" / ".workspace.mutation.lock"
    lock_path.parent.mkdir()
    first_guard = local_backend_module._RootMutationGuard(lock_path)
    second_guard = local_backend_module._RootMutationGuard(lock_path)
    original_open_waiter = local_backend_module._PosixAdvisoryLockWaiter.open
    original_flock = local_backend_module._fcntl.flock
    waiters: list[Any] = []
    nonblocking_attempts = 0

    def tracked_open_waiter(cls, path, *, create):
        waiter = original_open_waiter(path, create=create)
        if waiter is not None:
            waiters.append(waiter)
        return waiter

    def tracked_flock(fd, operation):
        nonlocal nonblocking_attempts
        if operation & local_backend_module._fcntl.LOCK_NB:
            nonblocking_attempts += 1
        return original_flock(fd, operation)

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "open",
        classmethod(tracked_open_waiter),
    )
    monkeypatch.setattr(local_backend_module._fcntl, "flock", tracked_flock)

    async def wait_for_second() -> None:
        async with second_guard.acquire():
            raise AssertionError("second guard acquired before cancellation")

    async with first_guard.acquire():
        waiter_task = asyncio.create_task(wait_for_second())
        await asyncio.sleep(0.09)
        assert waiter_task.done() is False
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task

    assert len(waiters) == 2
    assert 3 <= nonblocking_attempts <= 8


@pytest.mark.asyncio
async def test_advisory_lock_waiter_closes_descriptor_on_cancellation(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    if not local_backend_module._NativeAdvisoryLock.supported():
        pytest.skip("native advisory locking is unavailable")
    lock_path = tmp_path / "retention-lock-cancel" / ".workspace.mutation.lock"
    lock_path.parent.mkdir()
    first_guard = local_backend_module._RootMutationGuard(lock_path)
    second_guard = local_backend_module._RootMutationGuard(lock_path)
    original_open_waiter = local_backend_module._PosixAdvisoryLockWaiter.open
    waiters: list[Any] = []
    carrier_fds: list[int] = []

    def tracked_open_waiter(cls, path, *, create):
        waiter = original_open_waiter(path, create=create)
        if waiter is not None:
            waiters.append(waiter)
            carrier_fds.append(waiter._carrier_fd)
        return waiter

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "open",
        classmethod(tracked_open_waiter),
    )

    async def wait_for_second() -> None:
        async with second_guard.acquire():
            raise AssertionError("second guard acquired before cancellation")

    async with first_guard.acquire():
        waiter_task = asyncio.create_task(wait_for_second())
        await asyncio.sleep(0.02)
        assert len(waiters) == 2
        waiter_fd = carrier_fds[1]
        assert os.fstat(waiter_fd).st_ino > 0
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task
        with pytest.raises(OSError) as closed:
            os.fstat(waiter_fd)
        assert closed.value.errno == errno.EBADF


@pytest.mark.asyncio
async def test_advisory_lock_release_failure_preserves_committed_accounting(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    root = WorkspaceManager().create(tmp_path / "retention-lock-release-failure")
    execution_id = "exec-lock-release-failure"
    target = root.with_scope_node("executions", execution_id)
    discarded = await target.put(
        {"process": "committed before lock release"},
        collection="observations",
        kind="process",
    )
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    original_release = local_backend_module._AdvisoryLockHandle.release

    def fail_after_release(handle):
        original_release(handle)
        raise local_backend_module._AdvisoryLockReleaseError(
            "injected release failure after descriptor close"
        )

    monkeypatch.setattr(
        local_backend_module._AdvisoryLockHandle,
        "release",
        fail_after_release,
    )

    result = await target.apply_retention(preview)

    assert result["status"] == "deferred"
    assert [item.get("code") for item in result["diagnostics"]] == [
        "workspace.retention.advisory_lock_release_failed"
    ]
    assert "rolled back" not in str(result["diagnostics"][0].get("message"))
    assert result["accounting"]["logical_bytes_deleted"] > 0
    assert result["accounting"]["entities"]["record_ids"] > 0
    assert await cast(Any, root.backend).get_record(discarded["id"]) is None


@pytest.mark.asyncio
async def test_advisory_handle_root_close_failure_does_not_leak_native_lock(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    root = WorkspaceManager().create(tmp_path / "retention-lock-real-close-failure")
    execution_id = "exec-lock-real-close-failure"
    target = root.with_scope_node("executions", execution_id)
    discarded = await target.put(
        {"process": "real root fd close failure"},
        collection="observations",
        kind="process",
    )
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    original_open_waiter = local_backend_module._PosixAdvisoryLockWaiter.open
    original_close = local_backend_module.os.close
    waiters: list[Any] = []
    root_fds: list[int] = []
    failed = False

    def tracked_open_waiter(cls, path, *, create):
        waiter = original_open_waiter(path, create=create)
        if waiter is not None:
            waiters.append(waiter)
            root_fds.append(waiter._root_fd)
        return waiter

    def fail_first_guard_root_close(fd):
        nonlocal failed
        target_fd = root_fds[0] if root_fds else None
        original_close(fd)
        if not failed and fd == target_fd:
            failed = True
            raise OSError(errno.EIO, "injected guard root fd close failure")

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "open",
        classmethod(tracked_open_waiter),
    )
    monkeypatch.setattr(local_backend_module.os, "close", fail_first_guard_root_close)

    result = await target.apply_retention(preview)
    followup = await asyncio.wait_for(
        target.put(
            {"process": "lock available after close failure"},
            collection="observations",
            kind="process",
        ),
        timeout=1,
    )

    assert failed is True
    assert result["status"] == "deferred"
    assert [item.get("code") for item in result["diagnostics"]] == [
        "workspace.retention.advisory_lock_release_failed"
    ]
    assert result["accounting"]["logical_bytes_deleted"] > 0
    assert await cast(Any, root.backend).get_record(discarded["id"]) is None
    assert await cast(Any, root.backend).get_record(followup["id"]) is not None


@pytest.mark.asyncio
async def test_uncertain_native_release_poisons_root_guard_across_backend_gc(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    workspace_path = tmp_path / "retention-lock-poison"
    root = WorkspaceManager().create(workspace_path)
    execution_id = "exec-lock-poison"
    target = root.with_scope_node("executions", execution_id)
    await target.put(
        {"process": "poison uncertain native release"},
        collection="observations",
        kind="process",
    )
    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )
    original_flock = local_backend_module._fcntl.flock
    original_close = local_backend_module.os.close
    original_open_waiter = local_backend_module._PosixAdvisoryLockWaiter.open
    handles: list[Any] = []
    waiter_open_count = 0

    def tracked_open_waiter(cls, path, *, create):
        nonlocal waiter_open_count
        waiter_open_count += 1
        return original_open_waiter(path, create=create)

    def fail_unlock(fd, operation):
        if operation == local_backend_module._fcntl.LOCK_UN:
            raise OSError(errno.EIO, "injected native unlock failure")
        return original_flock(fd, operation)

    def fail_carrier_close(fd):
        if handles and fd == handles[0].carrier_fd:
            raise OSError(errno.EIO, "injected native carrier close failure")
        return original_close(fd)

    original_set_handle = local_backend_module._RootMutationGuard._set_advisory_handle

    def track_handle(guard, owner, handle):
        handles.append(handle)
        return original_set_handle(guard, owner, handle)

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "open",
        classmethod(tracked_open_waiter),
    )
    monkeypatch.setattr(local_backend_module._fcntl, "flock", fail_unlock)
    monkeypatch.setattr(local_backend_module.os, "close", fail_carrier_close)
    monkeypatch.setattr(
        local_backend_module._RootMutationGuard,
        "_set_advisory_handle",
        track_handle,
    )

    try:
        result = await target.apply_retention(preview)
        opens_after_poison = waiter_open_count
        guard = cast(Any, root.backend)._root_mutation_guard

        def traceback_depth(error: BaseException) -> int:
            depth = 0
            traceback = error.__traceback__
            while traceback is not None:
                depth += 1
                traceback = traceback.tb_next
            return depth

        async_errors: list[BaseException] = []
        async_depths: list[int] = []
        for attempt in range(5):
            with pytest.raises(
                local_backend_module._AdvisoryLockReleaseError,
                match="poison|release",
            ) as caught:
                await asyncio.wait_for(
                    target.put(
                        {"process": f"must fail fast {attempt}"},
                        collection="observations",
                        kind="process",
                    ),
                    timeout=0.1,
                )
            async_errors.append(caught.value)
            async_depths.append(traceback_depth(caught.value))
        assert waiter_open_count == opens_after_poison

        sync_errors: list[BaseException] = []
        sync_depths: list[int] = []
        for _ in range(5):
            with pytest.raises(
                local_backend_module._AdvisoryLockReleaseError,
                match="poison|release",
            ) as caught:
                with guard.try_acquire_sync():
                    pass
            sync_errors.append(caught.value)
            sync_depths.append(traceback_depth(caught.value))

        del target
        del root
        gc.collect()
        backend_errors: list[BaseException] = []
        backend_depths: list[int] = []
        for _ in range(5):
            with pytest.raises(
                local_backend_module._AdvisoryLockReleaseError,
                match="poison|release",
            ) as caught:
                WorkspaceManager().create(workspace_path, create=False)
            backend_errors.append(caught.value)
            backend_depths.append(traceback_depth(caught.value))

        assert result["status"] == "deferred"
        assert [item.get("code") for item in result["diagnostics"]] == [
            "workspace.retention.advisory_lock_release_failed"
        ]
        assert result["accounting"]["logical_bytes_deleted"] > 0
        assert len({id(error) for error in async_errors}) == 5
        assert len({id(error) for error in sync_errors}) == 5
        assert len({id(error) for error in backend_errors}) == 5
        assert len(set(async_depths)) == 1
        assert len(set(sync_depths)) == 1
        assert len(set(backend_depths)) == 1
        assert not any(
            isinstance(value, BaseException) for value in vars(guard).values()
        )
    finally:
        monkeypatch.setattr(local_backend_module._fcntl, "flock", original_flock)
        monkeypatch.setattr(local_backend_module.os, "close", original_close)
        for handle in handles:
            try:
                original_flock(handle.carrier_fd, local_backend_module._fcntl.LOCK_UN)
            except OSError:
                pass
            for fd in (handle.carrier_fd, handle.root_fd):
                try:
                    original_close(fd)
                except OSError:
                    pass


@pytest.mark.asyncio
async def test_advisory_waiter_cancel_close_failure_releases_process_reservation(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    lock_path = tmp_path / "retention-lock-cancel-close" / ".workspace.mutation.lock"
    lock_path.parent.mkdir()
    first_guard = local_backend_module._RootMutationGuard(lock_path)
    second_guard = local_backend_module._RootMutationGuard(lock_path)
    original_open_waiter = local_backend_module._PosixAdvisoryLockWaiter.open
    original_close = local_backend_module.os.close
    waiters: list[Any] = []
    carrier_fds: list[int] = []
    failed = False

    def tracked_open_waiter(cls, path, *, create):
        waiter = original_open_waiter(path, create=create)
        if waiter is not None:
            waiters.append(waiter)
            carrier_fds.append(waiter._carrier_fd)
        return waiter

    def fail_canceled_carrier_close(fd):
        nonlocal failed
        target_fd = carrier_fds[1] if len(carrier_fds) > 1 else None
        original_close(fd)
        if not failed and fd == target_fd:
            failed = True
            raise OSError(errno.EIO, "injected canceled waiter close failure")

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "open",
        classmethod(tracked_open_waiter),
    )
    monkeypatch.setattr(local_backend_module.os, "close", fail_canceled_carrier_close)

    async def contend() -> None:
        async with second_guard.acquire():
            return

    async with first_guard.acquire():
        task = asyncio.create_task(contend())
        while len(waiters) < 2:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await asyncio.wait_for(contend(), timeout=1)
    assert failed is True


def test_descriptor_delete_close_failure_attempts_every_owned_descriptor(
    tmp_path,
    monkeypatch,
):
    stores_module = importlib.import_module("agently.core.Workspace.Stores")
    owned_root = tmp_path / "descriptor-close-all"
    target = owned_root / "a" / "b" / "target.txt"
    target.parent.mkdir(parents=True)
    target.write_text("delete me", encoding="utf-8")
    original_open = stores_module.os.open
    original_close = stores_module.os.close
    opened: list[int] = []
    closed: list[int] = []
    failed = False

    def tracked_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_first_close_after_closing(fd):
        nonlocal failed
        closed.append(fd)
        original_close(fd)
        if not failed:
            failed = True
            raise OSError(errno.EIO, "injected descriptor close failure")

    monkeypatch.setattr(stores_module.os, "open", tracked_open)
    monkeypatch.setattr(stores_module.os, "close", fail_first_close_after_closing)

    with pytest.raises(OSError, match="descriptor close"):
        delete_owned_file_descriptor_relative(owned_root, "a/b/target.txt")

    assert set(closed) == set(opened)
    for fd in opened:
        with pytest.raises(OSError) as released:
            os.fstat(fd)
        assert released.value.errno == errno.EBADF


def test_descriptor_delete_body_error_wins_over_close_error(tmp_path, monkeypatch):
    stores_module = importlib.import_module("agently.core.Workspace.Stores")
    owned_root = tmp_path / "descriptor-body-priority"
    target = owned_root / "a" / "target.txt"
    target.parent.mkdir(parents=True)
    target.write_text("keep me", encoding="utf-8")
    original_close = stores_module.os.close

    def fail_unlink(*_args, **_kwargs):
        raise WorkspacePolicyError("injected delete body failure")

    def close_then_fail(fd):
        original_close(fd)
        raise OSError(errno.EIO, "injected close teardown failure")

    monkeypatch.setattr(stores_module.os, "unlink", fail_unlink)
    monkeypatch.setattr(stores_module.os, "close", close_then_fail)

    with pytest.raises(WorkspacePolicyError, match="delete body failure"):
        delete_owned_file_descriptor_relative(owned_root, "a/target.txt")


def test_advisory_waiter_open_preserves_acquisition_error_and_closes_all_fds(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    lock_path = tmp_path / "waiter-open-cleanup" / ".workspace.mutation.lock"
    lock_path.parent.mkdir()
    original_open = local_backend_module.os.open
    original_close = local_backend_module.os.close
    opened: list[int] = []
    closed: list[int] = []
    failed = False

    def tracked_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close_all_but_fail_first(fd):
        nonlocal failed
        closed.append(fd)
        original_close(fd)
        if not failed:
            failed = True
            raise OSError(errno.EIO, "injected waiter open cleanup failure")

    def fail_identity(_waiter):
        raise local_backend_module._AdvisoryLockCarrierError(
            "injected acquisition identity failure"
        )

    monkeypatch.setattr(local_backend_module.os, "open", tracked_open)
    monkeypatch.setattr(local_backend_module.os, "close", close_all_but_fail_first)
    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "_verify_named_identity",
        fail_identity,
    )

    with pytest.raises(
        local_backend_module._AdvisoryLockCarrierError,
        match="identity failure",
    ):
        local_backend_module._PosixAdvisoryLockWaiter.open(lock_path, create=True)

    assert set(closed) == set(opened)


def test_advisory_waiter_missing_preflight_close_failure_is_typed(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    lock_path = tmp_path / "waiter-missing-cleanup" / ".workspace.mutation.lock"
    lock_path.parent.mkdir()
    original_close = local_backend_module.os.close

    def close_then_fail(fd):
        original_close(fd)
        raise OSError(errno.EIO, "injected missing preflight close failure")

    monkeypatch.setattr(local_backend_module.os, "close", close_then_fail)

    with pytest.raises(
        local_backend_module._AdvisoryLockAcquisitionError,
        match="close",
    ):
        local_backend_module._PosixAdvisoryLockWaiter.open(lock_path, create=False)


def test_sync_advisory_guard_releases_reservation_once_when_cleanup_also_fails(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    lock_path = tmp_path / "sync-waiter-cleanup" / ".workspace.mutation.lock"
    lock_path.parent.mkdir()
    guard = local_backend_module._RootMutationGuard(lock_path)

    def fail_acquire(_waiter):
        raise local_backend_module._AdvisoryLockAcquisitionError(
            "injected sync acquisition failure"
        )

    def fail_close(_waiter):
        raise local_backend_module._AdvisoryLockAcquisitionError(
            "injected sync cleanup failure"
        )

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "try_acquire",
        fail_acquire,
    )
    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "close",
        fail_close,
    )

    with pytest.raises(
        local_backend_module._AdvisoryLockAcquisitionError,
        match="acquisition failure",
    ):
        with guard.try_acquire_sync():
            pass

    assert guard._owner is None
    assert guard._depth == 0


@pytest.mark.asyncio
async def test_retention_preflight_waiter_close_failure_returns_typed_deferral(
    tmp_path,
    monkeypatch,
):
    local_backend_module = importlib.import_module(
        "agently.core.Workspace.LocalBackend"
    )
    root = WorkspaceManager().create(tmp_path / "retention-preflight-close-failure")
    execution_id = "exec-preflight-close-failure"
    target = root.with_scope_node("executions", execution_id)
    await target.put(
        {"process": "preflight close failure"},
        collection="observations",
        kind="process",
    )

    def fail_close(_waiter):
        raise local_backend_module._AdvisoryLockAcquisitionError(
            "injected preflight waiter close failure"
        )

    monkeypatch.setattr(
        local_backend_module._PosixAdvisoryLockWaiter,
        "close",
        fail_close,
    )

    preview = await target.inspect_retention(
        {},
        lifecycle={**_terminal_lifecycle(execution_id), "state_version": None},
    )

    assert preview["status"] == "deferred"
    assert [item.get("code") for item in preview["diagnostics"]] == [
        "workspace.retention.advisory_lock_failed"
    ]


@pytest.mark.asyncio
async def test_workspace_profile_can_fan_out_public_puts_without_guard_deadlock(tmp_path):
    manager = WorkspaceManager()
    workspace = manager.create(tmp_path / "retention-profile-fanout")

    class ParallelProfile:
        async def ingest(self, *, workspace, **_kwargs: Any):
            return await asyncio.gather(
                workspace.put(
                    {"parallel": "left"},
                    collection="observations",
                    kind="parallel",
                    summary="parallel profile left",
                ),
                workspace.put(
                    {"parallel": "right"},
                    collection="observations",
                    kind="parallel",
                    summary="parallel profile right",
                ),
            )

    manager.register_profile("parallel", cast(Any, ParallelProfile()))
    refs = cast(
        list[dict[str, Any]],
        await asyncio.wait_for(
            workspace.put(
                {"request": "fan out"},
                collection="observations",
                profile="parallel",
            ),
            timeout=1,
        ),
    )

    assert len(refs) == 2
    assert {ref["summary"] for ref in refs} == {
        "parallel profile left",
        "parallel profile right",
    }
    resolved = [
        await workspace.get(cast(WorkspaceRecordRef, ref)) for ref in refs
    ]
    assert all(value is not None for value in resolved)


@pytest.mark.asyncio
async def test_physical_reclamation_below_threshold_reports_pending_blocks(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-physical-pending")
    execution_id = "exec-physical-pending"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    backend._retention_full_vacuum_min_bytes = 1 << 40
    backend._retention_full_vacuum_ratio = 2.0
    checkpoint_modes: list[str] = []
    original_checkpoint = backend._checkpoint_sqlite_wal_sync

    def track_checkpoint(mode: str):
        checkpoint_modes.append(mode)
        return original_checkpoint(mode)

    monkeypatch.setattr(backend, "_checkpoint_sqlite_wal_sync", track_checkpoint)
    await _seed_large_sqlite_retention_scope(target)
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )
    before = _sqlite_allocated_bytes(backend.db_path)

    result = await target.apply_retention(preview)
    after = _sqlite_allocated_bytes(backend.db_path)

    assert result["status"] == "applied"
    assert result["accounting"]["physical_bytes_reclaimed"] == max(0, before - after)
    assert result["accounting"]["physical_bytes_pending"] > 0
    manifest_ref = result["manifest_ref"]
    assert manifest_ref is not None
    assert manifest_ref["meta"]["accounting"] == result["accounting"]
    lock_path = root.root / ".workspace.mutation.lock"
    assert lock_path.is_file()
    assert lock_path.stat().st_blocks * 512 >= 0
    assert "TRUNCATE" not in checkpoint_modes


@pytest.mark.asyncio
async def test_forced_full_vacuum_reports_exact_allocated_block_reclaim(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-physical-full")
    execution_id = "exec-physical-full"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    backend._retention_full_vacuum_min_bytes = 1
    backend._retention_full_vacuum_ratio = 0.0
    checkpoint_modes: list[str] = []
    allocation_measurements = 0
    original_checkpoint = backend._checkpoint_sqlite_wal_sync
    original_allocated = backend._sqlite_allocated_bytes

    def track_checkpoint(mode: str):
        checkpoint_modes.append(mode)
        return original_checkpoint(mode)

    def track_allocated():
        nonlocal allocation_measurements
        allocation_measurements += 1
        return original_allocated()

    monkeypatch.setattr(backend, "_checkpoint_sqlite_wal_sync", track_checkpoint)
    monkeypatch.setattr(backend, "_sqlite_allocated_bytes", track_allocated)
    await _seed_large_sqlite_retention_scope(target, count=64)
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )
    before = _sqlite_allocated_bytes(backend.db_path)

    result = await target.apply_retention(preview)
    after = _sqlite_allocated_bytes(backend.db_path)

    assert result["status"] == "applied"
    assert before > after
    assert result["accounting"]["physical_bytes_reclaimed"] == before - after
    assert result["accounting"]["physical_bytes_pending"] >= 0
    manifest_ref = result["manifest_ref"]
    assert manifest_ref is not None
    assert manifest_ref["meta"]["accounting"] == result["accounting"]
    assert checkpoint_modes == ["TRUNCATE"]
    assert allocation_measurements == 2


@pytest.mark.asyncio
async def test_incremental_vacuum_is_bounded_and_keeps_remaining_pending(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-physical-incremental")
    execution_id = "exec-physical-incremental"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    with backend._connect() as conn:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")
    backend._retention_incremental_vacuum_pages = 1
    await _seed_large_sqlite_retention_scope(target, count=64)
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )

    result = await target.apply_retention(preview)

    with backend._connect() as conn:
        auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    assert auto_vacuum == 2
    assert result["status"] == "applied"
    assert freelist > 0
    assert result["accounting"]["physical_bytes_pending"] == max(
        0,
        freelist * page_size - result["accounting"]["physical_bytes_reclaimed"],
    )


@pytest.mark.asyncio
async def test_physical_maintenance_failure_preserves_logical_apply(tmp_path, monkeypatch):
    root = WorkspaceManager().create(tmp_path / "retention-physical-failure")
    execution_id = "exec-physical-failure"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    discarded = await target.put(
        {"row": "maintenance failure"},
        collection="observations",
        kind="large_sqlite_row",
        meta={"padding": "x" * 65536},
    )
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )

    def fail_maintenance(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is busy during maintenance")

    monkeypatch.setattr(
        backend,
        "_run_sqlite_physical_maintenance_sync",
        fail_maintenance,
        raising=False,
    )
    result = await target.apply_retention(preview)

    assert result["status"] == "applied"
    assert await backend.get_record(discarded["id"]) is None
    assert [item.get("code") for item in result["diagnostics"]] == [
        "workspace.retention.physical_maintenance_failed"
    ]
    assert result["diagnostics"][0].get("retryable") is True
    assert result["accounting"]["logical_bytes_deleted"] > 0
    assert result["accounting"]["physical_bytes_pending"] > 0


@pytest.mark.asyncio
async def test_physical_measurement_failure_is_postcommit_typed_deferred(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-physical-measurement-failure")
    execution_id = "exec-physical-measurement-failure"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    discarded = await target.put(
        {"row": "measurement failure"},
        collection="observations",
        kind="large_sqlite_row",
        meta={"padding": "x" * 65536},
    )
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )

    def fail_measurement():
        raise OSError(errno.EIO, "injected allocated-block measurement failure")

    monkeypatch.setattr(backend, "_sqlite_pending_bytes_sync", fail_measurement)
    result = await target.apply_retention(preview)

    assert result["status"] == "deferred"
    assert await backend.get_record(discarded["id"]) is None
    assert [item.get("code") for item in result["diagnostics"]] == [
        "workspace.retention.physical_measurement_failed"
    ]
    assert "rolled back" not in str(result["diagnostics"][0].get("message"))
    assert result["accounting"]["logical_bytes_deleted"] > 0


@pytest.mark.asyncio
async def test_physical_accounting_cas_failure_is_postcommit_deferred(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-physical-cas-failure")
    execution_id = "exec-physical-cas-failure"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    backend._retention_full_vacuum_min_bytes = 1 << 40
    backend._retention_full_vacuum_ratio = 2.0
    discarded = await target.put(
        {"row": "physical accounting CAS failure"},
        collection="observations",
        kind="large_sqlite_row",
        meta={"padding": "x" * 131072},
    )
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )
    original_update = backend._update_terminal_manifest_meta

    def fail_physical_update(manifest_ref, meta):
        accounting = cast(dict[str, Any], meta["accounting"])
        if (
            manifest_ref["meta"]["state"] == "applied"
            and accounting["physical_bytes_pending"] > 0
        ):
            raise sqlite3.OperationalError("injected physical accounting CAS failure")
        return original_update(manifest_ref, meta)

    monkeypatch.setattr(backend, "_update_terminal_manifest_meta", fail_physical_update)
    result = await target.apply_retention(preview)

    assert result["status"] == "deferred"
    assert await backend.get_record(discarded["id"]) is None
    assert [item.get("code") for item in result["diagnostics"]] == [
        "workspace.retention.physical_accounting_persist_failed"
    ]
    assert "rolled back" not in str(result["diagnostics"][0].get("message"))
    assert result["accounting"]["logical_bytes_deleted"] > 0
    assert result["accounting"]["physical_bytes_pending"] > 0


@pytest.mark.asyncio
async def test_physical_accounting_readback_mismatch_returns_persisted_conflict(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-physical-readback-conflict")
    execution_id = "exec-physical-readback-conflict"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    backend._retention_full_vacuum_min_bytes = 1 << 40
    backend._retention_full_vacuum_ratio = 2.0
    await _seed_large_sqlite_retention_scope(target, count=48)
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )
    original_update = backend._update_terminal_manifest_meta

    def return_equal_total_stale_distribution(manifest_ref, meta):
        accounting = cast(dict[str, Any], meta["accounting"])
        if manifest_ref["meta"]["state"] == "applied" and (
            accounting["physical_bytes_reclaimed"]
            or accounting["physical_bytes_pending"]
        ):
            stale = json.loads(json.dumps(manifest_ref))
            total = (
                accounting["physical_bytes_reclaimed"]
                + accounting["physical_bytes_pending"]
            )
            stale["meta"]["accounting"]["physical_bytes_reclaimed"] = total
            stale["meta"]["accounting"]["physical_bytes_pending"] = 0
            return stale
        return original_update(manifest_ref, meta)

    monkeypatch.setattr(
        backend,
        "_update_terminal_manifest_meta",
        return_equal_total_stale_distribution,
    )
    result = await target.apply_retention(preview)

    assert result["status"] == "deferred"
    assert [item.get("code") for item in result["diagnostics"]] == [
        "workspace.retention.physical_accounting_conflict"
    ]
    manifest_ref = result["manifest_ref"]
    assert manifest_ref is not None
    assert result["accounting"] == manifest_ref["meta"]["accounting"]
    detail = result["diagnostics"][0].get("detail")
    assert detail is not None
    assert detail["measured_accounting"] != result["accounting"]


@pytest.mark.asyncio
async def test_cancelled_apply_holds_root_guard_until_maintenance_worker_exits(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-maintenance-cancel")
    execution_id = "exec-maintenance-cancel"
    target = root.with_scope_node("executions", execution_id)
    backend = cast(Any, root.backend)
    second_root = WorkspaceManager().create(root.root, create=False)
    second_target = second_root.with_scope_node("executions", execution_id)
    await target.put(
        {"row": "cancel maintenance"},
        collection="observations",
        kind="large_sqlite_row",
        meta={"padding": "x" * 65536},
    )
    preview = await target.inspect_retention(
        {}, lifecycle={**_terminal_lifecycle(execution_id), "state_version": None}
    )
    maintenance_entered = threading.Event()
    maintenance_release = threading.Event()
    maintenance_exited = threading.Event()
    allocated_values = iter((100_000, 90_000, 90_000))

    def block_maintenance():
        maintenance_entered.set()
        maintenance_release.wait(timeout=5)
        maintenance_exited.set()

    monkeypatch.setattr(
        backend,
        "_run_sqlite_physical_maintenance_sync",
        block_maintenance,
    )
    monkeypatch.setattr(backend, "_sqlite_allocated_bytes", lambda: next(allocated_values))
    monkeypatch.setattr(backend, "_sqlite_pending_bytes_sync", lambda: 24_000)
    apply_task = asyncio.create_task(target.apply_retention(preview))
    while not maintenance_entered.is_set():
        await asyncio.sleep(0.001)
    apply_task.cancel()
    competing_task = asyncio.create_task(
        second_target.put(
            {"row": "must wait for maintenance"},
            collection="observations",
            kind="large_sqlite_row",
        )
    )
    await asyncio.sleep(0.01)
    apply_task.cancel()
    await asyncio.sleep(0.05)
    apply_done_before_release = apply_task.done()
    competing_done_before_release = competing_task.done()

    maintenance_release.set()
    with pytest.raises(asyncio.CancelledError):
        await apply_task
    competing_ref = await asyncio.wait_for(competing_task, timeout=2)

    assert apply_done_before_release is False
    assert competing_done_before_release is False
    assert maintenance_exited.is_set()
    assert await cast(Any, second_root.backend).get_record(competing_ref["id"]) is not None
    with backend._connect() as conn:
        manifest = backend._terminal_manifest_from_conn(
            conn,
            execution_id=execution_id,
        )
    assert manifest is not None
    assert manifest["meta"]["accounting"]["physical_bytes_reclaimed"] == 10_000
    assert manifest["meta"]["accounting"]["physical_bytes_pending"] == 14_000
    repeated = await target.apply_retention(preview)
    assert repeated["status"] == "noop"
    assert repeated["accounting"]["physical_bytes_reclaimed"] == 0
    with backend._connect() as conn:
        manifest_after_repeated_apply = backend._terminal_manifest_from_conn(
            conn,
            execution_id=execution_id,
        )
    assert manifest_after_repeated_apply is not None
    assert (
        manifest_after_repeated_apply["meta"]["accounting"][
            "physical_bytes_reclaimed"
        ]
        == 10_000
    )
    assert (
        manifest_after_repeated_apply["meta"]["accounting"]["physical_bytes_pending"]
        == 14_000
    )


@pytest.mark.asyncio
async def test_postcommit_finalization_consumes_worker_error_before_cancellation(
    tmp_path,
):
    root = WorkspaceManager().create(tmp_path / "retention-cancel-worker-error")
    backend = cast(Any, root.backend)
    worker_entered = asyncio.Event()
    worker_release = asyncio.Event()
    unhandled_contexts: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled_contexts.append(context))

    async def fail_after_release() -> None:
        worker_entered.set()
        await worker_release.wait()
        raise AssertionError("injected finalization programming error")

    finalization_task = asyncio.create_task(
        backend._await_completion_before_cancellation(fail_after_release())
    )
    try:
        await worker_entered.wait()
        finalization_task.cancel()
        worker_release.set()
        with pytest.raises(asyncio.CancelledError):
            await finalization_task
        await asyncio.sleep(0)
        assert unhandled_contexts == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_apply_retention_does_not_convert_programming_errors_to_deferred(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(tmp_path / "retention-programming-error")
    target = root.with_scope_node("executions", "exec-programming-error")
    await target.put(
        {"process": "programming error"},
        collection="observations",
        kind="process",
    )
    preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-programming-error"),
            "state_version": None,
        },
    )
    backend = cast(Any, root.backend)

    async def fail_with_programming_error(_relative_path: str) -> bool:
        raise AssertionError("injected programming error")

    monkeypatch.setattr(backend.content, "delete_content", fail_with_programming_error)
    with pytest.raises(AssertionError, match="injected programming error"):
        await target.apply_retention(preview)


@pytest.mark.asyncio
async def test_stale_old_worker_cannot_overwrite_new_fingerprint_manifest(
    tmp_path,
    monkeypatch,
):
    root = WorkspaceManager().create(
        tmp_path / "retention-stale-worker-cas",
        vector_store_provider="sqlite",
    )
    target = root.with_scope_node("executions", "exec-stale-worker-cas")
    discarded = await target.put(
        {"process": "old worker"},
        collection="observations",
        kind="process",
    )
    backend = cast(Any, root.backend)
    provider = backend.vector_store_provider
    await provider.index_record(discarded, [1.0])
    preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-stale-worker-cas"),
            "state_version": None,
        },
    )
    second_root = WorkspaceManager().create(
        root.root,
        create=False,
        vector_store_provider="sqlite",
    )
    second_target = second_root.with_scope_node(
        "executions",
        "exec-stale-worker-cas",
    )
    second_backend = cast(Any, second_root.backend)
    entered_delete = asyncio.Event()
    release_delete = asyncio.Event()
    original_delete_records = provider.delete_records

    async def pause_old_worker(record_ids):
        entered_delete.set()
        await release_delete.wait()
        await original_delete_records(record_ids)

    monkeypatch.setattr(provider, "delete_records", pause_old_worker)
    old_task = asyncio.create_task(target.apply_retention(preview))
    await asyncio.wait_for(entered_delete.wait(), timeout=2)
    # Simulate an independent process that is outside this process's
    # root-shared cooperative guard. The private entry still executes the real
    # SQLite transaction and terminal-manifest CAS; only the in-process guard
    # acquisition is deliberately bypassed for this inter-process race probe.
    same_plan = await second_backend._apply_retention_unlocked(preview)
    assert same_plan["status"] == "applied"
    successor_preview = await second_target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-stale-worker-cas"),
            "state_version": None,
            "terminal_at": "2026-07-11T00:01:00+00:00",
        },
    )
    assert successor_preview["plan_fingerprint"] != preview["plan_fingerprint"]
    successor = await second_backend._apply_retention_unlocked(successor_preview)
    assert successor["status"] == "applied"
    release_delete.set()
    old_result = await old_task

    assert old_result["status"] == "deferred"
    assert [diagnostic.get("code") for diagnostic in old_result["diagnostics"]] == [
        "workspace.retention.plan_conflict"
    ]
    _assert_zero_actual_accounting(cast(dict[str, Any], old_result))
    with cast(Any, second_root.backend)._connect() as conn:
        persisted = cast(Any, second_root.backend)._terminal_manifest_from_conn(
            conn,
            execution_id="exec-stale-worker-cas",
        )
    assert persisted["meta"]["plan_fingerprint"] == successor_preview["plan_fingerprint"]


@pytest.mark.asyncio
async def test_same_fingerprint_stale_manifest_updates_merge_monotonically(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-same-plan-cas")
    backend = cast(Any, root.backend)
    lifecycle = cast(
        WorkspaceRetentionLifecycle,
        {
            **_terminal_lifecycle("exec-same-plan-cas"),
            "state_version": None,
        },
    )
    preview = await root.inspect_retention(
        {"execution_id": "exec-same-plan-cas"},
        lifecycle=lifecycle,
    )
    base_meta = {
        "schema_version": "agently.workspace.terminal_manifest.v1",
        "plan_fingerprint": preview["plan_fingerprint"],
        "state": "derived_pending",
        "lifecycle": lifecycle,
        "retained_refs": [],
        "inline_result": None,
        "accounting": preview["accounting"],
        "derived_cleanup": {
            "pending": {
                "vector_record_ids": [],
                "content_paths": ["observations/content.json"],
                "file_paths": ["lineage/executions/exec-same-plan-cas/file.txt"],
                "scratch_paths": [],
            },
            "attempts": 0,
            "last_error": None,
        },
    }
    with backend._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        base_ref = backend._write_terminal_manifest_on_conn(
            conn,
            execution_id="exec-same-plan-cas",
            scope={"execution_id": "exec-same-plan-cas"},
            meta=base_meta,
        )
        conn.commit()

    first_meta = json.loads(json.dumps(base_meta))
    first_meta["derived_cleanup"]["pending"]["content_paths"] = []
    first_meta["derived_cleanup"]["attempts"] = 2
    stale_meta = json.loads(json.dumps(base_meta))
    stale_meta["derived_cleanup"]["pending"]["file_paths"] = []
    stale_meta["derived_cleanup"]["attempts"] = 1
    second_root = WorkspaceManager().create(root.root, create=False)
    second_backend = cast(Any, second_root.backend)
    await asyncio.gather(
        asyncio.to_thread(
            backend._update_terminal_manifest_meta,
            base_ref,
            first_meta,
        ),
        asyncio.to_thread(
            second_backend._update_terminal_manifest_meta,
            base_ref,
            stale_meta,
        ),
    )
    with backend._connect() as conn:
        merged = backend._terminal_manifest_from_conn(
            conn,
            execution_id="exec-same-plan-cas",
        )

    assert merged is not None
    assert merged["meta"]["derived_cleanup"]["pending"] == {
        "vector_record_ids": [],
        "content_paths": [],
        "file_paths": [],
        "scratch_paths": [],
    }
    assert merged["meta"]["derived_cleanup"]["attempts"] == 2
    applied_meta = json.loads(json.dumps(merged["meta"]))
    applied_meta["state"] = "applied"
    applied_meta["derived_cleanup"]["attempts"] = 3
    await asyncio.gather(
        asyncio.to_thread(
            backend._update_terminal_manifest_meta,
            merged,
            applied_meta,
        ),
        asyncio.to_thread(
            second_backend._update_terminal_manifest_meta,
            base_ref,
            base_meta,
        ),
    )
    with backend._connect() as conn:
        final = backend._terminal_manifest_from_conn(
            conn,
            execution_id="exec-same-plan-cas",
        )
    assert final is not None
    assert final["meta"]["state"] == "applied"
    assert final["meta"]["derived_cleanup"]["pending"] == {
        "vector_record_ids": [],
        "content_paths": [],
        "file_paths": [],
        "scratch_paths": [],
    }
    assert final["meta"]["derived_cleanup"]["attempts"] == 3


def test_same_plan_cas_preserves_equal_total_persisted_physical_distribution():
    pending = {
        "vector_record_ids": [],
        "content_paths": [],
        "file_paths": [],
        "scratch_paths": [],
    }
    current = {
        "plan_fingerprint": "a" * 64,
        "state": "applied",
        "derived_cleanup": {"pending": pending, "attempts": 1, "last_error": None},
        "accounting": {
            "entities": {},
            "logical_bytes_deleted": 100,
            "physical_bytes_reclaimed": 4096,
            "physical_bytes_pending": 8192,
        },
    }
    proposed = json.loads(json.dumps(current))
    proposed["accounting"]["physical_bytes_reclaimed"] = 8192
    proposed["accounting"]["physical_bytes_pending"] = 4096

    merged = cast(Any, LocalWorkspaceBackend)._merge_terminal_manifest_meta(
        current,
        proposed,
    )

    assert merged["accounting"] == current["accounting"]


@pytest.mark.asyncio
async def test_successive_fingerprints_do_not_embed_terminal_manifest_ledgers(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-ledger-bounds")
    target = root.with_scope_node("executions", "exec-ledger-bounds")
    retained = await target.put(
        {"deliverable": "bounded"},
        collection="artifacts",
        kind="report",
    )
    await target.put(
        {"process": "discard"},
        collection="observations",
        kind="process",
    )
    initial_preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-ledger-bounds"),
            "state_version": None,
        },
        retained_refs=[retained],
    )
    initial = await target.apply_retention(initial_preview)
    assert initial["status"] == "applied"
    manifest_sizes: list[int] = []
    terminal_refs_seen: list[bool] = []
    for index in range(1, 7):
        preview = await target.inspect_retention(
            {},
            lifecycle={
                **_terminal_lifecycle("exec-ledger-bounds"),
                "state_version": index,
            },
            retained_refs=[retained],
        )
        terminal_refs_seen.append(
            any(
                ref.get("kind") == "workspace_terminal_manifest"
                for ref in cast(list[dict[str, Any]], preview["retained_refs"])
            )
        )
        result = await target.apply_retention(preview)
        assert result["status"] == "applied"
        manifest = cast(dict[str, Any], result["manifest_ref"])
        manifest_sizes.append(len(json.dumps(manifest["meta"], sort_keys=True)))

    assert terminal_refs_seen == [False] * 6
    assert max(manifest_sizes) - min(manifest_sizes) < 512


@pytest.mark.asyncio
async def test_corrupt_terminal_manifest_returns_typed_nonretryable_deferred(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-ledger-corrupt")
    target = root.with_scope_node("executions", "exec-ledger-corrupt")
    await target.put(
        {"process": "corrupt ledger"},
        collection="observations",
        kind="process",
    )
    preview = await target.inspect_retention(
        {},
        lifecycle={
            **_terminal_lifecycle("exec-ledger-corrupt"),
            "state_version": None,
        },
    )
    applied = await target.apply_retention(preview)
    assert applied["status"] == "applied"
    manifest_id = cast(dict[str, Any], applied["manifest_ref"])["id"]
    backend = cast(Any, root.backend)
    with backend._connect() as conn:
        conn.execute(
            "UPDATE records SET meta_json = ? WHERE id = ?",
            (json.dumps({"schema_version": "broken"}), manifest_id),
        )
        conn.commit()

    result = await target.apply_retention(preview)

    assert result["status"] == "deferred"
    assert [diagnostic.get("code") for diagnostic in result["diagnostics"]] == [
        "workspace.retention.ledger_invalid"
    ]
    assert result["diagnostics"][0].get("retryable") is False
    _assert_zero_actual_accounting(cast(dict[str, Any], result))


@pytest.mark.asyncio
async def test_prune_scope_remains_unconditional_and_creates_no_terminal_manifest(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-prune-scope")
    target = root.with_scope_node("executions", "exec-prune")
    sibling = root.with_scope_node("executions", "exec-prune-sibling")
    target_ref = await target.put(
        {"target": True},
        collection="artifacts",
        kind="retained_if_policy_ran",
    )
    sibling_ref = await sibling.put(
        {"sibling": True},
        collection="artifacts",
        kind="sibling",
    )
    await target.write_file("reports/target.txt", "target")
    sibling_write = await sibling.write_file("reports/sibling.txt", "sibling")
    sibling_file = sibling.files_root / str(sibling_write["file_refs"][0]["path"])

    result = await root.prune_scope({"execution_id": "exec-prune"})

    assert result["records_deleted"] == 1
    backend = cast(Any, root.backend)
    assert await backend.get_record(target_ref["id"]) is None
    assert await backend.get_record(sibling_ref["id"]) == sibling_ref
    assert target.files_root.exists() is False
    assert sibling_file.read_bytes() == b"sibling"
    with backend._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM records WHERE kind = 'workspace_terminal_manifest'"
        ).fetchone() is None


def test_local_retention_capabilities_require_inspect_and_apply_methods(tmp_path):
    root = WorkspaceManager().create(tmp_path / "retention-capabilities")
    backend = cast(Any, root.backend)
    assert backend.capabilities()["features"]["supports_retention"] is True
    assert backend.capabilities()["features"]["supports_physical_reclamation"] is True

    async def anchors_only(*_args: Any, **_kwargs: Any):
        return []

    backend.db_store_provider = SimpleNamespace(
        add_retention_anchor=anchors_only,
        retention_anchors=anchors_only,
    )
    assert backend.capabilities()["features"]["supports_retention"] is False
    assert backend.capabilities()["features"]["supports_physical_reclamation"] is False
