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
        assert example["provider"] == "Explicitly configured online model"


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


def test_release_pinned_agent_execution_chain_and_debug_profiles_are_locked() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    lifecycle_gate = next(
        script
        for script in manifest["selected_scripts"]
        if script["path"].endswith("01_agent_execution_result_lifecycle.py")
    )
    debug_gate = next(
        script
        for script in manifest["selected_scripts"]
        if script["path"].endswith("04_debug_console_profiles.py")
    )

    lifecycle_source = (ROOT / lifecycle_gate["path"]).read_text(encoding="utf-8")
    debug_source = (ROOT / debug_gate["path"]).read_text(encoding="utf-8")

    assert "agent.input(...).info(...).instruct(...).output(...).get_result()" in lifecycle_gate[
        "protected_usage"
    ][1]
    assert '.info("Release-pinned supporting context.")' in lifecycle_source
    assert "debug=True remains the readable simple console profile" in debug_gate["protected_usage"]
    assert 'runtime.progress.' in debug_source
    assert "event_center_keeps_runtime_progress" in debug_source
    assert any(
        "ExecutionResource self-check" in item for item in debug_gate["protected_usage"]
    )
    assert "execution_resource_simple_self_check" in debug_source
    assert "execution_resource_simple_pull_is_readable" in debug_source
    assert any(
        "structured Action planning excludes" in item for item in debug_gate["protected_usage"]
    )
    assert "action_planning_projection_is_compact" in debug_source
    assert "concurrent_fifo_stream_blocks" in debug_source
    assert "concurrent_background_notice_once" in debug_source
    assert "concurrent_request_process_deferred" in debug_source
    assert "concurrent_deferred_details_after_results" in debug_source
    assert "concurrent_single_notice_no_resume_header" in debug_source
    assert "simple_result_without_stream_is_complete" in debug_source
    assert "simple_overflow_fallback_is_complete" in debug_source
    assert any(
        "concurrent ModelRequests keep execution concurrency" in item
        for item in debug_gate["protected_usage"]
    )
    assert any(
        "defers bounded Prompt/request/process/success diagnostics" in item
        for item in debug_gate["protected_usage"]
    )
    assert any(
        "simple mode preserves at least one complete successful response projection" in item
        for item in debug_gate["protected_usage"]
    )


def test_release_pinned_action_response_delivery_is_locked() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    action_response_gate = next(
        script
        for script in manifest["selected_scripts"]
        if script["path"].endswith("05_action_response_delivery.py")
    )
    source = (ROOT / action_response_gate["path"]).read_text(encoding="utf-8")

    assert "agent.input(...).info(...).use_action(...) preserves one AgentExecution" in action_response_gate[
        "protected_usage"
    ]
    assert "model_request_count=2" in source
    assert "fluent_chain_same_execution" in source
    assert "info_present_in_each_round" in source
    assert "simple_action_decision_hidden" in source
    assert "simple_response_once" in source
