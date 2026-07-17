from __future__ import annotations

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


def _development_manifest() -> dict:
    return json.loads(IN_DEVELOPMENT_PATH.read_text(encoding="utf-8"))


def test_current_release_manifest_matches_registry_release_file() -> None:
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    release_path = ROOT / index["release_files"][CURRENT_FRAMEWORK_VERSION]
    release_manifest = json.loads(release_path.read_text(encoding="utf-8"))
    current_manifest = get_current_release_manifest()

    assert index["latest_release"] == CURRENT_FRAMEWORK_VERSION
    assert current_manifest["framework"] == release_manifest["framework"]
    assert current_manifest["framework_version"] == release_manifest["framework_version"]
    assert current_manifest["release_train"] == release_manifest["release_train"]


def test_companion_views_still_derive_from_released_manifest() -> None:
    current = get_current_release_manifest()
    devtools = get_devtools_compatibility_manifest()
    skills = get_skills_compatibility_manifest()

    assert devtools["framework_version"] == CURRENT_FRAMEWORK_VERSION
    assert devtools["release_train"] == CURRENT_RELEASE_TRAIN
    assert devtools["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]
    assert skills["authoring_protocol"] == current["companions"]["skills"]["authoring_protocol"]


def test_in_development_manifest_declares_breaking_owner_split() -> None:
    manifest = _development_manifest()

    assert manifest["target_version"] == "4.1.4.2"
    assert manifest["release_train"] == "2026-07-4.1.4.2-dev"
    assert "TaskContext" in manifest["notes"]
    assert "TaskWorkspace owns task files" in manifest["notes"]
    assert "RecordStore owns records and durability" in manifest["notes"]
    assert "without shims" in manifest["notes"]

    companions = manifest["companions"]
    assert companions["task_context"]["reader"] == "ContextReader"
    assert companions["task_workspace"]["default_root"].endswith(
        ".agently/task_workspaces/<agent-id>"
    )
    assert companions["record_store"]["local_state"].endswith(
        ".agently/records/records.db"
    )
    assert companions["session_memory"]["storage_owner"] == "RecordStore"


def test_in_development_skill_contract_reconnects_to_agent_execution() -> None:
    manifest = _development_manifest()
    skills = manifest["companions"]["skills"]
    contract = skills["runtime_contract"]

    assert contract["installed_truth_owner"].startswith("SkillLibrary")
    assert contract["selection_and_binding_owner"].startswith("AgentExecution")
    assert "TaskContext" in contract["disclosure_owner"]
    assert "Agently.skills_executor" in contract["compatibility_facade"]
    assert "No Skills route" in contract["execution_policy"]
    assert "host code materializes remote sources" in contract["remote_source_policy"]

    request_contract = manifest["request_input"]["skills"]
    assert "AgentExecution.use_skills" in request_contract["surface"]
    assert "Agent.run_skills_task" in request_contract["surface"]
    assert "result-shaped adapter" in request_contract["contract"]


def test_in_development_blocks_and_devtools_keep_owner_boundaries() -> None:
    manifest = _development_manifest()
    blocks = manifest["companions"]["blocks"]
    devtools = manifest["companions"]["devtools"]

    assert blocks["removed_block_kinds"] == ["skill_activation", "workspace_operation"]
    assert "caller-bound ContextReader" in blocks["context_read_contract"]
    assert devtools["runtime_protocol"] == "agently-devtools.observation-runtime.v1"
    assert "TaskWorkspace is never an event store" in (
        devtools["runtime_control"]["record_store_contract"]
    )


def test_public_typing_contract_remains_explicit() -> None:
    public_typing = _development_manifest()["public_typing"]

    assert public_typing["status"] == "required"
    assert "compatibility/public-typing-allowlist.json" in public_typing["surface"]
    assert "typed parameters and returns" in public_typing["contract"]
    assert "not a public-method allowlist" in public_typing["compatibility_policy"]
