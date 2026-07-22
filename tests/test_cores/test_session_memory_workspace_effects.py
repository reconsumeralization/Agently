from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.core.storage import RecordStore, RecordStoreRegistry
from agently.core.session import Session
from agently.utils import Settings


def _memory_record() -> dict[str, Any]:
    return {
        "memory_scope": "SESSION_MEMORY",
        "kind": "session_memory",
        "summary": "prefers concise updates",
        "body": {"preference": "concise updates"},
        "tags": ["preference"],
        "importance": 0.8,
        "provenance": {
            "plugin": "AgentlyMemory",
            "session_id": "memory-effects",
            "turn_index": 1,
        },
    }


def _session(
    memory_store: RecordStore,
    *,
    vector_enabled: bool = False,
) -> Session:
    settings = Settings(name="SessionMemoryWorkspaceEffects", parent=Agently.settings)
    settings.set("session.memory.AgentlyMemory.vector_index.enabled", vector_enabled)
    session = Session(
        id="memory-effects",
        plugin_manager=Agently.plugin_manager,
        settings=settings,
        memory_store=memory_store,
    )
    session.use_memory(mode="AgentlyMemory")
    return session


def test_session_without_memory_does_not_materialize_record_store(tmp_path: Path) -> None:
    root = tmp_path / "project"
    memory_store = RecordStore(root)

    Session(
        id="memory-disabled",
        plugin_manager=Agently.plugin_manager,
        settings=Agently.settings,
        memory_store=memory_store,
    )

    assert memory_store._backend is None
    assert not root.exists()


def test_record_only_memory_metadata_does_not_materialize_record_store_backend(
    tmp_path: Path,
) -> None:
    memory_store = RecordStore(tmp_path / "project")
    session = _session(memory_store)
    memory = cast(Any, session.memory)

    metadata = memory._vector_index_meta(memory_store)

    assert metadata == {"requested": False, "backend": None, "available": False}
    assert memory_store._backend is None
    assert not memory_store.root.exists()


@pytest.mark.asyncio
async def test_record_only_memory_creates_no_vector_provider_or_vector_carrier(
    tmp_path: Path,
) -> None:
    manager = RecordStoreRegistry()
    calls = {"embedding": 0, "vector": 0}

    def unexpected_embedding(**_options: Any) -> Any:
        calls["embedding"] += 1
        raise AssertionError("record-only memory must not start embedding")

    def unexpected_vector(**_options: Any) -> Any:
        calls["vector"] += 1
        raise AssertionError("record-only memory must not start vector storage")

    manager.register_embedding_provider("probe", unexpected_embedding)
    manager.register_vector_store_provider("probe", unexpected_vector)
    memory_store = RecordStore(
        tmp_path / "project",
        manager,
        embedding_provider="probe",
        vector_store_provider="probe",
    )
    session = _session(memory_store)

    ref = await cast(Any, session.memory)._store_memory(
        memory_store,
        _memory_record(),
        session=session,
    )

    assert calls == {"embedding": 0, "vector": 0}
    assert memory_store.capabilities()["materialized_components"] == ["records"]
    assert (memory_store.root / ".agently" / "records" / "records.db").is_file()
    assert not (memory_store.root / ".agently" / "records" / "vectors").exists()
    stored = await memory_store.get_data(ref)
    assert stored["vector_index"] == {
        "requested": False,
        "backend": None,
        "available": False,
    }


@pytest.mark.asyncio
async def test_vector_enabled_memory_uses_explicit_record_store_vector_write(
    tmp_path: Path,
) -> None:
    manager = RecordStoreRegistry()
    calls = {"embedding": 0, "vector": 0, "indexed": 0}

    class EmbeddingProbe:
        name = "embedding-probe"

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text))] for text in texts]

    class VectorProbe:
        name = "vector-probe"

        async def index_record(self, ref: dict[str, Any], embedding: list[float]) -> None:
            _ = (ref, embedding)
            calls["indexed"] += 1

        async def search_by_embedding(
            self,
            embedding: list[float],
            filters: dict[str, Any] | None = None,
            limit: int = 8,
        ) -> list[dict[str, Any]]:
            _ = (embedding, filters, limit)
            return []

        async def delete_records(self, record_ids: list[str]) -> None:
            _ = record_ids

    def embedding_factory(**_options: Any) -> EmbeddingProbe:
        calls["embedding"] += 1
        return EmbeddingProbe()

    def vector_factory(**_options: Any) -> VectorProbe:
        calls["vector"] += 1
        return VectorProbe()

    manager.register_embedding_provider("probe", embedding_factory)
    manager.register_vector_store_provider("probe", vector_factory)
    memory_store = RecordStore(
        tmp_path / "project",
        manager,
        embedding_provider="probe",
        vector_store_provider="probe",
    )
    session = _session(memory_store, vector_enabled=True)

    ref = await cast(Any, session.memory)._store_memory(
        memory_store,
        _memory_record(),
        session=session,
    )

    assert calls == {"embedding": 1, "vector": 1, "indexed": 1}
    assert memory_store.capabilities()["materialized_components"] == [
        "embedding",
        "records",
        "vector",
    ]
    stored = await memory_store.get_data(ref)
    assert stored["vector_index"]["requested"] is True
