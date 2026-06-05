import json
from pathlib import Path

from agently.compatibility import (
    CURRENT_FRAMEWORK_VERSION,
    CURRENT_RELEASE_TRAIN,
    get_current_release_manifest,
    get_devtools_compatibility_manifest,
    get_skills_compatibility_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "compatibility" / "index.json"
IN_DEVELOPMENT_PATH = ROOT / "compatibility" / "in-development.json"


def test_current_release_manifest_matches_registry_release_file():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    release_path = ROOT / index["release_files"][CURRENT_FRAMEWORK_VERSION]
    release_manifest = json.loads(release_path.read_text(encoding="utf-8"))
    current_manifest = get_current_release_manifest()

    assert index["latest_release"] == CURRENT_FRAMEWORK_VERSION
    assert current_manifest["schema_version"] == release_manifest["schema_version"]
    assert current_manifest["framework"] == release_manifest["framework"]
    assert current_manifest["framework_version"] == release_manifest["framework_version"]
    assert current_manifest["release_train"] == release_manifest["release_train"]
    assert current_manifest["companions"]["skills"] == release_manifest["companions"]["skills"]

    current_devtools = dict(current_manifest["companions"]["devtools"])
    release_devtools = dict(release_manifest["companions"]["devtools"])
    current_devtools.pop("recommended_version_specifier", None)
    release_devtools.pop("recommended_version_specifier", None)
    assert current_devtools == release_devtools


def test_devtools_and_skills_companion_views_derive_from_current_release_manifest():
    current = get_current_release_manifest()
    devtools = get_devtools_compatibility_manifest()
    skills = get_skills_compatibility_manifest()

    assert devtools["framework_version"] == CURRENT_FRAMEWORK_VERSION
    assert devtools["release_train"] == CURRENT_RELEASE_TRAIN
    assert devtools["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]

    assert skills["framework_version"] == CURRENT_FRAMEWORK_VERSION
    assert skills["release_train"] == CURRENT_RELEASE_TRAIN
    assert skills["authoring_protocol"] == current["companions"]["skills"]["authoring_protocol"]
    assert (
        skills["devtools_guidance_protocol"]
        == current["companions"]["skills"]["devtools_guidance_protocol"]
    )


def test_in_development_manifest_is_registered_and_protocol_compatible():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    in_development = json.loads(IN_DEVELOPMENT_PATH.read_text(encoding="utf-8"))
    current = get_current_release_manifest()

    assert index["in_development_file"] == "compatibility/in-development.json"
    assert in_development["framework"] == "agently"
    assert index["latest_release"] == CURRENT_FRAMEWORK_VERSION
    assert in_development["target_version"] == "4.1.3.6"
    assert in_development["companions"]["devtools"]["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]
    assert in_development["companions"]["devtools"]["event_naming"] == {
        "preferred_event_type": "RuntimeEvent",
        "devtools_projection_type": "ObservationEvent",
        "event_center_dispatch": "RuntimeEvent",
        "compatibility_input_type": "ObservationEvent",
    }
    assert in_development["companions"]["devtools"]["runtime_control"] == {
        "runtime_event_ownership": {
            "official_event_producer": "core",
            "plugin_contract": "plugins return observations/errors/decisions; core maps them to official RuntimeEvent records",
            "builtin_direct_emitters_for_official_events": False,
            "agent_execution_stream_owner": "agently.core.application.AgentExecution.AgentExecutionStream",
        },
        "runtime_naming": {
            "agent_turn": "run_kind for one Agent-facing turn",
            "attempt_index": "model-request retry attempt metadata; not an agent turn counter",
        },
        "agent_execution_limits": ["max_seconds", "max_no_progress_seconds"],
        "provider_stream_idle_timeout": [
            "OpenAICompatible.stream_idle_timeout",
            "OpenAIResponsesCompatible.stream_idle_timeout",
        ],
        "response_materialization_idle_timeout": "response.materialization_idle_timeout",
        "typed_stall_error": "RuntimeStageStallError",
        "typed_provider_stall_stages": [
            "response_first_event",
            "response_stream",
        ],
        "action_runtime_stall_stages": [
            "action_planning",
            "tool_call_selection",
            "action_execution",
            "action_loop_close",
        ],
        "event_center_delivery_policy": {
            "register_hook_parameter": "delivery_policy",
            "hooker_attribute": "delivery_policy",
            "fields": [
                "mode",
                "dispatch",
                "emit_interval",
                "max_items",
                "high_frequency_only",
                "max_summary_items",
            ],
            "background_reclaim": "idle_flush_and_explicit_flush",
            "default_delivery": "raw",
            "summary_marker": "meta.coalesced",
        },
    }
    assert in_development["companions"]["skills"]["authoring_protocol"] == "agently-skills.authoring.v2"
    assert in_development["companions"]["skills"]["authoring_format"] == "standard SKILL.md only"
    runtime_capability_contract = in_development["companions"]["skills"]["runtime_capability_contract"]
    assert "agent.configure_skill_capabilities" in runtime_capability_contract["host_policy_surface"]
    assert "agent.configure_policy_approval" in runtime_capability_contract["host_policy_surface"]
    assert runtime_capability_contract["policy_modes"] == ["allow", "approval", "off"]
    assert "capability_needs" in runtime_capability_contract["skill_capability_needs"]
    assert "script_run" in runtime_capability_contract["auto_loadable_needs"]
    assert "allowed-actions" in runtime_capability_contract["removed_private_skill_fields"]
    assert (
        in_development["companions"]["skills"]["devtools_guidance_protocol"]
        == current["companions"]["skills"]["devtools_guidance_protocol"]
    )
    assert in_development["companions"]["skills"]["catalog_generation"] == "v2"
    assert in_development["companions"]["skills"]["recommended_bundle"] == "app"
    turn_contract = in_development["request_input"]["agent_turn_request_scope"]
    assert "AgentTurn" in turn_contract["surface"]
    assert "isolated AgentTurn request draft" in turn_contract["contract"]
    assert in_development["companions"]["skills"]["legacy_generations"] == [
        {
            "generation": "v1",
            "last_supported_framework_version": "4.1.1",
            "status": "frozen",
        }
    ]
