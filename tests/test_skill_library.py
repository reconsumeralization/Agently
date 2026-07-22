from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.application.SkillLibrary import (
    SkillLibrary,
    SkillPackageError,
    SkillPackageRevision,
)


def _write_skill(root: Path, *, body: str = "Follow the verified writing process.") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "references").mkdir(exist_ok=True)
    (root / "examples").mkdir(exist_ok=True)
    (root / "assets").mkdir(exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: Verified Writing\n"
        "description: Create and verify a structured report.\n"
        "version: 1.0.0\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    (root / "references" / "guide.md").write_text("Authoritative guide", encoding="utf-8")
    (root / "examples" / "report.md").write_text("# Example report", encoding="utf-8")
    (root / "assets" / "template.txt").write_text("TITLE={{title}}", encoding="utf-8")
    (root / "scripts" / "validate.py").write_text(
        "from pathlib import Path\nPath('executed.txt').write_text('bad')\n",
        encoding="utf-8",
    )
    return root


def test_skill_library_installs_lossless_real_world_package(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "source")
    library = SkillLibrary(tmp_path / "library")

    package = library.install(source, scope="project", trust="trusted")

    assert isinstance(package, SkillPackageRevision)
    assert package.skill_id == "verified-writing"
    assert package.canonical_ref == "skill:verified-writing"
    assert package.revision.startswith("sha256:")
    assert package.revision_ref == f"{package.canonical_ref}@{package.revision}"
    assert package.name == "Verified Writing"
    assert package.description == "Create and verify a structured report."
    assert package.version == "1.0.0"
    assert package.scope == "project"
    assert package.trust == "trusted"
    assert package.instruction_body == "Follow the verified writing process."
    assert Path(package.installed_path).is_dir()

    resources = {item.path: item for item in package.resources}
    assert resources["SKILL.md"].kind == "instruction"
    assert resources["references/guide.md"].kind == "reference"
    assert resources["examples/report.md"].kind == "example"
    assert resources["assets/template.txt"].kind == "asset"
    assert resources["scripts/validate.py"].kind == "script"
    assert resources["scripts/validate.py"].executable is True
    assert not (Path(package.installed_path) / "executed.txt").exists()


def test_identical_content_deduplicates_and_changed_content_creates_revision(
    tmp_path: Path,
) -> None:
    source = _write_skill(tmp_path / "source")
    library = SkillLibrary(tmp_path / "library")

    first = library.install(source, scope="explicit", trust="trusted")
    same = library.install(source, scope="explicit", trust="trusted")
    _write_skill(source, body="Use the revised verified writing process.")
    revised = library.install(source, scope="explicit", trust="trusted")

    assert same.revision_ref == first.revision_ref
    assert same.installed_path == first.installed_path
    assert revised.canonical_ref == first.canonical_ref
    assert revised.revision != first.revision
    assert revised.installed_path != first.installed_path
    assert library.resolve(first.revision_ref).instruction_body == first.instruction_body
    assert library.resolve(revised.canonical_ref).revision == revised.revision
    assert [item.revision for item in library.list_revisions(first.canonical_ref)] == [
        first.revision,
        revised.revision,
    ]


def test_mutable_source_cannot_change_installed_revision(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "source")
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(source, trust="trusted")

    (source / "references" / "guide.md").write_text("Tampered draft", encoding="utf-8")

    readback = library.read_resource(installed.revision_ref, "references/guide.md")
    assert readback.text == "Authoritative guide"
    assert readback.truncated is False
    assert library.resolve(installed.revision_ref).revision == installed.revision


def test_exact_resource_read_enforces_boundary_and_byte_limit(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "source")
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(source, trust="trusted")

    bounded = library.read_resource(
        installed.revision_ref,
        "references/guide.md",
        max_bytes=5,
    )

    assert bounded.data == b"Autho"
    assert bounded.text == "Autho"
    assert bounded.total_bytes == len(b"Authoritative guide")
    assert bounded.truncated is True
    assert bounded.sha256 == next(
        item.sha256 for item in installed.resources if item.path == "references/guide.md"
    )

    for unsafe in ("../outside.txt", "/etc/passwd", ".agently/package.json"):
        with pytest.raises(SkillPackageError, match="resource path"):
            library.read_resource(installed.revision_ref, unsafe)


def test_skill_library_never_executes_or_owns_task_behavior(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "source")
    library = SkillLibrary(tmp_path / "library")
    package = library.install(source, trust="trusted")

    assert not (source / "executed.txt").exists()
    assert not (Path(package.installed_path) / "executed.txt").exists()
    assert not hasattr(library, "execute")
    assert not hasattr(library, "run")
    assert not hasattr(library, "select_for_task")
    assert not hasattr(library, "register_action")
    assert not hasattr(library, "request")


def test_install_rejects_symlink_and_malformed_package_boundaries(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    missing = tmp_path / "missing"
    with pytest.raises(SkillPackageError, match="SKILL.md"):
        library.install(missing)

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / "SKILL.md").write_text("No frontmatter", encoding="utf-8")
    with pytest.raises(SkillPackageError, match="frontmatter.*name"):
        library.install(malformed)

    source = _write_skill(tmp_path / "symlinked")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (source / "references" / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Filesystem does not permit symlink creation.")
    with pytest.raises(SkillPackageError, match="symbolic link"):
        library.install(source)


def test_install_defaults_to_untrusted_explicit_scope(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "source")
    package = SkillLibrary(tmp_path / "library").install(source)

    assert package.scope == "explicit"
    assert package.trust == "untrusted"
