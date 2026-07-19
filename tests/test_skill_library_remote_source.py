from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agently.builtins.plugins.SkillSourceProvider import GitSkillSourceProvider
from agently.core.application.SkillLibrary import SkillLibrary
from agently.types.data import SkillSourceRequest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_skill(root: Path, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\nname: Remote Library Skill\n"
        "description: Exercise remote Skill installation.\n---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _repo(root: Path) -> tuple[Path, str]:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Agently Tests")
    _write_skill(root / "skills" / "remote", "first body")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "first")
    return root, _git(root, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_skill_library_installs_exact_remote_snapshot_with_provenance(tmp_path: Path) -> None:
    repo, commit = _repo(tmp_path / "repo")
    library = SkillLibrary(tmp_path / "library")
    library.register_source_provider(
        GitSkillSourceProvider(cache_root=tmp_path / "source-cache")
    )

    package = await library.async_install_source(
        SkillSourceRequest(
            source=str(repo),
            source_type="git",
            ref="main",
            subpath="skills/remote",
        ),
        scope="explicit",
        trust="trusted",
    )

    assert package.instruction_body == "first body"
    assert package.source == str(repo)
    assert package.source_provenance["provider_id"] == "git"
    assert package.source_provenance["requested_ref"] == "main"
    assert package.source_provenance["resolved_revision"] == commit
    assert str(package.source_provenance["source_digest"]).startswith("sha256:")
    assert "materialized_path" not in package.source_provenance


@pytest.mark.asyncio
async def test_remote_update_creates_new_content_revision_without_mutating_old(tmp_path: Path) -> None:
    repo, first_commit = _repo(tmp_path / "repo")
    library = SkillLibrary(tmp_path / "library")
    library.register_source_provider(
        GitSkillSourceProvider(cache_root=tmp_path / "source-cache")
    )
    request = SkillSourceRequest(
        source=str(repo),
        source_type="git",
        ref="main",
        subpath="skills/remote",
    )
    first = await library.async_install_source(request, trust="trusted")

    _write_skill(repo / "skills" / "remote", "second body")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "second")
    second_commit = _git(repo, "rev-parse", "HEAD")
    second = await library.async_install_source(
        SkillSourceRequest(
            source=str(repo),
            source_type="git",
            ref="main",
            subpath="skills/remote",
            update=True,
        ),
        trust="trusted",
    )

    assert first.revision != second.revision
    assert first.source_provenance["resolved_revision"] == first_commit
    assert second.source_provenance["resolved_revision"] == second_commit
    assert library.resolve(first.revision_ref).instruction_body == "first body"
    assert library.resolve(second.revision_ref).instruction_body == "second body"


@pytest.mark.asyncio
async def test_skill_library_fails_closed_for_unknown_remote_source_provider(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")

    with pytest.raises(ValueError, match="source provider|source_type"):
        await library.async_install_source(
            SkillSourceRequest(
                source="registry://example/skill",
                source_type="registry",
            )
        )


@pytest.mark.asyncio
async def test_skill_library_configure_rebinds_library_owned_source_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_skill(source, "configured root")
    first_root = tmp_path / "first-library"
    second_root = tmp_path / "second-library"
    library = SkillLibrary(first_root)

    library.configure(root=second_root)
    package = await library.async_install_source(
        SkillSourceRequest(source=str(source), source_type="local"),
        trust="trusted",
    )

    assert Path(package.installed_path).is_relative_to(second_root)
    assert (second_root / "source-cache").is_dir()
    assert not (first_root / "source-cache").exists()
