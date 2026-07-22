import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently.core.storage import RecordStore


async def embed_texts(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        normalized = text.lower()
        if "alpha" in normalized:
            vectors.append([1.0, 0.0, 0.0])
        elif "beta" in normalized:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


async def main() -> dict[str, object]:
    with TemporaryDirectory(prefix="agently-record-store-vector-providers-") as temp_dir:
        previous_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            record_store = RecordStore(
                ".",
                mode="read_write",
                db_store_provider="sqlite",
                embedding_provider=embed_texts,
                vector_store_provider="auto",
            )
            await record_store.put(
                {"memory": "alpha rollout controls"},
                collection="memory",
                kind="provider_probe",
                summary="alpha rollout controls",
                scope={"case_id": "provider-demo"},
                vector=True,
            )
            await record_store.put(
                {"memory": "beta billing exception"},
                collection="memory",
                kind="provider_probe",
                summary="beta billing exception",
                scope={"case_id": "provider-demo"},
                vector=True,
            )

            package = await record_store.retrieve(
                "alpha migration query",
                filters={"collection": "memory", "kind": "provider_probe"},
                scope={"case_id": "provider-demo"},
                method="vector",
                rerank=False,
            )
            selected_item = cast(dict[str, Any], package["items"][0])
            selected_ref = selected_item["ref"]
            selected_data = await record_store.get_data(selected_ref)
            capabilities = record_store.capabilities()
            vector_diagnostics = package["diagnostics"]["vector"]
            result = {
                "top_memory": selected_data["memory"],
                "vector_used": vector_diagnostics["used"],
                "materialized_components": capabilities["materialized_components"],
                "configured_db_store_provider": "sqlite",
                "configured_embedding_provider": "callable",
                "selected_vector_store_provider": vector_diagnostics.get("vector_store_provider"),
                "vector_diagnostics": vector_diagnostics,
            }
            assert result["top_memory"] == "alpha rollout controls"
            assert result["vector_used"] is True
            assert "records" in result["materialized_components"]
            assert result["configured_db_store_provider"] == "sqlite"
            assert result["configured_embedding_provider"] == "callable"
            assert result["selected_vector_store_provider"] in {"chroma", "sqlite"}
            return result
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    print(asyncio.run(main()))


# Expected key output:
# top_memory: alpha rollout controls
# vector_used: True
# configured_db_store_provider: sqlite
# configured_embedding_provider: callable
# selected_vector_store_provider: chroma when chromadb is installed and initializes, otherwise sqlite
#
# This infrastructure-only probe does not call a model. It demonstrates that
# record DB, embedding, and vector storage are separate RecordStore providers:
# db_store_provider="sqlite" stores records, the callable embedding provider
# produces vectors, and vector_store_provider="auto" uses Chroma when available
# or the SQLite vector table fallback otherwise.
