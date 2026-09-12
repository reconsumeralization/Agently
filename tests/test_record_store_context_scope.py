from __future__ import annotations

import asyncio
import copy
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest

from agently.core.context import ContextStaleError, TaskContext
from agently.core.storage import RecordStore, RecordStoreContextSource, RecordStoreRegistry
from agently.core.storage.LocalRecordStore import LocalRecordStore
from agently.types.data import ContextReadIntent, RecordRef


def source_for(store: RecordStore, **scope: Any) -> RecordStoreContextSource:
    return RecordStoreContextSource(store._bind_execution("fixture-view", search_scope=scope))


@pytest.mark.asyncio
async def test_unrelated_writes_keep_reader_current_but_visible_write_invalidates(tmp_path: Path) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("A visible fact", collection="facts", scope={"run_id": "A"})
    source = source_for(store, run_id="A")
    context = TaskContext("scope-reader")
    context.attach(source)
    reader = context.reader(consumer="scope-test")
    revision = source.source_revision
    other = await store.put_checkpoint("B", {"state_version": 1})
    await store.backend.put_record({**other, "summary": "changed B"})
    await store.delete_snapshot("B")
    assert source.source_revision == revision
    assert reader.is_current
    package = await reader.async_read(ContextReadIntent(query="fact", explicit_refs=(ref["id"],)))
    assert any("A visible fact" in str(block.content) for block in package.blocks)
    await store.put("second A fact", collection="facts", scope={"run_id": "A"})
    assert source.source_revision != revision
    assert not reader.is_current
    with pytest.raises(ContextStaleError):
        await reader.async_read("fact")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("collection", "other"), ("kind", "other"), ("path", "relative"),
    ("sha256", "updated-ref-digest"), ("size", 999), ("summary", "new summary"),
    ("scope", {"project": "A", "extra": True}), ("source", {"origin": "new"}),
    ("created_at", "2030-01-01T00:00:00+00:00"), ("meta", {"context_role": "artifact"}),
])
async def test_every_visible_ref_field_affects_revision(tmp_path: Path, field: str, value: Any) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("unchanged body", collection="facts", scope={"project": "A"})
    source = source_for(store, project="A")
    before = source.source_revision
    changed = cast(Any, {**ref, field: value})
    await store.backend.put_record(changed)
    assert source.source_revision != before
    assert await store.get_data(ref) == "unchanged body"


@pytest.mark.asyncio
async def test_scope_move_shared_change_and_snapshot_deletion(tmp_path: Path) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("shared", collection="facts", scope={"project": "A"})
    a = source_for(store, project="A")
    a2 = source_for(RecordStore(tmp_path), project="A")
    b = source_for(store, project="B")
    a_before, b_before = a.source_revision, b.source_revision
    assert a2.source_revision == a_before
    await store.backend.put_record({**ref, "scope": {"project": "B"}})
    assert a.source_revision != a_before
    assert a2.source_revision == a.source_revision
    assert b.source_revision != b_before
    with pytest.raises(KeyError, match="not visible"):
        await a.async_read_exact(ref["id"], max_chars=100)
    assert await store.get_data(ref) == "shared"  # Public read does not acquire a scope ACL.
    await store.backend.put_record(ref)
    assert a.source_revision == a_before
    assert b.source_revision == b_before
    all_source = source_for(store)
    original = all_source.source_revision
    snapshot = await store.put_snapshot("checkpoint-run", {"state_version": 1})
    assert all_source.source_revision != original
    await store.delete_snapshot("checkpoint-run")
    assert all_source.source_revision == original
    with pytest.raises(KeyError):
        await all_source.async_read_exact(snapshot["id"], max_chars=100)


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [None, "A", ["A", "B"], ("B", "A"), {"A", "B"},
                                         {"k": [1, "中"]}, [{"k": [1, "中"]}], (("A", "B"),)])
async def test_normalized_filters_keep_original_matching(tmp_path: Path, expected: Any) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    values: list[Any] = [None, "A", "B"]
    if not isinstance(expected, set):
        values.extend([{"k": [1, "中"]}, ["A", "B"]])
    for value in values:
        await store.put("body", collection="facts", scope={"value": value})
    source = source_for(store, value=expected)
    snapshot = source._local_snapshot(page=(0, 100))
    assert snapshot is not None
    assert [ref["id"] for ref in snapshot[1]] == [ref["id"] for ref in await source.record_store.search(None)]
    if isinstance(expected, (list, tuple, set)) and all(isinstance(item, str) for item in expected):
        assert source.source_revision == source_for(store, value=["B", "A", "A"]).source_revision


