import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.plugins.SkillsExecutor import SkillInstallError
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData
from agently.utils import Settings


class MockSkillsChainRequester:
    name = "MockSkillsChainRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "messages": self.prompt.to_messages(),
                "info": self.prompt.get("info"),
            },
            request_options={"stream": True},
            request_url="mock://skills-chain-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        request_text = json.dumps(request_data.data, ensure_ascii=False)
        info_text = json.dumps(request_data.data.get("info"), ensure_ascii=False)
        has_release_prompt = "release readiness" in request_text or "release notes" in request_text
        response = {
            "has_skill_cards": "skill_cards" in info_text,
            "has_primary_guidance": "Follow the release checklist" in info_text,
            "reply": "skills-visible" if "release-checklist" in info_text or has_release_prompt else "skills-missing",
            "plan": "use the release readiness path",
            "selected_branch": "ready",
            "reason": "release context is available",
        }
        yield "message", json.dumps(response, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text


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
kind: workflow
card:
  stage_roles: [record, validate]
  consumes:
    - role: task_request
      type: text
  produces:
    - role: release_note
      type: json
  artifact_types: [json]
  side_effects:
    - kind: local_record
      policy: allowed
  required_capabilities: [{ action }]
  complements: [repo-review]
  failure_modes: [missing_action]
  composition_hints: [run before release summary]
semantic_outputs:
  release_note:
    type: json
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


def _create_shell_skill(root: Path):
    _write(
        root / "skill.yaml",
        """
skill_id: shell-inspector
version: 0.1.0
display_name: Shell Inspector
purpose: Inspect the current workspace through a controlled Bash action.
trust_level: local
requires:
  actions: [run_bash]
stages:
  - id: inspect_workspace
    kind: action
    action: run_bash
    input:
      cmd: pwd
""",
    )
    _write(
        root / "SKILL.md",
        """---
name: Shell Inspector
description: Use controlled shell inspection.
keywords:
  - shell
---

Inspect the workspace with a controlled shell command.
""",
    )


def _create_model_skill(root: Path):
    _write(
        root / "skill.yaml",
        """
skill_id: model-writer
version: 0.1.0
display_name: Model Writer
purpose: Write a structured model response for a release task.
trust_level: local
activation:
  keywords: [model, release]
kind: workflow
card:
  stage_roles: [draft]
  produces:
    - role: reply
      type: text
semantic_outputs:
  reply:
    type: text
stages:
  - id: draft_reply
    kind: model
    prompt: "Draft a concise release readiness reply for ${task}."
    output_schema:
      reply:
        type: str
        description: Release readiness reply.
""",
    )
    _write(
        root / "SKILL.md",
        """---
name: Model Writer
description: Use model stage to draft release replies.
keywords:
  - release
---

Draft a concise release readiness reply.
""",
    )


def _create_model_plan_branch_skill(root: Path):
    _write(
        root / "skill.yaml",
        """
skill_id: model-plan-branch
version: 0.1.0
display_name: Model Plan Branch
purpose: Plan release work and choose a branch.
trust_level: local
activation:
  keywords: [release, branch]
kind: workflow
stages:
  - id: plan_steps
    kind: model_plan
    purpose: "Plan the release readiness path for ${task}."
    output_schema:
      plan:
        type: str
        description: Release plan.
  - id: choose_path
    kind: branch
    condition: ready
    branches:
      ready:
        description: Continue with release readiness.
      fallback:
        description: Ask for missing context.
""",
    )
    _write(
        root / "SKILL.md",
        """---
name: Model Plan Branch
description: Use model_plan and branch stages.
keywords:
  - release
---

Plan release work and choose a branch.
""",
    )


def _create_chain_test_agent():
    settings = Settings(name="SkillsChainTestSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="SkillsChainTestPluginManager")
    plugin_manager.register("ModelRequester", MockSkillsChainRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="skills-chain-agent",
    )


@pytest.fixture(autouse=True)
def isolated_skills(tmp_path):
    Agently.settings.set("skills.registry.root", str(tmp_path / "skills-registry"))
    Agently.settings._set_item_by_dot_path("skills.allowed_trust_levels", ["local"], cover=True)


def test_global_skills_install_list_inspect_remove(tmp_path):
    assert not hasattr(Agently, "skills")
    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)

    installed = Agently.skills_executor.install_skills(skill_root)
    assert installed.get("skill_id") == "release-checklist"
    assert installed.get("card", {}).get("display_name") == "Release Checklist"
    assert installed.get("action_requirements") == ["record_release_note"]
    assert installed.get("declarative_stages", [])[0].get("stage_id") == "record_note"
    assert installed.get("kind") == "workflow"
    assert installed.get("semantic_outputs", {}).get("release_note", {}).get("type") == "json"
    card = installed.get("card", {})
    assert card.get("stage_roles") == ["record", "validate"]
    assert card.get("consumes") == [{"role": "task_request", "type": "text"}]
    assert card.get("produces") == [{"role": "release_note", "type": "json"}]
    assert card.get("artifact_types") == ["json"]
    assert card.get("side_effects") == [{"kind": "local_record", "policy": "allowed"}]
    assert card.get("required_capabilities") == ["record_release_note"]
    assert card.get("complements") == ["repo-review"]
    assert card.get("failure_modes") == ["missing_action"]

    listed = Agently.skills_executor.list_skills()
    assert [item["skill_id"] for item in listed] == ["release-checklist"]

    inspected = Agently.skills_executor.inspect_skills("release-checklist")
    assert inspected.get("assets", {}).get("guidance_assets", [])[0].get("asset_id") == "primary-guidance"

    assert Agently.skills_executor.remove_skills("release-checklist") == {"removed": True, "skill_id": "release-checklist"}
    with pytest.raises(SkillInstallError):
        Agently.skills_executor.inspect_skills("release-checklist")


def test_install_skills_pack_records_pack_and_selects_by_skills_pack_name(tmp_path):
    pack_root = tmp_path / "release-pack"
    _create_skill(pack_root / "release-skill")
    _create_skill(pack_root / "rollback-skill", skill_id="rollback-checklist")

    report = Agently.skills_executor.install_skills_pack(pack_root, name="release-pack", trust_level="local")

    assert report["skills_pack_id"] == "release-pack"
    assert report["name"] == "release-pack"
    assert set(report["installed_skills"]) == {"release-checklist", "rollback-checklist"}
    assert Agently.skills_executor.inspect_skills_pack("release-pack")["status"] == "success"
    assert Agently.skills_executor.inspect_skills("release-checklist")["source"]["skills_pack_id"] == "release-pack"

    calls = []

    def record_release_note(text: str):
        calls.append(text)
        return {"recorded": text}

    agent = Agently.create_agent("skills-pack-agent")
    agent.register_action(
        name="record_release_note",
        desc="Record release note.",
        kwargs={"text": (str, "Release note text.")},
        func=record_release_note,
    )

    execution = agent.run_skills_task(
        "prepare release rollback notes",
        skills_packs=["release-pack"],
        mode="required",
        planner_mode="deterministic",
    )

    assert execution.status == "success"
    assert execution.plan.get("selected_skills_packs", [])[0].get("skills_pack_id") == "release-pack"
    assert {item.get("skills_pack_id") for item in execution.plan.get("selected_skills", [])} == {"release-pack"}
    assert calls == ["prepare release rollback notes", "prepare release rollback notes"]


def test_install_skills_pack_name_and_skills_pack_id_conflict_fails(tmp_path):
    pack_root = tmp_path / "release-pack"
    _create_skill(pack_root / "release-skill")

    with pytest.raises(SkillInstallError):
        Agently.skills_executor.install_skills_pack(pack_root, name="release-pack", skills_pack_id="other-pack")


def test_model_decision_use_skills_discloses_route_candidates_without_prompt_guidance(tmp_path):
    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    agent = Agently.create_agent("skills-card-agent")
    agent.use_skills(["release-checklist"], mode="model_decision")

    import asyncio

    for async_prefix in agent.extension_handlers.get("request_prefixes", []) or []:
        asyncio.run(async_prefix(agent.request.prompt, agent.settings))

    info_json = json.dumps(agent.request.prompt.get("info"), ensure_ascii=False)
    assert "skill_candidates" in info_json
    assert "skill_cards" not in info_json
    assert "release-checklist" in info_json
    assert "route candidates" in info_json
    assert "skill_guidance" not in info_json
    assert "Follow the release checklist" not in info_json


def test_chain_style_use_skills_uses_route_owned_candidate_summary(tmp_path):
    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    result = (
        _create_chain_test_agent()
        .use_skills(["release-checklist"])
        .input("Should this release be blocked?")
        .instruct("Use installed skills only if they fit the task.")
        .output(
            {
                "has_skill_cards": (bool,),
                "has_primary_guidance": (bool,),
                "reply": (str,),
            }
        )
        .start()
    )

    assert result["has_skill_cards"] is False
    assert result["has_primary_guidance"] is False
    assert result["reply"] == "skills-visible"


def test_model_decision_plan_can_select_matching_skill(tmp_path):
    calls = []

    def record_release_note(text: str):
        calls.append(text)
        return {"recorded": text}

    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    agent = Agently.create_agent("skills-plan-agent")
    agent.register_action(
        name="record_release_note",
        desc="Record release note.",
        kwargs={"text": (str, "Release note text.")},
        func=record_release_note,
    )

    plan = agent.resolve_skills_plan("prepare release rollback notes", mode="model_decision")
    assert plan.get("status") == "resolved"
    assert plan.get("selected_skills", [])[0].get("skill_id") == "release-checklist"
    assert plan.get("composed_stage_graph", [])[0].get("stage_id") == "record_note"


def test_required_mode_fails_closed_for_missing_action(tmp_path):
    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root, action="missing_release_action")
    Agently.skills_executor.install_skills(skill_root)

    agent = Agently.create_agent("skills-required-agent")
    execution = agent.run_skills_task(
        "prepare release notes",
        skills=["release-checklist"],
        mode="required",
    )

    assert execution.status == "blocked"
    assert execution.plan.get("status") == "blocked"
    assert execution.plan.get("rejected_skills", [])[0].get("reason_code") == "missing_action"
    output = cast(dict[str, Any], execution.output)
    assert "could not complete" in output["user_message"]
    assert "controlled substitute" in output["user_message"]
    assert output["resolution_suggestions"]


