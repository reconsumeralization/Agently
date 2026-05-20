import json
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.skills import SkillInstallError


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_skill(root: Path, *, skill_id: str = "release-checklist", action: str = "record_release_note"):
    _write(
        root / "skill.yaml",
        f"""
skill_id: { skill_id }
version: 0.1.0
display_name: Release Checklist
purpose: Check release readiness and record a release note.
trust_level: local
activation:
  keywords: [release, rollback]
requires:
  actions: [{ action }]
stages:
  - id: record_note
    kind: action
    action: { action }
    input:
      text: "${{task}}"
  - id: validate_note
    kind: validate
    validation:
      required_state: [record_note]
completion:
  output:
    release_ready: bool
""",
    )
    _write(
        root / "SKILL.md",
        """---
name: Release Checklist
description: Use for release and rollback review.
keywords:
  - release
---

Follow the release checklist before publishing.
""",
    )


@pytest.fixture(autouse=True)
def isolated_skills(tmp_path):
    Agently.settings.set("skills.registry.root", str(tmp_path / "skills-registry"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)


def test_global_skills_install_list_inspect_remove(tmp_path):
    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)

    installed = Agently.skills.install(skill_root)
    assert installed.get("skill_id") == "release-checklist"
    assert installed.get("card", {}).get("display_name") == "Release Checklist"
    assert installed.get("action_requirements") == ["record_release_note"]
    assert installed.get("declarative_stages", [])[0].get("stage_id") == "record_note"

    listed = Agently.skills.list()
    assert [item["skill_id"] for item in listed] == ["release-checklist"]

    inspected = Agently.skills.inspect("release-checklist")
    assert inspected.get("assets", {}).get("guidance_assets", [])[0].get("asset_id") == "primary-guidance"

    assert Agently.skills.remove("release-checklist") == {"removed": True, "skill_id": "release-checklist"}
    with pytest.raises(SkillInstallError):
        Agently.skills.inspect("release-checklist")


def test_model_decision_use_skills_discloses_cards_without_forcing_execution(tmp_path):
    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills.install(skill_root)

    agent = Agently.create_agent("skills-card-agent")
    agent.use_skills(["release-checklist"], mode="model_decision")

    import asyncio

    for async_prefix in agent.extension_handlers.get("request_prefixes", []) or []:
        asyncio.run(async_prefix(agent.request.prompt, agent.settings))

    info_json = json.dumps(agent.request.prompt.get("info"), ensure_ascii=False)
    assert "skill_cards" in info_json
    assert "release-checklist" in info_json
    assert "optional behavior-loop candidates" in info_json
    assert "skill_guidance" in info_json
    assert "Follow the release checklist" in info_json


def test_model_decision_plan_can_select_matching_skill(tmp_path):
    calls = []

    def record_release_note(text: str):
        calls.append(text)
        return {"recorded": text}

    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills.install(skill_root)

    agent = Agently.create_agent("skills-plan-agent")
    agent.register_action(
        name="record_release_note",
        desc="Record release note.",
        kwargs={"text": (str, "Release note text.")},
        func=record_release_note,
    )

    plan = agent.resolve_skill_plan("prepare release rollback notes", mode="model_decision")
    assert plan.get("status") == "resolved"
    assert plan.get("selected_skills", [])[0].get("skill_id") == "release-checklist"
    assert plan.get("composed_stage_graph", [])[0].get("stage_id") == "record_note"


def test_required_mode_fails_closed_for_missing_action(tmp_path):
    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root, action="missing_release_action")
    Agently.skills.install(skill_root)

    agent = Agently.create_agent("skills-required-agent")
    execution = agent.run_skill_task(
        "prepare release notes",
        skills=["release-checklist"],
        mode="required",
    )

    assert execution.status == "blocked"
    assert execution.plan.get("status") == "blocked"
    assert execution.plan.get("rejected_skills", [])[0].get("reason_code") == "missing_action"


def test_run_skill_task_executes_action_stage_and_preserves_logs(tmp_path):
    calls = []

    def record_release_note(text: str):
        calls.append(text)
        return {"recorded": text, "ready": True}

    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills.install(skill_root)

    agent = Agently.create_agent("skills-exec-agent")
    agent.register_action(
        name="record_release_note",
        desc="Record release note.",
        kwargs={"text": (str, "Release note text.")},
        func=record_release_note,
    )

    execution = agent.run_skill_task(
        "prepare release notes",
        skills=["release-checklist"],
        mode="required",
    )

    assert execution.status == "success"
    assert calls == ["prepare release notes"]
    output = cast(dict[str, Any], execution.output)
    assert output["record_note"] == {"recorded": "prepare release notes", "ready": True}
    assert execution.action_logs[0].get("action_id") == "record_release_note"
    assert execution.skill_logs[0]["kind"] == "action"
    assert execution.close_snapshot["status"] == "success"
