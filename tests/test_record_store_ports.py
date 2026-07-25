from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

import pytest

from agently.core.storage import (
    RecordStore,
    RecordStoreContextSource,
    RecordStoreRegistry,
)
from agently.types.plugins import ExecutionSnapshotRetentionStore


@pytest.mark.asyncio
async def test_record_store_is_record_only_and_supports_snapshot_port(tmp_path) -> None:
    store = RecordStore(tmp_path, mode="read_write")._bind_execution(
        "run-1",
        scope={"execution_id": "run-1"},
        search_scope={"execution_id": "run-1"},
    )

    record = await store.put("observed fact", collection="evidence", kind="fact")
    await store.put_snapshot("run-1", {"step": 2})

    assert await store.get_data(record) == "observed fact"
    assert await store.get_snapshot("run-1") == {"step": 2}
    assert not hasattr(store, "read_file")
    assert not hasattr(store, "grep_files")
    assert not hasattr(store, "build_context")
    assert isinstance(store, ExecutionSnapshotRetentionStore)


@pytest.mark.asyncio
async def test_local_record_store_keeps_latest_three_snapshots_by_default(tmp_path) -> None:
    store = RecordStore(tmp_path, mode="read_write")

    for version in range(5):
        await store.put_snapshot(
            "run-retained",
            {"state_version": version, "value": f"snapshot-{version}"},
        )

    history = await store.checkpoint_history("run-retained")

    assert [ref["meta"]["state_version"] for ref in history] == [4, 3, 2]
    assert await store.get_snapshot("run-retained") == {
        "state_version": 4,
        "value": "snapshot-4",
    }


@pytest.mark.asyncio
async def test_record_store_snapshot_retention_configuration_overrides_local_default(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": 2},
    )

    for version in range(4):
        await store.put_snapshot(
            "run-configured",
            {"state_version": version},
        )

    history = await store.checkpoint_history("run-configured")

    assert [ref["meta"]["state_version"] for ref in history] == [3, 2]


@pytest.mark.asyncio
async def test_record_store_snapshot_retention_can_disable_automatic_prune(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": None},
    )

    for version in range(5):
        await store.put_snapshot(
            "run-unbounded",
            {"state_version": version},
        )

    assert len(await store.checkpoint_history("run-unbounded")) == 5


@pytest.mark.asyncio
async def test_record_store_prune_snapshots_keeps_latest_recovery_point_and_reports_reclaim(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": None},
    )
    for version in range(4):
        await store.put_snapshot(
            "run-pruned",
            {"state_version": version, "body": "x" * 128},
        )

    result = await store.prune_snapshots("run-pruned", keep_last=1)
    history = await store.checkpoint_history("run-pruned")

    assert result["run_id"] == "run-pruned"
    assert result["keep_last"] == 1
    assert result["retained_records"] == 1
    assert result["deleted_records"] == 3
    assert result["deleted_bytes"] > 0
    assert [ref["meta"]["state_version"] for ref in history] == [3]
    latest = await store.latest_snapshot("run-pruned")
    assert latest is not None
    assert latest["id"] == history[0]["id"]


@pytest.mark.asyncio
async def test_record_store_generic_checkpoint_history_is_not_automatically_pruned(
    tmp_path,
) -> None:
    store = RecordStore(tmp_path, mode="read_write")

    for version in range(5):
        await store.put_checkpoint(
            "generic-checkpoints",
            {"state_version": version},
        )

    history = await store.checkpoint_history("generic-checkpoints")

    assert [ref["meta"]["state_version"] for ref in history] == [4, 3, 2, 1, 0]


class _LegacySnapshotBackend:
    def __init__(self, root: Path):
        self.root = root
        self.read_only = False
        self.put_calls = 0

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        _ = (run_id, state, step_id, expected_state_version)
        self.put_calls += 1
        return {"id": "legacy-snapshot"}


@pytest.mark.asyncio
async def test_record_store_keeps_legacy_snapshot_provider_compatible_without_retention_request(
    tmp_path,
) -> None:
    backend = _LegacySnapshotBackend(tmp_path)
    store = RecordStore(cast(Any, backend), mode="read_write")

    ref = await store.put_snapshot("legacy-run", {"state_version": 1})

    assert ref["id"] == "legacy-snapshot"
    assert backend.put_calls == 1


