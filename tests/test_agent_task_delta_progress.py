from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.core.application.AgentExecution.Stream import (
    AgentExecutionTextDeltaProjector,
    project_agent_execution_text_delta,
)
from agently.core.application.AgentExecution.Context import AgentExecutionContext
from agently.core.application.AgentTask import AgentTask
from agently.core.runtime import bind_runtime_context
from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.bridges import (
    normalize_action_log,
)
from agently.types.data import AgentExecutionStreamData


def _stream_item(
    path: str,
    value: Any,
    *,
    source: str = "agent_task",
    **meta: Any,
) -> AgentExecutionStreamData:
    return AgentExecutionStreamData(
        path=path,
        value=value,
        event_type="done",
        is_complete=True,
        source=source,
        meta=meta,
    )


def _stream_owner() -> AgentTask:
    task = object.__new__(AgentTask)
    task.id = "delta-progress-task"
    task.status = "running"
    task._stream_items = []
    task._stream_queues = []
    task._last_stream_emit_monotonic = 0.0
    return task


def test_flat_planned_parallel_action_batch_renders_pending_tasks() -> None:
    projector = AgentExecutionTextDeltaProjector()

    text = projector.project(
        _stream_item(
            "agent_task.action.batch.planned",
            {
                "command_count": 2,
                "parallel": True,
                "actions": [
                    {"position": 0, "action_id": "search", "purpose": "Find policy"},
                    {"position": 1, "action_id": "read", "purpose": "Read source"},
                ],
            },
            stream_kind="action_observation",
            phase="planned",
            strategy="flat",
        )
    )

    assert text is not None
    assert "🚀 Next parallel Action batch — 2 tasks" in text
    assert "- ⏳ `search` — Find policy" in text
    assert "- ⏳ `read` — Read source" in text


def test_flat_planned_action_batch_heading_reflects_dispatch_order() -> None:
    cases = [
        ([{"action_id": "read", "purpose": "Read source"}], False, "🚀 Next Action"),
        (
            [
                {"action_id": "read", "purpose": "Read source"},
                {"action_id": "write", "purpose": "Write report"},
            ],
            False,
            "🚀 Next ordered Action batch — 2 tasks",
        ),
        (
            [
                {"action_id": "read", "purpose": "Read source"},
                {"action_id": "write", "purpose": "Write report"},
            ],
            None,
            "🚀 Next Action batch — 2 tasks",
        ),
    ]

    for actions, parallel, expected_heading in cases:
        text = AgentExecutionTextDeltaProjector().project(
            _stream_item(
                "agent_task.action.batch.planned",
                {"actions": actions, "parallel": parallel},
                stream_kind="action_observation",
                phase="planned",
                strategy="flat",
            )
        )
        assert text is not None
        assert text.splitlines()[0] == expected_heading


def test_taskboard_action_batch_does_not_duplicate_board_in_public_delta() -> None:
    text = AgentExecutionTextDeltaProjector().project(
        _stream_item(
            "agent_task.action.batch.planned",
            {"actions": [{"action_id": "read", "purpose": "Read source"}]},
            stream_kind="action_observation",
            phase="planned",
            strategy="taskboard",
        )
    )

    assert text is None


def test_action_lifecycle_uses_emoji_without_raw_inputs_or_outputs() -> None:
    projector = AgentExecutionTextDeltaProjector()
    started = projector.project(
        _stream_item(
            "agent_task.action.started",
            {
                "action_id": "search",
                "status": "started",
                "action_input": {"api_key": "SECRET_MUST_NOT_LEAK"},
            },
            stream_kind="action_observation",
            phase="started",
            strategy="flat",
        )
    )
    completed = projector.project(
        _stream_item(
            "agent_task.action.completed",
            {
                "action_id": "search",
                "status": "completed",
                "success": True,
                "output_summary": {"items": ["one", "two"]},
                "output": {"raw": "RAW_JSON_MUST_NOT_LEAK"},
            },
            stream_kind="action_observation",
            phase="completed",
            strategy="flat",
        )
    )
    failed = projector.project(
        _stream_item(
            "agent_task.action.failed",
            {"action_id": "read", "status": "failed", "error": "source unavailable"},
            stream_kind="action_observation",
            phase="failed",
            strategy="flat",
        )
    )

    assert started == "🔄 `search` — Running\n\n"
    assert completed == "✅ `search` — Completed: returned 2 item(s)\n\n"
    assert failed == "❌ `read` — Failed: source unavailable\n\n"
    combined = f"{started}{completed}{failed}"
    assert "SECRET_MUST_NOT_LEAK" not in combined
    assert "RAW_JSON_MUST_NOT_LEAK" not in combined


