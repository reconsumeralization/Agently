from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.agent_extensions.SkillsExtension import SkillsExtension
from agently.core import SkillLibrary


def _write_skill(root: Path, *, name: str, description: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"Apply the {name} procedure.",
        encoding="utf-8",
    )
    return root


class _SelectionRequest:
    def __init__(self, selected_keys: list[str]) -> None:
        self.selected_keys = selected_keys
        self.slots: dict[str, Any] = {}

    def input(self, value: Any) -> "_SelectionRequest":
        self.slots["input"] = value
        return self

    def info(self, value: Any) -> "_SelectionRequest":
        self.slots["info"] = value
        return self

    def instruct(self, value: Any) -> "_SelectionRequest":
        self.slots["instruct"] = value
        return self

    def output(self, value: Any, *, format: str | None = None) -> "_SelectionRequest":
        self.slots["output"] = value
        self.slots["output_format"] = format
        return self

    async def async_get_data(self) -> dict[str, Any]:
        return {"selected_keys": self.selected_keys}


@pytest.mark.asyncio
async def test_model_decision_skill_selection_uses_host_keys_and_exact_revision(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    first = library.install(
        _write_skill(tmp_path / "first", name="Release Review", description="Review a release."),
        trust="trusted",
    )
    second = library.install(
        _write_skill(tmp_path / "second", name="Email Draft", description="Draft an email."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-selection-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    request = _SelectionRequest(["skill-option:2"])
    cast(Any, agent).create_temp_request = lambda: request
    execution = (
        agent.create_execution()
        .input("Draft the customer email")
        .use_skills([first.skill_id, second.skill_id], mode="model_decision")
    )

    await execution.async_prepare_task_context()

    cards = request.slots["info"]["offered_skills"]
    assert [card["skill_key"] for card in cards] == ["skill-option:1", "skill-option:2"]
    assert cards[0]["name"] == first.name
    assert "revision_ref" not in cards[0]
    assert "installed_path" not in cards[0]
    assert request.slots["output_format"] == "json"
    assert [binding.revision_ref for binding in execution.skill_bindings] == [
        second.revision_ref
    ]
    assert execution.skill_bindings[0].mode == "model_decision"


@pytest.mark.asyncio
async def test_invalid_model_skill_key_fails_closed_without_binding(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "skill", name="Safe Review", description="Review safely."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-selection-invalid-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    cast(Any, agent).create_temp_request = lambda: _SelectionRequest(["skill-option:unknown"])
    execution = agent.input("Review this").use_skills(package.skill_id)

    await execution.async_prepare_task_context()

    assert execution.skill_bindings == []
    assert execution.diagnostics["skill_selection"]["status"] == "invalid"


@pytest.mark.asyncio
async def test_required_skill_pack_resolves_to_exact_library_revisions(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    first = _write_skill(
        tmp_path / "pack" / "first",
        name="First Pack Skill",
        description="First procedure.",
    )
    second = _write_skill(
        tmp_path / "pack" / "second",
        name="Second Pack Skill",
        description="Second procedure.",
    )
    pack = library.install_pack(
        tmp_path / "pack",
        skill_pack_id="review-pack",
        name="Review Pack",
        trust="trusted",
    )
    agent = Agently.create_agent("skill-pack-binding-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    execution = agent.input("Run both procedures").use_skills_packs(
        pack.skill_pack_id,
        mode="required",
    )

    await execution.async_prepare_task_context()

    expected = {
        library.resolve("first-pack-skill").revision_ref,
        library.resolve("second-pack-skill").revision_ref,
    }
    assert set(pack.revision_refs) == expected
    assert {binding.revision_ref for binding in execution.skill_bindings} == expected
    assert all(binding.mode == "required" for binding in execution.skill_bindings)


@pytest.mark.asyncio
async def test_resolve_skills_plan_is_binding_and_route_preview(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "skill", name="Plan Preview", description="Preview a plan."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-plan-preview-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library

    plan = await agent.async_resolve_skills_plan(
        "Preview this task",
        skills=[package.skill_id],
        mode="required",
    )

    assert plan["schema_version"] == "agently.skill_binding_plan.compat.v2"
    assert plan["status"] == "resolved"
    assert plan["selected_skills"][0]["revision_ref"] == package.revision_ref
    assert plan["route_preview"]["selected_route"] in {"model_request", "agent_task"}
    assert plan["route_preview"]["selected_route"] != "skills"


@pytest.mark.asyncio
async def test_required_skill_availability_joins_revision_selector_to_canonical_id(
    tmp_path: Path,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.task_strategy import (
        _resolve_required_skill_availability,
    )

    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(
            tmp_path / "skill",
            name="Revision Identity",
            description="Exercise required Skill identity joins.",
        ),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-required-revision-identity").use_task_workspace(
        tmp_path / "work"
    )
    agent.skill_library = library
    execution = agent.input("Use the exact installed revision").use_skills(
        package.revision_ref,
        mode="required",
    )
    await execution.async_prepare_task_context()

    assert execution.required_skill_ids() == [package.revision_ref]
    selected, failure = await _resolve_required_skill_availability(
        execution,
        goal="Use the exact installed revision",
    )

    assert selected == [package.skill_id]
    assert failure is None


class _OrdinaryExecution:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.status = "created"
        self.id = "ordinary-execution"

    def input(self, value: Any) -> "_OrdinaryExecution":
        self.calls.append(("input", value))
        return self

    def output(self, value: Any, **kwargs: Any) -> "_OrdinaryExecution":
        self.calls.append(("output", value))
        return self

    def use_skills(self, value: Any, **kwargs: Any) -> "_OrdinaryExecution":
        self.calls.append(("use_skills", {"value": value, **kwargs}))
        return self

    def strategy(self, value: str) -> "_OrdinaryExecution":
        self.calls.append(("strategy", value))
        return self

    async def async_get_data(self) -> dict[str, Any]:
        self.status = "success"
        self.calls.append(("async_get_data", None))
        return {"result": "done"}


@pytest.mark.asyncio
async def test_run_skills_task_adapts_an_ordinary_agent_execution(tmp_path: Path) -> None:
    agent = Agently.create_agent("skill-run-adapter-test").use_task_workspace(tmp_path)
    ordinary = _OrdinaryExecution()
    cast(Any, agent).create_execution = lambda **_: ordinary

    result = await agent.async_run_skills_task(
        "Run the task",
        skills=["installed-skill"],
        mode="required",
        output={"result": str},
    )

    assert [call[0] for call in ordinary.calls] == [
        "input",
        "output",
        "use_skills",
        "async_get_data",
    ]
    assert result.output == {"result": "done"}
    assert result.execution is ordinary


def test_agent_skills_extension_has_no_second_execution_owner() -> None:
    module = inspect.getmodule(SkillsExtension)
    assert module is not None
    source = inspect.getsource(module)

    for forbidden in (
        "SkillsManager",
        "async_execute_plan",
        "async_execute_skills_plan",
        "effort_strateg",
        "create_agent_skills_manager_context",
    ):
        assert forbidden not in source


def test_agent_task_planner_snapshot_contains_actions_not_skill_context(tmp_path: Path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.task_strategy import (
        _planner_capability_snapshot,
    )

    class Execution:
        def action_candidates(self):
            return [{"action_id": "read_file", "description": "Read a file."}]

        def skill_candidate_summary(self):
            raise AssertionError("AgentTask planner must not read Skill candidates as capabilities.")

    assert _planner_capability_snapshot(cast(Any, Execution())) == [
        {
            "id": "read_file",
            "kind": "action",
            "route": "model_request",
            "guidance_access": "none",
            "description": "Read a file.",
        }
    ]


def test_agent_task_skill_lifecycle_event_names_do_not_claim_activation() -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules import (
        task_strategy,
    )

    source = inspect.getsource(task_strategy)

    assert '"skills.revisions.bound"' in source
    assert '"skills.activation.bound"' not in source
