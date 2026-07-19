from __future__ import annotations

import inspect

import pytest

from agently.core.storage import RecordStore, RecordStoreContextSource
from agently.types.data import ContextReadIntent


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
async def test_record_store_context_source_obeys_source_kind_filter(tmp_path) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    await store.put("Revenue increased", collection="evidence", kind="fact")
    source = RecordStoreContextSource(store)

    window = await source.async_list_candidates(
        ContextReadIntent(
            query="Revenue",
            filters={"source_kinds": ["record_store"]},
        ),
        limit=5,
    )
    excluded = await source.async_list_candidates(
        ContextReadIntent(
            query="Revenue",
            filters={"source_kinds": ["task_workspace"]},
        ),
        limit=5,
    )

    assert [candidate.metadata["collection"] for candidate in window.candidates] == ["evidence"]
    assert window.exhaustive is True
    assert window.next_cursor is None
    assert window.scope["selection"] == "top_n"
    assert window.scope["top_n"] == 5
    assert window.scope["source_wide_exhaustive"] is False
    assert excluded.candidates == ()
    assert excluded.exhaustive is True
