import json
import shutil
import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.agent_extensions.SkillsExtension._SkillsContext import create_agent_skills_runtime_context
from agently.core.application.AgentExecution import AgentExecutionStream
from agently.builtins.plugins.SkillsExecutor import SkillInstallError, SkillNormalizationError
from agently.core import PluginManager, TaskDAGExecutor
from agently.types.data import AgentlyRequestData
from agently.utils import Settings


class MockSkillsRequester:
    name = "MockSkillsRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}
    requests: list[str] = []
    settings_snapshots: list[dict[str, Any]] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        MockSkillsRequester.requests = []
        MockSkillsRequester.settings_snapshots = []

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        MockSkillsRequester.settings_snapshots.append(
            {
                "active": self.settings.get("plugins.ModelRequester.activate"),
                "model": self.settings.get("plugins.ModelRequester.MockSkillsRequester.model"),
                "base_url": self.settings.get("plugins.ModelRequester.MockSkillsRequester.base_url"),
                "api_key": self.settings.get("plugins.ModelRequester.MockSkillsRequester.api_key"),
            }
        )
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
        if "capability_need_discovery" in request_text:
            response = {
                "capability_needs": [
                    {
                        "skill_id": "ambiguous-research-skill",
                        "need": "http_request",
                        "evidence": "The Skill asks for integration discovery that may require read-only API inspection.",
                        "risk": "network",
                        "confidence": 0.72,
                    }
                ]
            }
        elif "candidate_skill_cards" in request_text:
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
    MockSkillsRequester.settings_snapshots = []


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


def test_build_context_pack_includes_task_relevant_examples_references_and_citations(tmp_path):
    source = tmp_path / "source"
    _skill(
        source,
        name="Model Setup Skill",
        description="Generate Agently provider setup code.",
        body="Read the reference and use the runnable example before producing setup code.",
    )
    _write(
        source / "references" / "provider-setup.md",
        "Use Agently.set_settings for provider model keys and keep API keys in environment variables.\n",
    )
    _write(
        source / "examples" / "model-setup-minimal.py",
        "from agently import Agently\nAgently.set_settings('model.OpenAICompatible.model', 'deepseek-chat')\n",
    )
    contract = Agently.skills_executor.install_skills(source)

    pack = Agently.skills_executor.build_context_pack(
        task="Generate Python provider setup code for DeepSeek.",
        intent="generate_code",
        skill_ids=[str(contract["skill_id"])],
        include_examples="auto",
        include_references=True,
        budget_chars=8000,
    )

    assert pack["schema_version"] == "agently.skills.context_pack.v1"
    assert pack["skills"][0]["guidance"]["citation"] == "skills/model-setup-skill/SKILL.md"
    resources = pack["skills"][0]["selected_resources"]
    paths = {item["path"] for item in resources}
    assert "examples/model-setup-minimal.py" in paths
    assert "references/provider-setup.md" in paths
    assert any("Agently.set_settings" in item["content"] for item in resources)
    assert "skills/model-setup-skill/examples/model-setup-minimal.py" in pack["citations"]
    assert "skills/model-setup-skill/references/provider-setup.md" in pack["citations"]


def test_build_context_pack_fails_closed_for_public_lookup_without_context(tmp_path):
    source = tmp_path / "source"
    _skill(source, name="Lookup Skill", description="Uses public lookup.")
    contract = Agently.skills_executor.install_skills(source)

    pack = Agently.skills_executor.build_context_pack(
        task="Refresh public facts before drafting.",
        skill_ids=[str(contract["skill_id"])],
        include_public_lookup=True,
    )

    assert pack["public_sources"] == []
    assert any(
        item["code"] == "skill_context_pack.public_lookup_context_missing"
        for item in pack["diagnostics"]
    )