def test_action_failure_does_not_serialize_structured_error_as_raw_json() -> None:
    text = AgentExecutionTextDeltaProjector().project(
        _stream_item(
            "agent_task.action.failed",
            {
                "action_id": "read",
                "status": "failed",
                "error": {
                    "message": "request failed at https://example.test/?api_key=SECRET_MUST_NOT_LEAK",
                    "request_data": {"query": "private"},
                },
            },
            stream_kind="action_observation",
            phase="failed",
            strategy="flat",
        )
    )

    assert text is not None
    assert "structured error details are available" in text
    assert "api_key" not in text
    assert "request_data" not in text
    assert "SECRET_MUST_NOT_LEAK" not in text


def test_action_structured_output_path_omits_url_query_and_fragment() -> None:
    text = AgentExecutionTextDeltaProjector().project(
        _stream_item(
            "agent_task.action.completed",
            {
                "action_id": "write",
                "status": "completed",
                "success": True,
                "output_summary": {
                    "path": "https://example.test/reports/final.md?api_key=SECRET#private",
                    "ok": True,
                },
            },
            stream_kind="action_observation",
            phase="completed",
            strategy="flat",
        )
    )

    assert text is not None
    assert "wrote final.md" in text
    assert "api_key" not in text
    assert "SECRET" not in text
    assert "private" not in text


def test_flat_stage_delta_uses_explicit_plan_execution_and_verification_labels() -> None:
    projector = AgentExecutionTextDeltaProjector()
    plan = projector.project(
        _stream_item(
            "agent_task.iteration.1.snapshot.plan",
            {
                "iteration": 1,
                "stage": "plan",
                "snapshot": {
                    "step_instruction": "Collect the authoritative evidence.",
                    "expected_evidence": "One verified source record.",
                },
            },
            task_id="flat-task",
            stream_kind="snapshot",
            stage="plan",
            iteration=1,
        )
    )
    execution = projector.project(
        _stream_item(
            "agent_task.iteration.1.snapshot.execution",
            {
                "iteration": 1,
                "stage": "execution",
                "snapshot": {"execution_result": {"short_summary": "Evidence collected."}},
            },
            task_id="flat-task",
            stream_kind="snapshot",
            stage="execution",
            iteration=1,
        )
    )
    verification = projector.project(
        _stream_item(
            "agent_task.iteration.1.snapshot.verification",
            {
                "iteration": 1,
                "stage": "verification",
                "snapshot": {"is_complete": True, "reason": "All criteria are met."},
            },
            task_id="flat-task",
            stream_kind="snapshot",
            stage="verification",
            iteration=1,
        )
    )

    assert plan is not None and plan.startswith("🧭 Iteration 1 — Plan ready")
    assert execution is not None and execution.startswith("✅ Iteration 1 — Step completed")
    assert verification is not None and verification.startswith("🔎 Iteration 1 — Verification passed")


def test_taskboard_delta_keeps_first_table_then_change_list_with_progress_labels() -> None:
    projector = AgentExecutionTextDeltaProjector()
    revision = {
        "board_id": "board-1",
        "revision_id": "rev-1",
        "graph": {
            "cards": [
                {"id": "collect", "objective": "Collect evidence", "status": "pending"},
                {"id": "report", "objective": "Write report", "status": "pending"},
            ]
        },
    }
    initial = projector.project(
        _stream_item("agent_task.taskboard.plan", {"revision": revision})
    )
    updated = projector.project(
        _stream_item(
            "agent_task.taskboard.tick.1.completed",
            {
                "revision": revision,
                "schedule": {"completed_card_ids": ["collect"], "runnable_card_ids": ["report"]},
                "card_results": {"collect": {"status": "completed"}},
            },
        )
    )

    assert initial is not None and initial.startswith("📋 TaskBoard")
    assert "📊 Overall progress: 0/2 completed" in initial
    assert "| State | Card | Task |" in initial
    assert updated is not None and updated.startswith("🔄 TaskBoard update")
    assert "📊 Overall progress: 1/2 completed" in updated
    assert "Changes:" in updated
    assert "| State | Card | Task |" not in updated


def test_taskboard_anonymous_tick_reuses_the_single_known_board_without_reprinting_table() -> None:
    projector = AgentExecutionTextDeltaProjector()
    long_objective = "Collect authoritative evidence " + ("with bounded context " * 12) + "UNBOUNDED_TAIL"
    initial = projector.project(
        _stream_item(
            "agent_task.taskboard.plan",
            {
                "revision": {
                    "board_id": "board-stable",
                    "revision_id": "rev-0",
                    "graph": {
                        "cards": [
                            {"id": "collect", "objective": long_objective},
                            {"id": "report", "objective": "Write the final report"},
                        ]
                    },
                }
            },
        )
    )
    scheduled = projector.project(
        _stream_item(
            "agent_task.taskboard.tick.1.scheduled",
            {
                "revision": {"revision_id": "rev-0"},
                "schedule": {
                    "revision_id": "rev-0",
                    "runnable_card_ids": ["collect"],
                    "blocked_card_ids": ["report"],
                },
            },
        )
    )

    assert initial is not None and initial.startswith("📋 TaskBoard")
    assert scheduled is not None and scheduled.startswith("🔄 TaskBoard update")
    assert "`board-stable`" in scheduled
    assert "Collect authoritative evidence" in scheduled
    assert "[truncated]" in scheduled
    assert "UNBOUNDED_TAIL" not in scheduled
    assert "| State | Card | Task |" not in scheduled