def test_empty_scope_partitions_are_distinct_stable_and_lazy(tmp_path: Path) -> None:
    calls: list[str] = []
    store = RecordStore(tmp_path, create=False, embedding_provider=lambda *_: calls.append("embedding"))
    a, b = source_for(store, project="A"), source_for(store, project="B")
    assert a.source_id == b.source_id
    assert a.source_revision != b.source_revision
    assert a.source_revision == source_for(store, project="A").source_revision
    context = TaskContext("scope-lazy")
    context.attach(a)
    snapshot = context.snapshot()
    assert context.is_snapshot_current(snapshot)
    assert a.record_store._backend is None
    assert b.record_store._backend is None
    assert store._backend is None
    assert not calls
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_unrelated_initial_materialization_and_empty_records_do_not_change_scope(tmp_path: Path) -> None:
    a = source_for(RecordStore(tmp_path, create=False), project="A")
    before = a.source_revision
    writer = RecordStore(tmp_path, mode="read_write")
    await writer.put("B", collection="facts", scope={"project": "B"})
    assert a.source_revision == before
    page = await a.async_enumerate_descriptors(profile={}, cursor=None, limit=5)
    assert not page.descriptors
    assert page.source_revision == before
    assert a.record_store._backend is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["中文😊正文", {"中文": [1, "b"]}, b"a\xffb", ""])
@pytest.mark.parametrize("offset,limit", [(0, 100), (1, 2), (100, 0)])
async def test_text_projection_and_bounded_digest_are_preserved(
    tmp_path: Path, content: Any, offset: int, limit: int,
) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put(content, collection="facts")
    source = source_for(store)
    expected = await store.read_bounded(ref, offset=offset, limit=limit)
    read = await source.async_read_exact(ref["id"], range_start=offset, max_chars=limit)
    assert read.content == expected["content"]
    assert read.content_digest == expected["digest"]
    assert read.metadata["size"] == expected["size"]
    assert read.metadata["total_size"] == expected["total_size"]
    assert read.completeness == ("complete" if expected["eof"] else "truncated")


@pytest.mark.asyncio
async def test_page_and_exact_visible_only_but_public_read_unchanged(tmp_path: Path) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("B secret", collection="facts", scope={"project": "B"})
    source = source_for(store, project="A")
    with pytest.raises(KeyError, match="not visible"):
        await source.async_read_exact(ref["id"], max_chars=100)
    assert (await source.record_store.read_bounded(ref))["content"] == "B secret"
    for index in range(3):
        await store.put(str(index), collection="facts", scope={"project": "A"})
    page = await source.async_enumerate_descriptors(profile={}, cursor=None, limit=2)
    assert len(page.descriptors) == 2 and page.next_cursor == "2"
    await store.put("new", collection="facts", scope={"project": "A"})
    next_page = await source.async_enumerate_descriptors(profile={}, cursor=page.next_cursor, limit=2)
    assert next_page.source_revision != page.source_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,method", [("facade", "search"), ("facade", "read_bounded"),
                                          ("backend", "search"), ("backend", "read_bounded")])
async def test_instance_adapters_keep_generic_behavior(tmp_path: Path, owner: str, method: str) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    await store.put("body", collection="facts")
    target = store if owner == "facade" else store.backend
    calls: list[str] = []
    original = getattr(target, method)

    async def observed(*args: Any, **kwargs: Any) -> Any:
        calls.append(method)
        return await original(*args, **kwargs)

    setattr(target, method, observed)
    source = source_for(store)
    assert source._local_snapshot() is None
    await source.async_enumerate_descriptors(profile={}, cursor=None, limit=2)
    assert calls


@pytest.mark.asyncio
async def test_subclasses_explicit_revision_and_custom_scope_keep_fallback(tmp_path: Path) -> None:
    class CustomStore(RecordStore):
        pass

    class CustomBackend(LocalRecordStore):
        pass

    class CustomManager(RecordStoreRegistry):
        pass

    class EqualA:
        def __eq__(self, other: object) -> bool:
            return other == "A"

    assert source_for(CustomStore(tmp_path))._local_snapshot() is None
    assert source_for(RecordStore(cast(Any, CustomBackend(tmp_path))))._local_snapshot() is None
    assert source_for(RecordStore(tmp_path, manager=CustomManager()))._local_snapshot() is None
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("body", collection="facts", scope={"project": "A"})
    source = source_for(store, project=EqualA())
    assert source._local_snapshot() is None
    page = await source.async_enumerate_descriptors(profile={}, cursor=None, limit=1)
    assert page.descriptors[0].source_ref == ref["id"]
    setattr(store, "source_revision", "user-token")
    source = source_for(store)
    assert source.source_revision == "user-token"
    assert source._local_snapshot() is None
    page = await source.async_enumerate_descriptors(profile={}, cursor=None, limit=1)
    assert page.source_revision == "user-token"