@pytest.mark.asyncio
async def test_agent_build_skills_context_pack_actionizes_scripts_only_when_allowed(monkeypatch, tmp_path):
    source = tmp_path / "source"
    _skill(
        source,
        name="Script Skill",
        description="Uses a bundled helper.",
        body="Run scripts/helper.sh only when the host policy allows script execution.",
    )
    _write(source / "scripts" / "helper.sh", "#!/usr/bin/env bash\necho helper\n")
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(auto_load={"script_run": "allow"})
    enable_calls: list[dict[str, Any]] = []
    executed = False

    def fake_enable_shell(**kwargs):
        nonlocal executed
        enable_calls.append(dict(kwargs))

        def fake_script_action(cmd):
            nonlocal executed
            executed = True
            return {"cmd": cmd}

        agent.action.register_action(
            action_id=kwargs["action_id"],
            desc="Run a bundled Skill script.",
            kwargs={"cmd": (list, "Command.")},
            func=fake_script_action,
            tags=[f"agent-{ agent.name }"],
            expose_to_model=bool(kwargs.get("expose_to_model", True)),
            side_effect_level="exec",
        )
        return agent

    monkeypatch.setattr(agent, "enable_shell", fake_enable_shell)

    pack = await agent.async_build_skills_context_pack(
        "Run the helper if useful.",
        skill_ids=["script-skill"],
        actionize_scripts=True,
    )

    skills = cast(list[dict[str, Any]], pack.get("skills", []))
    candidates = cast(list[dict[str, Any]], skills[0].get("action_candidates", []))
    assert candidates[0]["action_id"] == "run_script_skill_script"
    assert candidates[0]["source_path"] == "scripts/helper.sh"
    assert agent.action.action_registry.has("run_script_skill_script")
    assert enable_calls[0]["root"] == Path(Agently.skills_executor.inspect_skills("script-skill")["source"]["installed_path"])
    assert "scripts/helper.sh" in enable_calls[0]["commands"]
    assert executed is False


@pytest.mark.asyncio
async def test_skills_context_pack_task_dag_resolver_emits_pack_output(tmp_path):
    source = tmp_path / "source"
    _skill(
        source,
        name="DAG Skill",
        description="Feeds TaskDAG context.",
        body="Use the example when generating setup code.",
    )
    _write(source / "examples" / "setup.py", "from agently import Agently\n")
    contract = Agently.skills_executor.install_skills(source)
    graph = {
        "graph_id": "skills-context-pack-dag",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {
                "id": "pack",
                "kind": "skill",
                "inputs": {
                    "task": "Generate setup code.",
                    "skill_ids": [str(contract["skill_id"])],
                    "intent": "generate_code",
                    "include_examples": True,
                },
            }
        ],
        "semantic_outputs": {"pack": "pack"},
    }

    snapshot = await TaskDAGExecutor(
        Agently.skills_executor.task_dag_resolver()
    ).async_run(graph, timeout=1)

    result = snapshot["task_results"]["pack"]
    assert result["schema_version"] == "agently.skills.context_pack.v1"
    assert result["skills"][0]["skill_id"] == "dag-skill"
    assert result["skills"][0]["selected_resources"][0]["path"] == "examples/setup.py"
    assert snapshot["semantic_outputs"]["pack"]["result"]["schema_version"] == "agently.skills.context_pack.v1"