def test_agent_task_internal_model_delta_stays_out_of_public_text_delta() -> None:
    internal_items = [
        AgentExecutionStreamData(
            path="agent_task.iteration.2.execution.candidate_final_result",
            value=None,
            delta="RAW_CANDIDATE_BODY",
            event_type="delta",
            is_complete=False,
            source="agent_task",
            meta={"stream_kind": "child_execution", "child_source": "model_request"},
        ),
        AgentExecutionStreamData(
            path="agent_task.taskboard.card.report.control.artifact_markdown",
            value=None,
            delta="RAW_INTERMEDIATE_ARTIFACT",
            event_type="delta",
            is_complete=False,
            source="agent_task",
            meta={"stream_kind": "taskboard_control_request", "display_is_intermediate": True},
        ),
    ]
    top_level_model_delta = AgentExecutionStreamData(
        path="model.delta",
        value=None,
        delta="Top-level answer",
        event_type="delta",
        is_complete=False,
        source="model_request",
    )

    assert all(project_agent_execution_text_delta(item) is None for item in internal_items)
    assert project_agent_execution_text_delta(top_level_model_delta) == "Top-level answer"


def test_terminal_delta_keeps_final_response_under_explicit_overall_status() -> None:
    cases = [
        (
            {"status": "completed", "accepted": True, "artifact_status": "accepted"},
            "🎯 Task completed",
        ),
        (
            {"status": "completed", "accepted": True, "artifact_status": "degraded"},
            "⚠️ Task completed with limitations",
        ),
        (
            {"status": "max_iterations", "accepted": False, "artifact_status": "partial"},
            "⚠️ Task partially completed",
        ),
        (
            {"status": "blocked", "accepted": False, "artifact_status": "blocked"},
            "❌ Task failed",
        ),
    ]

    for terminal, expected_heading in cases:
        text = AgentExecutionTextDeltaProjector().project(
            _stream_item(
                "result",
                {**terminal, "final_response": "Final response body."},
            )
        )
        assert text is not None
        lines = text.strip().splitlines()
        assert lines[0] == expected_heading
        assert lines[-1] == "Final response body."


def test_terminal_delta_truncates_long_final_response_instead_of_streaming_the_full_body() -> None:
    long_response = "Concise result. " + ("supporting detail " * 80) + "UNBOUNDED_FINAL_TAIL"

    text = AgentExecutionTextDeltaProjector().project(
        _stream_item(
            "result",
            {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "final_response": long_response,
            },
        )
    )

    assert text is not None
    assert "Concise result." in text
    assert "[truncated]" in text
    assert "UNBOUNDED_FINAL_TAIL" not in text


def test_terminal_delta_does_not_serialize_structured_final_result_as_raw_json() -> None:
    text = AgentExecutionTextDeltaProjector().project(
        _stream_item(
            "result",
            {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "final_result": {"api_key": "secret", "action_input": {"query": "private"}},
            },
        )
    )

    assert text is not None
    assert "Structured final result is available in the full result stream." in text
    assert "api_key" not in text
    assert "action_input" not in text
    assert "secret" not in text


def test_taskboard_terminal_delta_reports_final_board_completion() -> None:
    projector = AgentExecutionTextDeltaProjector()
    revision = {
        "board_id": "board-terminal",
        "revision_id": "rev-2",
        "graph": {
            "cards": [
                {"id": "collect", "objective": "Collect evidence"},
                {"id": "report", "objective": "Write report"},
            ]
        },
    }
    projector.project(
        _stream_item(
            "agent_task.taskboard.tick.2.completed",
            {
                "revision": revision,
                "schedule": {"completed_card_ids": ["collect", "report"]},
                "card_results": {
                    "collect": {"status": "completed"},
                    "report": {"status": "completed"},
                },
            },
        )
    )

    terminal = projector.project(
        _stream_item(
            "result",
            {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "final_response": "Report delivered.",
            },
        )
    )

    assert terminal is not None
    assert "📊 Overall progress: 2/2 completed" in terminal
    assert "🎯 Task completed" in terminal


