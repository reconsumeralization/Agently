from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import pytest

from agently.core.application.SkillsExecutor import SkillsExecutor
from agently.core.application.SkillsManager import LocalSkillActionResolver
from agently.utils import Settings
from agently.utils.DeprecationWarnings import DeprecationWarnings


class _FakeActionRegistry:
    def __init__(self, actions: list[dict[str, Any]]):
        self._actions = actions

    def get_action_list(self, *, tags: list[str] | None = None):
        return list(self._actions)


class _FakeAgent:
    name = "skills-manager-test-agent"

    def __init__(self, actions: list[dict[str, Any]]):
        self.action = _FakeActionRegistry(actions)


class _FakeContext:
    async def async_request_model(self, **_kwargs):
        raise AssertionError("model-assisted resolution should be opt-in")


@pytest.mark.asyncio
async def test_local_skill_action_resolver_fuzzy_selects_existing_python_action():
    resolver = LocalSkillActionResolver()
    agent = _FakeAgent(
        [
            {
                "action_id": "local_python_runner",
                "name": "Workspace Python",
                "desc": "Run trusted workspace Python snippets.",
                "tags": ["agent-skills-manager-test-agent"],
            }
        ]
    )

    result = await resolver.async_resolve(
        agent=agent,
        context=_FakeContext(),
        need={"skill_id": "script-skill", "need": "python"},
        policy={"model_assisted": False},
    )

    assert result["status"] == "selected"
    assert result["selected_action_id"] == "local_python_runner"
    assert result["matched_by"] == "action_id_tokens"
    assert result["action_ids"] == ["local_python_runner"]


@pytest.mark.asyncio
async def test_local_skill_action_resolver_fails_closed_on_ambiguous_python_actions():
    resolver = LocalSkillActionResolver()
    agent = _FakeAgent(
        [
            {"action_id": "local_python_runner", "tags": ["agent-skills-manager-test-agent"]},
            {"action_id": "custom_python_action", "tags": ["agent-skills-manager-test-agent"]},
        ]
    )

    result = await resolver.async_resolve(
        agent=agent,
        context=_FakeContext(),
        need={"skill_id": "script-skill", "need": "python"},
        policy={"model_assisted": False},
    )

    assert result["status"] == "ambiguous"
    assert result["selected_action_id"] == ""
    assert sorted(item["action_id"] for item in result["alternatives"]) == [
        "custom_python_action",
        "local_python_runner",
    ]


def test_skills_executor_facade_delegates_to_skills_manager_with_warning():
    class FakeManager:
        plugin_manager = object()
        settings = Settings(name="fake-skills-manager")
        impl = object()
        registry = object()

        def build_context_pack(self):
            return {"delegated": True}

    DeprecationWarnings.reset_registry()
    facade = SkillsExecutor(cast(Any, object()), Settings(name="facade-test"), manager=cast(Any, FakeManager()))
    with pytest.warns(DeprecationWarning, match="SkillsExecutor is deprecated"):
        assert facade.build_context_pack() == {"delegated": True}

    assert facade.impl is FakeManager.impl
    assert facade.registry is FakeManager.registry


def test_internal_code_does_not_import_legacy_skills_executor_dependency():
    repo_root = Path(__file__).resolve().parents[1]
    scanned_roots = [
        repo_root / "agently" / "builtins" / "agent_extensions",
        repo_root / "agently" / "builtins" / "plugins" / "AgentOrchestrator",
        repo_root / "agently" / "builtins" / "plugins" / "Blocks",
        repo_root / "agently" / "core" / "application" / "AgentExecution",
        repo_root / "agently" / "core" / "application" / "DynamicTask",
        repo_root / "agently" / "core" / "orchestration" / "TaskDAG",
    ]
    violations: list[str] = []

    for root in scanned_roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    imported_names = {alias.name for alias in node.names}
                    if "SkillsExecutor" in module or "skills_executor" in imported_names:
                        violations.append(str(path.relative_to(repo_root)))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if "SkillsExecutor" in alias.name:
                            violations.append(str(path.relative_to(repo_root)))

    assert violations == []
