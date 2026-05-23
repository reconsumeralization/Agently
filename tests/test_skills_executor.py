import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.stream import AgentExecutionStream
from agently.builtins.plugins.SkillsExecutor import SkillInstallError, SkillNormalizationError
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData
from agently.utils import Settings


class MockSkillsRequester:
    name = "MockSkillsRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}
    requests: list[str] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        MockSkillsRequester.requests = []

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "info": self.prompt.get("info")},
            request_options={"stream": True},
            request_url="mock://skills",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        request_text = json.dumps(request_data.data, ensure_ascii=False)
        MockSkillsRequester.requests.append(request_text)
        if "candidate_skill_cards" in request_text:
            response = {"selected_skill_ids": ["beta-skill", "alpha-skill"], "reason": "Beta fits first."}
        elif "### html" in request_text:
            yield "message", "### html\n<section>OK</section>"
            return
        else:
            response = {
                "response": "Applied selected SKILL.md guidance.",
                "skill_trace": ["beta-skill", "alpha-skill"],
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


def _create_agent():
    settings = Settings(name="SkillsTestSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="SkillsTestPluginManager")
    plugin_manager.register("ModelRequester", MockSkillsRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name="skills-test-agent")


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _skill(root: Path, *, name: str = "Alpha Skill", description: str = "Use for alpha release review.", body: str = "Alpha guidance full sentence."):
    desc_line = f"description: {description}\n" if description else ""
    _write(
        root / "SKILL.md",
        f"""---
name: {name}
{desc_line}keywords:
  - release
---

# {name}

{body}
""",
    )


@pytest.fixture(autouse=True)
def isolated_skills(tmp_path):
    Agently.skills_executor.configure(
        registry_root=tmp_path / "skills-registry",
        allowed_trust_levels=["local"],
    )
    MockSkillsRequester.requests = []


def test_install_standard_skill_preserves_root_structure_and_writes_agently_metadata(tmp_path):
    source = tmp_path / "source"
    _skill(source, name="Release Review", body="Review release readiness.")
    _write(source / "scripts" / "skill.json", '{"ok": true}')

    contract = Agently.skills_executor.install_skills(source)

    assert contract["skill_id"] == "release-review"
    installed = Path(contract["source"]["installed_path"])
    assert (installed / "SKILL.md").is_file()
    assert (installed / "scripts" / "skill.json").is_file()
    assert not (installed / "content").exists()
    assert (installed / ".agently" / "install.json").is_file()
    assert (installed / ".agently" / "decision_card.json").is_file()
    assert (installed / ".agently" / "resource_index.json").is_file()
    assert (installed / ".agently" / "checksums.json").is_file()
    assert contract["decision_card"]["description"] == "Use for alpha release review."


def test_skills_executor_configure_sets_public_registry_options(tmp_path):
    registry_root = tmp_path / "configured-registry"

    result = Agently.skills_executor.configure(
        registry_root=registry_root,
        allowed_trust_levels=["local"],
    )

    assert result is Agently.skills_executor
    assert Agently.settings.get("skills.registry.root") == str(registry_root)
    assert Agently.settings.get("skills.allowed_trust_levels") == ["local"]


def test_skill_id_slug_and_conflict_rules(tmp_path):
    source = tmp_path / "source"
    _skill(source, name="  QA ++ Release Skill  ")

    contract = Agently.skills_executor.install_skills(source)
    assert contract["skill_id"] == "qa-release-skill"

    with pytest.raises(SkillInstallError):
        Agently.skills_executor.install_skills(source)

    updated = Agently.skills_executor.install_skills(source, update=True)
    assert updated["skill_id"] == "qa-release-skill"


def test_empty_slug_fails(tmp_path):
    source = tmp_path / "source"
    _skill(source, name="+++")

    with pytest.raises(SkillNormalizationError):
        Agently.skills_executor.install_skills(source)


def test_root_non_standard_manifest_fails_but_nested_skill_json_is_allowed(tmp_path):
    bad = tmp_path / "bad"
    _skill(bad)
    _write(bad / "skill.json", "{}")

    with pytest.raises(SkillInstallError):
        Agently.skills_executor.install_skills(bad)

    good = tmp_path / "good"
    _skill(good, name="Nested Json Skill")
    _write(good / "scripts" / "skill.json", "{}")
    contract = Agently.skills_executor.install_skills(good)
    assert contract["skill_id"] == "nested-json-skill"


def test_missing_description_installs_with_diagnostic(tmp_path):
    source = tmp_path / "source"
    _skill(source, name="No Description", description="")

    contract = Agently.skills_executor.install_skills(source)

    assert contract["card"]["description"] == ""
    assert contract["diagnostics"][0]["code"] == "missing_description"


def test_decision_card_can_be_rebuilt_and_cannot_gate_availability(tmp_path):
    source = tmp_path / "source"
    _skill(source, name="Repairable Skill")
    contract = Agently.skills_executor.install_skills(source)
    installed = Path(contract["source"]["installed_path"])
    card_path = installed / ".agently" / "decision_card.json"

    card_path.unlink()
    inspected = Agently.skills_executor.inspect_skills("repairable-skill")
    assert inspected["decision_card"]["skill_id"] == "repairable-skill"

    card = json.loads(card_path.read_text(encoding="utf-8"))
    card["only_when"] = ["too narrow"]
    card_path.write_text(json.dumps(card), encoding="utf-8")
    inspected = Agently.skills_executor.inspect_skills("repairable-skill")
    assert "only_when" not in inspected["decision_card"]


def test_install_skills_pack_records_standard_skills_and_failed_non_standard_dirs(tmp_path):
    pack = tmp_path / "pack"
    _skill(pack / "alpha", name="Alpha Skill")
    _skill(pack / "bad", name="Bad Skill")
    _write(pack / "bad" / "skill.yaml", "stages: []")

    report = Agently.skills_executor.install_skills_pack(pack, name="demo-pack")

    assert report["status"] == "partial"
    assert report["installed_skills"] == ["alpha-skill"]
    assert report["failed_skills"][0]["path"].endswith("bad")


def test_required_plan_preserves_user_order(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    _skill(tmp_path / "beta", name="Beta Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")
    Agently.skills_executor.install_skills(tmp_path / "beta")

    plan = _create_agent().resolve_skills_plan(
        "handle release",
        skills=["alpha-skill", "beta-skill"],
        mode="required",
    )

    selected_skills = cast(list[dict[str, Any]], plan.get("selected_skills", []))
    assert [item["skill_id"] for item in selected_skills] == ["alpha-skill", "beta-skill"]
    assert plan.get("status") == "resolved"


def test_required_plan_can_select_by_display_name(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    plan = _create_agent().resolve_skills_plan(
        "handle release",
        skills=["Alpha Skill"],
        mode="required",
    )

    selected_skills = cast(list[dict[str, Any]], plan.get("selected_skills", []))
    assert [item["skill_id"] for item in selected_skills] == ["alpha-skill"]


def test_planner_skips_unreadable_entries_when_scanning_registry(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")
    registry_root = Path(str(Agently.settings.get("skills.registry.root")))
    broken_root = registry_root / "broken-skill"
    broken_root.mkdir(parents=True)
    index_path = registry_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["skills"]["broken-skill"] = {
        "skill_id": "broken-skill",
        "display_name": "Broken Skill",
        "installed_path": str(broken_root),
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")

    plan = _create_agent().resolve_skills_plan("handle release", mode="model_decision")

    selected_skills = cast(list[dict[str, Any]], plan.get("selected_skills", []))
    assert [item["skill_id"] for item in selected_skills] == ["alpha-skill"]
    diagnostics = cast(list[dict[str, Any]], plan.get("diagnostics", []))
    assert diagnostics[0]["code"] == "skill_unreadable"
    assert diagnostics[0]["skill_id"] == "broken-skill"


def test_model_decision_orders_multiple_candidates_with_model(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    _skill(tmp_path / "beta", name="Beta Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")
    Agently.skills_executor.install_skills(tmp_path / "beta")

    plan = _create_agent().resolve_skills_plan(
        "handle release",
        skills=["alpha-skill", "beta-skill"],
        mode="model_decision",
        output_format="hybrid",
    )

    selected_skills = cast(list[dict[str, Any]], plan.get("selected_skills", []))
    assert [item["skill_id"] for item in selected_skills] == ["beta-skill", "alpha-skill"]
    assert plan.get("expected_result_format") == "hybrid"
    assert "candidate_skill_cards" in MockSkillsRequester.requests[-1]


def test_run_skills_task_uses_full_skill_guidance_not_only_decision_card(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill", body="Alpha guidance full sentence with detailed operating procedure.")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    execution = _create_agent().run_skills_task(
        "handle release",
        skills=["alpha-skill"],
        mode="required",
    )

    assert execution.status == "success"
    output = cast(dict[str, Any], execution.output)
    assert output["response"] == "Applied selected SKILL.md guidance."
    assert "Alpha guidance full sentence with detailed operating procedure." in MockSkillsRequester.requests[-1]
    assert execution.skill_logs[0]["execution_mode"] == "prompt_only"


def test_run_skills_task_passes_output_format_to_model_request(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill", body="Draft a render-ready HTML fragment.")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    execution = _create_agent().run_skills_task(
        "render release HTML",
        skills=["alpha-skill"],
        mode="required",
        semantic_outputs={"html": (str, "render-ready HTML", True)},
        output_format="flat_markdown",
    )

    assert execution.status == "success"
    assert execution.plan.get("expected_result_format") == "flat_markdown"
    assert "Required sections" in MockSkillsRequester.requests[-1]
    output = cast(dict[str, Any], execution.output)
    assert output["html"] == "<section>OK</section>"


def test_run_skills_task_sync_accepts_stream_handler(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")
    stream_items: list[dict[str, Any]] = []

    execution = _create_agent().run_skills_task(
        "handle release",
        skills=["alpha-skill"],
        mode="required",
        stream_handler=stream_items.append,
    )

    assert execution.status == "success"
    assert stream_items[0]["type"] == "skills.prompt_only.start"
    assert stream_items[-1]["type"] == "skills.prompt_only.done"


@pytest.mark.asyncio
async def test_orchestrator_stream_bridge_maps_prompt_only_skill_model_fields():
    stream = AgentExecutionStream()

    await stream.bridge_task_dag_item(
        {
            "type": "skills.model_stream",
            "action": "delta",
            "path": "response",
            "value": "partial",
            "delta": "partial",
            "is_complete": False,
        },
        route="skills",
    )

    assert stream.items[0].path == "skills.model.fields.response"
    assert stream.items[0].route == "skills"
    assert stream.items[0].source == "model_request"
    assert stream.items[0].event_type == "delta"
    assert stream.items[0].is_complete is False


def test_no_matching_skill_returns_no_match(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill", description="Use for alpha-only work.")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    execution = _create_agent().run_skills_task("unrelated billing issue", mode="model_decision")

    assert execution.status == "no_match"
    assert execution.output is None