def test_full_provider_is_not_materialized_for_revision(tmp_path: Path) -> None:
    calls: list[str] = []
    manager = RecordStoreRegistry()

    def fail_provider(**_: Any) -> Any:
        calls.append("provider")
        raise AssertionError("Provider must remain lazy for revision")

    manager.register_backend_provider("local", fail_provider)
    store = RecordStore(tmp_path, manager=manager, provider="local")
    source = source_for(store)
    assert source.source_revision
    assert source._local_snapshot() is None
    assert calls == []
    assert store._backend is None


@pytest.mark.asyncio
async def test_complete_third_party_backend_keeps_original_fallback(tmp_path: Path) -> None:
    local = LocalRecordStore(tmp_path)
    ref = await local.put("provider body", collection="facts")
    calls: list[str] = []

    class ThirdParty:
        root = tmp_path
        read_only = False
        source_revision = "backend-token-is-not-facade-token"

        async def put(self, content: Any, **kwargs: Any) -> Any:
            return await local.put(content, **kwargs)

        async def search(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("search")
            return await local.search(*args, **kwargs)

        async def get_data(self, *args: Any, **kwargs: Any) -> Any:
            return await local.get_data(*args, **kwargs)

        def capabilities(self) -> Any:
            return local.capabilities()

        async def read_bounded(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("read")
            return await local.read_bounded(*args, **kwargs)

    source = source_for(RecordStore(cast(Any, ThirdParty())))
    assert source._local_snapshot() is None
    before = source.source_revision
    assert before != ThirdParty.source_revision
    page = await source.async_enumerate_descriptors(profile={}, cursor=None, limit=2)
    read = await source.async_read_exact(ref["id"], max_chars=100)
    assert page.descriptors[0].source_ref == ref["id"]
    assert read.content == "provider body"
    assert page.source_revision == read.source_revision == before
    assert calls == ["search", "read", "read"]


@pytest.mark.asyncio
async def test_shared_view_changes_and_compound_scope_match(tmp_path: Path) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("A fact", collection="facts", scope={"project": "P", "run_id": "A"})
    shared_a = source_for(store, project="P")
    shared_b = source_for(RecordStore(tmp_path), project="P")
    narrow = source_for(store, project="P", run_id="A")
    before = shared_a.source_revision
    narrow_before = narrow.source_revision
    await store.put("B fact", collection="facts", scope={"project": "P", "run_id": "B"})
    assert shared_a.source_revision == shared_b.source_revision != before
    assert narrow.source_revision == narrow_before
    narrow_page = await narrow.async_enumerate_descriptors(profile={}, cursor=None, limit=100)
    assert [item.source_ref for item in narrow_page.descriptors] == [ref["id"]]
    assert narrow.source_revision != shared_a.source_revision


@pytest.mark.asyncio
async def test_existing_read_only_database_and_content_format_revision(tmp_path: Path) -> None:
    local = LocalRecordStore(tmp_path)
    ref = await local.put("body", collection="facts")
    reader = LocalRecordStore(tmp_path, create=False, mode="read_only")
    source = source_for(RecordStore(cast(Any, reader)))
    before_bytes = local.db_path.read_bytes()
    before = source.source_revision
    assert (await source.async_read_exact(ref["id"], max_chars=100)).content == "body"
    assert local.db_path.read_bytes() == before_bytes
    assert reader._materialized_components == set()
    # Storage fixture transition: format participates even when ref/body remain identical.
    with closing(sqlite3.connect(local.db_path)) as connection:
        connection.execute("UPDATE records SET content_format = ? WHERE id = ?", ("json", ref["id"]))
        connection.commit()
    assert source.source_revision != before


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, -1)])
async def test_negative_exact_bounds_keep_value_error(tmp_path: Path, offset: int, limit: int) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    ref = await store.put("body", collection="facts")
    source = source_for(store)
    with pytest.raises(ValueError, match="non-negative"):
        await source.async_read_exact(ref["id"], range_start=offset, max_chars=limit)