def test_flat_terminal_delta_reports_completed_stage_count() -> None:
    projector = AgentExecutionTextDeltaProjector()
    projector.project(
        _stream_item(
            "agent_task.iteration.1.snapshot.execution",
            {
                "iteration": 1,
                "stage": "execution",
                "snapshot": {"execution_result": {"short_summary": "Evidence collected."}},
            },
            task_id="flat-terminal",
            stream_kind="snapshot",
            stage="execution",
            iteration=1,
        )
    )
    projector.project(
        _stream_item(
            "agent_task.iteration.1.snapshot.verification",
            {
                "iteration": 1,
                "stage": "verification",
                "snapshot": {"is_complete": True, "reason": "All criteria are met."},
            },
            task_id="flat-terminal",
            stream_kind="snapshot",
            stage="verification",
            iteration=1,
        )
    )

    terminal = projector.project(
        _stream_item(
            "result",
            {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "final_response": "Report delivered.",
            },
            task_id="flat-terminal",
        )
    )

    assert terminal is not None
    assert terminal.startswith("🎯 Task completed")
    assert "📊 Overall progress: 2 completed stages · accepted" in terminal
    assert "🎯 Task completed" in terminal


@pytest.mark.asyncio
async def test_planned_action_batch_emits_only_bounded_host_owned_facts() -> None:
    task = _stream_owner()

    await task._emit_planned_action_batch(
        iteration_index=2,
        commands=[
            {
                "action_id": "search",
                "purpose": "Find the authoritative policy",
                "action_input": {"api_key": "SECRET_MUST_NOT_LEAK", "query": "policy"},
            },
            {"action_id": "read", "purpose": "Read the selected source"},
        ],
        execution_id="execution-1",
        round_index=3,
        concurrency=4,
        parallel=True,
        projection_source="action.plan_ready",
    )

    assert len(task._stream_items) == 1
    item = task._stream_items[0]
    assert item.path == "agent_task.action.batch.planned"
    assert item.meta == {
        "task_id": task.id,
        "status": "running",
        "stream_kind": "action_observation",
        "phase": "planned",
        "strategy": "flat",
        "iteration": 2,
        "round_index": 3,
        "execution_id": "execution-1",
        "projection_source": "action.plan_ready",
    }
    assert item.value == {
        "iteration": 2,
        "round_index": 3,
        "command_count": 2,
        "concurrency": 4,
        "parallel": True,
        "actions": [
            {
                "position": 0,
                "action_id": "search",
                "purpose": "Find the authoritative policy",
                "action_call_id": None,
            },
            {
                "position": 1,
                "action_id": "read",
                "purpose": "Read the selected source",
                "action_call_id": None,
            },
        ],
        "projection_source": "action.plan_ready",
    }
    assert "SECRET_MUST_NOT_LEAK" not in str(item.value)


@pytest.mark.asyncio
async def test_validated_flat_commands_emit_planned_and_started_before_dispatch() -> None:
    task = _stream_owner()

    class Registry:
        @staticmethod
        def has(action_id: str) -> bool:
            return action_id in {"search", "read"}

        @staticmethod
        def get_spec(action_id: str) -> dict[str, Any]:
            return {"action_id": action_id, "kwargs": {}}

    class Action:
        action_registry = Registry()

        async def _async_execute_action_calls(self, **_: Any) -> list[dict[str, Any]]:
            assert [item.path for item in task._stream_items] == [
                "agent_task.action.batch.planned",
                "agent_task.action.started",
                "agent_task.action.started",
            ]
            return [
                {"action_id": "search", "status": "success", "result_preview": "2 records"},
                {"action_id": "read", "status": "failed", "error": "source unavailable"},
            ]

    cast(Any, task).agent = SimpleNamespace(action=Action(), settings={}, name="delta-progress-agent")

    _, execution_meta = await task._execute_bounded_action_commands(
        raw_commands=[
            {"action_id": "search", "action_input": {}, "purpose": "Find sources"},
            {"action_id": "read", "action_input": {}, "purpose": "Read source"},
        ],
        required_action_ids=["search", "read"],
        execution_id="execution-2",
        code_prefix="test.flat",
        execution_kind="flat_bounded_action_calls",
        command_source="flat_plan",
        action_planning_model_requests=0,
        unit_label="Flat step",
        todo_suggestion="Finish the step.",
        concurrency=1,
        iteration_index=1,
        project_flat_action_batch=True,
    )

    assert execution_meta["status"] == "failed"
    assert [item.path for item in task._stream_items] == [
        "agent_task.action.batch.planned",
        "agent_task.action.started",
        "agent_task.action.started",
        "agent_task.action.completed",
        "agent_task.action.failed",
    ]
    assert task._stream_items[0].value["parallel"] is False
    assert [item.value["action_id"] for item in task._stream_items[-2:]] == ["search", "read"]
    assert all(
        item.value["projection_source"] == "validated_bounded_commands"
        and item.value["posthoc_projection"] is False
        for item in task._stream_items[1:]
    )


