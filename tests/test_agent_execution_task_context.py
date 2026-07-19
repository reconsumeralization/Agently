from __future__ import annotations

from pathlib import Path

import pytest

from agently import Agently
from agently.core import AgentTask, SkillLibrary, TaskContext, TaskWorkspace
from agently.types.data import ContextReadIntent


def _write_skill(root: Path, *, name: str = "Execution Skill") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Governs one execution.\n"
        "---\n\n"
        "Use the execution-specific procedure.",
        encoding="utf-8",
    )
    return root


def test_agent_exposes_task_workspace_without_reusing_task_workspace_owner(tmp_path: Path) -> None:
    agent = Agently.create_agent("task-context-workspace-test").use_task_workspace(
        tmp_path,
        mode="read_write",
    )

    assert isinstance(agent.task_workspace, TaskWorkspace)
    assert agent.task_workspace.root == tmp_path.resolve()
    assert not hasattr(agent.task_workspace, "build_context")


def test_agent_execution_creates_task_context_before_route_selection(tmp_path: Path) -> None:
    agent = Agently.create_agent("task-context-creation-test").use_task_workspace(tmp_path)

    execution = agent.create_execution()

    assert isinstance(execution.task_context, TaskContext)
    assert execution.task_context.task_id == execution.id
    assert execution.task_context.context_id == f"agent_execution:{execution.id}:context"
    assert execution.route_info == {}
    assert execution._selected_route is None
    snapshot = execution.task_context.snapshot()
    assert len(snapshot.bindings) == 2
    assert {binding.source_id.split(":", 1)[0] for binding in snapshot.bindings} == {
        "task-workspace",
        "record-store",
    }
    assert execution.task_workspace.root == tmp_path.resolve()


@pytest.mark.asyncio
async def test_agent_execution_prepares_prompt_facts_as_task_context_entries(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("task-context-prompt-test").use_task_workspace(tmp_path)
    execution = (
        agent.create_execution()
        .input("Prepare the release report")
        .info({"release": "4.x"})
        .instruct("Cite verified evidence")
    )

    await execution.async_prepare_task_context()
    snapshot = execution.task_context.snapshot()
    by_slot = {entry.metadata["prompt_slot"]: entry for entry in snapshot.entries}

    assert by_slot["input"].role == "state"
    assert by_slot["input"].required is True
    assert by_slot["info"].role == "information"
    assert by_slot["instruct"].role == "instruction"
    assert all(entry.metadata["already_in_prompt"] is True for entry in snapshot.entries)


@pytest.mark.asyncio
async def test_agent_execution_binds_active_session_memory_source(tmp_path: Path) -> None:
    agent = Agently.create_agent("task-context-session-memory")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    agent.use_task_workspace(tmp_path / "workspace")
    agent.activate_session(session_id="session-memory")
    assert agent.activated_session is not None
    agent.activated_session.use_memory(mode="AgentlyMemory")
    execution = agent.create_execution().input("Use the remembered delivery promise")

    context = await execution.async_prepare_task_context()

    assert "session_memory" in context.source_catalog()


@pytest.mark.asyncio
async def test_required_skill_is_bound_by_exact_revision_into_execution_task_context(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    agent = Agently.create_agent("task-context-skill-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    execution = agent.create_execution().require_skills(package.skill_id)

    await execution.async_prepare_task_context()

    assert [binding.revision_ref for binding in execution.skill_bindings] == [
        package.revision_ref
    ]
    snapshot = execution.task_context.snapshot()
    skill_binding = next(
        item for item in snapshot.bindings if item.source_id.startswith("skill-context:")
    )
    assert package.revision in skill_binding.source_revision


@pytest.mark.asyncio
async def test_missing_required_skill_blocks_during_context_preparation(tmp_path: Path) -> None:
    agent = Agently.create_agent("task-context-missing-skill-test").use_task_workspace(tmp_path)
    agent.skill_library = SkillLibrary(tmp_path / "library")
    execution = agent.create_execution().require_skills("missing-skill")

    with pytest.raises(RuntimeError, match="Required Skill.*missing-skill"):
        await execution.async_prepare_task_context()

    assert execution.route_info == {}


@pytest.mark.asyncio
async def test_direct_consumer_reads_skill_instruction_from_same_task_context(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    agent = Agently.create_agent("task-context-read-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    execution = (
        agent.create_execution()
        .input("Apply the installed procedure")
        .require_skills(package.skill_id)
    )

    await execution.async_prepare_task_context()
    context_package = await execution.async_read_task_context(
        consumer_id=f"model_request:{execution.id}",
        phase="direct",
    )

    assert context_package.task_context_id == execution.task_context.context_id
    instructions = [
        block for block in context_package.blocks if block.role == "instruction"
    ]
    assert [block.content for block in instructions] == [
        "Use the execution-specific procedure."
    ]
    assert all(
        not block.source_ref.endswith(":prompt:input")
        for block in context_package.blocks
    )
    consumption = execution.record_context_consumption(
        context_package,
        request_id="observed-request-id",
    )
    assert consumption.block_ids == tuple(
        block.block_id for block in context_package.blocks
    )
    assert execution.logs["context_consumptions"][0]["request_id"] == (
        "observed-request-id"
    )


@pytest.mark.asyncio
async def test_execution_context_read_accepts_consumer_specific_input_intent(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("task-context-intent-test").use_task_workspace(tmp_path)
    execution = agent.create_execution().input("Prepare the overall release report")
    source_ref = "caller://release/security-section"
    execution.task_context.put(
        role="information",
        content="The security section must disclose the unresolved audit gap.",
        entry_id="security-section",
        source_ref=source_ref,
    )

    context_package = await execution.async_read_task_context(
        consumer_id=f"model_request:{execution.id}:security",
        phase="security_draft",
        intent=ContextReadIntent(
            query="security audit gaps only",
            explicit_refs=(source_ref,),
        ),
    )

    assert context_package.consumer_id == f"model_request:{execution.id}:security"
    assert context_package.phase == "security_draft"
    selected = next(
        block for block in context_package.blocks if block.source_ref == source_ref
    )
    assert selected.content == (
        "The security section must disclose the unresolved audit gap."
    )


def test_agent_task_accepts_same_task_context_and_task_workspace_from_execution(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("task-context-handoff-test").use_task_workspace(
        tmp_path,
        mode="read_write",
    )
    execution = agent.create_execution()

    task = AgentTask(
        agent,
        goal="Produce the report",
        success_criteria=["The report exists."],
        task_context=execution.task_context,
        task_workspace=execution.task_workspace,
    )

    assert task.task_context is execution.task_context
    assert task.task_workspace is execution.task_workspace
    source_ids = [binding.source_id for binding in task.task_context.snapshot().bindings]
    assert len([source_id for source_id in source_ids if source_id.startswith("record-store:")]) == 1
