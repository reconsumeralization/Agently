# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agently.core.Workspace import Workspace, WorkspaceManager


def _private_files(root: Path) -> tuple[str, ...]:
    private_root = root / ".agently"
    if not private_root.exists():
        return ()
    return tuple(
        sorted(
            str(path.relative_to(root))
            for path in private_root.rglob("*")
            if path.is_file()
        )
    )


@pytest.mark.asyncio
async def test_plain_record_uses_one_lazy_sqlite_copy_without_fts_or_vector(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager()
    vector_factory_calls: list[dict[str, Any]] = []

    def vector_factory(**options: Any) -> Any:
        vector_factory_calls.append(options)
        raise AssertionError("ordinary Workspace records must not start a vector store")

    manager.register_vector_store_provider("probe", vector_factory)
    workspace = Workspace(
        tmp_path,
        manager,
        vector_store_provider="probe",
    )

    assert _private_files(tmp_path) == ()
    assert vector_factory_calls == []

    ref = await workspace.put(
        {"answer": 42, "detail": "stored once"},
        collection="observations",
        kind="result",
    )

    assert vector_factory_calls == []
    assert await workspace.get_data(ref) == {"answer": 42, "detail": "stored once"}
    assert _private_files(tmp_path) == (".agently/workspace.db",)
    assert not (tmp_path / ".agently" / "content").exists()
    assert not (tmp_path / ".agently" / "files").exists()
    assert not (tmp_path / ".agently" / "vectors").exists()
    assert not (tmp_path / ".agently" / "workspace.meta.json").exists()
    assert not (tmp_path / ".agently" / "AGENTLY_WORKSPACE.md").exists()

    with sqlite3.connect(tmp_path / ".agently" / "workspace.db") as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        stored = conn.execute(
            "SELECT content FROM records WHERE id = ?",
            (ref["id"],),
        ).fetchone()

    assert "records" in tables
    assert {name for name in tables if not name.startswith("sqlite_")} == {"records"}
    assert not any(name.startswith("records_fts") for name in tables)
    assert stored is not None
    assert stored[0] is not None


@pytest.mark.asyncio
async def test_text_index_is_materialized_only_by_explicit_index_operation(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    await workspace.put("plain body", collection="observations")

    with sqlite3.connect(tmp_path / ".agently" / "workspace.db") as conn:
        before = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not any(name.startswith("records_fts") for name in before)

    ref = await workspace.put(
        "needle body",
        collection="observations",
        indexed=True,
    )

    with sqlite3.connect(tmp_path / ".agently" / "workspace.db") as conn:
        after = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "records_fts" in after
    assert [item["id"] for item in await workspace.search("needle")] == [ref["id"]]


@pytest.mark.asyncio
async def test_vector_components_are_materialized_only_by_explicit_vector_write(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager()
    calls = {"embedding": 0, "vector": 0, "indexed": 0}

    class EmbeddingProbe:
        name = "embedding-probe"

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text))] for text in texts]

    class VectorProbe:
        name = "vector-probe"
        read_only = False

        async def index_record(self, ref: dict[str, Any], embedding: list[float]) -> None:
            calls["indexed"] += 1

        async def search_by_embedding(
            self,
            embedding: list[float],
            filters: dict[str, Any] | None = None,
            limit: int = 8,
        ) -> list[dict[str, Any]]:
            return []

        async def delete_records(self, record_ids: list[str]) -> None:
            return None

    def embedding_factory(**options: Any) -> EmbeddingProbe:
        calls["embedding"] += 1
        return EmbeddingProbe()

    def vector_factory(**options: Any) -> VectorProbe:
        calls["vector"] += 1
        return VectorProbe()

    manager.register_embedding_provider("probe", embedding_factory)
    manager.register_vector_store_provider("probe", vector_factory)
    workspace = Workspace(
        tmp_path,
        manager,
        embedding_provider="probe",
        vector_store_provider="probe",
    )

    await workspace.put("ordinary", collection="memory")
    assert calls == {"embedding": 0, "vector": 0, "indexed": 0}

    await workspace.put("semantic", collection="memory", vector=True)

    assert calls == {"embedding": 1, "vector": 1, "indexed": 1}
    assert not (tmp_path / ".agently" / "vectors").exists()


@pytest.mark.asyncio
async def test_auto_vector_store_provider_resolves_lazily_on_vector_write(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager()
    calls = {"embedding": 0, "chroma": 0, "indexed": 0}

    class EmbeddingProbe:
        name = "embedding-probe"

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text))] for text in texts]

    class VectorProbe:
        name = "chroma-probe"

        def __init__(self) -> None:
            self.records: list[dict[str, Any]] = []

        async def index_record(self, ref: dict[str, Any], embedding: list[float]) -> None:
            calls["indexed"] += 1
            self.records.append(ref)

        async def search_by_embedding(
            self,
            embedding: list[float],
            filters: dict[str, Any] | None = None,
            limit: int = 8,
        ) -> list[dict[str, Any]]:
            return list(self.records[:limit])

        async def delete_records(self, record_ids: list[str]) -> None:
            return None

    def embedding_factory(**options: Any) -> EmbeddingProbe:
        calls["embedding"] += 1
        return EmbeddingProbe()

    def chroma_factory(**options: Any) -> VectorProbe:
        calls["chroma"] += 1
        return VectorProbe()

    manager.register_embedding_provider("probe", embedding_factory)
    manager.register_vector_store_provider("chroma", chroma_factory)
    workspace = Workspace(
        tmp_path,
        manager,
        embedding_provider="probe",
        vector_store_provider="auto",
    )

    assert calls == {"embedding": 0, "chroma": 0, "indexed": 0}

    await workspace.put("semantic", collection="memory", vector=True)

    assert calls == {"embedding": 1, "chroma": 1, "indexed": 1}
    package = await workspace.retrieve("semantic", method="vector", rerank=False)
    assert package["diagnostics"]["vector"]["used"] is True
    assert len(package["items"]) == 1


@pytest.mark.asyncio
async def test_recovery_schema_is_created_only_by_explicit_snapshot(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    ref = await workspace.put_snapshot(
        "run-1",
        {"state_version": 1, "status": "waiting"},
        step_id="pause",
    )

    assert await workspace.get_data(ref) == {"state_version": 1, "status": "waiting"}
    with sqlite3.connect(tmp_path / ".agently" / "workspace.db") as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(row[0]).startswith("sqlite_")
        }
    assert tables == {"records", "checkpoints", "manifests"}
    assert _private_files(tmp_path) == (".agently/workspace.db",)


@pytest.mark.asyncio
async def test_runtime_event_schema_is_created_only_by_explicit_audit_write(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)

    event = await workspace.append_runtime_event(
        "run-1",
        {"event_type": "explicit.audit", "payload": {"value": 1}},
    )

    assert event["event_type"] == "explicit.audit"
    with sqlite3.connect(tmp_path / ".agently" / "workspace.db") as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(row[0]).startswith("sqlite_")
        }
    assert tables == {"runtime_events"}
    assert _private_files(tmp_path) == (".agently/workspace.db",)
