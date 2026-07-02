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
    current_runtime_control = dict(current_devtools.pop("runtime_control"))
    release_runtime_control = dict(release_devtools.pop("runtime_control"))
    assert current_devtools == release_devtools
    assert release_runtime_control.items() <= current_runtime_control.items()
    assert current_runtime_control["model_request_telemetry_contract"].startswith(
        "Existing model RuntimeEvents may carry payload.model_request_telemetry"
    )


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
    assert in_development["target_version"] == "4.1.3.8"
    assert in_development["release_train"] == "2026-06-4.1.3.8-dev"
    assert "framework-level bug fixes and structural cleanup" in in_development["notes"]
    assert "business-specific special-case fixes" in in_development["notes"]
    assert in_development["companions"]["devtools"]["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]
    assert in_development["companions"]["devtools"]["event_naming"] == {
        "preferred_event_type": "RuntimeEvent",
        "devtools_projection_type": "ObservationEvent",
        "event_center_dispatch": "RuntimeEvent",
        "compatibility_input_type": "ObservationEvent",
    }
    expected_runtime_control = {
        "runtime_event_ownership": {
            "official_event_producer": "core",
            "plugin_contract": "plugins return observations/errors/decisions; core maps them to official RuntimeEvent records",
            "builtin_direct_emitters_for_official_events": False,
            "agent_execution_stream_owner": "agently.core.application.AgentExecution.AgentExecutionStream",
        },
        "runtime_naming": {
            "agent_execution": "run_kind for one AgentExecution-owned Agent run",
            "attempt_index": "model-request retry attempt metadata; not an AgentExecution counter",
        },
        "agent_execution_limits": ["max_seconds", "max_no_progress_seconds"],
        "provider_stream_idle_timeout": [
            "OpenAICompatible.stream_idle_timeout",
            "OpenAIResponsesCompatible.stream_idle_timeout",
            "AnthropicCompatible.stream_idle_timeout",
        ],
        "response_materialization_idle_timeout": "response.materialization_idle_timeout",
        "typed_stall_error": "RuntimeStageStallError",
        "typed_provider_stall_stages": [
            "response_first_event",
            "response_stream",
            "response_materialization",
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
    in_development_runtime_control = in_development["companions"]["devtools"]["runtime_control"]
    assert expected_runtime_control.items() <= in_development_runtime_control.items()
    assert in_development_runtime_control["model_request_telemetry_contract"].startswith(
        "Existing model RuntimeEvents may carry payload.model_request_telemetry"
    )
    assert in_development_runtime_control["model_request_result_stream_status_contract"].startswith(
        "ModelRequestResult reserves $status"
    )
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
    artifact_stream_contract = in_development["companions"]["blocks"]["agent_task_artifact_stream_contract"]
    assert "without .output()" in artifact_stream_contract
    assert "<$retry>...</$retry> is a consumer-side replay boundary" in artifact_stream_contract
    assert "targeted_readbacks" in artifact_stream_contract
    execution_contract = in_development["request_input"]["agent_execution_request_scope"]
    assert "AgentExecution" in execution_contract["surface"]
    assert "AgentExecutionResult" in execution_contract["surface"]
    assert "AgentTurn" not in execution_contract["surface"]
    assert "isolated AgentExecution draft" in execution_contract["contract"]
    assert "are removed from the 4.1.3.7 development line" in execution_contract["contract"]
    task_loop_contract = in_development["request_input"]["agent_execution_task_loop"]
    assert "Agent.goal" in task_loop_contract["surface"]
    assert "Agent.goals" in task_loop_contract["surface"]
    assert "AgentExecution.goal" in task_loop_contract["surface"]
    assert "AgentExecution.goals" in task_loop_contract["surface"]
    assert "Agent.create_task" in task_loop_contract["surface"]
    assert "Agent.create_task_loop" in task_loop_contract["surface"]
    assert "AgentExecution.use_dynamic_task" not in task_loop_contract["surface"]
    assert "Agent.resume" in task_loop_contract["surface"]
    assert "Agent.async_resume" in task_loop_contract["surface"]
    assert "AgentExecution.success_criteria" not in task_loop_contract["surface"]
    assert "AgentExecutionResult.task_refs" in task_loop_contract["surface"]
    assert "without .output()" in task_loop_contract["artifact_stream_contract"]
    assert "targeted_readbacks" in task_loop_contract["artifact_stream_contract"]
    assert "content/content_preview/text/excerpt/snippet" in task_loop_contract["contract"]
    assert "TaskBoard final verification carries board source_refs" in task_loop_contract["contract"]
    assert "agent.goal(goal_or_goals, success_criteria=None)" in task_loop_contract["contract"]
    assert "4.1.3.8 development target" in task_loop_contract["contract"]
    assert "planner-visible capability summaries" in task_loop_contract["contract"]
    assert "structured bounded-step scope" in task_loop_contract["contract"]
    assert "capability/evidence requirements" in task_loop_contract["contract"]
    assert "business-specific special-case fixes" in task_loop_contract["contract"]
    assert "plural alias agent.goals(...)" in task_loop_contract["contract"]
    assert "effort budget values such as iteration_limit" in task_loop_contract["contract"]
    assert "soft strategy metadata" in task_loop_contract["contract"]
    assert "do not silently set task-strategy max_iterations or AgentExecution hard limits" in task_loop_contract["contract"]
    assert "AgentTask does not impose model-request, iteration, TaskBoard tick, or Action round quotas" in task_loop_contract["contract"]
    assert "no-progress and idle timeouts remain liveness guards" in task_loop_contract["contract"]
    assert "TaskDAG is no longer an AgentTask bounded-step strategy" in task_loop_contract["contract"]
    assert "compatibility/convenience facade over DAG" in task_loop_contract["contract"]
    assert "Agent.use_dynamic_task(...) and AgentExecution.use_dynamic_task(...) fail fast" in task_loop_contract["contract"]
    assert "Agently.create_dynamic_task(...)" in task_loop_contract["contract"]
    assert "not current public surfaces" in task_loop_contract["contract"]
    assert "task-strategy AgentExecution drafts" in task_loop_contract["contract"]
    assert "not a separate recommended AgentTask execution owner" in task_loop_contract["contract"]
    assert "accepted=true" in task_loop_contract["contract"]
    assert "artifact_status=partial" in task_loop_contract["contract"]
    assert "agent.resume(task_id)" in task_loop_contract["contract"]
    assert "compatibility aliases only" in task_loop_contract["contract"]
    assert "planner-visible capability summaries instead of provider- or example-specific prompt patches" in task_loop_contract["scope"]["current_slice"]
    assert "structured bounded-step capability scope instead of prose-only step instructions" in task_loop_contract["scope"]["current_slice"]
    assert "multi-task scheduling" in task_loop_contract["scope"]["deferred"]
    assert (
        "distributed pause/resume beyond the single-task agent.resume(...) snapshot slice"
        in task_loop_contract["scope"]["deferred"]
    )
    assert "TriggerFlow-backed AdaptiveLoop or BootstrapLoop packaging" in task_loop_contract["scope"]["deferred"]
    assert "AgentExecutionResult as the common consumption surface" in task_loop_contract["compatibility_policy"]
    assert in_development["companions"]["skills"]["archived_catalog_generations"] == [
        {
            "generation": "v1",
            "branch": "update/archive-legacy-v1-catalog",
            "last_supported_framework_version": "4.1.1",
            "status": "frozen",
        }
    ]
