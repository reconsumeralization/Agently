# Copyright 2023-2026 AgentEra(Agently.Tech)

from __future__ import annotations

import asyncio
from pathlib import Path

from agently.types.data import SkillSourceRequest, SkillSourceSnapshot

from ._snapshot import materialize_snapshot, select_source_root


class LocalPathSkillSourceProvider:
    name = "LocalPathSkillSourceProvider"
    DEFAULT_SETTINGS: dict[str, object] = {}
    provider_id = "local"
    source_types: tuple[str, ...] = ("local", "path")

    def __init__(
        self,
        *,
        cache_root: str | Path = ".agently/skill-source-cache",
        max_files: int = 10000,
        max_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.cache_root = Path(cache_root).expanduser().resolve()
        self.max_files = int(max_files)
        self.max_bytes = int(max_bytes)

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    async def async_materialize(
        self,
        request: SkillSourceRequest,
    ) -> SkillSourceSnapshot:
        source = Path(request.source)
        selected = select_source_root(source, request.subpath)
        path, digest, file_count, total_bytes = await asyncio.to_thread(
            materialize_snapshot,
            selected,
            cache_root=self.cache_root,
            provider_id=self.provider_id,
            max_files=self.max_files,
            max_bytes=self.max_bytes,
        )
        return SkillSourceSnapshot(
            provider_id=self.provider_id,
            source_type="local",
            requested_source=request.source,
            requested_ref=request.ref,
            resolved_revision=digest,
            subpath=request.subpath,
            materialized_path=str(path),
            source_digest=digest,
            metadata={"file_count": file_count, "total_bytes": total_bytes},
        )


__all__ = ["LocalPathSkillSourceProvider"]