@pytest.mark.asyncio
async def test_validated_flat_batch_does_not_claim_parallelism_without_concurrency_fact() -> None:
    task = _stream_owner()

    class Registry:
        @staticmethod
        def has(_: str) -> bool:
            return True

        @staticmethod
        def get_spec(action_id: str) -> dict[str, Any]:
            return {"action_id": action_id, "kwargs": {}}

    class Action:
        action_registry = Registry()

        @staticmethod
        async def _async_execute_action_calls(**kwargs: Any) -> list[dict[str, Any]]:
            return [
                {"id": command["action_id"], "status": "success", "success": True}
                for command in kwargs["action_calls"]
            ]

    cast(Any, task).agent = SimpleNamespace(action=Action(), settings={}, name="delta-progress-agent")

    await task._execute_bounded_action_commands(
        raw_commands=[
            {"action_id": "search", "action_input": {}, "purpose": "Find sources"},
            {"action_id": "read", "action_input": {}, "purpose": "Read source"},
        ],
        required_action_ids=["search", "read"],
        execution_id="execution-unknown-concurrency",
        code_prefix="test.flat",
        execution_kind="flat_bounded_action_calls",
        command_source="flat_plan",
        action_planning_model_requests=0,
        unit_label="Flat step",
        todo_suggestion="Finish the step.",
        concurrency=None,
        iteration_index=1,
        project_flat_action_batch=True,
    )

    assert task._stream_items[0].path == "agent_task.action.batch.planned"
    assert task._stream_items[0].value["parallel"] is None


@pytest.mark.asyncio
async def test_live_action_phase_wins_over_posthoc_fallback_without_hiding_later_round() -> None:
    task = _stream_owner()
    execution_meta = {"execution_id": "execution-3", "route": {}}
    owner_context = {"iteration": 1, "strategy": "flat"}

    await task._emit_normalized_action_event(
        "started",
        {"id": "search", "status": "started", "command_index": 0, "round_index": 0},
        execution_meta=execution_meta,
        owner_context=owner_context,
        projection_source="action.started",
        posthoc_projection=False,
    )
    await task._emit_normalized_action_event(
        "started",
        {
            "id": "search",
            "status": "success",
            "command_index": 0,
            "round_index": 0,
            "result_preview": "2 records",
        },
        execution_meta=execution_meta,
        owner_context=owner_context,
    )
    await task._emit_normalized_action_event(
        "started",
        {"id": "search", "status": "started", "command_index": 0, "round_index": 1},
        execution_meta=execution_meta,
        owner_context=owner_context,
        projection_source="action.started",
        posthoc_projection=False,
    )

    assert len(task._stream_items) == 2
    assert [item.value["round_index"] for item in task._stream_items] == [0, 1]
    assert all(item.value["posthoc_projection"] is False for item in task._stream_items)


@pytest.mark.asyncio
async def test_live_started_dedupes_realistic_compacted_posthoc_action_log() -> None:
    task = _stream_owner()
    execution_meta = {
        "execution_id": "execution-compacted-log",
        "route": {},
        "logs": {
            "action_logs": [
                {
                    "action_id": "search",
                    "status": "success",
                    "success": True,
                    "round_index": 0,
                    "command_index": 0,
                    "result_preview": "2 records",
                }
            ]
        },
    }
    owner_context = {"iteration": 1, "strategy": "flat"}

    await task._emit_normalized_action_event(
        "started",
        {"action_id": "search", "status": "started", "round_index": 0, "command_index": 0},
        execution_meta=execution_meta,
        owner_context=owner_context,
        projection_source="action.started",
        posthoc_projection=False,
    )
    await task._emit_action_observation_events(
        1,
        execution_meta=execution_meta,
        owner_context=owner_context,
    )

    assert [item.path for item in task._stream_items] == [
        "agent_task.action.started",
        "agent_task.action.completed",
    ]
    assert task._stream_items[0].value["posthoc_projection"] is False
    assert task._stream_items[1].value["posthoc_projection"] is True


