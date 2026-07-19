from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agently.builtins.plugins.SkillSourceProvider import (
    GitSkillSourceProvider,
    LocalPathSkillSourceProvider,
)
from agently.types.data import SkillSourceRequest


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_skill(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\nname: Remote Test\ndescription: Test remote materialization.\n---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _create_git_fixture(root: Path) -> tuple[Path, str]:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Agently Tests")
    _write_skill(root / "skills" / "remote-test", "first revision")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "first")
    return root, _git(root, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_local_provider_materializes_an_immutable_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source, "original")
    provider = LocalPathSkillSourceProvider(cache_root=tmp_path / "cache")

    snapshot = await provider.async_materialize(
        SkillSourceRequest(source=str(source), source_type="local")
    )
    snapshot_path = Path(snapshot.materialized_path)
    (source / "SKILL.md").write_text("tampered", encoding="utf-8")

    assert snapshot.provider_id == "local"
    assert snapshot.source_type == "local"
    assert snapshot.source_digest.startswith("sha256:")
    assert "original" in (snapshot_path / "SKILL.md").read_text(encoding="utf-8")
    assert "tampered" not in (snapshot_path / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_git_provider_resolves_branch_to_exact_commit_and_freezes_content(tmp_path: Path) -> None:
    repo, first_commit = _create_git_fixture(tmp_path / "repo")
    provider = GitSkillSourceProvider(cache_root=tmp_path / "cache")

    snapshot = await provider.async_materialize(
        SkillSourceRequest(
            source=str(repo),
            source_type="git",
            ref="main",
            subpath="skills/remote-test",
        )
    )
    frozen = Path(snapshot.materialized_path) / "SKILL.md"
    _write_skill(repo / "skills" / "remote-test", "second revision")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "second")

    assert snapshot.requested_ref == "main"
    assert snapshot.resolved_revision == first_commit
    assert "first revision" in frozen.read_text(encoding="utf-8")
    assert "second revision" not in frozen.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_git_provider_reports_missing_subpath_without_leaking_cache_state(tmp_path: Path) -> None:
    repo, _ = _create_git_fixture(tmp_path / "repo")
    provider = GitSkillSourceProvider(cache_root=tmp_path / "cache")

    with pytest.raises(ValueError, match="subpath|not.*directory"):
        await provider.async_materialize(
            SkillSourceRequest(
                source=str(repo),
                source_type="git",
                ref="main",
                subpath="skills/missing",
            )
        )


@pytest.mark.asyncio
async def test_source_provider_rejects_symlink_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source, "safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (source / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Filesystem does not permit symlink creation.")

    with pytest.raises(ValueError, match="symbolic link"):
        await LocalPathSkillSourceProvider(
            cache_root=tmp_path / "cache"
        ).async_materialize(
            SkillSourceRequest(source=str(source), source_type="local")
        )


@pytest.mark.asyncio
async def test_local_provider_rejects_symlink_selected_root_escape(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    _write_skill(outside, "secret outside skill")
    try:
        (source / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Filesystem does not permit symlink creation.")

    with pytest.raises(ValueError, match="symbolic link|escapes"):
        await LocalPathSkillSourceProvider(
            cache_root=tmp_path / "cache"
        ).async_materialize(
            SkillSourceRequest(
                source=str(source),
                source_type="local",
                subpath="escape",
            )
        )


@pytest.mark.asyncio
async def test_git_provider_rejects_symlink_selected_root_escape(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Agently Tests")
    outside = tmp_path / "outside"
    _write_skill(outside, "secret outside skill")
    (repo / "skills").mkdir()
    try:
        (repo / "skills" / "escape").symlink_to(
            outside,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("Filesystem does not permit symlink creation.")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "malicious symlink")

    with pytest.raises(ValueError, match="symbolic link|escapes"):
        await GitSkillSourceProvider(
            cache_root=tmp_path / "cache"
        ).async_materialize(
            SkillSourceRequest(
                source=str(repo),
                source_type="git",
                ref="main",
                subpath="skills/escape",
            )
        )
