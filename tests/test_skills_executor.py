import json
import shutil
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.agent_extensions.SkillsExtension._SkillsContext import create_agent_skills_runtime_context
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
        elif "finalize" in request_text and "Produce the final user-facing result" in request_text:
            response = {
                "response": "Finalized through the Skills runtime chain.",
                "skill_trace": ["alpha-skill"],
            }
        elif "verify" in request_text and "Validate the execution output" in request_text:
            response = {"passed": True, "issues": [], "reason": "The output satisfies the selected Skill guidance."}
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


def _create_git_skills_pack(root: Path) -> str:
    _skill(root / "skills" / "docx", name="Docx Skill")
    _skill(root / "skills" / "xlsx", name="Xlsx Skill")
    _write(root / "skills" / "docx" / "scripts" / "helper.py", "print('asset only')\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "initial skills",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def test_install_skills_pack_fetches_git_url_and_records_source_metadata(tmp_path):
    repo = tmp_path / "remote-pack"
    expected_commit = _create_git_skills_pack(repo)

    report = Agently.skills_executor.install_skills_pack(
        repo.as_uri(),
        fetch=True,
        trust_level="remote",
        update=True,
    )

    assert report["status"] == "success"
    assert sorted(report["installed_skills"]) == ["docx-skill", "xlsx-skill"]
    assert report["source_type"] == "git"
    assert report["source_url"] == repo.as_uri()
    assert report["source_commit"] == expected_commit
    contract = Agently.skills_executor.inspect_skills("docx-skill")
    assert contract["trust_level"] == "remote"
    assert contract["source"]["source_url"] == repo.as_uri()
    assert contract["source"]["source_commit"] == expected_commit
    assert (Path(contract["source"]["installed_path"]) / "scripts" / "helper.py").is_file()


def test_install_skills_pack_supports_github_shorthand_and_subpath(monkeypatch, tmp_path):
    from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules import registry as registry_module

    repo = tmp_path / "github-pack"
    _create_git_skills_pack(repo)
    expected_commit = "abc123remotecommit"
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:4] == ["git", "clone", "--depth", "1"]:
            destination = Path(command[-1])
            shutil.copytree(repo, destination)
            return subprocess.CompletedProcess(command, 0, "", "")
        if len(command) >= 5 and command[0] == "git" and command[1] == "-C" and command[3:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, f"{ expected_commit }\n", "")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(registry_module.subprocess, "run", fake_run)

    report = Agently.skills_executor.install_skills_pack(
        "anthropics/skills",
        fetch=True,
        subpath="skills/docx",
        trust_level="remote",
        update=True,
    )

    assert report["status"] == "success"
    assert report["installed_skills"] == ["docx-skill"]
    assert report["source_url"] == "https://github.com/anthropics/skills.git"
    assert report["source_commit"] == expected_commit
    assert report["source_subpath"] == "skills/docx"
    contract = Agently.skills_executor.inspect_skills("docx-skill")
    assert contract["source"]["source_package"] == "anthropics/skills"
    assert contract["source"]["source_subpath"] == "skills/docx"


def test_use_skills_source_selector_installs_only_when_planning_hits(tmp_path):
    pack = tmp_path / "pack"
    _skill(pack / "skills" / "docx", name="Docx Skill")

    agent = _create_agent().use_skills(str(pack), mode="required", auto_allow=True)

    assert Agently.skills_executor.list_skills() == []

    plan = agent.resolve_skills_plan("draft a document", mode="required")

    selected_skills = cast(list[dict[str, Any]], plan.get("selected_skills", []))
    assert [item["skill_id"] for item in selected_skills] == ["docx-skill"]
    assert Agently.skills_executor.list_skills()[0]["skill_id"] == "docx-skill"
    diagnostics = cast(list[dict[str, Any]], plan.get("diagnostics", []))
    assert any(item["code"] == "source_discovered" for item in diagnostics)
    assert any(item["code"] == "source_installed" for item in diagnostics)
    assert plan.get("capability_policy", {}).get("auto_allow") is True
    assert plan.get("stage_model_keys", {}).get("planner") == "planner"
    assert plan.get("stage_model_keys", {}).get("verifier") == "verifier"
    assert plan.get("stage_model_keys", {}).get("finalizer") == "finalizer"


