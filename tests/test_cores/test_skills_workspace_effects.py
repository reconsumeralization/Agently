from __future__ import annotations

from pathlib import Path

from agently import Agently
from agently.core import PluginManager
from agently.core.application.SkillsManager import SkillsManager
from agently.utils import Settings


def _write_skill(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        """---
name: Workspace Effect Probe
description: Probe Workspace-local Skills storage effects.
---

# Workspace Effect Probe

Use this Skill only to inspect storage ownership.
""",
        encoding="utf-8",
    )


def _private_entries(workspace_root: Path) -> list[str]:
    private_root = workspace_root / ".agently"
    if not private_root.exists():
        return []
    return sorted(path.name for path in private_root.iterdir())


def test_configuring_workspace_local_skills_registry_is_side_effect_free(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace = Agently.create_workspace(workspace_root)
    settings = Settings(name="WorkspaceSkillsEffects", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="WorkspaceSkillsEffectsPlugins",
    )
    manager = SkillsManager(plugin_manager, settings).configure(
        registry_root=workspace_root / ".agently" / "skills",
        allowed_trust_levels=["local"],
    )

    assert manager.registry.root == workspace_root / ".agently" / "skills"
    assert _private_entries(workspace_root) == []
    assert workspace.capabilities()["materialized_components"] == []


def test_workspace_local_skill_discovery_and_install_create_only_skills_state(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace = Agently.create_workspace(workspace_root)
    source = tmp_path / "source-skill"
    _write_skill(source)
    settings = Settings(name="WorkspaceSkillsInstall", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="WorkspaceSkillsInstallPlugins",
    )
    manager = SkillsManager(plugin_manager, settings).configure(
        registry_root=workspace_root / ".agently" / "skills",
        allowed_trust_levels=["local"],
    )

    assert manager.discover_skill_capabilities() == []
    contract = manager.install_skills(source)
    activated = manager.activate_skill(contract["skill_id"], task="Inspect storage effects")

    assert activated.skill_id == "workspace-effect-probe"
    assert _private_entries(workspace_root) == ["skills"]
    assert (workspace_root / ".agently" / "skills" / "index.json").is_file()
    assert not (workspace_root / ".agently" / "workspace.db").exists()
    assert not (workspace_root / ".agently" / "vectors").exists()
    assert not (workspace_root / ".agently" / "recovery").exists()
    assert not (workspace_root / ".agently" / "memory").exists()
    assert not (workspace_root / ".agently" / "files").exists()
    assert workspace.capabilities()["materialized_components"] == []
