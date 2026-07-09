import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


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
    with TemporaryDirectory(prefix="agently-workspace-vector-providers-") as temp_dir:
        previous_cwd = os.getcwd()
        os.chdir(temp_dir)
        try:
            workspace = Agently.create_workspace(
                ".agently/workspaces/provider-demo",
                db_store_provider="sqlite",
                embedding_provider=embed_texts,
                vector_store_provider="auto",
            )
            await workspace.put(
                {"memory": "alpha rollout controls"},
                collection="memory",
                kind="provider_probe",
                summary="alpha rollout controls",
                scope={"case_id": "provider-demo"},
            )
            await workspace.put(
                {"memory": "beta billing exception"},
                collection="memory",
                kind="provider_probe",
                summary="beta billing exception",
                scope={"case_id": "provider-demo"},
            )

            package = await workspace.retrieve(
                "alpha migration query",
                filters={"collection": "memory", "kind": "provider_probe"},
                scope={"case_id": "provider-demo"},
                method="vector",
                rerank=False,
            )
            selected_item = cast(dict[str, Any], package["items"][0])
            selected_ref = selected_item["ref"]
            selected_data = await workspace.get_data(selected_ref)
            capabilities = workspace.capabilities()["components"]
            result = {
                "top_memory": selected_data["memory"],
                "vector_used": package["diagnostics"]["vector"]["used"],
                "db_store_provider": capabilities["db_store_provider"],
                "embedding_provider": capabilities["embedding_provider"],
                "vector_store_provider": capabilities["vector_store_provider"],
            }
            assert result["top_memory"] == "alpha rollout controls"
            assert result["vector_used"] is True
            assert result["db_store_provider"] == "sqlite"
            assert result["embedding_provider"] == "callable"
            assert result["vector_store_provider"] in {"chroma", "sqlite"}
            return result
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    print(asyncio.run(main()))


# Expected key output:
# top_memory: alpha rollout controls
# vector_used: True
# db_store_provider: sqlite
# embedding_provider: callable
# vector_store_provider: chroma when chromadb is installed and initializes, otherwise sqlite
#
# This infrastructure-only probe does not call a model. It demonstrates that
# record DB, embedding, and vector storage are separate Workspace providers:
# db_store_provider="sqlite" stores records, the callable embedding provider
# produces vectors, and vector_store_provider="auto" uses Chroma when available
# or the SQLite vector table fallback otherwise.