def test_use_skills_source_selector_dedupes_installed_and_discovered(tmp_path):
    pack = tmp_path / "pack"
    _skill(pack / "skills" / "docx", name="Docx Skill")

    agent = _create_agent().use_skills(
        {"source": str(pack), "subpath": "skills/docx", "trust_level": "local"},
        mode="required",
    )

    first = agent.resolve_skills_plan("draft a document", mode="required")
    second = agent.resolve_skills_plan("draft a document", mode="required")

    assert [item.get("skill_id") for item in first.get("selected_skills", [])] == ["docx-skill"]
    assert [item.get("skill_id") for item in second.get("selected_skills", [])] == ["docx-skill"]


def test_discover_skills_pack_does_not_install_full_skill(tmp_path):
    pack = tmp_path / "pack"
    _skill(pack / "skills" / "docx", name="Docx Skill")

    report = Agently.skills_executor.discover_skills_pack(
        pack,
        subpath="skills/docx",
        trust_level="remote",
    )

    assert report["status"] == "success"
    assert report["contracts"][0]["skill_id"] == "docx-skill"
    assert Agently.skills_executor.list_skills() == []


@pytest.mark.asyncio
async def test_skill_declared_http_mcp_mounts_through_agent_runtime(monkeypatch, tmp_path):
    source = tmp_path / "source"
    _write(
        source / "SKILL.md",
        """---
name: MCP Skill
description: Uses a remote MCP service.
mcp: https://example.com/mcp
allowed-tools: [remote_tool]
---

# MCP Skill

Use the declared MCP service.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent()
    mounted: list[Any] = []

    async def fake_use_mcp(config, **kwargs):
        mounted.append(config)
        return agent

    monkeypatch.setattr(agent, "async_use_mcp", fake_use_mcp)

    execution = await agent.async_run_skills_task(
        "use mcp",
        skills=["mcp-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert mounted == ["https://example.com/mcp"]


@pytest.mark.asyncio
async def test_skill_declared_bash_mounts_runtime_action_when_auto_allowed(tmp_path):
    source = tmp_path / "source"
    _write(
        source / "SKILL.md",
        """---
name: Bash Skill
description: Uses a bundled script through Bash.
allowed-tools: [Bash]
---

# Bash Skill

Use Bash only when the task needs the bundled helper.
""",
    )
    _write(source / "scripts" / "helper.sh", "#!/usr/bin/env bash\necho helper\n")
    Agently.skills_executor.install_skills(source)
    agent = _create_agent()

    execution = await agent.async_run_skills_task(
        "use bash",
        skills=[{"id": "bash-skill", "auto_allow": True}],
        mode="required",
    )

    assert execution.status == "success"
    assert agent.action.action_registry.has("Bash")
    bash_spec = agent.action.action_registry.get_spec("Bash")
    assert bash_spec is not None
    assert bash_spec.get("side_effect_level") == "exec"
    assert any(
        item.get("capability") == "bash_action" and item.get("action_id") == "Bash"
        for item in execution.runtime_stream
    )


@pytest.mark.asyncio
async def test_skill_declared_bash_requires_auto_allow(tmp_path):
    source = tmp_path / "source"
    _write(
        source / "SKILL.md",
        """---
name: Bash Skill
description: Uses a bundled script through Bash.
allowed-tools: [Bash]
---

# Bash Skill

Use Bash only when the task needs the bundled helper.
""",
    )
    _write(source / "scripts" / "helper.sh", "#!/usr/bin/env bash\necho helper\n")
    Agently.skills_executor.install_skills(source)
    agent = _create_agent()

    execution = await agent.async_run_skills_task(
        "use bash",
        skills=["bash-skill"],
        mode="required",
    )

    assert execution.status == "blocked"
    assert not agent.action.action_registry.has("Bash")
    assert isinstance(execution.output, dict)
    diagnostics = execution.output.get("diagnostics", [])
    assert diagnostics[0]["code"] == "approval_required"
    assert diagnostics[0]["capability"] == "bash_action"


@pytest.mark.asyncio
async def test_missing_pure_python_capability_synthesizes_sandbox_action(tmp_path):
    source = tmp_path / "source"
    _write(
        source / "SKILL.md",
        """---
name: Budget Skill
description: Calculates budget totals.
allowed-tools: [calculate_budget]
---

# Budget Skill

Use calculate_budget for deterministic in-memory budget math.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent()

    execution = await agent.async_run_skills_task(
        "calculate a budget",
        skills=["budget-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert agent.action.action_registry.has("calculate_budget")
    spec = agent.action.action_registry.get_spec("calculate_budget")
    assert spec is not None
    assert spec.get("sandbox_required") is True
    assert any(
        item.get("capability") == "python_sandbox_action"
        and item.get("action_id") == "calculate_budget"
        for item in execution.runtime_stream
    )


@pytest.mark.asyncio
async def test_missing_business_capability_fails_closed(tmp_path):
    source = tmp_path / "source"
    _write(
        source / "SKILL.md",
        """---
name: Email Skill
description: Sends business email.
allowed-tools: [send_email]
---

# Email Skill

Use send_email only through a real mail backend.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent()

    execution = await agent.async_run_skills_task(
        "send a customer email",
        skills=[{"id": "email-skill", "auto_allow": True}],
        mode="required",
    )

    assert execution.status == "blocked"
    assert not agent.action.action_registry.has("send_email")
    assert isinstance(execution.output, dict)
    diagnostics = execution.output.get("diagnostics", [])
    assert diagnostics[0]["code"] == "capability_missing"
    assert diagnostics[0]["capability"] == "business_action"
    assert diagnostics[0]["required"] == ["send_email"]


@pytest.mark.asyncio
async def test_effort_normal_runs_full_runtime_chain(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    execution = await _create_agent().async_run_skills_task(
        "handle release",
        skills=["alpha-skill"],
        mode="required",
        effort="normal",
    )

    assert execution.status == "success"
    assert execution.close_snapshot["execution_mode"] == "runtime_chain"
    phases = [
        item.get("phase")
        for item in execution.runtime_stream
        if item.get("type") == "skills.runtime_chain.phase_start"
    ]
    assert phases == ["preflight", "research", "plan", "execute", "verify", "reflect", "finalize"]
    assert isinstance(execution.output, dict)
    assert execution.output["response"] == "Finalized through the Skills runtime chain."


@pytest.mark.asyncio
async def test_custom_effort_strategy_handler_executes(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    async def custom_strategy(**kwargs):
        context = kwargs["context"]
        await context.async_emit_runtime_stream({
            "type": "test.custom_strategy.checkpoint",
            "action": "checkpoint",
        })
        return {
            "task": kwargs["task"],
            "effort": kwargs["effort"],
            "strategy": kwargs["effort_config"]["strategy"],
            "custom_budget": kwargs["effort_config"]["custom_budget"],
            "selected": [item["skill_id"] for item in kwargs["plan"]["selected_skills"]],
        }

    Agently.skills_executor.register_effort_strategy("audit_plus", custom_strategy, replace=True)
    try:
        agent = _create_agent()
        agent.set_settings("effort_presets", {
            "audit_plus": {"strategy": "audit_plus", "custom_budget": 7},
        })

        execution = await agent.async_run_skills_task(
            "handle release",
            skills=["alpha-skill"],
            mode="required",
            effort="audit_plus",
        )
    finally:
        Agently.skills_executor.unregister_effort_strategy("audit_plus")

    assert execution.status == "success"
    assert execution.close_snapshot["execution_mode"] == "custom:audit_plus"
    assert execution.output == {
        "task": "handle release",
        "effort": "audit_plus",
        "strategy": "audit_plus",
        "custom_budget": 7,
        "selected": ["alpha-skill"],
    }
    assert [item.get("type") for item in execution.runtime_stream] == [
        "skills.custom_strategy.start",
        "test.custom_strategy.checkpoint",
        "skills.custom_strategy.done",
    ]


def test_effort_strategy_registration_rejects_duplicate_names():
    def first(**kwargs):
        return kwargs

    def second(**kwargs):
        return kwargs

    Agently.skills_executor.register_effort_strategy("duplicate-test", first, replace=True)
    try:
        with pytest.raises(ValueError):
            Agently.skills_executor.register_effort_strategy("duplicate-test", second)
        for strategy_name in ["single_shot", "runtime_chain", "staged", "react"]:
            assert strategy_name in Agently.skills_executor.list_effort_strategies()
        assert "duplicate-test" in Agently.skills_executor.list_effort_strategies()
    finally:
        assert Agently.skills_executor.unregister_effort_strategy("duplicate-test") is True


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
        output={"html": (str, "render-ready HTML", True)},
        output_format="flat_markdown",
    )

    assert execution.status == "success"
    assert execution.plan.get("expected_result_format") == "flat_markdown"
    assert "Required sections" in MockSkillsRequester.requests[-1]
    output = cast(dict[str, Any], execution.output)
    assert output["html"] == "<section>OK</section>"


def test_run_skills_task_rejects_output_and_semantic_outputs_together(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    with pytest.raises(ValueError, match="Use either output= or semantic_outputs="):
        _create_agent().run_skills_task(
            "handle release",
            skills=["alpha-skill"],
            mode="required",
            output={"decision": (str, "decision", True)},
            semantic_outputs={"decision": (str, "decision", True)},
        )


def test_run_skills_task_accepts_semantic_outputs_as_deprecated_alias(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill", body="Draft a render-ready HTML fragment.")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    with pytest.warns(DeprecationWarning):
        execution = _create_agent().run_skills_task(
            "render release HTML",
            skills=["alpha-skill"],
            mode="required",
            semantic_outputs={"html": (str, "render-ready HTML", True)},
            output_format="flat_markdown",
        )

    assert execution.status == "success"
    assert execution.plan.get("expected_result_shape") == {"html": (str, "render-ready HTML", True)}


def test_run_skills_task_consumes_agent_prompt_output_contract(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill", body="Draft a render-ready HTML fragment.")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    agent = _create_agent()
    execution = (
        agent
        .set_agent_prompt("info", {"product": "Agently"})
        .input("render release HTML")
        .output({"html": (str, "render-ready HTML", True)}, format="flat_markdown")
        .run_skills_task(skills=["alpha-skill"], mode="required")
    )

    assert execution.status == "success"
    assert execution.plan.get("expected_result_format") == "flat_markdown"
    assert "product" in MockSkillsRequester.requests[-1]
    assert "Required sections" in MockSkillsRequester.requests[-1]
    output = cast(dict[str, Any], execution.output)
    assert output["html"] == "<section>OK</section>"
    assert agent.request.prompt.get(inherit=False) == {}
    assert agent.agent_prompt.get("info") == {"product": "Agently"}


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
async def test_skills_runtime_context_executes_action_specs_through_action_flow():
    agent = _create_agent()
    agent.action.register_action(
        action_id="alpha_lookup",
        desc="Return alpha data.",
        kwargs={"value": (str, "value")},
        func=lambda value: {"alpha": value},
    )
    agent.action.register_action(
        action_id="beta_lookup",
        desc="Return beta data.",
        kwargs={"value": (str, "value")},
        func=lambda value: {"beta": value},
    )
    context = create_agent_skills_runtime_context(agent)

    results = await context.async_execute_action_specs(
        [
            {"type": "tool", "name": "alpha_lookup", "kwargs": {"value": "a"}},
            {"type": "tool", "name": "beta_lookup", "kwargs": {"value": "b"}},
        ],
        concurrency=2,
    )

    assert [item["status"] for item in results] == ["success", "success"]
    assert results[0]["result"] == {"alpha": "a"}
    assert results[1]["result"] == {"beta": "b"}


@pytest.mark.asyncio
async def test_execute_skills_plans_uses_triggerflow_orchestration(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    _skill(tmp_path / "beta", name="Beta Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")
    Agently.skills_executor.install_skills(tmp_path / "beta")
    agent = _create_agent()
    alpha_plan = agent.resolve_skills_plan("handle release", skills=["alpha-skill"], mode="required")
    beta_plan = agent.resolve_skills_plan("handle release", skills=["beta-skill"], mode="required")

    concurrent = await agent.async_execute_skills_plans(
        "handle release",
        plans=[alpha_plan, beta_plan],
        mode="concurrent",
    )
    sequential = await agent.async_execute_skills_plans(
        "handle release",
        plans=[alpha_plan, beta_plan],
        mode="sequential",
    )

    assert [item.status for item in concurrent] == ["success", "success"]
    assert [item.status for item in sequential] == ["success", "success"]


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