@pytest.mark.asyncio
async def test_real_actionflow_context_identity_dedupes_live_and_posthoc_phases() -> None:
    agent = Agently.create_agent("delta-progress-real-actionflow-dedupe")
    context = AgentExecutionContext(
        execution_id="child-real-actionflow",
        lineage={},
        limits={},
    )
    observations: list[dict[str, Any]] = []

    async def planning_handler(run_context: dict[str, Any], _: dict[str, Any]) -> dict[str, Any]:
        if run_context.get("done_plans"):
            return {"next_action": "response", "use_action": False, "action_calls": []}
        return {
            "next_action": "execute",
            "use_action": True,
            "action_calls": [
                {"action_id": "search", "purpose": "Find sources", "action_input": {}}
            ],
        }

    async def execution_handler(_: dict[str, Any], request: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "action_id": command["action_id"],
                "status": "success",
                "success": True,
                "result_preview": "2 records",
            }
            for command in request["action_calls"]
        ]

    with bind_runtime_context(agent_execution_context=context):
        await agent.action.action_flow.async_run(
            action=agent.action,
            prompt=agent.request.prompt,
            settings=agent.settings,
            action_list=[{"action_id": "search", "desc": "Search", "kwargs": {}}],
            planning_handler=planning_handler,
            execution_handler=execution_handler,
            max_rounds=1,
            runtime_observation_handler=observations.append,
        )

    assert len(context.action_records) == 1
    assert context.action_records[0]["round_index"] == 0
    assert context.action_records[0]["command_index"] == 0
    normalized_logs = [
        normalize_action_log(record, route="model_request", source="ActionFlow")
        for record in context.action_records
    ]
    assert normalized_logs[0]["round_index"] == 0
    assert normalized_logs[0]["command_index"] == 0

    task = _stream_owner()
    for observation in observations:
        await task._project_live_action_observation(
            1,
            observation,
            child_execution_id=context.execution_id,
        )
    live_paths = [item.path for item in task._stream_items]
    assert live_paths == [
        "agent_task.action.batch.planned",
        "agent_task.action.started",
        "agent_task.action.completed",
    ]

    await task._emit_action_observation_events(
        1,
        execution_meta={
            "execution_id": context.execution_id,
            "route": {},
            "logs": {"action_logs": normalized_logs},
        },
        owner_context={"iteration": 1, "strategy": "flat"},
    )

    assert [item.path for item in task._stream_items] == live_paths


@pytest.mark.asyncio
async def test_action_observation_reuses_bounded_carrier_for_agent_execution_progress() -> None:
    agent = Agently.create_agent("delta-progress-action-observation")
    progress_events: list[dict[str, Any]] = []

    class ExecutionContext:
        @staticmethod
        def record_progress(**event: Any) -> None:
            progress_events.append(event)

    with bind_runtime_context(agent_execution_context=ExecutionContext()):
        await agent.action._async_emit_action_flow_observation(
            {
                "kind": "plan_ready",
                "source": "ActionFlow",
                "message": "Action plan ready.",
                "payload": {
                    "round_index": 0,
                    "decision": {
                        "next_action": "execute",
                        "action_calls": [
                            {
                                "action_id": "search",
                                "purpose": "Find evidence",
                                "action_input": {"api_key": "SECRET_MUST_NOT_LEAK"},
                            }
                        ],
                    },
                },
            }
        )

    assert len(progress_events) == 1
    progress = progress_events[0]
    assert progress["stage"] == "action_observation"
    assert progress["status"] == "plan_ready"
    visible = progress["meta"]["action_observation"]
    assert visible["kind"] == "plan_ready"
    assert "SECRET_MUST_NOT_LEAK" not in str(visible)


