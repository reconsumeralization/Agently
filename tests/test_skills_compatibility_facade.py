from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from agently.builtins.plugins.SkillSourceProvider import GitSkillSourceProvider
from agently.core.application.SkillLibrary import SkillLibrary
from agently.core.application.SkillsExecutor import SkillsExecutor


def _write_skill(root: Path) -> Path:
    (root / "references").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: Compatibility Skill\n"
        "description: Exercises the released facade.\n"
        "---\n\n"
        "Apply the compatibility procedure.",
        encoding="utf-8",
    )
    (root / "references" / "guide.md").write_text("Compatibility guide", encoding="utf-8")
    (root / "scripts" / "check.py").write_text("print('check')", encoding="utf-8")
    return root


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _remote_pack(root: Path) -> tuple[Path, str]:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Agently Tests")
    _write_skill(root / "skills" / "compatibility-skill")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "remote pack")
    return root, _git(root, "rev-parse", "HEAD")


def test_released_install_inspect_list_and_read_delegate_to_skill_library(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    facade = SkillsExecutor(library=library)
    calls: list[tuple[str, object]] = []
    original_install = library.install
    original_resolve = library.resolve
    original_list = library.list
    original_read = library.read_resource

    def install(*args, **kwargs):
        calls.append(("install", args[0]))
        return original_install(*args, **kwargs)

    def resolve(*args, **kwargs):
        calls.append(("resolve", args[0]))
        return original_resolve(*args, **kwargs)

    def list_packages():
        calls.append(("list", None))
        return original_list()

    def read(*args, **kwargs):
        calls.append(("read", args[1]))
        return original_read(*args, **kwargs)

    monkeypatch.setattr(library, "install", install)
    monkeypatch.setattr(library, "resolve", resolve)
    monkeypatch.setattr(library, "list", list_packages)
    monkeypatch.setattr(library, "read_resource", read)

    installed = facade.install_skills(_write_skill(tmp_path / "skill"))
    inspected = facade.inspect_skills(installed["skill_id"])
    listed = facade.list_skills()
    content = facade.read_resource(installed["skill_id"], "references/guide.md")

    assert [item[0] for item in calls] == ["install", "resolve", "list", "resolve", "read"]
    assert installed["skill_id"] == "compatibility-skill"
    assert installed["source"]["installed_path"]
    assert installed["guidance"]["content"] == "Apply the compatibility procedure."
    assert inspected["checksums"]["root_checksum"]
    assert listed[0]["skill_id"] == installed["skill_id"]
    assert content == "Compatibility guide"
    assert facade.registry.library is library


def test_pack_management_is_a_thin_skill_library_projection(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    facade = SkillsExecutor(library=library).configure(
        allowed_trust_levels=["local"],
    )
    pack_root = tmp_path / "pack"
    _write_skill(pack_root / "first")
    second = _write_skill(pack_root / "second")
    (second / "SKILL.md").write_text(
        "---\nname: Second Skill\ndescription: Second procedure.\n---\n\nApply it.",
        encoding="utf-8",
    )

    installed = facade.install_skills_pack(
        pack_root,
        name="compatibility-pack",
        trust_level="local",
    )

    assert installed["status"] == "success"
    assert installed["skill_pack_id"] == "compatibility-pack"
    assert installed["installed_skills"] == ["compatibility-skill", "second-skill"]
    assert facade.inspect_skills_pack("compatibility-pack") == installed
    assert facade.list_skills_packs() == [installed]
    listed_pack_skills = facade.list_skills()
    assert {item["skills_pack_id"] for item in listed_pack_skills} == {
        "compatibility-pack"
    }
    assert {item["skills_pack_name"] for item in listed_pack_skills} == {
        "compatibility-pack"
    }
    assert facade.inspect_skills("compatibility-skill")["source"][
        "skills_pack_id"
    ] == "compatibility-pack"

    discovery_root = tmp_path / "discover-only"
    _write_skill(discovery_root / "skill")
    discovered = facade.discover_skills_pack(
        discovery_root,
        name="discovery-pack",
        trust_level="local",
    )
    assert discovered["status"] == "success"
    assert discovered["contracts"][0]["skill_id"] == "compatibility-skill"
    assert {item["skill_id"] for item in facade.list_skills()} == {
        "compatibility-skill",
        "second-skill",
    }


def test_configure_rebinds_the_canonical_skill_library_in_place(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "initial-library")
    facade = SkillsExecutor(library=library)
    canonical_reference = facade.library

    facade.configure(registry_root=tmp_path / "configured-library")
    installed = facade.install_skills(_write_skill(tmp_path / "configured-skill"))

    assert facade.library is canonical_reference
    assert facade.registry.library is canonical_reference
    assert canonical_reference.root == (tmp_path / "configured-library").resolve()
    assert canonical_reference.resolve(installed["skill_id"]).revision_ref


@pytest.mark.asyncio
async def test_context_pack_accepts_an_installed_skill_pack_selector(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    facade = SkillsExecutor(library=library)
    pack_root = tmp_path / "pack"
    _write_skill(pack_root / "first")
    second = _write_skill(pack_root / "second")
    (second / "SKILL.md").write_text(
        "---\nname: Second Skill\ndescription: Second procedure.\n---\n\nApply it.",
        encoding="utf-8",
    )
    installed_pack = facade.install_skills_pack(
        pack_root,
        skills_pack_id="context-pack",
        trust_level="local",
    )

    compatibility = await facade.async_build_context_pack(
        task="Apply both procedures",
        skills_packs=[installed_pack["skill_pack_id"]],
        include_guidance=True,
        budget_chars=4000,
    )

    assert {item["skill_id"] for item in compatibility["skills"]} == {
        "compatibility-skill",
        "second-skill",
    }
    assert not any(
        item["code"] == "skills.compat.pack_selector_unsupported"
        for item in compatibility["diagnostics"]
    )


@pytest.mark.asyncio
async def test_context_pack_is_projection_of_generic_context_package(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    facade = SkillsExecutor(library=library)
    installed = facade.install_skills(_write_skill(tmp_path / "skill"))

    compatibility = await facade.async_build_context_pack(
        task="Use the compatibility guide",
        skill_ids=[installed["skill_id"]],
        include_guidance=True,
        include_references=True,
        include_examples=False,
        include_assets=False,
        budget_chars=4000,
    )

    assert compatibility["schema_version"] == "agently.skills.context_pack.compat.v2"
    assert compatibility["context_package_id"].startswith("context_package:")
    assert compatibility["task_context_id"].startswith("skills_compat:")
    assert compatibility["skills"][0]["skill_id"] == installed["skill_id"]
    assert compatibility["skills"][0]["guidance"]["excerpt"] == (
        "Apply the compatibility procedure."
    )
    assert compatibility["skills"][0]["selected_resources"][0]["path"] == (
        "references/guide.md"
    )
    assert compatibility["used_chars"] > 0


@pytest.mark.asyncio
async def test_context_pack_never_actionizes_scripts_or_grants_permissions(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    facade = SkillsExecutor(library=library)
    installed = facade.install_skills(_write_skill(tmp_path / "skill"))

    compatibility = await facade.async_build_context_pack(
        task="Check the package",
        skill_ids=[installed["skill_id"]],
        include_references=False,
        actionize_scripts=True,
    )

    assert compatibility["skills"][0]["action_candidates"] == []
    assert any(
        item["code"] == "skills.compat.actionize_scripts_ignored"
        for item in compatibility["diagnostics"]
    )
    assert not hasattr(facade, "register_effort_strategy")
    assert not hasattr(facade, "async_execute_plan")


@pytest.mark.asyncio
async def test_task_dag_resolver_calls_same_context_reader_projection(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    facade = SkillsExecutor(library=library)
    installed = facade.install_skills(_write_skill(tmp_path / "skill"))
    resolver = facade.task_dag_resolver(defaults={"include_references": True})

    result = await resolver["skill"](
        {"task": "Read the guide", "skill_ids": [installed["skill_id"]]}
    )

    assert result["context_package_id"].startswith("context_package:")
    assert result["skills"][0]["selected_resources"][0]["path"] == "references/guide.md"


def test_facade_source_has_no_old_internal_owner_or_execution_strategy_imports() -> None:
    module = inspect.getmodule(SkillsExecutor)
    assert module is not None
    source = inspect.getsource(module)

    for forbidden in (
        "SkillsManager",
        "SkillPlanner",
        "SkillExecutor(",
        "effort_strateg",
        "AgentlySkillsExecutor",
        "ActionRuntime",
        "PolicyApproval",
        "AgentTask",
    ):
        assert forbidden not in source


def test_compatibility_facade_resolves_remote_pack_through_skill_library(
    tmp_path: Path,
) -> None:
    source, commit = _remote_pack(tmp_path / "remote")
    library = SkillLibrary(tmp_path / "library")
    library.register_source_provider(
        GitSkillSourceProvider(cache_root=tmp_path / "source-cache")
    )
    facade = SkillsExecutor(library=library)

    result = facade.install_skills_pack(
        source,
        fetch=True,
        ref="main",
        subpath="skills",
        source_type="git",
        trust_level="trusted",
        skills_pack_id="remote-pack",
    )

    assert result["source_provenance"]["resolved_revision"] == commit
    assert result["source_provenance"]["requested_ref"] == "main"
    assert result["installed_skills"] == ["compatibility-skill"]


def test_compatibility_facade_defaults_remote_pack_to_untrusted(
    tmp_path: Path,
) -> None:
    source, _commit = _remote_pack(tmp_path / "remote")
    library = SkillLibrary(tmp_path / "library")
    library.register_source_provider(
        GitSkillSourceProvider(cache_root=tmp_path / "source-cache")
    )
    facade = SkillsExecutor(library=library)

    result = facade.install_skills_pack(
        source,
        fetch=True,
        ref="main",
        subpath="skills",
        source_type="git",
        skills_pack_id="remote-pack",
    )

    assert result["trust_level"] == "untrusted"
    assert facade.inspect_skills("compatibility-skill")["trust_level"] == (
        "untrusted"
    )
