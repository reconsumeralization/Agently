from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agently import Agently
from agently.core import AgentTask, SkillLibrary
from agently.core.application.SkillLibrary import SkillBinding, SkillContextSource
from agently.core.context import TaskContext
from agently.types.data import ContextReadIntent


def _write_skill(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: Governed Work\n"
        "description: Governs every task phase.\n"
        "---\n\n"
        "Apply this instruction in every governed model phase.",
        encoding="utf-8",
    )
    return root


class _FailingRequiredContextSource:
    source_id = "source:failing-required"
    source_revision = "rev:1"

    async def async_list_candidates(
        self,
        _intent: ContextReadIntent,
        *,
        limit: int,
        filters=None,
    ):
        del limit, filters
        raise RuntimeError("required source unavailable")

    async def async_read(self, *_args, **_kwargs):
        raise AssertionError("candidate reads must not start after list failure")


@pytest.mark.asyncio
async def test_agent_task_phase_readers_share_context_but_not_disclosure_history(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    skill = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    agent = Agently.create_agent("task-reader-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    execution = agent.input("Complete the governed work").require_skills(skill.skill_id)
    await execution.async_prepare_task_context()
    task = AgentTask(
        agent,
        goal="Complete the governed work",
        success_criteria=["The work follows the installed procedure."],
        task_context=execution.task_context,
        task_workspace=execution.task_workspace,
        context_budget={"chars": 6000},
    )

    planning = await task._read_task_context_package(
        phase="planning",
        consumer_id="agent_task:planner",
    )
    verification = await task._read_task_context_package(
        phase="verification",
        consumer_id="agent_task:verifier",
    )

    assert planning.task_context_id == execution.task_context.context_id
    assert verification.task_context_id == execution.task_context.context_id
    assert planning.package_id != verification.package_id
    assert task.context_readers[("agent_task:planner", "planning")] is not (
        task.context_readers[("agent_task:verifier", "verification")]
    )
    assert [
        block.content for block in planning.blocks if block.role == "instruction"
    ] == ["Apply this instruction in every governed model phase."]
    assert [
        block.content for block in verification.blocks if block.role == "instruction"
    ] == ["Apply this instruction in every governed model phase."]
    assert planning.source_revisions == verification.source_revisions


@pytest.mark.asyncio
async def test_agent_task_fails_closed_when_required_source_cannot_list_candidates(
    tmp_path: Path,
) -> None:
    task_context = TaskContext("required-source-task")
    task_context.attach(
        _FailingRequiredContextSource(),
        binding_id="binding:failing-required",
        required=True,
    )
    agent = Agently.create_agent("required-source-task").use_task_workspace(
        tmp_path / "work"
    )
    task = AgentTask(
        agent,
        task_id="required-source-task",
        goal="Use required context",
        success_criteria=["Required context is used."],
        task_context=task_context,
        task_workspace=agent.task_workspace,
    )

    with pytest.raises(RuntimeError, match="Required TaskContext content"):
        await task._read_task_context_package(
            phase="planning",
            consumer_id="agent_task:required-source-task:planner",
        )


@pytest.mark.asyncio
async def test_agent_task_context_budget_can_explicitly_allow_lossy_required_digest(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "large-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: Large Governed Work\n"
        "description: Large protected procedure.\n"
        "---\n\n"
        + ("Preserve this protected instruction.\n" * 500),
        encoding="utf-8",
    )
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(skill_root, trust="trusted")
    binding = SkillBinding.create(installed, task_id="large-task", mode="required")
    task_context = TaskContext("large-task")
    task_context.attach(SkillContextSource(library, bindings=(binding,)), required=True)
    agent = Agently.create_agent("large-task-reader").use_task_workspace(tmp_path / "work")
    task = AgentTask(
        agent,
        task_id="large-task",
        goal="Apply the large procedure",
        success_criteria=["The procedure remains traceable."],
        task_context=task_context,
        task_workspace=agent.task_workspace,
        context_budget={"chars": 800, "required_overflow": "lossy_digest"},
    )

    package = await task._read_task_context_package(
        phase="planning",
        consumer_id="agent_task:large-task:planner",
    )

    assert len(package.blocks) == 1
    assert package.blocks[0].completeness == "lossy"
    assert package.blocks[0].metadata["original_chars"] == len(installed.instruction_body)
    assert not any(item.required for item in package.omissions)


@pytest.mark.asyncio
async def test_agent_task_child_model_read_inherits_required_overflow_policy(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "large-child-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: Large Child Work\n"
        "description: Large child execution procedure.\n"
        "---\n\n"
        + ("Preserve this child instruction.\n" * 500),
        encoding="utf-8",
    )
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(skill_root, trust="trusted")
    agent = Agently.create_agent("large-child-reader").use_task_workspace(
        tmp_path / "work"
    )
    agent.skill_library = library
    agent.require_skills(installed.revision_ref, always=True)
    task = AgentTask(
        agent,
        goal="Apply the large child procedure",
        success_criteria=["The child read remains traceable."],
        context_budget={"chars": 800, "required_overflow": "lossy_digest"},
    )

    child = task._create_bounded_child_execution(
        lineage={
            "task_id": task.id,
            "iteration_id": "iteration-1",
            "step_id": "child-read",
        }
    )
    package = await child.async_read_task_context(
        consumer_id=f"model_request:{child.id}",
        phase="direct",
    )

    assert child.options["context_budget"] == {
        "chars": 800,
        "required_overflow": "lossy_digest",
    }
    assert len(package.blocks) == 1
    assert package.blocks[0].completeness == "lossy"
    assert not any(item.required for item in package.omissions)


@pytest.mark.asyncio
async def test_agent_task_records_consumption_only_for_concrete_request(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("task-consumption-test").use_task_workspace(tmp_path)
    execution = agent.input("Read task context")
    await execution.async_prepare_task_context()
    task = AgentTask(
        agent,
        goal="Read task context",
        success_criteria=["The context is read."],
        task_context=execution.task_context,
        task_workspace=execution.task_workspace,
    )

    package = await task._read_task_context_package(
        phase="planning",
        consumer_id="agent_task:planner",
    )

    assert task.context_consumptions == []
    consumption = task._record_task_context_consumption(
        package,
        request_id="planner-request-1",
    )
    assert consumption.package_id == package.package_id
    assert consumption.request_id == "planner-request-1"
    assert consumption.block_ids == tuple(block.block_id for block in package.blocks)


@pytest.mark.asyncio
async def test_agent_task_skill_projection_uses_skill_domain_binding_and_mode(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    binding = SkillBinding.create(
        installed,
        task_id="projection-task",
        mode="model_decision",
        binding_id="skill_binding:projection-task:1",
    )
    task_context = TaskContext("projection-task")
    task_context.attach(
        SkillContextSource(library, bindings=(binding,)),
        binding_id="skill_context_binding:projection-task",
    )
    package = await task_context.reader(consumer="planner").async_read("Apply the Skill")

    projected = AgentTask._project_task_context_package(package)
    skill = projected["skill_projection"]["skills"][0]

    assert projected["source_coverage"] == package.to_dict()["source_coverage"]
    assert projected["continuation_available"] is False
    assert skill["binding_id"] == binding.binding_id
    assert skill["mode"] == "model_decision"
    assert projected["skill_projection"]["required_skill_ids"] == []


@pytest.mark.asyncio
async def test_required_skill_context_event_uses_registered_skill_binding_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    agent = Agently.create_agent("skill-context-event-test").use_task_workspace(tmp_path / "work")
    binding = SkillBinding.create(
        installed,
        task_id="skill-context-event-task",
        mode="required",
        binding_id="skill_binding:skill-context-event-task:1",
    )
    task_context = TaskContext("skill-context-event-task")
    task_context.attach(
        SkillContextSource(library, bindings=(binding,)),
        binding_id="skill_context_binding:skill-context-event-task",
    )
    task = AgentTask(
        agent,
        task_id="skill-context-event-task",
        goal="Apply the governed procedure",
        success_criteria=["The procedure is applied."],
        task_context=task_context,
        task_workspace=agent.task_workspace,
        options={
            "capability_constraints": {"skills": {"required": [installed.skill_id]}},
            "skill_bindings": [
                {
                    "binding_id": binding.binding_id,
                    "canonical_skill_id": installed.skill_id,
                    "mode": binding.mode,
                    "resolved_revision": binding.revision_ref,
                }
            ],
        },
    )
    package = await task._read_task_context_package(
        phase="planning",
        consumer_id="agent_task:skill-context-event-task:planner",
    )
    projected = task._project_task_context_package(package)
    events: list[tuple[str, dict]] = []

    async def record_event(name, payload, **_kwargs):
        events.append((name, payload))

    monkeypatch.setattr(task, "_emit", record_event)
    await task._emit_required_skill_context_bound(
        projected,
        request_id="observed-model-request",
        phase="work.plan",
    )

    assert events[0][0] == "skills.context.bound"
    assert events[0][1]["binding_ids"] == [binding.binding_id]
    assert task._lifecycle_state.skill_bindings[binding.binding_id]["contexts"][0][
        "request_id"
    ] == "observed-model-request"


class _PlanRequest:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.id = "planner-request-observed"
        self.error = error
        self.input_value = None

    def input(self, value):
        self.input_value = value
        return self

    def instruct(self, _value):
        return self

    def output(self, _value, *, format=None):
        return self

    def get_result(self):
        return self

    async def async_get_data(self):
        if self.error is not None:
            raise self.error
        return {
            "execution_shape": "direct",
            "step_instruction": "Complete one bounded step.",
            "rationale": "The task is linear.",
        }


@pytest.mark.asyncio
async def test_planner_records_exact_context_consumption_only_after_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = Agently.create_agent("planner-consumption-test").use_task_workspace(tmp_path)
    task = AgentTask(
        agent,
        goal="Use the governed facts",
        success_criteria=["One bounded step is planned."],
    )
    task.task_context.put(
        role="information",
        content="The release remains blocked pending review.",
        entry_id="release-fact",
        required=True,
    )
    request = _PlanRequest()
    monkeypatch.setattr(agent, "create_temp_request", lambda: request)
    monkeypatch.setattr(task, "_apply_language_policy_to_request", lambda *_args, **_kwargs: None)

    await task._request_plan(1, {"profile": "preflight", "items": []})

    assert len(task.context_consumptions) == 1
    consumption = task.context_consumptions[0]
    assert consumption.request_id == request.id
    assert consumption.consumer_id == f"agent_task:{task.id}:planner:iteration:1"
    assert consumption.phase == "planning"
    consumed_package = next(
        package for package in task.context_packages if package.package_id == consumption.package_id
    )
    assert consumption.block_ids == tuple(block.block_id for block in consumed_package.blocks)
    assert request.input_value is not None
    assert request.input_value["context_pack"]["package_id"] == consumed_package.package_id


@pytest.mark.asyncio
async def test_failed_planner_request_records_no_context_consumption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = Agently.create_agent("failed-planner-consumption-test").use_task_workspace(tmp_path)
    task = AgentTask(
        agent,
        goal="Use the governed facts",
        success_criteria=["One bounded step is planned."],
    )
    task.task_context.put(
        role="information",
        content="The release remains blocked pending review.",
        entry_id="release-fact",
        required=True,
    )
    request = _PlanRequest(error=RuntimeError("provider failed"))
    monkeypatch.setattr(agent, "create_temp_request", lambda: request)
    monkeypatch.setattr(task, "_apply_language_policy_to_request", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="provider failed"):
        await task._request_plan(1, {"profile": "preflight", "items": []})

    assert task.context_consumptions == []


@pytest.mark.asyncio
async def test_worker_reads_and_records_a_package_independent_from_planner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = Agently.create_agent("worker-context-consumption-test").use_task_workspace(tmp_path)
    task = AgentTask(
        agent,
        task_id="worker-context-consumption-task",
        goal="Use one fact in planning and execution",
        success_criteria=["Both consumers receive governed context."],
    )
    task.task_context.put(
        role="information",
        content="The release remains blocked pending review.",
        entry_id="release-fact",
        required=True,
    )
    planner_request = _PlanRequest()
    monkeypatch.setattr(agent, "create_temp_request", lambda: planner_request)
    monkeypatch.setattr(task, "_apply_language_policy_to_request", lambda *_args, **_kwargs: None)
    await task._request_plan(1, {"profile": "preflight", "items": []})

    class _WorkerExecution:
        id = "bounded-worker-execution"

        def input(self, value):
            self.input_value = value

        def info(self, value):
            self.info_value = value

        def language(self, value):
            self.language_value = value

        def instruct(self, value):
            self.instruction_value = value

        def output(self, value, *, format):
            self.output_value = value
            self.output_format = format

        async def async_get_data(self):
            return {"status": "completed"}

        async def async_get_meta(self):
            return {
                "status": "success",
                "logs": {"model_response_ids": ["worker-model-response"]},
            }

    worker = _WorkerExecution()
    await task._run_bounded_child_execution(
        execution=worker,
        language_policy={"language": "en"},
        input_payload={"goal": task.goal},
        instruction="Execute one bounded step.",
        output_schema={"status": (str, "status", True)},
        output_format="json",
        started_event="agent_task.worker.execution.started",
        started_payload={},
        stream_bridge=lambda _execution: asyncio.sleep(0),
    )

    assert [item.request_id for item in task.context_consumptions] == [
        planner_request.id,
        "worker-model-response",
    ]
    planner_consumption, worker_consumption = task.context_consumptions
    assert planner_consumption.package_id != worker_consumption.package_id
    assert worker_consumption.consumer_id == (
        "agent_task:worker-context-consumption-task:worker:bounded-worker-execution"
    )
    assert worker_consumption.phase == "execution"
    assert worker.input_value["context_pack"]["package_id"] == worker_consumption.package_id


@pytest.mark.asyncio
async def test_agent_task_resume_restores_skill_context_and_context_audit(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    agent = (
        Agently.create_agent("context-resume-test")
        .use_task_workspace(tmp_path / "work")
        .use_record_store(tmp_path / "records", mode="read_write")
    )
    agent.skill_library = library
    execution = agent.input("Resume governed work").require_skills(installed.skill_id)
    await execution.async_prepare_task_context()
    skill_binding = execution.skill_bindings[0]
    task = AgentTask(
        agent,
        task_id="context-resume-task",
        goal="Resume governed work",
        success_criteria=["The exact Skill revision remains available."],
        record_store=execution.record_store,
        task_context=execution.task_context,
        task_workspace=execution.task_workspace,
        options={
            "record_store_recovery": True,
            "skill_bindings": [
                {
                    "binding_id": skill_binding.binding_id,
                    "canonical_skill_id": installed.skill_id,
                    "mode": skill_binding.mode,
                    "resolved_revision": skill_binding.revision_ref,
                }
            ],
        },
    )
    package = await task._read_task_context_package(
        phase="planning",
        consumer_id="agent_task:context-resume-task:planner",
    )
    task._record_task_context_consumption(package, request_id="request-before-resume")
    original_reader_state = task.context_readers[
        ("agent_task:context-resume-task:planner", "planning")
    ]._export_state()
    await task._write_resume_snapshot(
        1,
        {
            "is_complete": False,
            "requires_block": False,
            "reason": "Continue after restart.",
            "missing_criteria": ["One more step is required."],
        },
    )

    resumed = await AgentTask.async_resume(
        agent,
        task.id,
        task_workspace=execution.task_workspace,
        record_store=agent.record_store,
    )

    skill_sources = [
        binding
        for binding in resumed.task_context.snapshot().bindings
        if binding.source_id.startswith("skill-context:")
    ]
    assert len(skill_sources) == 1
    restored_package = next(
        item for item in resumed.context_packages if item.package_id == package.package_id
    )
    assert restored_package.source_coverage == package.source_coverage
    restored_skill = resumed._project_task_context_package(restored_package)["skill_projection"]["skills"][0]
    assert restored_skill["binding_id"] == skill_binding.binding_id
    assert restored_skill["revision_ref"] == skill_binding.revision_ref
    assert [item.to_dict() for item in resumed.context_consumptions] == [
        item.to_dict() for item in task.context_consumptions
    ]
    restored_reader_state = resumed.context_readers[
        ("agent_task:context-resume-task:planner", "planning")
    ]._export_state()
    assert restored_reader_state["disclosed"] == original_reader_state["disclosed"]