@pytest.mark.asyncio
async def test_action_flow_plan_ready_carries_owner_confirmed_parallel_dispatch() -> None:
    agent = Agently.create_agent("delta-progress-action-flow")
    observations: list[dict[str, Any]] = []

    async def planning_handler(context: dict[str, Any], _: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [
                {"action_id": "search", "purpose": "Find sources", "action_input": {}},
                {"action_id": "read", "purpose": "Read source", "action_input": {}},
            ],
        }

    async def execution_handler(_: dict[str, Any], request: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"id": command["action_id"], "status": "success", "success": True, "ok": True}
            for command in request["action_calls"]
        ]

    await agent.action.action_flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=[
            {"action_id": "search", "desc": "Search", "kwargs": {}},
            {"action_id": "read", "desc": "Read", "kwargs": {}},
        ],
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=2,
        concurrency=2,
        runtime_observation_handler=observations.append,
    )

    planned = next(
        observation
        for observation in observations
        if observation.get("kind") == "plan_ready"
        and observation.get("payload", {}).get("decision", {}).get("action_calls")
    )
    assert planned["stream_projection"] == {
        "concurrency": 2,
        "parallel": True,
        "dispatch_confirmed": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
async def test_action_flow_max_rounds_plan_is_not_projected_as_upcoming_batch(flow_name: str) -> None:
    agent = Agently.create_agent(f"delta-progress-max-rounds-{flow_name}")
    flow = agent.action._flow_controller.create_named_action_flow(flow_name)
    observations: list[dict[str, Any]] = []

    async def planning_handler(_: dict[str, Any], __: dict[str, Any]) -> dict[str, Any]:
        return {
            "next_action": "execute",
            "use_action": True,
            "action_calls": [
                {"action_id": "search", "purpose": "Find sources", "action_input": {}}
            ],
        }

    async def execution_handler(_: dict[str, Any], __: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("max_rounds=0 must not dispatch an Action")

    await flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=[{"action_id": "search", "desc": "Search", "kwargs": {}}],
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=0,
        runtime_observation_handler=observations.append,
    )

    planned = next((item for item in observations if item.get("kind") == "plan_ready"), None)
    if planned is None:
        assert flow_name == "TriggerFlowActionFlow"
        return
    assert planned["stream_projection"]["dispatch_confirmed"] is False

    task = _stream_owner()
    await task._project_live_action_observation(
        1,
        planned,
        child_execution_id=f"child-{flow_name}",
    )
    assert task._stream_items == []


@pytest.mark.asyncio
async def test_flat_child_action_plan_projects_batch_before_raw_child_audit_item() -> None:
    task = _stream_owner()
    child = SimpleNamespace(id="child-execution-1")
    child_item = AgentExecutionStreamData(
        path="runtime.progress.action_observation.plan_ready",
        value={
            "stage": "action_observation",
            "status": "plan_ready",
            "meta": {
                "action_observation": {
                    "kind": "plan_ready",
                    "payload": {
                        "round_index": 0,
                        "decision": {
                            "next_action": "execute",
                            "action_calls": [
                                {
                                    "action_id": "search",
                                    "purpose": "Find evidence",
                                    "action_input": {"api_key": "SECRET_MUST_NOT_LEAK"},
                                },
                                {
                                    "action_id": "read",
                                    "purpose": "Read source",
                                    "action_input": {},
                                },
                            ],
                        },
                    },
                    "stream_projection": {
                        "concurrency": 2,
                        "parallel": True,
                        "dispatch_confirmed": True,
                    },
                }
            },
        },
        event_type="done",
        is_complete=True,
        source="agent_execution",
        meta={"stream_kind": "runtime_progress"},
    )

    await task._emit_step_execution_stream_item(
        1,
        child,
        child_item,
        execution_shape="actions",
    )

    assert [item.path for item in task._stream_items] == [
        "agent_task.action.batch.planned",
        "agent_task.iteration.1.execution.runtime.progress.action_observation.plan_ready",
    ]
    planned = task._stream_items[0]
    assert planned.value["parallel"] is True
    assert planned.value["actions"][0] == {
        "position": 0,
        "action_id": "search",
        "purpose": "Find evidence",
        "action_call_id": None,
    }
    assert "SECRET_MUST_NOT_LEAK" not in str(planned.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_shape", ["direct", "model", "skill"])
async def test_flat_non_action_shape_keeps_child_observation_high_level(
    execution_shape: str,
) -> None:
    task = _stream_owner()
    child = SimpleNamespace(id=f"child-{execution_shape}")
    child_item = AgentExecutionStreamData(
        path="runtime.progress.action_observation.plan_ready",
        value={
            "stage": "action_observation",
            "status": "plan_ready",
            "meta": {
                "action_observation": {
                    "kind": "plan_ready",
                    "payload": {
                        "round_index": 0,
                        "decision": {
                            "next_action": "execute",
                            "action_calls": [
                                {
                                    "action_id": "internal_detail",
                                    "purpose": "Implementation detail",
                                    "action_input": {"secret": "MUST_NOT_PROJECT"},
                                }
                            ],
                        },
                    },
                    "stream_projection": {"concurrency": 1, "parallel": False},
                }
            },
        },
        event_type="done",
        is_complete=True,
        source="agent_execution",
        meta={"stream_kind": "runtime_progress"},
    )

    await task._emit_step_execution_stream_item(
        1,
        child,
        child_item,
        execution_shape=execution_shape,
    )

    assert len(task._stream_items) == 1
    assert task._stream_items[0].path.startswith("agent_task.iteration.1.execution.")
    assert all(item.path != "agent_task.action.batch.planned" for item in task._stream_items)


@pytest.mark.asyncio
async def test_flat_live_action_lifecycle_projects_started_completed_and_failed() -> None:
    task = _stream_owner()
    observations = [
        {
            "kind": "action_started",
            "payload": {
                "round_index": 0,
                "command_index": 0,
                "action_name": "search",
                "command": {
                    "action_id": "search",
                    "action_input": {"api_key": "SECRET_MUST_NOT_LEAK"},
                },
            },
        },
        {
            "kind": "action_completed",
            "payload": {
                "round_index": 0,
                "record_index": 0,
                "action_name": "search",
                "record": {
                    "action_id": "search",
                    "status": "success",
                    "success": True,
                    "result_preview": "2 records",
                },
            },
        },
        {
            "kind": "action_failed",
            "payload": {
                "round_index": 0,
                "record_index": 1,
                "action_name": "read",
                "record": {
                    "action_id": "read",
                    "status": "failed",
                    "success": False,
                    "error": "source unavailable",
                },
            },
        },
    ]

    for observation in observations:
        await task._project_live_action_observation(
            1,
            observation,
            child_execution_id="child-execution-2",
        )

    assert [item.path for item in task._stream_items] == [
        "agent_task.action.started",
        "agent_task.action.completed",
        "agent_task.action.failed",
    ]
    assert [item.value["action_id"] for item in task._stream_items] == ["search", "search", "read"]
    assert all(item.value["posthoc_projection"] is False for item in task._stream_items)
    assert task._stream_items[1].value["output_summary"] == "2 records"
    assert "SECRET_MUST_NOT_LEAK" not in str([item.value for item in task._stream_items])


@pytest.mark.asyncio
async def test_action_delta_links_only_task_workspace_readback_verified_file_refs(tmp_path: Any) -> None:
    agent = Agently.create_agent("delta-progress-file-ref").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        task_id="delta-progress-file-ref-task",
        goal="Write the report.",
        success_criteria=["The report file is readable."],
        execution="flat",
    )
    written = await task.task_workspace.write_file("reports/final.md", "# Final report\n")
    trusted_ref = written["file_refs"][0]
    outside_path = tmp_path / "outside-secret.txt"
    outside_path.write_text("secret", encoding="utf-8")

    await task._emit_normalized_action_event(
        "completed",
        {
            "id": "write_file",
            "status": "success",
            "success": True,
            "result_preview": "Report written",
            "file_refs": [trusted_ref],
            "command_index": 0,
            "round_index": 0,
        },
        execution_meta={"execution_id": "execution-4", "route": {}},
        owner_context={"iteration": 1, "strategy": "flat"},
        projection_source="action.completed",
        posthoc_projection=False,
    )
    await task._emit_normalized_action_event(
        "completed",
        {
            "id": "untrusted_model_path",
            "status": "success",
            "success": True,
            "file_refs": [
                {
                    "path": "missing.md",
                    "bytes": 10,
                    "sha256": "not-a-real-digest",
                    "available": True,
                    "open_path": str(outside_path),
                }
            ],
            "command_index": 1,
            "round_index": 0,
        },
        execution_meta={"execution_id": "execution-4", "route": {}},
        owner_context={"iteration": 1, "strategy": "flat"},
        projection_source="action.completed",
        posthoc_projection=False,
    )

    trusted_item, untrusted_item = task._stream_items[-2:]
    expected_open_path = str(task.task_workspace.resolve_file_path(trusted_ref["path"]))
    assert trusted_item.value["file_refs"][0]["open_path"] == expected_open_path
    trusted_text = AgentExecutionTextDeltaProjector().project(trusted_item)
    assert trusted_text is not None
    assert f"[final.md](<{expected_open_path}>)" in trusted_text
    untrusted_text = AgentExecutionTextDeltaProjector().project(untrusted_item)
    assert untrusted_text is not None
    assert "open_path" not in untrusted_item.value["file_refs"][0]
    assert "](" not in untrusted_text


def test_renderer_rejects_unverified_open_path_even_when_absolute() -> None:
    item = _stream_item(
        "agent_task.action.completed",
        {
            "action_id": "model_claim",
            "status": "completed",
            "success": True,
            "file_refs": [
                {
                    "path": "claimed.md",
                    "open_path": "/tmp/model-claimed-secret.md",
                    "open_path_verified": True,
                }
            ],
        },
        stream_kind="action_observation",
        phase="completed",
        strategy="flat",
    )

    text = AgentExecutionTextDeltaProjector().project(item)

    assert text is not None
    assert "claimed.md" in text
    assert "](" not in text


def test_action_delta_reports_refs_omitted_after_three_unique_labels() -> None:
    text = AgentExecutionTextDeltaProjector().project(
        _stream_item(
            "agent_task.action.completed",
            {
                "action_id": "collect",
                "status": "completed",
                "success": True,
                "file_refs": [
                    {"path": "reports/one.md"},
                    {"path": "reports/two.md"},
                    {"path": "reports/three.md"},
                    {"path": "reports/four.md"},
                    {"path": "reports/five.md"},
                ],
            },
            stream_kind="action_observation",
            phase="completed",
            strategy="flat",
        )
    )

    assert text is not None
    assert "one.md, two.md, three.md (+2 more)" in text
    assert "four.md" not in text
    assert "five.md" not in text


def test_action_delta_ref_label_omits_url_query_and_fragment() -> None:
    text = AgentExecutionTextDeltaProjector().project(
        _stream_item(
            "agent_task.action.completed",
            {
                "action_id": "download",
                "status": "completed",
                "success": True,
                "source_refs": [
                    {"value": "https://example.test/reports/final.json?api_key=SECRET#private"}
                ],
            },
            stream_kind="action_observation",
            phase="completed",
            strategy="flat",
        )
    )

    assert text is not None
    assert "final.json" in text
    assert "api_key" not in text
    assert "SECRET" not in text
    assert "private" not in text
