from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_ROOT = ROOT / "examples" / "release_pinned_usage"
MANIFEST_PATH = PINNED_ROOT / "pinned_usage_manifest.json"


def test_release_pinned_usage_manifest_paths_exist() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["policy"]["directory"] == "examples/release_pinned_usage"
    assert "maintainer confirmation" in manifest["policy"]["edit_rule"]
    assert "all-allowed test capability policy" in manifest["policy"]["release_test_permissions"]

    selected_scripts = manifest["selected_scripts"]
    assert selected_scripts
    for script in selected_scripts:
        path = script["path"]
        assert path.startswith("examples/release_pinned_usage/")
        assert (ROOT / path).is_file()
        assert script["requires_human_confirmation_for_edits"] is True
        assert script["protected_usage"]
        assert script["release_gate_reason"]

    model_examples = manifest["model_backed_release_examples"]
    assert model_examples
    for example in model_examples:
        assert (ROOT / example["path"]).is_file()
        assert example["provider"] == "DeepSeek or local Ollama"


def test_release_pinned_usage_readme_records_confirmation_policy() -> None:
    readme = (PINNED_ROOT / "README.md").read_text(encoding="utf-8")

    assert "release gates" in readme
    assert "must not be edited, replaced, or removed without explicit" in readme
    assert "ask whether the release should accept that usage update" in readme
    assert "all-allowed test capability policy" in readme


def test_release_pinned_skill_usage_tracks_current_owner_boundaries() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    skill_gate = next(
        script
        for script in manifest["selected_scripts"]
        if script["path"].endswith("03_skill_library_agent_binding.py")
    )
    source = (ROOT / skill_gate["path"]).read_text(encoding="utf-8")

    assert "Agently.skill_library.resolve(...)" in skill_gate["protected_usage"]
    assert "agent.require_skills(exact_revision_ref)" in skill_gate["protected_usage"]
    assert "resolve_skills_plan" not in source
    assert "prompt_bindings" not in source
    assert "guidance_injected" not in source