@pytest.mark.asyncio
async def test_private_skill_capability_fields_do_not_mount_by_default(monkeypatch, tmp_path):
    source = tmp_path / "source"
    _write(
        source / "SKILL.md",
        """---
name: Private Capability Skill
description: Contains Agently-private capability fields.
mcp: https://example.com/mcp
allowed-tools: [Bash, calculate_budget]
allowed-actions: [send_email]
allow-scripts: true
---

# Private Capability Skill

Use the guidance.
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
        "use private fields",
        skills=["private-capability-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert mounted == []
    assert not agent.action.action_registry.has("Bash")
    assert not agent.action.action_registry.has("calculate_budget")
    assert not agent.action.action_registry.has("send_email")


def test_skill_capability_needs_discovered_from_guidance_resources_and_public_metadata(tmp_path):
    source = tmp_path / "research-writer"
    _write(
        source / "SKILL.md",
        """---
name: Research Writer
description: Research public context and write a report.
compatibility:
  network: required for HTTP API inspection
metadata:
  environment: MCP server may be useful when configured by the host.
---

# Research Writer

Search public sources, browse relevant pages, run scripts/helper.py when the
host allows it, and write the final Markdown deliverable to a workspace file.
""",
    )
    _write(source / "scripts" / "helper.py", "print('helper')\n")
    Agently.skills_executor.install_skills(source)

    plan = _create_agent().resolve_skills_plan(
        "prepare a research report",
        skills=["research-writer"],
        mode="required",
    )

    needs = cast(list[dict[str, Any]], plan.get("capability_needs", []))
    need_names = {item["need"] for item in needs}
    assert {"web_search", "web_browse", "workspace_write", "script_run", "python", "http_request", "mcp"} <= need_names
    assert any(item["source"] == "body" and item["need"] == "web_search" for item in needs)
    assert any(item["source"] == "resource_index" and item["resource_path"] == "scripts/helper.py" for item in needs)
    assert any(item["source"] == "compatibility" and item["need"] == "http_request" for item in needs)
    assert any(item["source"] == "metadata" and item["need"] == "mcp" for item in needs)


def test_skill_capability_needs_can_use_model_assisted_discovery(tmp_path):
    source = tmp_path / "ambiguous-research-skill"
    _write(
        source / "SKILL.md",
        """---
name: Ambiguous Research Skill
description: Evaluate integration readiness for a partner system.
---

# Ambiguous Research Skill

Understand the partner integration surface and identify what host-side
inspection capabilities are needed before producing recommendations.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent()
    agent.settings.set("skills.capability_discovery.model_assisted", True)

    plan = agent.resolve_skills_plan(
        "prepare integration readiness notes",
        skills=["ambiguous-research-skill"],
        mode="required",
    )

    needs = cast(list[dict[str, Any]], plan.get("capability_needs", []))
    assert any(
        item["need"] == "http_request"
        and item["source"] == "model_inference"
        and item["risk"] == "network"
        for item in needs
    )
    assert "capability_need_discovery" in MockSkillsRequester.requests[-1]


@pytest.mark.asyncio
async def test_skill_capability_policy_allows_builtin_action_loading(tmp_path):
    source = tmp_path / "research-writer"
    _write(
        source / "SKILL.md",
        """---
name: Research Writer
description: Research public context and write a report.
---

# Research Writer

Search public sources, browse relevant pages, run Python for small data shaping,
and write the final Markdown deliverable to a workspace file.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(
        auto_load={
            "web_search": "allow",
            "web_browse": "allow",
            "workspace_write": "allow",
            "python": "allow",
        },
        workspace_root=str(tmp_path / "workspace"),
    )

    execution = await agent.async_run_skills_task(
        "prepare a research report",
        skills=["research-writer"],
        mode="required",
    )

    assert execution.status == "success"
    assert agent.action.action_registry.has("search")
    assert agent.action.action_registry.has("browse")
    assert agent.action.action_registry.has("write_file")
    assert agent.action.action_registry.has("run_python")
    mounted = [item for item in execution.runtime_stream if item.get("type") == "skills.capability.mounted"]
    assert {item["need"] for item in mounted} >= {"web_search", "web_browse", "workspace_write", "python"}


@pytest.mark.asyncio
async def test_execution_scoped_capabilities_are_released_after_run(tmp_path):
    """ISSUE-002: capability_scope='execution' reverses mounts after the run."""
    source = tmp_path / "research-writer-scoped"
    _write(
        source / "SKILL.md",
        """---
name: Research Writer Scoped
description: Research public context and write a report.
---

# Research Writer Scoped

Search public sources and write the final Markdown deliverable to a file.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(
        auto_load={"web_search": "allow", "workspace_write": "allow"},
        workspace_root=str(tmp_path / "workspace"),
        capability_scope="execution",
    )

    execution = await agent.async_run_skills_task(
        "prepare a research report",
        skills=["research-writer-scoped"],
        mode="required",
    )

    assert execution.status == "success"
    mounted = [item for item in execution.runtime_stream if item.get("type") == "skills.capability.mounted"]
    assert mounted  # capabilities were mounted during the run
    # ...but execution-scoped capabilities must not persist on the agent afterward.
    assert not agent.action.action_registry.has("search")
    assert not agent.action.action_registry.has("write_file")


@pytest.mark.asyncio
async def test_skill_web_search_mounts_without_environment_mutation(monkeypatch, tmp_path):
    source = tmp_path / "search-skill"
    _write(
        source / "SKILL.md",
        """---
name: Search Skill
description: Search public sources.
---

# Search Skill

Search public sources before answering.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(
        auto_load={"web_search": "allow"},
    )
    shell_calls: list[dict[str, Any]] = []

    def fake_enable_shell(**kwargs):
        shell_calls.append(dict(kwargs))
        return agent

    monkeypatch.setattr(agent, "enable_shell", fake_enable_shell)

    execution = await agent.async_run_skills_task(
        "answer with public context",
        skills=["search-skill"],
        mode="required",
    )

    assert execution.status == "success"
    # Mounting web_search must never shell out to mutate the host environment.
    assert shell_calls == []
    search_spec = agent.action.action_registry.get_spec("search")
    assert search_spec is not None
    assert search_spec.get("meta", {}).get("provider") == "ddgs"
    assert search_spec.get("meta", {}).get("backend") == "auto"


@pytest.mark.asyncio
async def test_skill_capability_policy_requires_approval_for_script_run(tmp_path):
    source = tmp_path / "script-skill"
    _write(
        source / "SKILL.md",
        """---
name: Script Skill
description: Uses a bundled helper.
---

# Script Skill

Run scripts/helper.sh when the host permits script execution.
""",
    )
    _write(source / "scripts" / "helper.sh", "#!/usr/bin/env bash\necho helper\n")
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(auto_load={"script_run": "approval"})

    execution = await agent.async_run_skills_task(
        "run helper",
        skills=["script-skill"],
        mode="required",
    )

    assert execution.status == "blocked"
    assert not agent.action.action_registry.has("run_script_skill_script")
    diagnostics = cast(dict[str, Any], execution.output).get("diagnostics", [])
    assert diagnostics[0]["code"] == "approval_required"
    assert diagnostics[0]["need"] == "script_run"


@pytest.mark.asyncio
async def test_skill_capability_approval_auto_approve_mounts_requested_capability(tmp_path):
    source = tmp_path / "script-skill"
    _write(
        source / "SKILL.md",
        """---
name: Script Skill
description: Uses a bundled helper.
---

# Script Skill

Run scripts/helper.sh when the host permits script execution.
""",
    )
    _write(source / "scripts" / "helper.sh", "#!/usr/bin/env bash\necho helper\n")
    Agently.skills_executor.install_skills(source)
    agent = (
        _create_agent()
        .configure_skill_capabilities(auto_load={"script_run": "approval"})
        .configure_policy_approval(handler="auto_approve")
    )

    execution = await agent.async_run_skills_task(
        "run helper",
        skills=["script-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert agent.action.action_registry.has("run_script_skill_script")
    assert any(item.get("type") == "skills.capability.approved" for item in execution.runtime_stream)
    assert any(
        item.get("type") == "skills.capability.mounted"
        and item.get("need") == "script_run"
        and item.get("policy") == "approval"
        for item in execution.runtime_stream
    )


@pytest.mark.asyncio
async def test_skill_capability_policy_wraps_script_resource_as_shell_action(tmp_path):
    source = tmp_path / "script-skill"
    _write(
        source / "SKILL.md",
        """---
name: Script Skill
description: Uses a bundled helper.
---

# Script Skill

Run scripts/helper.sh when the host permits script execution.
""",
    )
    _write(source / "scripts" / "helper.sh", "#!/usr/bin/env bash\necho helper\n")
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(auto_load={"script_run": "allow"})

    execution = await agent.async_run_skills_task(
        "run helper",
        skills=["script-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert agent.action.action_registry.has("run_script_skill_script")
    spec = agent.action.action_registry.get_spec("run_script_skill_script")
    assert spec is not None
    assert spec.get("side_effect_level") == "exec"
    assert any(
        item.get("type") == "skills.capability.mounted"
        and item.get("need") == "script_run"
        and "run_script_skill_script" in item.get("action_ids", [])
        for item in execution.runtime_stream
    )


@pytest.mark.asyncio
async def test_skill_capability_policy_allows_http_request_loading(tmp_path):
    source = tmp_path / "http-skill"
    _write(
        source / "SKILL.md",
        """---
name: HTTP Skill
description: Inspects read-only HTTP APIs.
---

# HTTP Skill

Use an HTTP API request to inspect read-only public data.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(auto_load={"http_request": "allow"})

    execution = await agent.async_run_skills_task(
        "inspect the public API",
        skills=["http-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert agent.action.action_registry.has("http_request")
    assert any(
        item.get("type") == "skills.capability.mounted"
        and item.get("need") == "http_request"
        and "http_request" in item.get("action_ids", [])
        for item in execution.runtime_stream
    )


@pytest.mark.asyncio
async def test_skill_capability_policy_allows_configured_mcp_loading(monkeypatch, tmp_path):
    source = tmp_path / "mcp-skill"
    _write(
        source / "SKILL.md",
        """---
name: MCP Skill
description: Uses MCP when the host configures it.
---

# MCP Skill

Use MCP tools when the host has configured the server.
""",
    )
    Agently.skills_executor.install_skills(source)
    mcp_config = {"mcpServers": {"demo": {"url": "https://example.com/mcp"}}}
    agent = _create_agent().configure_skill_capabilities(
        auto_load={"mcp": "allow"},
        mcp_config=mcp_config,
    )
    mounted: list[Any] = []

    async def fake_use_mcp(config, **kwargs):
        mounted.append(config)
        return agent

    monkeypatch.setattr(agent, "async_use_mcp", fake_use_mcp)

    execution = await agent.async_run_skills_task(
        "use configured mcp",
        skills=["mcp-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert mounted == [mcp_config]
    assert any(
        item.get("type") == "skills.capability.mounted"
        and item.get("need") == "mcp"
        and "mcp" in item.get("action_ids", [])
        for item in execution.runtime_stream
    )


@pytest.mark.asyncio
async def test_skill_capability_policy_off_blocks_missing_builtin_loading(tmp_path):
    source = tmp_path / "search-skill"
    _write(
        source / "SKILL.md",
        """---
name: Search Skill
description: Searches public context.
---

# Search Skill

Search public sources before answering.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().configure_skill_capabilities(auto_load={"web_search": "off"})

    execution = await agent.async_run_skills_task(
        "answer with public context",
        skills=["search-skill"],
        mode="required",
    )

    assert execution.status == "blocked"
    assert not agent.action.action_registry.has("search")
    diagnostics = cast(dict[str, Any], execution.output).get("diagnostics", [])
    assert diagnostics[0]["code"] == "capability_disabled"
    assert diagnostics[0]["need"] == "web_search"


@pytest.mark.asyncio
async def test_skill_workspace_capability_prefers_workspace_owned_file_actions(monkeypatch, tmp_path):
    source = tmp_path / "writer-skill"
    _write(
        source / "SKILL.md",
        """---
name: Writer Skill
description: Writes the requested report.
---

# Writer Skill

Write the final Markdown deliverable to a workspace file.
""",
    )
    Agently.skills_executor.install_skills(source)
    agent = _create_agent().use_workspace(tmp_path / "workspace").configure_skill_capabilities(
        auto_load={"workspace_write": "allow"},
    )
    workspace = agent.workspace
    assert workspace is not None
    calls: list[dict[str, Any]] = []
    original = workspace.enable_file_actions

    def wrapped_enable_file_actions(agent_arg, **kwargs):
        calls.append(dict(kwargs))
        return original(agent_arg, **kwargs)

    monkeypatch.setattr(workspace, "enable_file_actions", wrapped_enable_file_actions)

    execution = await agent.async_run_skills_task(
        "write the report to outputs/report.md",
        skills=["writer-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert calls and calls[0]["write"] is True
    assert agent.action.action_registry.has("write_file")
    spec = agent.action.action_registry.get_spec("write_file")
    assert spec is not None
    assert spec.get("meta", {}).get("root") == str(workspace.files_root)


@pytest.mark.asyncio
async def test_interview_skill_uses_policy_loaded_research_and_workspace_actions(monkeypatch, tmp_path):
    source = tmp_path / "interview-question-preparer"
    _write(
        source / "SKILL.md",
        """---
name: interview-question-preparer
description: Prepare an evidence-backed blog/media interview preparation brief for a specified person.
---

# Interview Question Preparer

Use this Skill to prepare a serious blog/media interview brief for a specified
person, author, founder, maintainer, or project owner. This is not a hiring
interview, recruiting screen, or candidate evaluation.

## Workflow

1. Clarify the interview target, audience, and intended article angle.
2. Research public context before drafting. Search broadly first, then browse
   the most relevant pages.
3. Keep compact source notes.
4. Reflect on whether the information is sufficient before finalizing.
5. Write the final Markdown deliverable to the requested workspace path.
6. After writing or revising the requested file, read file back from the
   workspace when a workspace read capability is available and include a
   concise validation checklist.

## Output Requirements

The final Markdown file must include source notes, a story/interview angle,
sufficiency reflection, grouped blog/media interview questions, and at least
eight concrete questions.
""",
    )
    Agently.skills_executor.install_skills(source)
    workspace_root = tmp_path / "workspace"
    agent = _create_agent().configure_skill_capabilities(
        auto_load={
            "web_search": "allow",
            "web_browse": "allow",
            "workspace_read": "allow",
            "workspace_write": "allow",
        },
        workspace_root=str(workspace_root),
    )
    agent.set_settings("effort_presets", {"react": {"step_budget": 5}})

    action_rounds: list[set[str]] = []

    async def fake_plan_and_execute(**kwargs):
        action_ids = {
            str(item.get("action_id") or item.get("name") or "")
            for item in kwargs.get("action_list", [])
        }
        action_rounds.append(action_ids)
        round_no = len(action_rounds)
        if round_no == 1:
            assert {"search", "browse", "read_file", "write_file"} <= action_ids
            return [
                {
                    "status": "success",
                    "success": True,
                    "action_id": "search",
                    "purpose": "find public context",
                    "result": [
                        {
                            "title": "Anthropic public context",
                            "href": "https://www.anthropic.com/company",
                            "body": "Jared Kaplan is associated with Anthropic and frontier AI research.",
                        },
                        {
                            "title": "OpenAI public context",
                            "href": "https://openai.com/about/",
                            "body": "Sam Altman is OpenAI CEO.",
                        }
                    ],
                    "data": [
                        {
                            "title": "Anthropic public context",
                            "href": "https://www.anthropic.com/company",
                        },
                        {
                            "title": "OpenAI public context",
                            "href": "https://openai.com/about/",
                        }
                    ],
                }
            ]
        if round_no == 2:
            return [
                {
                    "status": "success",
                    "success": True,
                    "action_id": "browse",
                    "purpose": "read selected source",
                    "result": {
                        "url": "https://www.anthropic.com/company",
                        "title": "Anthropic",
                        "content": "Anthropic public company context for blog interview preparation.",
                    },
                    "data": {
                        "url": "https://www.anthropic.com/company",
                        "title": "Anthropic",
                    },
                }
            ]
        if round_no == 3:
            markdown = """# Jared Kaplan and Sam Altman Blog Interview Preparation

## Source Notes

- https://www.anthropic.com/company - public Anthropic context.
- https://openai.com/about/ - public OpenAI context.

## Story / Interview Angle

Compare how Anthropic and OpenAI frame frontier model scaling, safety,
productization, developer adoption, and governance tradeoffs.

## Sufficiency Reflection

Public information is enough for a first blog interview brief, but internal
roadmap choices, safety-review details, and product decision evidence need
direct follow-up.

## Blog Interview Questions

1. Jared Kaplan: How has Anthropic's research culture shaped its model-scaling choices?
2. Jared Kaplan: Which safety lessons from frontier model development are most under-discussed?
3. Jared Kaplan: How should developers reason about capability gains versus deployment risk?
4. Jared Kaplan: What evidence would change your mind about current scaling assumptions?
5. Sam Altman: How does OpenAI prioritize product speed against governance constraints?
6. Sam Altman: What should developers expect from OpenAI's platform roadmap?
7. Sam Altman: Which user-facing AI product failures have most shaped OpenAI's strategy?
8. Sam Altman: How should startups decide between building on OpenAI APIs and open models?
9. Comparative: Where do Anthropic and OpenAI genuinely disagree about safe deployment?
10. Comparative: How should enterprise developers evaluate trust, reliability, and cost?
11. Comparative: What should regulators understand about frontier lab incentives?
12. Comparative: What developer ecosystem signals matter most over the next two years?

## Follow-up Probes

- Ask both speakers for concrete examples rather than slogans.
- Separate public claims from internal evidence they can discuss.
"""
            write_result = agent.action.execute_action(
                "write_file",
                {
                    "path": "outputs/kaplan_altman_interview_questions.md",
                    "content": markdown,
                },
            )
            return [
                {
                    "status": write_result.get("status"),
                    "success": write_result.get("status") == "success",
                    "action_id": "write_file",
                    "purpose": "write final blog interview brief",
                    "result": write_result.get("data"),
                    "data": write_result.get("data"),
                }
            ]
        if round_no == 4:
            read_result = agent.action.execute_action(
                "read_file",
                {
                    "path": "outputs/kaplan_altman_interview_questions.md",
                    "max_bytes": 20000,
                },
            )
            return [
                {
                    "status": read_result.get("status"),
                    "success": read_result.get("status") == "success",
                    "action_id": "read_file",
                    "purpose": "read back final blog interview brief",
                    "result": read_result.get("data"),
                    "data": read_result.get("data"),
                }
            ]
        return []

    monkeypatch.setattr(agent.action, "async_plan_and_execute", fake_plan_and_execute)

    execution = await agent.async_run_skills_task(
        (
            "Prepare a comparative blog interview preparation brief for Jared Kaplan from Anthropic "
            "and Sam Altman from OpenAI. Search and browse public evidence, reflect on "
            "information sufficiency, and write Markdown to outputs/kaplan_altman_interview_questions.md."
        ),
        skills=["interview-question-preparer"],
        mode="required",
        effort="react",
    )

    output_path = workspace_root / "outputs" / "kaplan_altman_interview_questions.md"
    file_text = output_path.read_text(encoding="utf-8")
    plan_needs = cast(list[dict[str, Any]], execution.plan.get("capability_needs", []))
    mounted_needs = {
        item.get("need")
        for item in execution.runtime_stream
        if item.get("type") == "skills.capability.mounted"
    }

    assert execution.status == "success"
    assert output_path.is_file()
    assert {"web_search", "web_browse", "workspace_read", "workspace_write"} <= {item.get("need") for item in plan_needs}
    assert {"web_search", "web_browse", "workspace_read", "workspace_write"} <= mounted_needs
    assert any(item.get("type") == "skills.react.action_runtime_round" for item in execution.runtime_stream)
    assert len(action_rounds) >= 4
    assert "allowed-actions" not in (source / "SKILL.md").read_text(encoding="utf-8")
    assert "allow-scripts" not in (source / "SKILL.md").read_text(encoding="utf-8")
    assert "mcpServers" not in (source / "SKILL.md").read_text(encoding="utf-8")
    assert file_text.count("？") + file_text.count("?") >= 12
    assert "Sufficiency Reflection" in file_text
    assert "Jared Kaplan" in file_text
    assert "Sam Altman" in file_text
    assert "https://www.anthropic.com/company" in file_text
    assert "https://openai.com/about/" in file_text


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
async def test_staged_strategy_without_stages_reports_error(tmp_path):
    """ISSUE-014: staged with no execution_stages must report error, not success."""
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")
    agent = _create_agent()
    agent.set_settings("effort_presets", {"staged_preset": {"strategy": "staged"}})

    execution = await agent.async_run_skills_task(
        "handle release",
        skills=["alpha-skill"],
        mode="required",
        effort="staged_preset",
    )

    assert execution.status == "error"
    assert "execution_stages" in str(execution.output)


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


@pytest.mark.asyncio
async def test_skills_stage_model_key_resolves_model_profile_base_url_and_key_pool(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")
    agent = _create_agent()
    agent.set_settings("skills.runtime.stage_model_keys", {"finalizer": "hosted-finalizer"})
    agent.set_settings("model_pool", {"hosted-finalizer": "mock-finalizer-profile"})
    agent.set_settings(
        "model_profiles",
        {
            "mock-finalizer-profile": {
                "provider": "MockSkillsRequester",
                "model": "mock-finalizer-model",
                "base_url": "mock://finalizer",
                "api_key_pool": "mock-finalizer-keys",
            }
        },
    )
    agent.set_settings(
        "api_key_pools",
        {
            "mock-finalizer-keys": {
                "strategy": "fixed",
                "keys": [{"id": "primary", "value": "mock-finalizer-key"}],
            }
        },
    )

    execution = await agent.async_run_skills_task(
        "handle release",
        skills=["alpha-skill"],
        mode="required",
    )

    assert execution.status == "success"
    assert any(
        snapshot == {
            "active": "MockSkillsRequester",
            "model": "mock-finalizer-model",
            "base_url": "mock://finalizer",
            "api_key": "mock-finalizer-key",
        }
        for snapshot in MockSkillsRequester.settings_snapshots
    )


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


def test_required_plan_defaults_plain_skill_to_single_shot(tmp_path):
    _skill(tmp_path / "plain", name="Plain Skill")
    Agently.skills_executor.install_skills(tmp_path / "plain")

    plan = _create_agent().resolve_skills_plan(
        "handle release",
        skills=["plain-skill"],
        mode="required",
    )

    assert plan.get("execution_strategy") == "single_shot"
    assert plan.get("execution_stages") == []


def test_required_plan_ignores_private_staged_strategy_and_declared_stages(tmp_path):
    source = tmp_path / "multi-step-writer"
    _write(
        source / "SKILL.md",
        """---
name: Multi Step Writer
description: Complete writing tasks in sequential stages.
execution: staged
stages:
  - Read and analyze the task requirements.
  - Write a complete first draft.
  - Review and polish the result.
---

# Multi Step Writer

Use the declared stages in order.
""",
    )
    Agently.skills_executor.install_skills(source)

    plan = _create_agent().resolve_skills_plan(
        "draft release notes",
        skills=["multi-step-writer"],
        mode="required",
    )

    assert plan.get("execution_strategy") == "single_shot"
    assert plan.get("execution_stages") == []


def test_required_plan_ignores_private_strategy_fields_by_default(tmp_path):
    source = tmp_path / "private-fields"
    _write(
        source / "SKILL.md",
        """---
name: Private Fields
description: Contains non-standard Agently strategy fields.
execution: staged
allowed-tools: [search]
stages:
  - Analyze.
---

# Private Fields

Use the guidance.
""",
    )
    Agently.skills_executor.install_skills(source)

    plan = _create_agent().resolve_skills_plan(
        "use the skill",
        skills=["private-fields"],
        mode="required",
    )

    assert plan.get("execution_strategy") == "single_shot"
    assert plan.get("execution_stages") == []


def test_required_plan_ignores_private_react_and_staged_fields(tmp_path):
    staged = tmp_path / "staged"
    _write(
        staged / "SKILL.md",
        """---
name: Staged Skill
description: Uses staged execution.
execution: staged
stages:
  - Analyze.
  - Draft.
---

# Staged Skill

Use the declared stages.
""",
    )
    react = tmp_path / "react"
    _write(
        react / "SKILL.md",
        """---
name: Research Assistant
description: Uses tools when external facts are needed.
allowed-tools: [search, calculate]
---

# Research Assistant

Use the declared tools when needed.
""",
    )
    Agently.skills_executor.install_skills(staged)
    Agently.skills_executor.install_skills(react)

    plan = _create_agent().resolve_skills_plan(
        "verify a fact and calculate a total",
        skills=["staged-skill", "research-assistant"],
        mode="required",
    )

    assert plan.get("execution_strategy") == "single_shot"
    assert plan.get("execution_stages") == []


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


@pytest.mark.parametrize("output_format", ["xml_field", "yaml_literal"])
def test_resolve_skills_plan_accepts_new_structured_output_formats(tmp_path, output_format):
    _skill(tmp_path / "alpha", name="Alpha Skill")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    plan = _create_agent().resolve_skills_plan(
        "handle release",
        skills=["alpha-skill"],
        mode="required",
        output_format=output_format,
    )

    assert plan.get("expected_result_format") == output_format


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


def test_http_capability_blocks_private_hosts_by_default():
    """ISSUE-015: the built-in HTTP capability must default-deny SSRF targets."""
    from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.executor import SkillExecutor

    assert SkillExecutor._http_host_allowed("127.0.0.1", {}) is False
    assert SkillExecutor._http_host_allowed("169.254.169.254", {}) is False  # cloud metadata
    assert SkillExecutor._http_host_allowed("10.0.0.5", {}) is False
    assert SkillExecutor._http_host_allowed("8.8.8.8", {}) is True  # public IP literal, no DNS
    # Explicit host allowlist opens an internal host.
    assert SkillExecutor._http_host_allowed("127.0.0.1", {"allow_hosts": ["127.0.0.1"]}) is True
    assert SkillExecutor._http_host_allowed("10.0.0.5", {"allow_private": True}) is True
    # Denylist overrides allowance for a public host.
    assert SkillExecutor._http_host_allowed("8.8.8.8", {"deny_hosts": ["8.8.8.8"]}) is False


def test_skill_script_commands_exclude_package_runners():
    """ISSUE-015: npx/npm must not be in the default script allowlist."""
    from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.executor import SkillExecutor

    executor = SkillExecutor(registry=Agently.skills_executor.registry)
    commands = executor._script_commands({"resource_index": {"resources": []}}, {})
    assert "npx" not in commands
    assert "npm" not in commands
    assert "python" in commands


def test_no_matching_skill_returns_no_match(tmp_path):
    _skill(tmp_path / "alpha", name="Alpha Skill", description="Use for alpha-only work.")
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    execution = _create_agent().run_skills_task("unrelated billing issue", mode="model_decision")

    assert execution.status == "no_match"
    assert execution.output is None


def test_model_decision_matches_on_keywords_not_description_words(tmp_path):
    """ISSUE-006: candidate selection must not over-match on description words.

    The Skill declares keyword ``release``; a task that merely shares a common
    description word (``review``) but not the keyword/name must not activate it.
    """
    _skill(
        tmp_path / "alpha",
        name="Alpha Skill",
        description="Use this to review the alpha release.",
    )
    Agently.skills_executor.install_skills(tmp_path / "alpha")

    miss = _create_agent().run_skills_task("review the quarterly billing numbers", mode="model_decision")
    assert miss.status == "no_match"

    hit = _create_agent().run_skills_task("prepare the release notes", mode="model_decision")
    assert hit.status != "no_match"
