from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently.core import LazyWorkspace, WorkspaceManager
from agently.core.Workspace.Retention import (
    canonical_retention_fingerprint,
    resolve_retention_policy,
    serialized_size,
    stable_checkpoint_row_identities,
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
