# Copyright 2023-2026 AgentEra(Agently.Tech)

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from agently.types.data import SkillSourceRequest, SkillSourceSnapshot

from ._snapshot import materialize_snapshot, select_source_root


class GitSkillSourceProvider:
    name = "GitSkillSourceProvider"
    DEFAULT_SETTINGS: dict[str, object] = {}
    provider_id = "git"
    source_types: tuple[str, ...] = ("git",)

    def __init__(
        self,
        *,
        cache_root: str | Path = ".agently/skill-source-cache",
        git_binary: str = "git",
        timeout: float = 120.0,
        max_files: int = 10000,
        max_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.cache_root = Path(cache_root).expanduser().resolve()
        self.git_binary = str(git_binary)
        self.timeout = float(timeout)
        self.max_files = int(max_files)
        self.max_bytes = int(max_bytes)

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    async def _git(self, *args: str, cwd: Path | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            self.git_binary,
            *args,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout,
            )
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"Git Skill source command failed: {message[:2000]}")
        return stdout.decode("utf-8", errors="replace").strip()

    async def async_materialize(
        self,
        request: SkillSourceRequest,
    ) -> SkillSourceSnapshot:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix="git-skill-source-", dir=self.cache_root)
        )
        checkout = temporary / "checkout"
        try:
            await self._git("clone", "--no-checkout", "--quiet", request.source, str(checkout))
            requested_ref = request.ref or "HEAD"
            resolved = await self._git(
                "rev-parse",
                "--verify",
                f"{requested_ref}^{{commit}}",
                cwd=checkout,
            )
            await self._git("checkout", "--detach", "--quiet", resolved, cwd=checkout)
            selected = select_source_root(checkout, request.subpath)
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
                source_type="git",
                requested_source=request.source,
                requested_ref=request.ref,
                resolved_revision=resolved,
                subpath=request.subpath,
                materialized_path=str(path),
                source_digest=digest,
                metadata={"file_count": file_count, "total_bytes": total_bytes},
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, temporary, True)


__all__ = ["GitSkillSourceProvider"]
