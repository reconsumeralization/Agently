"""Real temporary SQLite tests; no model calls or semantic acceptance claims."""
from __future__ import annotations

import asyncio
from contextlib import closing, nullcontext
import gc
import sqlite3
from typing import Any
import warnings

import pytest

from agently.core.storage.Errors import RecordStorePolicyError
from agently.core.storage.LocalRecordStore import LocalRecordStore
from agently.core.storage.Stores import SQLiteVectorStoreProvider


@pytest.fixture
def connections(monkeypatch):
    original = sqlite3.connect
    opened = []
    failure = {"sql_prefix": ""}

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql: str, parameters: Any = ()):
            if failure["sql_prefix"] and sql.startswith(failure["sql_prefix"]):
                raise sqlite3.OperationalError("Injected SQLite setup failure")
            return super().execute(sql, parameters)

        def close(self) -> None:
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = TrackedConnection
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        yield opened, failure, original
    finally:
        # A failing regression must not leak its own connections into later tests.
        for connection in opened:
            connection.close()


def store_for(kind, tmp_path):
    if kind == "records":
        return LocalRecordStore(tmp_path)
    return SQLiteVectorStoreProvider(tmp_path / "vectors.db", create=False)


def connect_to(store):
    return store._connect(write=True) if isinstance(store, LocalRecordStore) else store._connect()


@pytest.mark.parametrize("kind", ["records", "vectors"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_transaction_settles_then_closes(tmp_path, connections, kind, outcome):
    opened, _failure, original = connections
    store = store_for(kind, tmp_path)
    error_type = asyncio.CancelledError if outcome == "cancel" else RuntimeError
    expected_error = pytest.raises(error_type) if outcome != "success" else nullcontext()
    with expected_error:
        with connect_to(store) as connection:
            connection.execute("CREATE TABLE probe (value INTEGER)")
            connection.commit()
            connection.execute("INSERT INTO probe VALUES (7)")
            if outcome != "success":
                raise error_type("exercise transaction exit")
    assert len(opened) == 1
    assert opened[0].closed
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
    # Inspect persisted data using a separate explicitly closed real connection.
    with closing(original(store.db_path)) as readback:
        values = [row[0] for row in readback.execute("SELECT value FROM probe").fetchall()]
    assert values == ([7] if outcome == "success" else [])


@pytest.mark.parametrize("kind", ["records", "vectors"])
def test_setup_error_closes_before_yield(tmp_path, connections, kind):
    opened, failure, _original = connections
    store = store_for(kind, tmp_path)
    failure["sql_prefix"] = "PRAGMA"
    with pytest.raises(sqlite3.OperationalError, match="Injected SQLite setup failure"):
        with connect_to(store):
            raise AssertionError("Setup failure must not enter the caller's block")
    assert len(opened) == 1
    assert opened[0].closed


@pytest.mark.parametrize("kind", ["records", "vectors"])
def test_commit_error_rolls_back_and_closes(tmp_path, connections, kind):
    opened, _failure, original = connections
    store = store_for(kind, tmp_path)
    # A real deferred constraint fails during SQLite's context-exit commit.
    with pytest.raises(sqlite3.IntegrityError):
        with connect_to(store) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            connection.execute(
                "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
            )
            connection.commit()
            connection.execute("INSERT INTO child VALUES (99)")
    assert len(opened) == 1
    assert opened[0].closed
    with closing(original(store.db_path)) as readback:
        assert readback.execute("SELECT parent_id FROM child").fetchall() == []


@pytest.mark.asyncio
async def test_record_roundtrip_and_read_only_keep_policy(tmp_path, connections):
    opened, _failure, _original = connections
    writable = LocalRecordStore(tmp_path / "records")
    assert not writable.db_path.exists()
    ref = await writable.put(collection="notes", content={"value": 7})
    assert await writable.get_data(ref) == {"value": 7}
    readonly = LocalRecordStore(writable.root, mode="read_only")
    assert await readonly.get_data(ref) == {"value": 7}
    before = len(opened)
    with pytest.raises(RecordStorePolicyError):
        with readonly._connect(write=True):
            raise AssertionError("Read-only write must fail before opening SQLite")
    assert len(opened) == before
    assert all(connection.closed for connection in opened)


@pytest.mark.asyncio
async def test_vector_roundtrip_and_read_only_keep_policy(tmp_path, connections):
    opened, _failure, _original = connections
    records = LocalRecordStore(tmp_path / "records")
    ref = await records.put(collection="notes", content="value", summary="value")
    writable = SQLiteVectorStoreProvider(tmp_path / "vectors.db")
    await writable.index_record(ref, [1.0, 0.0])
    readonly = SQLiteVectorStoreProvider(writable.db_path, read_only=True)
    assert [item["id"] for item in await readonly.search_by_embedding([1.0, 0.0])] == [ref["id"]]
    before = len(opened)
    with pytest.raises(RecordStorePolicyError):
        await readonly.index_record(ref, [0.0, 1.0])
    with pytest.raises(RecordStorePolicyError):
        await readonly.delete_records([ref["id"]])
    assert len(opened) == before
    await writable.delete_records([ref["id"]])
    assert await readonly.search_by_embedding([1.0, 0.0]) == []
    assert all(connection.closed for connection in opened)


def test_missing_read_only_store_does_not_create_sqlite(tmp_path, connections):
    opened, _failure, _original = connections
    store = LocalRecordStore(tmp_path / "absent", mode="read_only")
    with pytest.raises(FileNotFoundError):
        with store._connect():
            raise AssertionError("A missing read database cannot yield a connection")
    assert opened == []
    assert not store.root.exists()


@pytest.mark.asyncio
async def test_cancel_waiting_for_record_lock_opens_no_connection(tmp_path, connections):
    opened, _failure, _original = connections
    store = LocalRecordStore(tmp_path / "records")
    await store._lock.acquire()
    pending = asyncio.create_task(store.put(collection="notes", content="cancelled"))
    try:
        await asyncio.sleep(0)
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert opened == []
    finally:
        store._lock.release()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending


@pytest.mark.parametrize("kind", ["records", "vectors"])
def test_real_connections_emit_no_resource_warning(tmp_path, kind):
    # No tracking/fake connection: use the actual factory and collect actual GC warnings.
    # In particular, Python 3.14 reports SQLite connections left unclosed.
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if kind == "records":
            store = LocalRecordStore(tmp_path / "records")
            asyncio.run(store.put(collection="notes", content="value"))
        else:
            SQLiteVectorStoreProvider(tmp_path / "vectors.db")
        gc.collect()
    assert caught == []
