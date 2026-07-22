# Copyright 2023-2026 AgentEra(Agently.Tech)

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path


def select_source_root(source: Path, subpath: str | None = None) -> Path:
    """Resolve a snapshot root without following a source-owned symlink path."""

    root = source.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Skill source is not a directory: {root}")
    if not subpath:
        return root

    raw_subpath = Path(subpath)
    if raw_subpath.is_absolute() or ".." in raw_subpath.parts:
        raise ValueError(f"Skill source subpath escapes its source root: {subpath!r}")
    selected = root
    for part in raw_subpath.parts:
        if part in {"", "."}:
            continue
        selected = selected / part
        if selected.is_symlink():
            raise ValueError(
                f"Skill source subpath contains a symbolic link: {subpath!r}"
            )
    try:
        resolved = selected.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        raise ValueError(
            f"Skill source subpath escapes its source root or does not exist: {subpath!r}"
        ) from error
    if not resolved.is_dir():
        raise ValueError(f"Skill source subpath is not a directory: {subpath!r}")
    return resolved


def inspect_source_tree(
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[str, int, int]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"Skill source snapshot is not a directory: {resolved}")
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted(resolved.rglob("*")):
        relative = path.relative_to(resolved)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise ValueError(
                f"Skill source snapshot contains a symbolic link: {relative.as_posix()}"
            )
        if not path.is_file():
            continue
        file_count += 1
        size = path.stat().st_size
        total_bytes += size
        if file_count > max_files:
            raise ValueError(f"Skill source snapshot exceeds max_files={max_files}.")
        if total_bytes > max_bytes:
            raise ValueError(f"Skill source snapshot exceeds max_bytes={max_bytes}.")
        relative_bytes = relative.as_posix().encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", file_count, total_bytes


def materialize_snapshot(
    source: Path,
    *,
    cache_root: Path,
    provider_id: str,
    max_files: int,
    max_bytes: int,
) -> tuple[Path, str, int, int]:
    digest, file_count, total_bytes = inspect_source_tree(
        source,
        max_files=max_files,
        max_bytes=max_bytes,
    )
    destination = cache_root / provider_id / digest.removeprefix("sha256:")
    if destination.is_dir():
        return destination.resolve(), digest, file_count, total_bytes
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="skill-source-", dir=destination.parent))
    try:
        for item in sorted(source.iterdir()):
            if item.name == ".git":
                continue
            target = temporary / item.name
            if item.is_symlink():
                raise ValueError(f"Skill source snapshot contains a symbolic link: {item.name}")
            if item.is_dir():
                shutil.copytree(item, target, symlinks=True)
            elif item.is_file():
                shutil.copy2(item, target)
        # Reinspect the copied tree so the digest is evidence about the actual
        # immutable materialization, not merely the mutable source.
        copied_digest, copied_files, copied_bytes = inspect_source_tree(
            temporary,
            max_files=max_files,
            max_bytes=max_bytes,
        )
        if (copied_digest, copied_files, copied_bytes) != (
            digest,
            file_count,
            total_bytes,
        ):
            raise ValueError("Skill source changed while its snapshot was materialized.")
        try:
            temporary.replace(destination)
        except FileExistsError:
            shutil.rmtree(temporary, ignore_errors=True)
        return destination.resolve(), digest, file_count, total_bytes
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


__all__ = ["inspect_source_tree", "materialize_snapshot", "select_source_root"]
