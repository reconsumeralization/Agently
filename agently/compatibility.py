from __future__ import annotations

from copy import deepcopy
from typing import Any


CURRENT_COMPATIBILITY_SCHEMA_VERSION = 1
CURRENT_FRAMEWORK_VERSION = "4.1.3.3"
CURRENT_RELEASE_TRAIN = "2026-06-4.1.3.3"

DEVTOOLS_RUNTIME_PROTOCOL = "agently-devtools.observation-runtime.v1"
SKILLS_AUTHORING_PROTOCOL = "agently-skills.authoring.v2"
SKILLS_DEVTOOLS_GUIDANCE_PROTOCOL = "agently-skills.devtools-guidance.v1"
DOCS_PUBLIC_SURFACE_PROTOCOL = "agently-docs.public-surface.v1"

_CURRENT_RELEASE_MANIFEST: dict[str, Any] = {
    "schema_version": CURRENT_COMPATIBILITY_SCHEMA_VERSION,
    "framework": "agently",
    "framework_version": CURRENT_FRAMEWORK_VERSION,
    "release_train": CURRENT_RELEASE_TRAIN,
    "released_at": "2026-06-02",
    "notes": (
        "This manifest is the offline compatibility surface for the installed "
        "Agently package. Historical release manifests live in the source "
        "repository compatibility registry."
    ),
    "companions": {
        "devtools": {
            "companion_package": "agently-devtools",
            "runtime_protocol": DEVTOOLS_RUNTIME_PROTOCOL,
            "event_naming": {
                "preferred_event_type": "RuntimeEvent",
                "devtools_projection_type": "ObservationEvent",
                "event_center_dispatch": "RuntimeEvent",
                "compatibility_input_type": "ObservationEvent",
            },
            "runtime_control": {
                "runtime_event_ownership": {
                    "official_event_producer": "core",
                    "plugin_contract": (
                        "plugins return observations/errors/decisions; core maps them to official RuntimeEvent records"
                    ),
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
                "typed_provider_stall_stages": ["response_first_event", "response_stream"],
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
            },
            "recommended_version_specifier": ">=0.1.6,<0.2.0",
        },
        "skills": {
            "repository": "Agently-Skills",
            "authoring_protocol": SKILLS_AUTHORING_PROTOCOL,
            "authoring_format": "standard SKILL.md only",
            "devtools_guidance_protocol": SKILLS_DEVTOOLS_GUIDANCE_PROTOCOL,
            "catalog_generation": "v2",
            "recommended_bundle": "app",
            "recommended_ref": "main",
            "legacy_generations": [
                {
                    "generation": "v1",
                    "last_supported_framework_version": "4.1.1",
                    "status": "frozen",
                }
            ],
        },
        "docs": {
            "repository": "docs",
            "public_surface_protocol": DOCS_PUBLIC_SURFACE_PROTOCOL,
        },
        "configuration": {
            "settings_contract": "dict-compatible typed helpers under agently.types.settings",
            "options_contract": "dict-compatible typed helpers under agently.types.options",
            "plugin_schema_registration": [
                "SETTINGS_SCHEMAS",
                "OPTIONS_SCHEMAS",
            ],
            "model_routing_layers": [
                "model_pool",
                "model_profiles",
                "api_key_pools",
            ],
            "api_key_pools": [
                "selection_policy",
                "failover_policy",
                "selection_handler",
                "failover_handler",
            ],
        },
    },
    "request_input": {
        "image": {
            "status": "released",
            "surface": [
                "ModelRequest.image",
                "Agent.image",
            ],
            "shape": "image(question=..., file=...|url=...|files=[...]|urls=[...])",
            "attachment_contract": (
                "Builds text plus image_url rich-content blocks and keeps attachment as the low-level passthrough."
            ),
            "local_image_mime_types": [
                "image/png",
                "image/jpeg",
                "image/webp",
                "image/gif",
                "image/bmp",
            ],
            "common_file_handling_target": "4.1.4",
        }
    },
}


def get_current_release_manifest() -> dict[str, Any]:
    return deepcopy(_CURRENT_RELEASE_MANIFEST)


def get_devtools_compatibility_manifest() -> dict[str, Any]:
    manifest = get_current_release_manifest()
    devtools = deepcopy(manifest["companions"]["devtools"])
    devtools["framework_version"] = manifest["framework_version"]
    devtools["release_train"] = manifest["release_train"]
    return devtools


def get_skills_compatibility_manifest() -> dict[str, Any]:
    manifest = get_current_release_manifest()
    skills = deepcopy(manifest["companions"]["skills"])
    skills["framework_version"] = manifest["framework_version"]
    skills["release_train"] = manifest["release_train"]
    return skills


__all__ = [
    "CURRENT_COMPATIBILITY_SCHEMA_VERSION",
    "CURRENT_FRAMEWORK_VERSION",
    "CURRENT_RELEASE_TRAIN",
    "DEVTOOLS_RUNTIME_PROTOCOL",
    "SKILLS_AUTHORING_PROTOCOL",
    "SKILLS_DEVTOOLS_GUIDANCE_PROTOCOL",
    "DOCS_PUBLIC_SURFACE_PROTOCOL",
    "get_current_release_manifest",
    "get_devtools_compatibility_manifest",
    "get_skills_compatibility_manifest",
]