def test_required_mode_auto_binds_controlled_bash_action_for_shell_skill(tmp_path):
    skill_root = tmp_path / "shell-skill"
    _create_shell_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    agent = Agently.create_agent("skills-auto-bash-agent")
    execution = agent.run_skills_task(
        "inspect workspace with shell",
        skills=["shell-inspector"],
        mode="required",
    )

    assert execution.status == "success"
    assert execution.action_logs[0].get("action_id") == "run_bash"
    action_data = cast(dict[str, Any], execution.action_logs[0].get("data"))
    assert action_data["ok"] is True
    assert action_data["stdout"].strip()


def test_run_skills_task_executes_action_stage_and_preserves_logs(tmp_path):
    calls = []

    def record_release_note(text: str):
        calls.append(text)
        return {"recorded": text, "ready": True}

    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    agent = Agently.create_agent("skills-exec-agent")
    agent.register_action(
        name="record_release_note",
        desc="Record release note.",
        kwargs={"text": (str, "Release note text.")},
        func=record_release_note,
    )

    execution = agent.run_skills_task(
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
    assert execution.plan.get("dynamic_task_graph", {}).get("tasks")
    task_dag = execution.close_snapshot["task_dag"]
    assert task_dag["task_results"]
    assert task_dag["semantic_outputs"]["record_note"]["task_id"]
    assert any(item.get("type") == "task_dag.task" for item in execution.runtime_stream)


def test_model_stage_calls_model_and_stores_output(tmp_path):
    skill_root = tmp_path / "model-skill"
    _create_model_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    execution = _create_chain_test_agent().run_skills_task(
        "prepare release notes",
        skills=["model-writer"],
        mode="required",
    )

    assert execution.status == "success"
    output = cast(dict[str, Any], execution.output)
    assert output["draft_reply"]["reply"] == "skills-visible"
    assert execution.skill_logs[0]["kind"] == "model"
    assert execution.skill_logs[0]["status"] == "success"
    assert execution.skill_logs[0]["output_keys"] == ["reply"]
    assert any(item.get("type") == "skills.stage_field" for item in execution.runtime_stream)


def test_model_plan_and_branch_stages_execute(tmp_path):
    skill_root = tmp_path / "model-plan-branch"
    _create_model_plan_branch_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    execution = _create_chain_test_agent().run_skills_task(
        "prepare release notes",
        skills=["model-plan-branch"],
        mode="required",
    )

    output = cast(dict[str, Any], execution.output)

    assert execution.status == "success"
    assert output["plan_steps"]["plan"] == "use the release readiness path"
    assert output["choose_path"]["selected_branch"] == "ready"
    assert [log["kind"] for log in execution.skill_logs] == ["model_plan", "branch"]


@pytest.mark.asyncio
async def test_agent_execution_routes_skills_and_streams_stage_progress(tmp_path):
    calls = []

    def record_release_note(text: str):
        calls.append(text)
        return {"recorded": text}

    skill_root = tmp_path / "release-skill"
    _create_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    agent = Agently.create_agent("skills-auto-exec-agent")
    agent.register_action(
        name="record_release_note",
        desc="Record release note.",
        kwargs={"text": (str, "Release note text.")},
        func=record_release_note,
    )

    execution = (
        agent
        .use_skills(["release-checklist"], mode="required")
        .input("prepare release notes")
        .create_execution()
    )

    stream_items = []
    async for item in execution.get_async_generator(type="instant"):
        if item.is_complete:
            stream_items.append(item)

    data = cast(dict[str, Any], await execution.async_get_data())
    meta = await execution.async_get_meta()

    assert data["record_note"] == {"recorded": "prepare release notes"}
    assert calls == ["prepare release notes"]
    assert meta["route_plan"]["selected_route"] == "skills"
    assert any(item.path == "route.selected" and item.route == "skills" for item in stream_items)
    assert any(item.path.startswith("task_dag.tasks.") for item in stream_items)
    assert any(item.path == "skills.stages.record_note" for item in stream_items)
    assert any(item.path == "actions.record_release_note" for item in stream_items)


@pytest.mark.asyncio
async def test_agent_execution_streams_skill_model_stage_field_delta(tmp_path):
    skill_root = tmp_path / "model-skill"
    _create_model_skill(skill_root)
    Agently.skills_executor.install_skills(skill_root)

    execution = (
        _create_chain_test_agent()
        .use_skills(["model-writer"], mode="required")
        .input("prepare release notes")
        .create_execution()
    )

    stream_items = []
    async for item in execution.get_async_generator(type="instant"):
        stream_items.append(item)

    data = cast(dict[str, Any], await execution.async_get_data())

    assert data["draft_reply"]["reply"] == "skills-visible"
    assert any(
        item.path == "skills.stages.draft_reply.fields.reply"
        and item.event_type == "delta"
        and item.delta
        for item in stream_items
    )
    assert any(
        item.path == "skills.stages.draft_reply.fields.reply"
        and item.is_complete
        for item in stream_items
    )
