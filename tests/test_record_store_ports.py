from __future__ import annotations

import inspect

import pytest

from agently.core.storage import RecordStore, RecordStoreContextSource


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


def test_record_store_retrieval_contract_has_no_task_file_lane() -> None:
    parameters = inspect.signature(RecordStore.retrieve).parameters

    assert "sources" not in parameters
    assert "file_options" not in parameters


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