@pytest.mark.asyncio
async def test_record_store_rejects_explicit_retention_before_calling_unsupported_provider(
    tmp_path,
) -> None:
    backend = _LegacySnapshotBackend(tmp_path)
    store = RecordStore(
        cast(Any, backend),
        mode="read_write",
        snapshot_retention={"keep_last": 2},
    )

    with pytest.raises(TypeError, match="retention"):
        await store.put_snapshot("legacy-run", {"state_version": 1})

    assert backend.put_calls == 0


@pytest.mark.asyncio
async def test_snapshot_prune_preserves_business_records_and_runtime_events(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": None},
    )
    business_record = await store.put(
        {"status": "approved"},
        collection="business",
        kind="decision",
    )
    await store.append_runtime_event(
        "execution-audit",
        {
            "event_id": "event-1",
            "event_type": "triggerflow.signal",
            "payload": {"value": "observed"},
        },
    )
    for version in range(3):
        await store.put_snapshot(
            "run-preserve-boundaries",
            {"state_version": version},
        )

    await store.prune_snapshots("run-preserve-boundaries", keep_last=1)

    assert await store.get_data(business_record) == {"status": "approved"}
    events = await store.query_runtime_events("execution-audit")
    assert [event["event_id"] for event in events] == ["event-1"]


@pytest.mark.parametrize(
    ("policy", "error_type"),
    [
        ({"keep_last": 0}, ValueError),
        ({"keep_last": True}, TypeError),
        ({"keep_last": 1.5}, TypeError),
        ({"unknown": 3}, ValueError),
    ],
)
def test_record_store_rejects_invalid_snapshot_retention_policy(
    tmp_path,
    policy,
    error_type,
) -> None:
    with pytest.raises(error_type):
        RecordStore(
            tmp_path,
            mode="read_write",
            snapshot_retention=policy,
        )


def test_record_store_retrieval_contract_has_no_task_file_lane() -> None:
    parameters = inspect.signature(RecordStore.retrieve).parameters

    assert "sources" not in parameters
    assert "file_options" not in parameters


@pytest.mark.asyncio
async def test_record_store_registry_forwards_snapshot_retention_configuration(
    tmp_path,
) -> None:
    store = RecordStoreRegistry().create(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": 1},
    )

    await store.put_snapshot("registry-run", {"state_version": 1})
    await store.put_snapshot("registry-run", {"state_version": 2})

    assert len(await store.checkpoint_history("registry-run")) == 1


@pytest.mark.asyncio
async def test_record_store_scope_isolation(tmp_path) -> None:
    root = RecordStore(tmp_path, mode="read_write")
    first = root._bind_execution(
        "run-1",
        scope={"execution_id": "run-1"},
        search_scope={"execution_id": "run-1"},
    )
    second = root._bind_execution(
        "run-2",
        scope={"execution_id": "run-2"},
        search_scope={"execution_id": "run-2"},
    )

    await first.put("first", collection="events")
    await second.put("second", collection="events")

    assert [await first.get_data(ref) for ref in await first.search()] == ["first"]
    assert [await second.get_data(ref) for ref in await second.search()] == ["second"]


@pytest.mark.asyncio
async def test_record_store_context_source_enumerates_bound_scope_without_retrieval_method(
    tmp_path,
) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    await store.put("Revenue increased", collection="evidence", kind="fact")
    source = RecordStoreContextSource(store)

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=5,
    )

    assert [item.metadata["collection"] for item in page.descriptors] == ["evidence"]
    assert page.next_cursor is None
    assert all("method" not in item.metadata for item in page.descriptors)


@pytest.mark.asyncio
async def test_record_store_context_source_enumerates_descriptors_without_intent(
    tmp_path,
) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("Revenue increased", collection="evidence", kind="fact")
    source = RecordStoreContextSource(store)

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=5,
    )
    readback = await source.async_read_exact(ref["id"], max_chars=100)

    assert source.source_kind == "record_store"
    assert [item.source_ref for item in page.descriptors] == [ref["id"]]
    assert readback.content == "Revenue increased"