@pytest.mark.asyncio
async def test_set_filter_keeps_original_unhashable_error(tmp_path: Path) -> None:
    store = RecordStore(tmp_path, mode="read_write")
    await store.put("body", collection="facts", scope={"value": ["A"]})
    source = source_for(store, value={"A", "B"})
    with pytest.raises(TypeError, match="unhashable"):
        await source.record_store.search(None)
    with pytest.raises(TypeError, match="unhashable"):
        _ = source.source_revision


def test_corrupt_database_and_real_lock_errors_propagate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalRecordStore(tmp_path)
    backend.db_path.write_bytes(b"this is not SQLite")
    source = source_for(RecordStore(cast(Any, backend)))
    with pytest.raises(sqlite3.DatabaseError):
        _ = source.source_revision
    backend.db_path.unlink()  # Only this test-owned corrupt fixture file.
    asyncio.run(backend.put("body", collection="facts"))
    original_connect = sqlite3.connect

    def quick_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["timeout"] = 0.01
        return original_connect(*args, **kwargs)

    with closing(original_connect(backend.db_path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(sqlite3, "connect", quick_connect)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _ = source.source_revision
        writer.rollback()
    assert source.source_revision  # Failure did not leak the candidate reader lock.


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                    reason="POSIX chmod denial requires a non-root identity; lock/corruption tests remain active")
def test_real_permission_error_is_not_an_empty_revision(tmp_path: Path) -> None:
    root = tmp_path / "private-store"
    backend = LocalRecordStore(root)
    asyncio.run(backend.put("body", collection="facts"))
    source = source_for(RecordStore(cast(Any, backend)))
    root.chmod(0)
    try:
        try:
            backend.db_path.stat()
        except PermissionError:
            pass
        else:
            pytest.skip("This filesystem/credential does not enforce chmod(0) traversal denial")
        with pytest.raises(PermissionError):
            _ = source.source_revision
    finally:
        root.chmod(0o700)


@pytest.mark.parametrize("mode", ["page", "exact"])
@pytest.mark.parametrize("aba", [False, True])
def test_read_transaction_keeps_metadata_and_body_version_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, aba: bool,
) -> None:
    backend = LocalRecordStore(tmp_path)
    ref = asyncio.run(backend.put("body", collection="facts", summary="before"))
    with closing(sqlite3.connect(backend.db_path)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    source = source_for(RecordStore(cast(Any, backend)))
    original_revision = source.source_revision
    middle: RecordRef = {**copy.deepcopy(ref), "summary": "middle"}
    if aba:
        asyncio.run(backend.put_record(middle))
    expected_revision = source.source_revision
    metadata_captured = threading.Event()
    writer_finished = threading.Event()
    failures: list[BaseException] = []
    original_connect = sqlite3.connect

    def writer() -> None:
        try:
            assert metadata_captured.wait(10), "Reader never reached metadata boundary"
            other = LocalRecordStore(tmp_path)
            asyncio.run(other.put_record(ref if aba else middle))
        except BaseException as error:
            failures.append(error)
        finally:
            writer_finished.set()

    class HookCursor(sqlite3.Cursor):
        intercept = False

        def fetchall(self) -> Any:
            result = super().fetchall()
            if self.intercept:
                metadata_captured.set()
                assert writer_finished.wait(10), "Concurrent writer failed to finish"
            return result

    class HookConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            cursor = self.cursor(factory=HookCursor)
            cursor.intercept = sql.startswith("SELECT id, collection, kind, path, sha256")
            return cursor.execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if kwargs.get("uri"):
            kwargs["factory"] = HookConnection
        return original_connect(*args, **kwargs)

    worker = threading.Thread(target=writer)
    worker.start()
    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        if mode == "page":
            result = asyncio.run(source.async_enumerate_descriptors(profile={}, cursor=None, limit=5))
            assert result.descriptors[0].summary == ("middle" if aba else "before")
        else:
            result = asyncio.run(source.async_read_exact(ref["id"], max_chars=100))
            assert result.content == "body"
        assert result.source_revision == expected_revision
    finally:
        worker.join(10)
        monkeypatch.setattr(sqlite3, "connect", original_connect)
    assert not worker.is_alive() and not failures
    assert source.source_revision != expected_revision
    if aba:
        assert source.source_revision == original_revision
