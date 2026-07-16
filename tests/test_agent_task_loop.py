from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import time
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.core.orchestration import (
    TaskBoard,
    build_task_board_acceptance_index,
    build_task_board_evidence_view,
    resolve_task_board_planning_policy,
    task_board_planning_output_schema,
)
from agently.core.application.AgentTask.BlockCarrier import (
    WorkUnitIntent,
    WorkUnitResult,
    scoped_retrieval_policy,
    select_carrier_output_policy,
)
from agently.core.application.AgentTask import AgentTask
from agently.core.application.AgentExecution.Stream import (
    AgentExecutionTextDeltaProjector,
    project_agent_execution_text_delta,
)
from agently.types.data import (
    AgentlyRequestData,
    AgentExecutionStreamData,
    ExecutionBlockGraph,
    TaskBoardCard,
    TaskBoardCardResult,
    TaskBoardGraph,
    TaskBoardRevision,
    WorkspaceFileRef,
)
from agently.utils import DataFormatter, Settings
from examples.agent_task.interview_question_preparation import judge_interview_semantics


def test_task_shared_star_export_includes_evidence_ledger_helpers():
    namespace: dict[str, Any] = {}
    exec("from agently.core.application.AgentTask.TaskShared import *", namespace)

    for helper_name in (
        "acceptance_locator_view_from_ledger",
        "collect_evidence_use",
        "evidence_envelope_from_value",
        "evidence_ledger_view",
        "source_refs_from_ledger",
        "validate_evidence_use",
        "workspace_artifacts_from_ledger",
    ):
        assert callable(namespace.get(helper_name))


def test_agent_task_process_summary_is_compact_and_not_evidence():
    payload = {
        "decision_basis": ["Use the available bounded evidence."],
        "short_summary": "x" * 800,
        "progress_message": "Drafted a bounded artifact section for review.",
        "criterion_checks": [
            {
                "criterion": "Report includes sources.",
                "status": "partial",
                "summary": "One source remains unverified.",
                "api_key": "secret-value",
                "content": "large body should not be carried as process summary",
            }
        ],
        "evidence_use": [{"claim": "not a process field", "evidence_ids": ["e1"]}],
    }

    summary = AgentTask._process_summary_from_value(payload, stage="execution")
    next_step = AgentTask._combined_process_summary(
        plan=payload,
        execution_result=payload,
        verification=payload,
    )

    assert summary["stage"] == "execution"
    assert summary["criterion_checks"][0]["api_key"] == "[redacted]"
    assert "content" not in summary["criterion_checks"][0]
    assert len(summary["short_summary"]) <= 380
    assert "evidence_use" not in summary
    assert "decision_basis" not in next_step.get("plan", {})
    assert "progress_message" not in next_step["execution"]
    assert next_step["execution"]["short_summary"]


def test_agent_task_prompts_do_not_expose_workspace_streaming_mechanics():
    source_files = [
        "agently/core/application/AgentTask/TaskBoardCardExecution.py",
        "agently/core/application/AgentTask/ArtifactDelivery.py",
        "agently/core/application/AgentTask/FlatStrategy.py",
        "agently/core/application/AgentTask/TaskBoardFinalization.py",
    ]
    forbidden_phrases = [
        "AgentTask will stream",
        "will stream the long body",
        "will write/read back",
        "The framework will stream",
        "produce trusted file_refs",
        "Verify every success criterion",
    ]
    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for relative_path in source_files:
        text = (repo_root / relative_path).read_text(encoding="utf-8")
        for phrase in forbidden_phrases:
            if phrase in text:
                offenders.append(f"{relative_path}: {phrase}")
    assert offenders == []


def test_taskboard_prompts_keep_model_contract_surface_simple():
    source_files = [
        "agently/core/application/AgentTask/TaskBoardCardExecution.py",
        "agently/core/application/AgentTask/TaskBoardStrategy.py",
        "agently/core/orchestration/TaskBoard/TaskBoardPlanning.py",
        "agently/core/application/AgentTask/ArtifactDelivery.py",
    ]
    forbidden_phrases = [
        "AgentExecution step",
        "bounded AgentExecution",
        "framework-prefetched",
        "TaskBoardEvidenceView",
        "TaskBoard and AgentTask",
        "framework canonicalizes",
        "framework remaps",
        "for the AgentTask",
    ]
    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for relative_path in source_files:
        text = (repo_root / relative_path).read_text(encoding="utf-8")
        for phrase in forbidden_phrases:
            if phrase in text:
                offenders.append(f"{relative_path}: {phrase}")
    assert offenders == []


@pytest.mark.asyncio
async def test_taskboard_final_prompt_omits_duplicate_revision_card_results(tmp_path, monkeypatch):
    agent = Agently.create_agent("taskboard-final-prompt-compaction").use_workspace(
        tmp_path / "taskboard-final-prompt-compaction"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-final-prompt-compaction",
        goal="Summarize the collected evidence.",
        success_criteria=["Use the collected evidence."],
        execution="taskboard",
    )
    graph = TaskBoardGraph.from_value(
        {
            "graph_id": "taskboard-final-prompt-compaction.graph",
            "cards": [
                {
                    "id": "collect",
                    "objective": "Collect evidence.",
                    "required_outputs": ["evidence"],
                    "status": "completed",
                }
            ],
        }
    )
    ledger = {
        "items": [
            {
                "id": "evidence-1",
                "kind": "note",
                "status": "ok",
                "body_state": "bounded",
                "body": "Collected evidence body.",
            }
        ]
    }
    revision = TaskBoardRevision.create(
        board_id="taskboard-final-prompt-compaction",
        graph=graph,
        revision_id="rev-0",
    ).next_revision(
        graph,
        status="completed",
        card_results={
            "collect": TaskBoardCardResult.from_value(
                {
                    "card_id": "collect",
                    "status": "completed",
                    "preview": "Collected evidence body.",
                    "metadata": {"evidence_ledger": ledger},
                }
            )
        },
    )
    evidence_view = build_task_board_evidence_view(revision).to_dict()
    captured: dict[str, Any] = {}

    class _Prompt:
        def __init__(self) -> None:
            self.values: dict[str, Any] = {}

        def get(self, key: str, default: Any = None, inherit: bool = False) -> Any:
            _ = inherit
            return self.values.get(key, default)

        def set(self, key: str, value: Any) -> None:
            self.values[key] = value

    class _Request:
        def __init__(self) -> None:
            self.prompt = _Prompt()

        def input(self, payload: Mapping[str, Any]) -> "_Request":
            captured["payload"] = dict(payload)
            return self

        def instruct(self, instruction: str) -> "_Request":
            captured["instruction"] = instruction
            return self

        def output(self, schema: Mapping[str, Any], format: str = "json") -> "_Request":
            captured["output_schema"] = dict(schema)
            captured["output_format"] = format
            return self

        async def async_get_data(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            _ = args, kwargs
            return {
                "accepted": True,
                "reason": "Accepted.",
                "final_result": "Collected evidence body.",
                "missing_criteria": [],
                "evidence_use": [{"claim": "Collected evidence", "evidence_ids": ["e1"], "support_type": "content"}],
            }

    monkeypatch.setattr(agent, "create_temp_request", lambda: _Request())

    await task._request_taskboard_final(
        revision,
        evidence_view,
        schedule=TaskBoard(revision, handler=lambda _context: None).schedule(),
    )

    payload = captured["payload"]
    prompt_revision = payload["revision"]
    assert "card_results" not in prompt_revision
    assert prompt_revision["card_result_statuses"] == {"collect": "completed"}
    assert payload["taskboard_evidence_view"]["cards"][0]["preview"]
    prompt_evidence = payload["evidence_ledger"]["items"][0]
    assert prompt_evidence["body_preview"] == "Collected evidence body."
    assert prompt_evidence["reference_id"].startswith("ref_")
    assert "id" not in prompt_evidence
    assert "evidence_id" not in prompt_evidence
    assert "cite_as" not in prompt_evidence
    assert "aliases" not in prompt_evidence


def test_taskboard_dependency_prompt_projection_omits_recursive_result_state():
    huge = "dependency-payload-" * 4000
    result = TaskBoardCardResult(
        card_id="collect",
        status="completed",
        output_digest="Collected bounded market evidence.",
        preview={
            "status": "completed",
            "answer": "Collected bounded market evidence.",
            "short_summary": "NVDA and AVGO evidence is available through stable refs.",
            "evidence_ledger": {f"item-{index}": huge for index in range(40)},
            "execution_meta": {f"record-{index}": huge for index in range(40)},
        },
        metadata={
            "evidence_ledger": {"items": [{"body": huge} for _ in range(40)]},
            "execution_id": "exec-collect",
        },
    )

    compact = AgentTask._compact_taskboard_dependency_results({"collect": result})
    compact_text = json.dumps(compact, ensure_ascii=False)

    assert len(compact_text) < 8000
    assert "Collected bounded market evidence." in compact_text
    assert "NVDA and AVGO evidence is available through stable refs." in compact_text
    assert "evidence_ledger" not in compact["collect"]["preview"]
    assert "execution_meta" not in compact["collect"]["preview"]


def test_taskboard_card_binding_ledger_prioritizes_current_execution_evidence_before_history():
    task = AgentTask(
        _create_agent("taskboard-current-binding-priority"),
        task_id="taskboard-current-binding-priority",
        goal="Bind current evidence before historical evidence.",
        success_criteria=["The current evidence remains selectable."],
        execution="taskboard",
    )
    historical_items = [
        {
            "id": f"historical:{index}",
            "kind": "taskboard_diagnostic",
            "status": "ok",
            "body_state": "bounded",
            "body": f"Historical evidence {index}",
        }
        for index in range(100)
    ]
    current_item = {
        "id": "workspace_artifact_readback:repair:final.md",
        "kind": "workspace_artifact.readback",
        "status": "ok",
        "body_state": "bounded",
        "path": "final.md",
        "body": "The corrected current artifact body.",
    }

    historical_ledger = task._stable_evidence_ledger_view(
        {"evidence_items": historical_items},
        max_items=120,
        body_chars=1800,
    )
    current_ledger = task._stable_evidence_ledger_view(
        {"evidence_items": [current_item]},
        max_items=16,
        body_chars=1800,
    )
    current_reference_id = current_ledger["items"][0]["reference_id"]
    ledger = task._taskboard_card_binding_evidence_ledger(historical_ledger, current_ledger)
    candidates = task._evidence_binding_repair_candidate_refs(ledger, max_items=80)

    assert candidates[0]["reference_id"] == current_reference_id
    assert any(item["reference_id"] == current_reference_id for item in candidates)
    assert len(candidates) == 80


def test_taskboard_revision_prompt_projection_omits_recursive_diagnostics_and_acceptance_metadata():
    huge = "revision-payload-" * 4000
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "bounded-revision",
            "revision_id": "rev-9",
            "status": "blocked",
            "graph": {
                "graph_id": "bounded-revision-graph",
                "cards": [{"id": "final", "objective": "Finalize the report."}],
            },
            "diagnostics": [
                {
                    "code": f"terminal.grounding.{index}",
                    "status": "blocked",
                    "message": "Grounding repair remains required.",
                    "recursive": {f"claim-{claim_index}": huge for claim_index in range(20)},
                }
                for index in range(12)
            ],
            "metadata": {
                "taskboard_acceptance_index": {f"criterion-{index}": huge for index in range(30)},
                "terminal_repair_count": 2,
            },
        }
    )

    compact = AgentTask._compact_taskboard_revision_for_prompt(revision, include_card_results=False)
    compact_text = json.dumps(compact, ensure_ascii=False)

    assert len(compact_text) < 10000
    assert "terminal.grounding.0" in compact_text
    assert "Grounding repair remains required." in compact_text
    assert "taskboard_acceptance_index" not in compact.get("metadata", {})
    assert huge not in compact_text


def test_verifier_prompt_keeps_optional_risk_sections_optional():
    source_path = Path(__file__).resolve().parents[1] / "agently/core/application/AgentTask/Verification.py"
    text = source_path.read_text(encoding="utf-8")

    assert "Do not require risk, uncertainty, limitation, or caveat sections" in text
    assert "unless the user task, output contract," in text
    assert "verifier-visible evidence limitations explicitly require them" in text
    assert "Output-contract section labels are content" in text
    assert "not exact heading-text mandates" in text
    assert "do not reject a " in text
    assert "long artifact solely because an exact locator label missed" in text
    assert "Precise taxonomies, module lists, item counts" in text
    assert "verification page, or title-only ref is not enough" in text


def test_agent_task_process_progress_delta_uses_only_explicit_progress_event():
    assert AgentTask._is_process_summary_stream_path("self_check")
    assert AgentTask._is_process_summary_stream_path("artifact.progress_message")

    suppressed_child_delta = AgentExecutionStreamData(
        path="agent_task.iteration.1.execution.self_check",
        value="internal self check",
        delta=None,
        event_type="delta",
        is_complete=False,
        source="agent_task",
        meta={"stream_kind": "child_execution"},
    )
    progress_item = AgentExecutionStreamData(
        path="agent_task.process.progress",
        value={"message": "Reading the bounded source evidence."},
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "progress", "progress_source": "process_summary"},
    )

    assert project_agent_execution_text_delta(suppressed_child_delta) is None
    assert project_agent_execution_text_delta(progress_item) == "Reading the bounded source evidence.\n\n"


def test_taskboard_delta_projects_structured_status_table():
    item = AgentExecutionStreamData(
        path="agent_task.taskboard.tick.2.completed",
        value={
            "revision": {
                "board_id": "demo-board",
                "revision_id": "rev-2",
                "status": "running",
                "graph": {
                    "cards": [
                        {"id": "collect", "objective": "Collect source facts.", "status": "pending"},
                        {"id": "draft", "objective": "Draft the answer.", "status": "pending"},
                        {"id": "final", "objective": "Finalize the user-facing answer.", "status": "pending"},
                        {
                            "id": "audit",
                            "objective": "Run the required audit.",
                            "failure_policy": "required",
                        },
                        {
                            "id": "optional-source",
                            "objective": "Try an optional source.",
                            "failure_policy": "optional",
                        },
                    ],
                },
            },
            "schedule": {
                "revision_id": "rev-1",
                "runnable_card_ids": ["draft"],
                "blocked_card_ids": ["final"],
                "completed_card_ids": ["collect"],
            },
            "card_results": {
                "collect": {"card_id": "collect", "status": "completed"},
                "audit": {"card_id": "audit", "status": "failed"},
                "optional-source": {"card_id": "optional-source", "status": "failed"},
            },
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
    )

    text = project_agent_execution_text_delta(item)

    assert text is not None
    assert text.startswith("**TaskBoard tick 2 updated** `demo-board` - revision `rev-2`")
    assert "Progress: 1/5 completed - 1 in progress - 1 not started - 1 failed - 1 degraded" in text
    assert "| ✅ Completed | `collect` | Collect source facts. |" in text
    assert "| 🔄 In progress | `draft` | Draft the answer. |" in text
    assert "| ⏳ Not started | `final` | Finalize the user-facing answer. |" in text
    assert "| ❌ Failed | `audit` | Run the required audit. |" in text
    assert "| ⚠️ Degraded | `optional-source` | Try an optional source. |" in text
    assert "card_results" not in text


def test_taskboard_delta_projector_compacts_repeated_tick_updates():
    revision = {
        "board_id": "demo-board",
        "revision_id": "rev-2",
        "graph": {
            "cards": [
                {"id": "collect", "objective": "Collect source facts.", "status": "pending"},
                {"id": "draft", "objective": "Draft the answer.", "status": "pending"},
            ]
        },
    }
    planned = AgentExecutionStreamData(
        path="agent_task.taskboard.plan",
        value={"revision": revision},
        event_type="done",
        is_complete=True,
        source="agent_task",
    )
    tick = AgentExecutionStreamData(
        path="agent_task.taskboard.tick.1.completed",
        value={
            "revision": revision,
            "schedule": {
                "revision_id": "rev-2",
                "completed_card_ids": ["collect"],
                "runnable_card_ids": ["draft"],
            },
            "card_results": {"collect": {"status": "completed"}},
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
    )
    projector = AgentExecutionTextDeltaProjector()

    planned_text = projector.project(planned)
    tick_text = projector.project(tick)

    assert planned_text is not None
    assert "| State | Card | Task |" in planned_text
    assert tick_text is not None
    assert "Changes:" in tick_text
    assert "| State | Card | Task |" not in tick_text
    assert "✅ Completed `collect` Collect source facts. (was ⏳ Not started)" in tick_text
    assert "🔄 In progress `draft` Draft the answer. (was ⏳ Not started)" in tick_text


def test_flat_delta_projector_describes_plan_with_previous_completed_action():
    context = AgentExecutionStreamData(
        path="agent_task.iteration.1.snapshot.context",
        value={
            "message": "Iteration 1: context pack ready with 2 item(s).",
            "iteration": 1,
            "stage": "context",
            "snapshot": {"context_item_count": 2},
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        task_id="flat-task",
        meta={"stream_kind": "snapshot", "stage": "context", "iteration": 1, "task_id": "flat-task"},
    )
    item = AgentExecutionStreamData(
        path="agent_task.iteration.1.snapshot.plan",
        value={
            "message": "Iteration 1: plan ready; next bounded step is selected.",
            "iteration": 1,
            "stage": "plan",
            "snapshot": {
                "execution_shape": "direct",
                "step_instruction": "Read the source file and draft the final answer.",
                "expected_evidence": "Source facts are bounded and cited.",
                "rationale": "The task needs one focused pass over the source.",
            },
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        task_id="flat-task",
        meta={"stream_kind": "snapshot", "stage": "plan", "iteration": 1, "task_id": "flat-task"},
    )
    projector = AgentExecutionTextDeltaProjector()

    context_text = projector.project(context)
    text = projector.project(item)

    assert context_text is not None
    assert context_text == "Iteration 1: context is ready with 2 item(s).\n\n"
    assert text is not None
    assert text.startswith("Iteration 1: plan ready.")
    assert text.endswith("\n\n")
    assert "Previous completed action: prepared the working context with 2 item(s)." in text
    assert "Current action plan: Read the source file and draft the final answer." in text
    assert "Expected evidence: Source facts are bounded and cited." in text
    assert "| State | Step | Detail |" not in text
    assert "execution_result" not in text


def test_flat_delta_projector_summarizes_completed_actions_and_terminal_result():
    projector = AgentExecutionTextDeltaProjector()
    context = AgentExecutionStreamData(
        path="agent_task.iteration.1.snapshot.context",
        value={
            "message": "Iteration 1: context pack ready with 2 item(s).",
            "iteration": 1,
            "stage": "context",
            "snapshot": {"context_item_count": 2},
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        task_id="flat-task",
        meta={"stream_kind": "snapshot", "stage": "context", "iteration": 1, "task_id": "flat-task"},
    )
    execution = AgentExecutionStreamData(
        path="agent_task.iteration.1.snapshot.execution",
        value={
            "message": "Iteration 1: bounded step finished; execution evidence was captured.",
            "iteration": 1,
            "stage": "execution",
            "snapshot": {
                "execution_result": {
                    "short_summary": "Drafted the answer from bounded source evidence.",
                    "remaining_work": ["Verify final acceptance."],
                }
            },
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        task_id="flat-task",
        meta={"stream_kind": "snapshot", "stage": "execution", "iteration": 1, "task_id": "flat-task"},
    )
    verification = AgentExecutionStreamData(
        path="agent_task.iteration.1.snapshot.verification",
        value={
            "message": "Iteration 1: verification passed.",
            "iteration": 1,
            "stage": "verification",
            "snapshot": {
                "is_complete": True,
                "reason": "All requested facts are covered.",
            },
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        task_id="flat-task",
        meta={"stream_kind": "snapshot", "stage": "verification", "iteration": 1, "task_id": "flat-task"},
    )
    result_item = AgentExecutionStreamData(
        path="result",
        value={
            "status": "completed",
            "accepted": True,
            "artifact_status": "accepted",
            "final_result": "Final answer prepared from bounded source evidence.",
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        task_id="flat-task",
    )

    context_text = projector.project(context)
    execution_text = projector.project(execution)
    verification_text = projector.project(verification)
    result_text = projector.project(result_item)

    assert context_text is not None
    assert context_text == "Iteration 1: context is ready with 2 item(s).\n\n"
    assert execution_text is not None
    assert execution_text.endswith("\n\n")
    assert "Iteration 1: completed action: Drafted the answer from bounded source evidence." in execution_text
    assert "| State | Step | Detail |" not in execution_text
    assert verification_text is not None
    assert verification_text.endswith("\n\n")
    assert "Iteration 1: verification passed: All requested facts are covered." in verification_text
    assert result_text is not None
    assert result_text.endswith("\n\n")
    assert result_text.startswith("Task summary:")
    assert "What was done:" in result_text
    assert "- Drafted the answer from bounded source evidence." in result_text
    assert "- verified the final result." in result_text
    assert "Result:" in result_text
    assert "Final answer prepared from bounded source evidence." in result_text


def test_text_delta_projector_separates_model_delta_from_process_projection_and_retry_marker():
    projector = AgentExecutionTextDeltaProjector()
    body = AgentExecutionStreamData(
        path="model.delta",
        value=None,
        delta="Drafting first attempt...",
        event_type="delta",
        is_complete=False,
        source="model_request",
    )
    retry = AgentExecutionStreamData(
        path="$status",
        value={"status": "failed", "retry": True, "reason": "provider stream reset"},
        event_type="done",
        is_complete=True,
        source="model_request",
    )
    replacement = AgentExecutionStreamData(
        path="model.delta",
        value=None,
        delta="Drafting replacement...",
        event_type="delta",
        is_complete=False,
        source="model_request",
    )
    verification = AgentExecutionStreamData(
        path="agent_task.iteration.2.snapshot",
        value={"stage": "verification", "snapshot": {"is_complete": True}},
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "snapshot", "stage": "verification", "iteration": 2},
    )

    rendered = "".join(
        text
        for text in (
            projector.project(body),
            projector.project(retry),
            projector.project(replacement),
            projector.project(verification),
        )
        if text is not None
    )

    assert "Drafting first attempt...\n\n<$retry>provider stream reset</$retry>\n\nDrafting replacement..." in rendered
    assert "Drafting replacement...\n\nIteration 2: verification passed." in rendered


def test_taskboard_stream_revision_keeps_objective_for_delta_status_table():
    revision = TaskBoardRevision.create(
        board_id="stream-objective",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "stream-objective-graph",
                "cards": [{"id": "collect", "objective": "Collect source facts for the status table."}],
            }
        ),
    )

    compact = AgentTask._compact_taskboard_revision_for_stream(revision)

    assert compact["cards"][0]["objective"] == "Collect source facts for the status table."


class _FakeChildExecutionStream:
    id = "child-execution-1"

    def __init__(self, source_item: AgentExecutionStreamData):
        self._source_item = source_item
        self.requested_types: list[str] = []

    async def get_async_generator(self, type: str = "instant"):
        self.requested_types.append(type)
        if type == "instant":
            yield self._source_item
            yield AgentExecutionStreamData(
                path="$delta",
                value=self._source_item.delta,
                delta=self._source_item.delta,
                event_type="delta",
                is_complete=False,
                source="agent_execution",
                route=self._source_item.route,
                meta={
                    "stream_kind": "text_projection",
                    "projection_source_path": self._source_item.path,
                    "projection_source_event_type": self._source_item.event_type,
                },
            )
            return
        assert type == "all"
        yield ("agent_execution", self._source_item)


def _minimal_agent_task_stream_owner() -> Any:
    task = object.__new__(AgentTask)
    task.id = "agent-task-stream-test"
    task.status = "running"
    task._stream_items = []
    task._stream_queues = []
    task._last_stream_emit_monotonic = 0.0
    return task


def _child_delta_item() -> AgentExecutionStreamData:
    return AgentExecutionStreamData(
        path="answer",
        value="A",
        delta="A",
        event_type="delta",
        is_complete=False,
        source="model_request",
        route="model_request",
        meta={"field_path": "answer", "response_id": "response-1"},
    )


@pytest.mark.asyncio
async def test_agent_task_flat_child_stream_uses_raw_events_without_synthetic_delta_projection():
    task = _minimal_agent_task_stream_owner()
    child_execution = _FakeChildExecutionStream(_child_delta_item())

    await task._bridge_step_execution_stream(1, child_execution)

    delta_chunks = [
        chunk
        for chunk in (AgentTask._project_stream_item(item, "delta") for item in task._stream_items)
        if chunk is not None
    ]
    child_paths = [(item.meta or {}).get("child_path") for item in task._stream_items]

    assert delta_chunks == ["A"]
    assert child_paths == ["answer"]
    assert child_execution.requested_types == ["all"]


@pytest.mark.asyncio
async def test_agent_task_taskboard_child_stream_uses_raw_events_without_synthetic_delta_projection():
    task = _minimal_agent_task_stream_owner()
    child_execution = _FakeChildExecutionStream(_child_delta_item())

    await task._bridge_taskboard_card_execution_stream("collect", child_execution)

    delta_chunks = [
        chunk
        for chunk in (AgentTask._project_stream_item(item, "delta") for item in task._stream_items)
        if chunk is not None
    ]
    child_paths = [(item.meta or {}).get("child_path") for item in task._stream_items]

    assert delta_chunks == ["A"]
    assert child_paths == ["answer"]
    assert child_execution.requested_types == ["all"]


@pytest.mark.asyncio
async def test_agent_task_taskboard_control_stream_preserves_done_event_type():
    task = _minimal_agent_task_stream_owner()
    done_item = AgentExecutionStreamData(
        path="final_result",
        value="complete",
        event_type="done",
        is_complete=True,
        source="model_request",
    )

    emitted = await task._emit_taskboard_control_stream_item("collect", done_item)

    assert emitted.event_type == "done"
    assert emitted.is_complete is True
    assert emitted.delta is None


def test_agent_task_action_observation_delta_projects_safe_progress_text():
    started = AgentExecutionStreamData(
        path="agent_task.action.started",
        value={
            "action_id": "grep_workspace",
            "status": "started",
            "kind": "shell_search",
            "input_summary": {"query": "deadline", "scope": "workspace"},
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "action_observation", "phase": "started"},
    )
    completed = AgentExecutionStreamData(
        path="agent_task.action.completed",
        value={
            "action_id": "grep_workspace",
            "status": "success",
            "kind": "shell_search",
            "success": True,
            "output_summary": {"path": "notes.md", "content": "deadline is 2026-07-01"},
            "source_refs": [{"value": "notes.md"}],
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "action_observation", "phase": "completed"},
    )
    failed = AgentExecutionStreamData(
        path="agent_task.action.failed",
        value={"action_id": "read_file", "status": "failed", "error": "file not found"},
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "action_observation", "phase": "failed"},
    )

    assert project_agent_execution_text_delta(started) == (
        "Action started: grep_workspace (shell_search). Input: query=deadline\n\n"
    )
    completed_text = project_agent_execution_text_delta(completed)
    assert completed_text is not None
    assert completed_text.endswith("\n\n")
    assert "Action completed: grep_workspace (shell_search)." in completed_text
    assert "Result: path notes.md" in completed_text
    assert "Refs: notes.md" in completed_text
    assert project_agent_execution_text_delta(failed) == "Action setback: read_file failed. Error: file not found\n\n"


def test_evidence_ledger_guard_rejects_structurally_invalid_support():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {"id": "quote.failed", "kind": "action_evidence", "status": "failed", "body_state": "bounded"},
            {"id": "repo.path", "kind": "locator_ref", "status": "ok", "body_state": "ref_only", "path": "src/app.py"},
        ]
    }

    guard = validate_evidence_use(
        [
            {"claim": "The quote was 123.45.", "evidence_ids": ["quote.failed"], "support_type": "content"},
            {"claim": "src/app.py defines Foo.", "evidence_ids": ["repo.path"], "support_type": "content"},
            {"claim": "Unknown.", "evidence_ids": ["missing"], "support_type": "content"},
        ],
        ledger,
    )

    assert guard["valid"] is False
    assert guard["blocking_count"] == 3
    assert {diagnostic["code"] for diagnostic in guard["diagnostics"] if diagnostic.get("blocking") is True} == {
        "evidence_ledger.unavailable_item_used_as_positive_support",
        "evidence_ledger.ref_only_item_used_as_content_support",
        "evidence_ledger.invalid_evidence_id",
    }


def test_evidence_ledger_acceptance_locator_is_ref_pointer_only():
    from agently.core.application.AgentTask.EvidenceLedger import (
        acceptance_locator_view_from_ledger,
        evidence_ledger_view,
        validate_evidence_use,
    )

    ledger = {
        "evidence_items": [
            {
                "id": "locator.final.middle",
                "kind": "workspace_artifact.acceptance_locator",
                "status": "ok",
                "body_state": "ref_only",
                "path": "reports/final.md",
                "criterion_id": "middle",
                "claim": "The middle section is present.",
                "heading": "Middle Section",
                "line_start": 42,
                "line_end": 48,
                "byte_offset": 16000,
                "byte_end": 17500,
                "source_evidence_ids": ["workspace_artifact_readback:test:reports/final.md"],
            }
        ]
    }

    view = evidence_ledger_view(ledger)
    locator_view = acceptance_locator_view_from_ledger(view)
    assert locator_view["items"][0]["line_start"] == 42
    assert locator_view["items"][0]["heading"] == "Middle Section"

    invalid_guard = validate_evidence_use(
        [
            {
                "claim": "The middle section is present.",
                "evidence_ids": ["locator.final.middle"],
                "support_type": "content",
            }
        ],
        ledger,
    )
    assert invalid_guard["valid"] is False
    assert any(
        item["code"] == "evidence_ledger.ref_only_item_used_as_content_support" for item in invalid_guard["diagnostics"]
    )

    pointer_guard = validate_evidence_use(
        [
            {
                "claim": "reports/final.md has a middle-section locator.",
                "evidence_ids": ["locator.final.middle"],
                "support_type": "ref_pointer",
            }
        ],
        ledger,
    )
    assert pointer_guard["valid"] is True


def test_acceptance_locator_matches_unicode_dash_heading_variants():
    from agently.core.application.AgentTask.AcceptanceLocator import (
        build_workspace_artifact_acceptance_locator_items,
    )

    items = build_workspace_artifact_acceptance_locator_items(
        path="final.md",
        source="test",
        text=(
            "# Final\n\n"
            "## Source\u2011backed Evidence Table\n\n"
            "Evidence rows.\n\n"
            "## Implementation & Product Highlights\n\n"
            "Highlights.\n"
        ),
        manifest={
            "sections": [
                {
                    "id": "implementation_highlights",
                    "title": "Implementation / Product Highlights",
                }
            ]
        },
        acceptance_points=[
            {
                "criterion": "The final artifact includes the source-backed evidence table.",
                "expected_anchor": "Source-backed Evidence Table",
            }
        ],
    )

    locator = next(item for item in items if item.get("criterion_id") == "acceptance_point:0")
    assert locator["status"] == "ok"
    assert locator["heading"] == "Source\u2011backed Evidence Table"
    assert locator["line_start"] == 3
    assert locator["requirement_level"] == "advisory"

    required_locator = next(item for item in items if item.get("criterion_id") == "implementation_highlights")
    assert required_locator["status"] == "ok"
    assert required_locator["requirement_level"] == "required"
    assert required_locator["heading"] == "Implementation & Product Highlights"


def test_acceptance_locator_projects_every_actual_markdown_heading_for_progressive_readback():
    from agently.core.application.AgentTask.AcceptanceLocator import (
        build_workspace_artifact_acceptance_locator_items,
    )

    items = build_workspace_artifact_acceptance_locator_items(
        path="final.md",
        source="test",
        text=(
            "# Semiconductor Portfolio Risk Brief\n\n"
            "### NVDA\n\nNVDA facts.\n\n"
            "### AMD\n\nAMD facts.\n\n"
            "### AVGO\n\nAVGO facts.\n\n"
            "### Data Boundary and Non-Investment-Advice\n\nBoundary.\n"
        ),
        acceptance_points=[
            {
                "criterion": "Each ticker section contains the required evidence.",
                "expected_anchor": "all ticker evidence sections",
            }
        ],
    )

    structure_headings = {
        item.get("heading")
        for item in items
        if item.get("point_source") == "artifact_structure" and item.get("status") == "ok"
    }
    assert {"NVDA", "AMD", "AVGO", "Data Boundary and Non-Investment-Advice"}.issubset(structure_headings)


def test_acceptance_locator_matches_cjk_numeric_spacing_variants():
    from agently.core.application.AgentTask.AcceptanceLocator import (
        build_workspace_artifact_acceptance_locator_items,
    )

    items = build_workspace_artifact_acceptance_locator_items(
        path="final.md",
        source="test",
        text=(
            "# LMCC Mock Exam\n\n"
            "### 第一大题：单选题（每题 3 分，共 60 分）\n\n"
            "**第 1-10 题：基础概念**\n\n"
            "题目内容。\n"
        ),
        acceptance_points=[
            {
                "criterion_id": "single_choice_heading",
                "criterion": "The single-choice section heading is present.",
                "expected_anchor": "### 第一大题：单选题（每题3分，共60分）",
            },
            {
                "criterion_id": "question_range",
                "criterion": "The first question range anchor is present.",
                "expected_anchor": "第1-10题：基础概念",
            },
        ],
    )

    statuses = {item.get("criterion_id"): item.get("status") for item in items}
    assert statuses == {
        "single_choice_heading": "ok",
        "question_range": "ok",
    }


def test_acceptance_locator_uses_manifest_outline_ordinal_for_heading_label_variants():
    from agently.core.application.AgentTask.AcceptanceLocator import (
        build_workspace_artifact_acceptance_locator_items,
    )

    items = build_workspace_artifact_acceptance_locator_items(
        path="final.md",
        source="test",
        text=(
            "# Final Report\n\n"
            "## 1. Scope\n\n"
            "Scope content.\n\n"
            "## 2. Details\n\n"
            "### Nested Detail A\n\n"
            "Nested content.\n\n"
            "### Nested Detail B\n\n"
            "Details content.\n\n"
            "## 3. Closing Boundary\n\n"
            "Closing content.\n"
        ),
        manifest={
            "section_outline": [
                "scope",
                "details",
                "closing statement",
            ]
        },
    )

    locator = next(item for item in items if item.get("criterion_id") == "section_outline:2")
    assert locator["status"] == "ok"
    assert locator["requirement_level"] == "required"
    assert locator["heading"] == "3. Closing Boundary"
    assert locator["line_start"] == 17
    assert locator["byte_offset"] < locator["byte_end"]


def test_evidence_ledger_guard_reconciles_visible_aliases_to_canonical_ids():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "ledger.workspace.readme",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "full",
                "path": "README.md",
                "record_id": "record-readme",
                "source_url": "https://example.test/readme",
                "body": "README content",
                "provenance": {"action_id": "repo_read", "action_call_id": "call-1"},
            },
            {
                "id": "ledger.workspace.init",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "skillopt/__init__.py",
                "body": "__version__ = '1.0'",
            },
        ]
    }

    guard = validate_evidence_use(
        [
            {"claim": "The README content was read.", "evidence_ids": ["README.md"], "support_type": "content"},
            {
                "claim": "The readme record is the selected source.",
                "evidence_ids": ["record-readme"],
                "support_type": "content",
            },
            {
                "claim": "The readme URL is available.",
                "evidence_ids": ["https://example.test/readme"],
                "support_type": "content",
            },
            {
                "claim": "The repository read action produced this result.",
                "evidence_ids": ["action_result_repo_read"],
                "support_type": "content",
            },
            {
                "claim": "The package initializer was read.",
                "evidence_ids": ["skillopt/__init__.py"],
                "support_type": "content",
            },
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["blocking_count"] == 0
    assert [entry["evidence_ids"] for entry in guard["normalized_evidence_use"]] == [
        ["ledger.workspace.readme"],
        ["ledger.workspace.readme"],
        ["ledger.workspace.readme"],
        ["ledger.workspace.readme"],
        ["ledger.workspace.init"],
    ]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])
    assert "available_evidence_refs" in guard


def test_evidence_ledger_guard_uses_item_declared_aliases():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "ledger.source.guide",
                "kind": "action_evidence",
                "status": "ok",
                "body_state": "bounded",
                "aliases": ["load_source:docs/guide.md"],
                "body": "Guide content from the action result.",
            }
        ]
    }

    guard = validate_evidence_use(
        [
            {
                "claim": "The guide content was read.",
                "evidence_ids": ["load_source:docs/guide.md"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["ledger.source.guide"]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])


def test_blocks_action_evidence_declares_generic_action_ref_aliases():
    from agently.builtins.plugins.Blocks.AgentlyBlocks import EvidenceMapperRegistry
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    graph = ExecutionBlockGraph.from_value({"graph_id": "graph-generic-alias", "source_plan_id": "plan-generic-alias"})
    envelope = EvidenceMapperRegistry().map_evidence(
        graph,
        {
            "blocks": {
                "execution_block_results": [
                    {
                        "kind": "agent_step",
                        "execution_block_id": "exec-load-source",
                        "status": "completed",
                        "output": {
                            "execution_meta": {
                                "execution_id": "run-generic-alias",
                                "logs": {
                                    "action_logs": [
                                        {
                                            "id": "load_source",
                                            "status": "success",
                                            "action_call_id": "call-load-source",
                                            "input_preview": {"path": "docs/guide.md"},
                                            "result_preview": {
                                                "path": "docs/guide.md",
                                                "content": "Guide content from the action result.",
                                            },
                                        }
                                    ]
                                },
                            }
                        },
                    }
                ]
            }
        },
    )

    action_items = [item for item in envelope.evidence_items if item.get("kind") == "action_evidence"]
    assert len(action_items) == 1
    action_item = action_items[0]
    assert "load_source:docs/guide.md" in action_item.get("aliases", [])

    guard = validate_evidence_use(
        [
            {
                "claim": "The guide content was read.",
                "evidence_ids": ["load_source:docs/guide.md"],
                "support_type": "content",
            }
        ],
        envelope,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == [action_item["id"]]


def test_evidence_ledger_guard_blocks_ambiguous_basename_aliases():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "docs.readme",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "full",
                "path": "docs/README.md",
            },
            {
                "id": "pkg.readme",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "full",
                "path": "packages/README.md",
            },
        ]
    }

    guard = validate_evidence_use(
        [{"claim": "The README explains the package.", "evidence_ids": ["README.md"], "support_type": "content"}],
        ledger,
    )

    assert guard["valid"] is False
    assert guard["blocking_count"] == 1
    diagnostic = next(item for item in guard["diagnostics"] if item.get("blocking") is True)
    assert diagnostic["code"] == "evidence_ledger.ambiguous_evidence_alias"
    assert set(diagnostic["candidates"]) == {"docs.readme", "pkg.readme"}


def test_evidence_ledger_alias_reconciliation_preserves_status_guards():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "quote.failed",
                "kind": "action_evidence",
                "status": "failed",
                "body_state": "ref_only",
                "action_id": "quote_lookup",
            }
        ]
    }

    guard = validate_evidence_use(
        [{"claim": "The quote was 123.45.", "evidence_ids": ["action_result_quote_lookup"], "support_type": "content"}],
        ledger,
    )

    assert guard["valid"] is False
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["quote.failed"]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])
    assert any(
        item["code"] == "evidence_ledger.unavailable_item_used_as_positive_support" and item.get("blocking") is True
        for item in guard["diagnostics"]
    )


def test_evidence_binding_repair_resolves_missing_id_from_unique_claim_body():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "search.amd",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "action_id": "web_search",
                "body": "AMD shares up 261% over past year and 132% YTD; hit 52-week high $564.76.",
            },
            {
                "id": "search.avgo",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "action_id": "web_search",
                "body": "AVGO approved a quarterly dividend.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "AMD shares up 261% over past year and 132% YTD; hit 52-week high $564.76",
                "evidence_ids": [],
                "support_type": "content",
            }
        ],
        ledger,
    )

    repaired = AgentTask._deterministic_evidence_binding_repair(guard, ledger)

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "AMD shares up 261% over past year and 132% YTD; hit 52-week high $564.76",
            "evidence_ids": ["search.amd"],
            "support_type": "content",
        }
    ]
    merged = AgentTask._merge_repaired_evidence_use(guard["normalized_evidence_use"], repaired)
    assert validate_evidence_use(merged, ledger)["valid"] is True


def test_evidence_binding_repair_resolves_missing_unavailability_id_from_failed_action_body():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "browse.reuters.failed",
                "kind": "agent_task.action.result",
                "status": "failed",
                "body_state": "bounded",
                "action_id": "browse",
                "body": (
                    "Can not browse 'https://www.reuters.com/business/broadcom-tumbles-revenue-miss/'. "
                    "Fallback failed: Page.goto net::ERR_CONNECTION_CLOSED. Error: curl exited 35."
                ),
            }
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "Reuters article browse returned error",
                "evidence_ids": [],
                "support_type": "unavailability",
            }
        ],
        ledger,
    )

    repaired = AgentTask._deterministic_evidence_binding_repair(guard, ledger)

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "Reuters article browse returned error",
            "evidence_ids": ["browse.reuters.failed"],
            "support_type": "unavailability",
        }
    ]
    merged = AgentTask._merge_repaired_evidence_use(guard["normalized_evidence_use"], repaired)
    assert validate_evidence_use(merged, ledger)["valid"] is True


def test_evidence_binding_repair_leaves_missing_id_unresolved_when_claim_body_is_ambiguous():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "search.one",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "body": "The source says the project risk is material.",
            },
            {
                "id": "search.two",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "body": "Another source says the project risk is material.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "The project risk is material.",
                "evidence_ids": [],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == []


def test_evidence_binding_repair_prunes_incompatible_auxiliary_ids_when_valid_support_remains():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "readback.final",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "final.md",
                "body": "Canonical bounded readback content.",
            },
            {
                "id": "locator.final",
                "kind": "workspace_artifact.acceptance_locator",
                "status": "ok",
                "body_state": "ref_only",
                "path": "final.md",
            },
            {
                "id": "taskboard.failed",
                "kind": "agent_task.taskboard.diagnostic",
                "status": "failed",
                "body_state": "bounded",
                "body": "An earlier repair attempt failed.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "The completed report is available for final verification.",
                "evidence_ids": [
                    "readback.final",
                    "locator.final",
                    "taskboard.failed",
                ],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is False
    repaired = AgentTask._deterministic_evidence_binding_repair(guard, ledger)

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "The completed report is available for final verification.",
            "evidence_ids": ["readback.final"],
            "support_type": "content",
        }
    ]
    merged = AgentTask._merge_repaired_evidence_use(
        guard["normalized_evidence_use"],
        repaired,
    )
    assert validate_evidence_use(merged, ledger)["valid"] is True


def test_evidence_binding_repair_keeps_fail_closed_when_no_compatible_support_remains():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "locator.final",
                "kind": "workspace_artifact.acceptance_locator",
                "status": "ok",
                "body_state": "ref_only",
                "path": "final.md",
            },
            {
                "id": "taskboard.failed",
                "kind": "agent_task.taskboard.diagnostic",
                "status": "failed",
                "body_state": "bounded",
                "body": "An earlier repair attempt failed.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "The completed report is available for final verification.",
                "evidence_ids": ["locator.final", "taskboard.failed"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is False
    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == []


def test_evidence_binding_repair_does_not_hide_unknown_id_while_pruning_incompatible_support():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "readback.final",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "final.md",
                "body": "Canonical bounded readback content.",
            },
            {
                "id": "taskboard.failed",
                "kind": "agent_task.taskboard.diagnostic",
                "status": "failed",
                "body_state": "bounded",
                "body": "An earlier repair attempt failed.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "The completed report is available for final verification.",
                "evidence_ids": [
                    "readback.final",
                    "missing.evidence",
                    "taskboard.failed",
                ],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is False
    assert {item["code"] for item in guard["diagnostics"] if item.get("blocking") is True} == {
        "evidence_ledger.invalid_evidence_id",
        "evidence_ledger.unavailable_item_used_as_positive_support",
    }
    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == []


def test_evidence_binding_repair_legacy_candidate_path_does_not_hide_mixed_blocker():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "action.good",
                "kind": "agent_task.action.result",
                "action_id": "required_probe_action",
                "status": "ok",
                "body_state": "bounded",
                "body": "Canonical Action result content.",
            },
            {
                "id": "action.failed",
                "kind": "agent_task.action.result",
                "action_id": "other_action",
                "status": "failed",
                "body_state": "bounded",
                "body": "A different Action failed.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "The required Action produced usable evidence.",
                "evidence_ids": [
                    "action.good",
                    "action_result_unknown",
                    "action.failed",
                ],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is False
    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == []


def test_evidence_ledger_view_reassigns_unique_cite_as_on_remerge():
    # The cumulative ledger re-renders sub-ledger items that each carried their own
    # e1..eN handle. The view must own cite_as and reassign unique handles so a single
    # view never exposes duplicate cite_as (which would read as ambiguous aliases).
    from agently.core.application.AgentTask.EvidenceLedger import evidence_ledger_view

    merged = {
        "evidence_items": [
            {
                "id": "iter1.read",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "cite_as": "e1",
                "body": "report body",
            },
            {
                "id": "iter2.read",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "data.csv",
                "cite_as": "e1",
                "body": "data body",
            },
            {
                "id": "iter3.read",
                "kind": "action_evidence",
                "status": "ok",
                "body_state": "bounded",
                "cite_as": "e1",
                "body": "action body",
            },
        ]
    }

    view = evidence_ledger_view(merged)
    cite_as_values = [item["cite_as"] for item in view["items"]]
    assert cite_as_values == ["e1", "e2", "e3"]
    assert len(set(cite_as_values)) == 3


def test_cite_as_handle_resolves_unambiguously_after_remerge():
    from agently.core.application.AgentTask.EvidenceLedger import evidence_ledger_view, validate_evidence_use

    merged = {
        "evidence_items": [
            {
                "id": "iter1.read",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "cite_as": "e1",
                "body": "a",
            },
            {
                "id": "iter2.read",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "data.csv",
                "cite_as": "e1",
                "body": "b",
            },
            {
                "id": "iter3.read",
                "kind": "action_evidence",
                "status": "ok",
                "body_state": "bounded",
                "cite_as": "e1",
                "body": "c",
            },
        ]
    }
    view = evidence_ledger_view(merged)

    guard = validate_evidence_use(
        [{"claim": "The data file was read.", "evidence_ids": ["e2"], "support_type": "content"}],
        view,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["iter2.read"]


def test_task_reference_identity_survives_ledger_reordering_and_normalizes_live_alias():
    from agently.core.application.AgentTask.EvidenceLedger import evidence_ledger_view, validate_evidence_use
    from agently.core.application.AgentTask.TaskReferences import TaskReferenceCatalog

    catalog = TaskReferenceCatalog("agent_task_stable_refs")
    report = {
        "id": "iter1.read",
        "kind": "workspace_artifact.readback",
        "status": "ok",
        "body_state": "bounded",
        "path": "report.md",
        "body": "report body",
    }
    data = {
        "id": "iter2.read",
        "kind": "workspace_artifact.readback",
        "status": "ok",
        "body_state": "bounded",
        "path": "data.csv",
        "body": "data body",
    }

    first = evidence_ledger_view({"evidence_items": [report, data]}, task_references=catalog)
    reordered = evidence_ledger_view({"evidence_items": [data, report]}, task_references=catalog)
    first_by_original = {item["id"]: item for item in first["items"]}
    reordered_by_original = {item["id"]: item for item in reordered["items"]}

    assert first_by_original["iter1.read"]["reference_id"] == reordered_by_original["iter1.read"]["reference_id"]
    assert first_by_original["iter1.read"]["evidence_id"] == reordered_by_original["iter1.read"]["evidence_id"]
    assert first_by_original["iter1.read"]["cite_as"] == "e1"
    assert reordered_by_original["iter1.read"]["cite_as"] == "e2"

    guard = validate_evidence_use(
        [{"claim": "The report was read.", "evidence_ids": ["e1"], "support_type": "content"}],
        first,
    )
    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == [first_by_original["iter1.read"]["reference_id"]]


def test_task_reference_identity_rejoins_compact_projection_but_changes_for_new_snapshot():
    from agently.core.application.AgentTask.TaskReferences import TaskReferenceCatalog

    catalog = TaskReferenceCatalog("agent_task_projection_rejoin")
    full = catalog.add_evidence(
        {
            "id": "workspace_artifact_readback:repair-2:final.md",
            "kind": "workspace_artifact.readback",
            "status": "ok",
            "body_state": "full",
            "path": "final.md",
            "sha256": "a" * 64,
            "body": "complete artifact body",
            "provenance": {"source": "repair-2", "sha256": "a" * 64},
        }
    )
    compact_projection = catalog.add_evidence(
        {
            "id": "workspace_artifact_readback:repair-2:final.md",
            "kind": "workspace_artifact.readback",
            "status": "ok",
            "body_state": "full",
            "path": "final.md",
            "cite_as": "e8",
            "body": "complete artifact body\n[truncated for verifier prompt]",
        }
    )
    changed_snapshot = catalog.add_evidence(
        {
            "id": "workspace_artifact_readback:repair-2:final.md",
            "kind": "workspace_artifact.readback",
            "status": "ok",
            "body_state": "full",
            "path": "final.md",
            "sha256": "b" * 64,
            "body": "changed artifact body",
            "provenance": {"source": "repair-2", "sha256": "b" * 64},
        }
    )

    assert compact_projection["evidence_id"] == full["evidence_id"]
    assert compact_projection["reference_id"] == full["reference_id"]
    assert changed_snapshot["evidence_id"] != full["evidence_id"]
    assert changed_snapshot["reference_id"] != full["reference_id"]


def test_request_local_cite_as_is_not_guessed_from_a_new_ledger_render():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    guard = validate_evidence_use(
        [{"claim": "The report was read.", "evidence_ids": ["e1"], "support_type": "content"}],
        {
            "evidence_items": [
                {
                    "id": "report.read",
                    "kind": "workspace_artifact.readback",
                    "status": "ok",
                    "body_state": "bounded",
                    "path": "report.md",
                    "body": "report body",
                }
            ]
        },
    )

    assert guard["valid"] is False
    assert "evidence_ledger.expired_cite_as" in {
        item["code"] for item in guard["diagnostics"] if item.get("blocking") is True
    }


def test_task_reference_tokens_and_host_joins_fail_closed():
    from agently.core.application.AgentTask.TaskReferences import (
        TaskReferenceCatalog,
        validate_reference_tokens,
    )

    catalog = TaskReferenceCatalog("agent_task_host_join")
    action = catalog.add_evidence(
        {
            "id": "action.result",
            "kind": "agent_task.action.result",
            "action_call_id": "call_opaque_1",
            "action_id": "research.fetch",
            "status": "ok",
            "body_state": "bounded",
            "body": "action result",
        }
    )
    readback = catalog.add_evidence(
        {
            "id": "workspace.readback",
            "kind": "workspace_artifact.readback",
            "path": "report.md",
            "status": "ok",
            "body_state": "bounded",
            "body": "report body",
        }
    )
    action_offer = catalog.offer_reference(str(action["evidence_id"]), required_role="action")

    assert set(action_offer) == {"reference_id", "kind", "status", "body_state", "source_role"}
    assert catalog.resolve(str(action["reference_id"]))["target"]["action_call_id"] == "call_opaque_1"
    assert catalog.bind("criterion:action_succeeded", [str(action["reference_id"])], required_role="action")[
        "binding_id"
    ].startswith("bnd_")
    with pytest.raises(ValueError, match="role"):
        catalog.bind("criterion:action_succeeded", [str(readback["reference_id"])], required_role="action")

    offered = {str(action_offer["reference_id"]): action_offer}
    token = f"[[ref:{action_offer['reference_id']}]]"
    assert validate_reference_tokens(token, offered)["reference_ids"] == [action_offer["reference_id"]]
    with pytest.raises(ValueError, match="duplicate"):
        validate_reference_tokens(f"{token} {token}", offered)
    with pytest.raises(ValueError, match="offered"):
        validate_reference_tokens("[[ref:ref_unknown]]", offered)
    with pytest.raises(ValueError, match="malformed"):
        validate_reference_tokens("[[ref:ref_!]]", offered)

    other = TaskReferenceCatalog("agent_task_other")
    other_action = other.add_evidence(
        {
            "id": "other.action",
            "kind": "agent_task.action.result",
            "action_call_id": "call_other",
            "status": "ok",
            "body_state": "bounded",
            "body": "other task",
        }
    )
    with pytest.raises(ValueError, match="task"):
        catalog.resolve(str(other_action["reference_id"]), task_id="agent_task_other")
    with pytest.raises(ValueError, match="canonical"):
        catalog.bind("criterion:x", [str(action["evidence_id"])])


def test_task_reference_catalog_is_shared_and_zero_state_until_persisted(tmp_path: Path):
    from agently.core.application.AgentTask.TaskReferences import TaskReferenceCatalog

    catalog = TaskReferenceCatalog("agent_task_siblings")
    first = catalog.add_evidence(
        {"id": "card.a", "kind": "action_evidence", "status": "ok", "body_state": "bounded", "body": "a"}
    )
    second = catalog.add_evidence(
        {"id": "card.b", "kind": "action_evidence", "status": "ok", "body_state": "bounded", "body": "b"}
    )

    assert first["evidence_id"] != second["evidence_id"]
    assert first["reference_id"] != second["reference_id"]
    assert not (tmp_path / ".agently").exists()


def test_task_reference_catalog_snapshot_preserves_tokens_and_rejects_stale_targets():
    from agently.core.application.AgentTask.TaskReferences import TaskReferenceCatalog

    catalog = TaskReferenceCatalog("agent_task_resume_refs")
    evidence = catalog.add_evidence(
        {"id": "source.one", "kind": "action_evidence", "status": "ok", "body_state": "bounded", "body": "one"}
    )
    snapshot = catalog.snapshot()
    restored = TaskReferenceCatalog.from_snapshot("agent_task_resume_refs", snapshot)

    assert restored.resolve(str(evidence["reference_id"]))["target"]["id"] == "source.one"

    damaged = json.loads(json.dumps(snapshot))
    damaged["references"][str(evidence["reference_id"])]["evidence_id"] = "evd_missing"
    with pytest.raises(ValueError, match="stale"):
        TaskReferenceCatalog.from_snapshot("agent_task_resume_refs", damaged)


def test_terminal_convergence_uses_stable_issue_keys_and_structured_state_digest():
    from agently.core.application.AgentTask.TerminalConvergence import (
        TerminalIssue,
        relevant_state_digest,
    )

    issue = TerminalIssue(
        gate_kind="factual_grounding",
        issue_code="unsupported_material_claim",
        contract_subject="artifact:factual_integrity",
    )
    first = relevant_state_digest(
        {
            "candidate_content_version_ids": ["cv_2"],
            "source_reference_targets": {"ref_4": "cv_3"},
            "capability_facts": {"research.fetch": "succeeded"},
            "criterion_subjects": ["criterion:1"],
            "output_subjects": ["artifact:report.md"],
            "repair_contract": {"subject": "artifact:factual_integrity", "claims": ["claim:1"]},
            "request_id": "volatile-one",
            "timestamp": 1,
            "progress_message": "first wording",
        }
    )
    equivalent = relevant_state_digest(
        {
            "candidate_content_version_ids": ["cv_2"],
            "source_reference_targets": {"ref_4": "cv_3"},
            "capability_facts": {"research.fetch": "succeeded"},
            "criterion_subjects": ["criterion:1"],
            "output_subjects": ["artifact:report.md"],
            "repair_contract": {"subject": "artifact:factual_integrity", "claims": ["claim:1"]},
            "request_id": "volatile-two",
            "timestamp": 999,
            "progress_message": "different wording",
        }
    )
    changed = relevant_state_digest(
        {
            "candidate_content_version_ids": ["cv_5"],
            "source_reference_targets": {"ref_4": "cv_3"},
            "capability_facts": {"research.fetch": "succeeded"},
            "criterion_subjects": ["criterion:1"],
            "output_subjects": ["artifact:report.md"],
            "repair_contract": {"subject": "artifact:factual_integrity", "claims": ["claim:1"]},
        }
    )

    assert issue.key == (
        "factual_grounding",
        "unsupported_material_claim",
        "artifact:factual_integrity",
    )
    assert first == equivalent
    assert changed != first


def test_terminal_convergence_stops_third_same_issue_without_a_fourth_repair():
    from agently.core.application.AgentTask.TerminalConvergence import (
        TerminalConvergenceState,
        TerminalIssue,
    )

    state = TerminalConvergenceState("agent_task_convergence")
    issue = TerminalIssue("factual_grounding", "unsupported_material_claim", "artifact:factual_integrity")

    first = state.record_detection(issue, "a" * 64, repair_contract={"claim_keys": ["claim:1"]})
    second = state.record_detection(
        issue,
        "a" * 64,
        repair_contract={"claim_keys": ["claim:1"]},
        verifier_called=False,
    )
    third = state.record_detection(
        issue,
        "b" * 64,
        repair_contract={"claim_keys": ["claim:1"]},
    )

    assert first == {
        "occurrence": 1,
        "state_changed": True,
        "verifier_called": True,
        "should_repair": True,
        "terminal": False,
        "skip_verifier": False,
        "repair_count": 1,
    }
    assert second["occurrence"] == 2
    assert second["state_changed"] is False
    assert second["skip_verifier"] is True
    assert second["should_repair"] is True
    assert second["repair_count"] == 2
    assert third["occurrence"] == 3
    assert third["terminal"] is True
    assert third["should_repair"] is False
    assert third["repair_count"] == 2
    with pytest.raises(RuntimeError, match="terminal"):
        state.record_detection(issue, "c" * 64, repair_contract={"claim_keys": ["claim:1"]})

    snapshot = state.snapshot()
    restored = TerminalConvergenceState.from_snapshot("agent_task_convergence", snapshot)
    assert restored.snapshot() == snapshot


def test_terminal_convergence_does_not_merge_different_issue_codes_for_the_same_gate_subject():
    from agently.core.application.AgentTask.TerminalConvergence import (
        TerminalConvergenceState,
        TerminalIssue,
    )

    state = TerminalConvergenceState("agent_task_convergence_family")
    unsupported = TerminalIssue(
        "factual_grounding",
        "unsupported_material_claim",
        "artifact:factual_integrity",
    )
    contradicted = TerminalIssue(
        "factual_grounding",
        "contradicted_material_claim",
        "artifact:factual_integrity",
    )

    first = state.record_detection(unsupported, "a" * 64, repair_contract={"claim_keys": ["claim:1"]})
    second = state.record_detection(unsupported, "b" * 64, repair_contract={"claim_keys": ["claim:2"]})
    independent = state.record_detection(contradicted, "c" * 64, repair_contract={"claim_keys": ["claim:3"]})

    assert first["occurrence"] == 1
    assert second["occurrence"] == 2
    assert independent["occurrence"] == 1
    assert independent["terminal"] is False
    assert independent["should_repair"] is True
    assert independent["repair_count"] == 1
    assert len(state.active_records()) == 2


def test_terminal_convergence_keeps_resolved_and_independent_issue_counts():
    from agently.core.application.AgentTask.TerminalConvergence import (
        TerminalConvergenceState,
        TerminalIssue,
    )

    state = TerminalConvergenceState("agent_task_convergence_independent")
    factual = TerminalIssue("factual_grounding", "unsupported", "artifact:factual_integrity")
    capability = TerminalIssue("capability", "action_unavailable", "action:research.fetch")

    state.record_detection(factual, "a" * 64, repair_contract={})
    state.mark_resolved(factual)
    reappeared = state.record_detection(factual, "b" * 64, repair_contract={})
    immediate = state.record_detection(
        capability,
        "c" * 64,
        repair_contract={},
        unrecoverable=True,
    )

    assert reappeared["occurrence"] == 2
    assert immediate["occurrence"] == 1
    assert immediate["terminal"] is True
    assert immediate["should_repair"] is False


@pytest.mark.asyncio
async def test_successful_terminal_gate_refreshes_resolved_convergence_diagnostics(
    tmp_path,
):
    from agently.core.application.AgentTask.TerminalConvergence import TerminalIssue

    agent = _create_agent("terminal-convergence-diagnostics").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="terminal-convergence-diagnostics",
        goal="Return the completed result.",
        success_criteria=["The result is complete."],
    )
    issue = TerminalIssue(
        "output_contract",
        "terminal_verifier_output_invalid",
        "verification:response",
    )
    task._terminal_convergence_state.record_detection(
        issue,
        "a" * 64,
        repair_contract={"requirements": ["Use offered ids."]},
    )
    task.diagnostics["terminal_convergence"] = (
        task._terminal_convergence_state.snapshot()
    )

    result = await task._apply_strict_terminal_gates(
        {"is_complete": True, "requires_block": False},
        candidate={"text": "Completed result."},
        execution_evidence_summary={},
        verifier_called=True,
    )

    record = next(
        iter(task.diagnostics["terminal_convergence"]["records"].values())
    )
    assert result["terminal_convergence"] == {
        "resolved": True,
        "verifier_called": True,
    }
    assert record["active"] is False
    assert record["resolved"] is True


def test_taskboard_required_card_setback_stops_on_third_cross_tick_occurrence(tmp_path):
    agent = _create_agent("agent-taskboard-card-convergence").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-card-convergence",
        goal="Produce the required evidence.",
        success_criteria=["The required evidence is present."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-card-convergence",
        graph={
            "graph_id": "taskboard-card-convergence-graph",
            "cards": [
                {
                    "id": "final-verification-repair",
                    "objective": "Repair the final evidence gap.",
                    "failure_policy": "required",
                    "allowed_execution_shape": "auto",
                    "metadata": {
                        "generated_by": "agent_task.taskboard.final_verification_repair",
                    },
                }
            ],
        },
    )
    revision = revision.next_revision(
        revision.graph,
        card_results={
            "final-verification-repair": {
                "card_id": "final-verification-repair",
                "status": "setback",
                "preview": {
                    "next_board_action": "continue",
                    "gaps": ["Evidence is still unavailable."],
                },
                "artifact_refs": [],
                "file_refs": [],
                "diagnostics": [],
                "metadata": {},
            }
        },
    )

    first = task._taskboard_card_convergence_result(revision)
    second = task._taskboard_card_convergence_result(revision)
    third = task._taskboard_card_convergence_result(revision)

    assert first is None
    assert second is None
    assert third is not None
    assert third["status"] == "blocked"
    assert third["accepted"] is False
    assert third["artifact_status"] == "partial"
    assert third["terminal_convergence"]["occurrence"] == 3
    assert third["terminal_convergence"]["issue"] == {
        "gate_kind": "taskboard_card",
        "issue_code": "required_card_unsatisfied",
        "contract_subject": "taskboard_card:final-verification-repair",
    }
    assert third["terminal_convergence"]["stopped_after_third_occurrence"] is True
    assert task.status == "blocked"
    assert task.result == third
    assert task.diagnostics["terminal_convergence"]["records"]


def test_taskboard_required_repair_converges_across_non_satisfying_statuses(tmp_path):
    agent = _create_agent("agent-taskboard-repair-status-convergence").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-repair-status-convergence",
        goal="Repair the terminal deliverable.",
        success_criteria=["The terminal deliverable passes verification."],
        execution="taskboard",
    )
    terminal = None
    for index, status in enumerate(("setback", "failed", "blocked"), start=1):
        card_id = "final-verification-repair" if index == 1 else f"final-verification-repair-{index}"
        revision = TaskBoardRevision.create(
            board_id=f"taskboard-repair-status-convergence-{index}",
            graph={
                "graph_id": f"taskboard-repair-status-convergence-graph-{index}",
                "cards": [
                    {
                        "id": card_id,
                        "objective": "Repair the same terminal verification gap.",
                        "failure_policy": "required",
                        "allowed_execution_shape": "auto",
                        "metadata": {
                            "generated_by": "agent_task.taskboard.final_verification_repair",
                            "terminal_convergence_subject": "taskboard_final_verification",
                        },
                    }
                ],
            },
        ).next_revision(
            TaskBoardGraph.from_value(
                {
                    "graph_id": f"taskboard-repair-status-convergence-graph-{index}",
                    "cards": [
                        {
                            "id": card_id,
                            "objective": "Repair the same terminal verification gap.",
                            "failure_policy": "required",
                            "allowed_execution_shape": "auto",
                            "metadata": {
                                "generated_by": "agent_task.taskboard.final_verification_repair",
                                "terminal_convergence_subject": "taskboard_final_verification",
                            },
                        }
                    ],
                }
            ),
            card_results={
                card_id: TaskBoardCardResult(
                    card_id=card_id,
                    status=status,
                    preview={"status": status, "remaining_work": ["The terminal gap remains."]},
                )
            },
        )
        terminal = task._taskboard_card_convergence_result(
            revision,
            executed_card_ids=(card_id,),
        )
        if index < 3:
            assert terminal is None

    assert terminal is not None
    assert terminal["terminal_convergence"]["occurrence"] == 3
    assert terminal["terminal_convergence"]["issue"] == {
        "gate_kind": "taskboard_card",
        "issue_code": "required_card_unsatisfied",
        "contract_subject": "taskboard_final_verification",
    }
    assert terminal["terminal_convergence"]["stopped_after_third_occurrence"] is True


@pytest.mark.parametrize("stale_status", ("setback", "failed", "blocked"))
def test_taskboard_convergence_does_not_recount_stale_result_from_other_tick(
    tmp_path,
    stale_status,
):
    agent = _create_agent("agent-taskboard-stale-setback").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-stale-setback",
        goal="Produce the required evidence.",
        success_criteria=["The required evidence is present."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-stale-setback",
        graph={
            "graph_id": "taskboard-stale-setback-graph",
            "cards": [
                {
                    "id": "stale-repair",
                    "objective": "Repair the old evidence gap.",
                    "failure_policy": "required",
                },
                {
                    "id": "current-work",
                    "objective": "Perform independent current work.",
                    "failure_policy": "required",
                },
            ],
        },
    ).next_revision(
        TaskBoardGraph.from_value(
            {
                "graph_id": "taskboard-stale-setback-graph",
                "cards": [
                    {
                        "id": "stale-repair",
                        "objective": "Repair the old evidence gap.",
                        "failure_policy": "required",
                    },
                    {
                        "id": "current-work",
                        "objective": "Perform independent current work.",
                        "failure_policy": "required",
                    },
                ],
            }
        ),
        card_results={
            "stale-repair": TaskBoardCardResult(
                card_id="stale-repair",
                status=stale_status,
            ),
            "current-work": TaskBoardCardResult(
                card_id="current-work",
                status="completed",
            ),
        },
    )

    assert (
        task._taskboard_card_convergence_result(
            revision,
            executed_card_ids=("current-work",),
        )
        is None
    )
    assert task._terminal_convergence_state.snapshot()["records"] == {}


@pytest.mark.asyncio
async def test_taskboard_lifecycle_does_not_schedule_fourth_repeated_setback_tick(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-taskboard-lifecycle-convergence").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-lifecycle-convergence",
        goal="Produce the required evidence.",
        success_criteria=["The required evidence is present."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-lifecycle-convergence",
        graph={
            "graph_id": "taskboard-lifecycle-convergence-graph",
            "cards": [
                {
                    "id": "required-repair",
                    "objective": "Repair the required evidence gap.",
                    "failure_policy": "required",
                    "allowed_execution_shape": "auto",
                }
            ],
        },
    )
    planning_policy = resolve_task_board_planning_policy(
        task._taskboard_effort(),
        metadata={"task_id": task.id},
    )
    card_attempts = 0
    emitted_paths: list[str] = []
    original_emit = task._emit

    async def build_context():
        return {}

    async def request_plan(_context_pack):
        return SimpleNamespace(
            revision=revision,
            planning_policy=planning_policy,
        )

    async def run_card(context, _context_pack):
        nonlocal card_attempts
        card_attempts += 1
        return TaskBoardCardResult(
            card_id=context.card.id,
            status="setback",
            preview={
                "next_board_action": "continue",
                "gaps": ["The same evidence remains unavailable."],
            },
        )

    async def fail_finalize(*_args, **_kwargs):
        raise AssertionError("terminal convergence must bypass finalization")

    async def capture_emit(path, value, *args, **kwargs):
        emitted_paths.append(path)
        await original_emit(path, value, *args, **kwargs)

    monkeypatch.setattr(task, "_build_context", build_context)
    monkeypatch.setattr(task, "_request_taskboard_plan", request_plan)
    monkeypatch.setattr(task, "_taskboard_should_fallback_to_flat", lambda _revision: False)
    monkeypatch.setattr(task, "_run_taskboard_card", run_card)
    monkeypatch.setattr(task, "_finalize_taskboard", fail_finalize)
    monkeypatch.setattr(task, "_emit", capture_emit)

    result = await task._run_taskboard()

    assert result["status"] == "blocked"
    assert result["accepted"] is False
    assert result["terminal_convergence"]["occurrence"] == 3
    assert card_attempts == 3
    assert "agent_task.taskboard.tick.3.completed" in emitted_paths
    assert "agent_task.taskboard.tick.4.scheduled" not in emitted_paths
    assert emitted_paths.count("agent_task.terminal_convergence") == 1


@pytest.mark.asyncio
async def test_strict_grounding_overrides_broad_legacy_completion_and_stops_third_unchanged_issue(
    tmp_path,
):
    agent = _create_agent("strict-grounding-terminal-convergence").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="strict-grounding-terminal-convergence",
        goal="Return a grounded report.",
        success_criteria=["Every material claim is grounded."],
        execution="flat",
    )

    carrier_id = "inline:" + "a" * 64
    candidate = {
        "kind": "inline_final_result",
        "carrier_id": carrier_id,
        "text": "Unsupported external claim.",
        "path": "",
        "content_version_id": carrier_id,
        "diagnostics": [],
    }
    candidate["carriers"] = [dict(candidate)]
    semantic_verification = task._normalize_verification({
        "is_complete": True,
        "requires_block": False,
            "reason": "All explicit criteria are satisfied.",
            "missing_criteria": [],
            "final_result": candidate["text"],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "The criterion is evaluated by the material-claim audit.",
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
        "material_claim_checks": [
            {
                "claim_key": "claim_1",
                "claim_kind": "external_fact",
                "state": "unsupported",
                "evidence_ids": [],
                "reason": "No offered evidence supports this external fact.",
            }
        ],
    }, execution_evidence_summary={}, terminal_candidate=candidate)

    first = await task._apply_strict_terminal_gates(
        semantic_verification,
        candidate=candidate,
        execution_evidence_summary={},
        verifier_called=True,
    )
    second = task._terminal_convergence_preflight(
        candidate=candidate,
        execution_evidence_summary={},
    )
    third = task._terminal_convergence_preflight(
        candidate=candidate,
        execution_evidence_summary={},
    )

    assert first["is_complete"] is False
    assert first["material_claim_audit"]["valid"] is False
    assert first["terminal_convergence"]["occurrence"] == 1
    assert second is not None and second["terminal_convergence"]["occurrence"] == 2
    assert second["requires_block"] is False
    assert third is not None and third["terminal_convergence"]["occurrence"] == 3
    assert third["requires_block"] is True
    assert third["final_result"] == candidate["text"]
    assert task._terminal_convergence_state.snapshot()["records"]


@pytest.mark.asyncio
async def test_terminal_carrier_promotes_file_and_reuses_unchanged_identity(tmp_path):
    agent = _create_agent("terminal-carrier-reused-identity").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="terminal-carrier-reused-identity",
        goal="Return a grounded report file.",
        success_criteria=["The report is grounded."],
        execution="flat",
    )
    content = "Evidence-backed report body.\n"
    write_result = await task.workspace.write_file("reports/final.md", content)
    file_ref = {**write_result["file_refs"][0], "role": "workspace_artifact"}

    await task._replace_terminal_carriers(
        execution_result={"artifact_refs": [file_ref], "final_result": "reports/final.md"},
        execution_evidence_summary={},
        source_work_result_id="work:1",
    )
    first = await task._current_terminal_candidate()
    await task._replace_terminal_carriers(
        execution_result={"artifact_refs": [file_ref], "final_result": "reports/final.md"},
        execution_evidence_summary={},
        source_work_result_id="work:2",
    )
    second = await task._current_terminal_candidate()

    assert first["text"] == content
    assert str(first["content_version_id"]).startswith("cv_")
    assert str(first["carrier_id"]).startswith("car_")
    assert second["carrier_id"] == first["carrier_id"]
    assert second["content_version_id"] == first["content_version_id"]


@pytest.mark.asyncio
async def test_strict_grounding_prefers_required_workspace_deliverable_over_inline_summary(tmp_path):
    agent = _create_agent("strict-grounding-required-deliverable").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="strict-grounding-required-deliverable",
        goal="Write the report to final.md and return a compact summary.",
        success_criteria=["The report is grounded."],
        execution="flat",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    report = "# Grounded Report\n\nEvidence-backed file body.\n"
    summary = "Compact inline summary that is a separate return carrier."
    await task.workspace.write_file("final.md", report)

    await task._replace_terminal_carriers(
        execution_result={"candidate_final_result": summary, "file_refs": []},
        execution_evidence_summary={},
        source_work_result_id="work:required-deliverable",
    )
    candidate = await task._current_terminal_candidate()

    assert candidate["path"] == "final.md"
    assert candidate["text"] == report
    assert str(candidate["content_version_id"]).startswith("cv_")
    assert [carrier["kind"] for carrier in candidate["carriers"]] == [
        "workspace_artifact",
        "inline_final_result",
    ]
    assert candidate["carriers"][1]["text"] == summary
    assert str(candidate["carriers"][1]["content_version_id"]).startswith("inline:")


@pytest.mark.asyncio
async def test_strict_grounding_excludes_intermediate_workspace_carrier_when_required_path_exists(
    tmp_path,
):
    agent = _create_agent("strict-grounding-single-required-file").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="strict-grounding-single-required-file",
        goal="Write the report to final.md.",
        success_criteria=["final.md is the terminal deliverable."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    working_write = await task.workspace.write_file(
        "working/taskboard/synthesize/final.md",
        "# Working draft\n\nIntermediate content.\n",
    )
    final_write = await task.workspace.write_file(
        "final.md",
        "# Final report\n\nDelivered content.\n",
    )
    working_ref = {
        **working_write["file_refs"][0],
        "role": "workspace_artifact",
    }
    final_ref = {
        **final_write["file_refs"][0],
        "role": "workspace_artifact",
    }

    await task._replace_terminal_carriers(
        execution_result={
            "final_result": "final.md",
            "file_refs": [working_ref, final_ref],
        },
        execution_evidence_summary={"artifact_refs": [working_ref, final_ref]},
        source_work_result_id="work:required-file",
    )
    candidate = await task._current_terminal_candidate()

    assert [carrier["path"] for carrier in candidate["carriers"]] == ["final.md"]
    assert candidate["carriers"][0]["required"] is True


@pytest.mark.asyncio
async def test_strict_grounding_reuses_cumulative_trusted_artifact_when_current_step_returns_inline(tmp_path):
    agent = _create_agent("strict-grounding-cumulative-artifact").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="strict-grounding-cumulative-artifact",
        goal="Write the report and return a compact summary.",
        success_criteria=["The report is grounded."],
        execution="flat",
    )
    report = "# Grounded Report\n\nEvidence-backed file body.\n"
    summary = "Compact inline summary from a later readback step."
    write_result = await task.workspace.write_file("final.md", report)
    trusted_ref = {
        **write_result["file_refs"][0],
        "path": "final.md",
        "role": "workspace_artifact",
    }

    await task._replace_terminal_carriers(
        execution_result={"candidate_final_result": summary, "file_refs": []},
        execution_evidence_summary={"artifact_refs": [trusted_ref]},
        source_work_result_id="work:cumulative-artifact",
    )
    candidate = await task._current_terminal_candidate()

    assert candidate["path"] == "final.md"
    assert candidate["text"] == report
    assert str(candidate["content_version_id"]).startswith("cv_")
    assert [carrier["kind"] for carrier in candidate["carriers"]] == [
        "workspace_artifact",
        "inline_final_result",
    ]
    assert candidate["carriers"][1]["text"] == summary


@pytest.mark.asyncio
async def test_strict_grounding_validates_workspace_and_inline_terminal_carriers_separately(
    tmp_path,
):
    agent = _create_agent("strict-grounding-multiple-terminal-carriers").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="strict-grounding-multiple-terminal-carriers",
        goal="Write final.md and return a compact summary.",
        success_criteria=["Both returned carriers are grounded."],
        execution="flat",
    )
    source = task._task_reference_catalog.add_evidence(
        {
            "id": "file:grounded",
            "kind": "workspace_artifact.readback",
            "status": "ok",
            "body_state": "bounded",
            "body": "Grounded file body.",
        }
    )
    workspace_candidate = {
        "kind": "workspace_artifact",
        "carrier_id": "car_file",
        "text": "Grounded file body.",
        "path": "final.md",
        "content_version_id": "cv_1",
        "diagnostics": [],
    }
    inline_candidate = {
        "kind": "inline_final_result",
        "carrier_id": "car_inline",
        "text": "Unsupported inline summary claim.",
        "path": "",
        "content_version_id": "inline:" + "c" * 64,
        "diagnostics": [],
    }
    candidate = {**workspace_candidate, "carriers": [workspace_candidate, inline_candidate]}
    normalized = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "Both carriers look complete.",
            "missing_criteria": [],
            "acceptance_delta": [],
            "final_result": inline_candidate["text"],
            "material_claim_coverage_complete": True,
                "material_claim_checks": [
                    {
                        "claim_key": "claim_1",
                        "claim_kind": "external_fact",
                    "state": "supported",
                    "evidence_ids": [source["reference_id"]],
                    "reason": "The file fact is directly supported.",
                    },
                    {
                        "claim_key": "claim_2",
                        "claim_kind": "external_fact",
                    "state": "unsupported",
                    "evidence_ids": [],
                    "reason": "The inline claim has no offered support.",
                },
            ],
        },
        execution_evidence_summary={},
        terminal_candidate=candidate,
    )
    result = await task._apply_strict_terminal_gates(
        normalized,
        candidate=candidate,
        execution_evidence_summary={},
        verifier_called=True,
    )

    assert result["is_complete"] is False
    assert result["material_claim_audit"]["valid"] is False
    assert [check["carrier_id"] for check in result["material_claim_checks"]] == [
        "car_file",
        "car_inline",
    ]
    assert result["material_claim_repair_contract"]["requirements"][0]["carrier_id"] == (
        "car_inline"
    )
    assert result["material_claim_repair_contract"]["requirements"][0]["claim_key"] == "claim_2"
    assert result["material_claim_repair_contract"]["requirements"][0]["path"] == ""
    assert result["material_claim_repair_contract"]["requirements"][0]["content_version_id"] == (
        inline_candidate["content_version_id"]
    )
    assert not hasattr(task, "_latest_grounding_candidate")


@pytest.mark.asyncio
async def test_taskboard_terminal_candidate_refs_follow_unique_leaf_deliverable_owner(tmp_path):
    agent = _create_agent("taskboard-current-leaf-deliverable").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-current-leaf-deliverable",
        goal="Write the report to final.md.",
        success_criteria=["final.md is the terminal deliverable."],
        execution="taskboard",
    )
    content = "# Report\n\nSame bytes in working and final paths.\n"
    working_write = await task.workspace.write_file("working/taskboard/synthesize/final.md", content)
    final_write = await task.workspace.write_file("final.md", content)
    working_ref = {
        **working_write["file_refs"][0],
        "role": "workspace_artifact",
        "source": "agent_task.taskboard.card.synthesize.workspace_artifact",
    }
    final_ref = {
        **final_write["file_refs"][0],
        "role": "workspace_artifact",
        "source": "agent_task.taskboard.card.deliver.workspace_artifact",
    }
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-2",
            "status": "completed",
            "graph": {
                "graph_id": f"{task.id}.taskboard",
                "cards": [
                    {
                        "id": "synthesize",
                        "objective": "Create the working report.",
                        "required_outputs": ["Working report"],
                    },
                    {
                        "id": "deliver",
                        "objective": "Deliver final.md.",
                        "depends_on": ["synthesize"],
                        "required_outputs": ["final.md"],
                    },
                ],
            },
            "card_results": {
                "synthesize": TaskBoardCardResult(
                    card_id="synthesize",
                    status="completed",
                    preview={"candidate_final_result": content},
                    file_refs=(working_ref,),
                    artifact_refs=(working_ref,),
                ).to_dict(),
                "deliver": TaskBoardCardResult(
                    card_id="deliver",
                    status="completed",
                    preview={"final_result": "Delivered to final.md."},
                    file_refs=(final_ref,),
                    artifact_refs=(final_ref,),
                ).to_dict(),
            },
        }
    )

    selected = task._taskboard_terminal_candidate_refs(
        revision,
        [working_ref, final_ref],
    )

    assert [item["path"] for item in selected] == [final_ref["path"]]
    assert selected[0]["source"] == "agent_task.taskboard.card.deliver.workspace_artifact"


@pytest.mark.asyncio
async def test_taskboard_terminal_candidate_refs_do_not_substitute_working_file_for_declared_leaf_path(tmp_path):
    agent = _create_agent("taskboard-declared-leaf-path").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-declared-leaf-path",
        goal="Write the report to final.md.",
        success_criteria=["final.md is the terminal deliverable."],
        execution="taskboard",
    )
    working_write = await task.workspace.write_file(
        "working/taskboard/collect/final.md",
        "# Upstream Evidence\n\nThis is not the terminal deliverable.\n",
    )
    working_ref = {
        **working_write["file_refs"][0],
        "role": "workspace_artifact",
        "source": "agent_task.taskboard.card.collect.workspace_artifact",
    }
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-declared-path",
            "status": "completed",
            "graph": {
                "graph_id": f"{task.id}.taskboard",
                "cards": [
                    {
                        "id": "collect",
                        "objective": "Collect upstream evidence.",
                        "required_outputs": ["Working evidence"],
                    },
                    {
                        "id": "synthesize",
                        "objective": "Deliver final.md.",
                        "depends_on": ["collect"],
                        "required_outputs": ["final.md"],
                    },
                ],
            },
            "card_results": {
                "collect": TaskBoardCardResult(
                    card_id="collect",
                    status="completed",
                    preview={"candidate_final_result": "Upstream evidence only."},
                    file_refs=(working_ref,),
                    artifact_refs=(working_ref,),
                ).to_dict(),
                "synthesize": TaskBoardCardResult(
                    card_id="synthesize",
                    status="completed",
                    preview={
                        "status": "completed",
                        "sufficient": True,
                        "candidate_final_result": "# Final Report\n\nComplete candidate body.\n",
                        "artifact_manifest": {"path": "final.md"},
                        "remaining_work": ["Materialize and read back final.md."],
                    },
                ).to_dict(),
            },
        }
    )

    selected = task._taskboard_terminal_candidate_refs(revision, [working_ref])

    assert selected == []


@pytest.mark.asyncio
async def test_unavailable_required_action_fails_closed_even_with_workspace_readback(tmp_path):
    agent = _create_agent("strict-capability-fail-closed").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="strict-capability-fail-closed",
        goal="Use the specified research Action.",
        success_criteria=["The research Action succeeds."],
        execution="flat",
        options={
            "capability_evidence_requirements": [
                {
                    "capability_id": "research.fetch",
                    "capability_kind": "action",
                    "kind": "action_succeeded",
                    "required": True,
                    "criterion_id": "criterion:1",
                }
            ]
        },
    )
    normalized = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "A report file was read back.",
            "missing_criteria": [],
            "final_result": "Readback-only report.",
        },
        execution_evidence_summary={
            "status": "completed",
            "capability_evidence_requirements": task.options["capability_evidence_requirements"],
            "capability_evidence": {"actions": {"succeeded": []}, "artifacts": {"readback": ["report.md"]}},
        },
        candidate_final_result="Readback-only report.",
    )
    result = await task._apply_strict_terminal_gates(
        normalized,
        candidate={
            "text": "Readback-only report.",
            "path": "",
            "content_version_id": "inline:" + "b" * 64,
            "diagnostics": [],
        },
        execution_evidence_summary={
            "status": "completed",
            "capability_evidence_requirements": task.options["capability_evidence_requirements"],
        },
        verifier_called=True,
    )

    assert normalized["is_complete"] is False
    assert normalized["missing_capability_evidence"] == ["research.fetch"]
    assert result["requires_block"] is True
    assert result["terminal_convergence"]["occurrence"] == 1
    assert result["terminal_convergence"]["terminal"] is True
    assert result["terminal_convergence"]["issue"]["contract_subject"] == "action:research.fetch"


@pytest.mark.asyncio
async def test_denied_action_policy_fails_on_first_detection(
    tmp_path,
):
    agent = _create_agent("strict-unrecoverable-terminal-facts").use_workspace(tmp_path / "workspace")
    denied_task = AgentTask(
        agent,
        task_id="strict-denied-action-policy",
        goal="Complete the policy-bound action.",
        success_criteria=["The action completes."],
        execution="flat",
    )
    denied = await denied_task._apply_strict_terminal_gates(
        {
            "is_complete": False,
            "requires_block": False,
            "reason": "Structured execution did not complete.",
            "missing_criteria": ["The action completes."],
            "guard_reasons": ["execution_risk_actions_present"],
            "final_result": "Partial result.",
        },
        candidate={
            "text": "Partial result.",
            "path": "",
            "content_version_id": "inline:" + "d" * 64,
            "diagnostics": [],
        },
        execution_evidence_summary={"blocked_actions": ["publish.report"]},
        verifier_called=True,
    )

    assert denied["requires_block"] is True
    assert denied["terminal_convergence"]["occurrence"] == 1
    assert denied["terminal_convergence"]["issue"]["issue_code"] == "action_policy_blocked"


@pytest.mark.asyncio
async def test_flat_task_stops_third_unchanged_material_claim_issue_with_partial_artifact(
    tmp_path,
):
    class CompleteLegacyVerifier(MockAgentTaskRequester):
        name = "CompleteLegacyVerifierForConvergence"
        verifier_calls = 0

        async def request_model(self, request_data: AgentlyRequestData):
            request_payload = DataFormatter.sanitize(request_data.data)
            text = json.dumps(request_payload, ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                self.__class__.verifier_calls += 1

                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "Legacy verification passed.",
                    "missing_criteria": [],
                    "final_result": "Unchanged unsupported report claim.",
                    "criterion_checks": [
                        {
                            "criterion_id": "criterion:1",
                            "satisfied": True,
                            "summary": "The report was produced; factual support is audited separately.",
                            "evidence_ids": [],
                        }
                    ],
                    "material_claim_coverage_complete": True,
                    "material_claim_checks": [
                        {
                            "claim_key": "claim_1",
                            "claim_kind": "external_fact",
                            "state": "unsupported",
                            "evidence_ids": [],
                            "reason": "No offered evidence supports this external fact.",
                        }
                    ],
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "Repair the unchanged report.",
                    "expected_evidence": "Grounded report evidence.",
                    "rationale": "The terminal gate requires repair.",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="flat-third-convergence-settings", parent=Agently.settings)
    plugins = PluginManager(settings, parent=Agently.plugin_manager, name="flat-third-convergence-plugins")
    plugins.register("ModelRequester", CompleteLegacyVerifier, activate=True)
    agent = Agently.AgentType(plugins, parent_settings=settings, name="flat-third-convergence")
    task = agent.create_task(
        task_id="flat-third-convergence",
        goal="Return a grounded report.",
        success_criteria=["Every material claim is grounded."],
        workspace=tmp_path / "workspace",
        execution="flat",
        max_iterations=5,
    )

    async def unchanged_step(iteration_index, plan, context_pack):
        _ = plan, context_pack
        return (
            {
                "candidate_final_result": "Unchanged unsupported report claim.",
                "remaining_work": [],
            },
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": unchanged_step}
    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "blocked"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert "final.md" in result["final_result"]
    assert result["artifact_refs"]
    assert len(meta["iterations"]) == 3
    assert CompleteLegacyVerifier.verifier_calls == 1
    assert meta["iterations"][-1]["verification"]["terminal_convergence"]["occurrence"] == 3
    assert meta["iterations"][-1]["verification"]["requires_block"] is True


def test_evidence_guard_binds_composite_file_locator_to_matching_readback():
    # A composite/locator reference ("<file> <sub-locator>") that exact-alias cannot
    # match must bind to the readback whose path anchor it names -- and never another.
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "rb.report",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "body": "| project-a | 42 |",
            },
            {
                "id": "rb.data",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "data.csv",
                "body": "raw rows",
            },
        ]
    }

    guard = validate_evidence_use(
        [
            {
                "claim": "project-a throughput is 42",
                "evidence_ids": ["report.md table row for project-a"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["rb.report"]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])


def test_evidence_guard_narrows_composite_section_locator_by_heading():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "rb.report.summary",
                "kind": "workspace_artifact.targeted_readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "heading": "Summary",
                "body": "Summary content",
            },
            {
                "id": "rb.report.risks",
                "kind": "workspace_artifact.targeted_readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "heading": "Risks",
                "body": "Risk content",
            },
        ]
    }

    guard = validate_evidence_use(
        [
            {
                "claim": "The risks are enumerated.",
                "evidence_ids": ["report.md Risks section"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["rb.report.risks"]


def test_resolve_evidence_reference_reports_tiers():
    from agently.core.application.AgentTask.EvidenceLedger import resolve_evidence_reference

    ledger = {
        "evidence_items": [
            {
                "id": "rb.report",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "body": "x",
            },
            {
                "id": "rb.data",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "data.csv",
                "body": "y",
            },
        ]
    }

    assert resolve_evidence_reference("rb.report", ledger)["status"] == "resolved"
    anchor = resolve_evidence_reference("report.md table row for project-a", ledger)
    assert anchor["status"] == "resolved"
    assert anchor["id"] == "rb.report"
    assert resolve_evidence_reference("some opaque handle that names nothing", ledger)["status"] == "unresolved"


def test_resolve_evidence_reference_reports_ambiguous_basename():
    from agently.core.application.AgentTask.EvidenceLedger import resolve_evidence_reference

    ledger = {
        "evidence_items": [
            {
                "id": "docs.readme",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "full",
                "path": "docs/README.md",
            },
            {
                "id": "pkg.readme",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "full",
                "path": "pkg/README.md",
            },
        ]
    }

    resolution = resolve_evidence_reference("README.md", ledger)
    assert resolution["status"] == "ambiguous"
    assert set(resolution["candidates"]) == {"docs.readme", "pkg.readme"}


def test_execution_meta_action_results_enter_canonical_evidence_ledger():
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "market_quotes",
                    "status": "partial_success",
                    "action_call_id": "call-quotes",
                    "raw": {
                        "kwargs": {"symbols": ["NVDA", "AMD", "AVGO"]},
                        "data": {
                            "quotes": [
                                {"symbol": "NVDA", "last": "194.97", "as_of": "2026-06-29"},
                                {"symbol": "AMD", "last": "539.49", "as_of": "2026-06-29"},
                            ],
                            "history_status": "unavailable",
                        },
                    },
                }
            ],
            "route_logs": {},
        },
    }

    ledger = AgentTask._evidence_ledger_from_execution_meta(execution_meta)
    action_items = [
        item
        for item in ledger["items"]
        if item.get("kind") == "agent_task.action.result" and item.get("action_id") == "market_quotes"
    ]

    assert len(action_items) == 1
    item = action_items[0]
    assert item["status"] == "ok"
    assert item["body_state"] == "bounded"
    assert item["action_call_id"] == "call-quotes"
    assert "action_result_market_quotes" in item["aliases"]
    assert "NVDA" in json.dumps(item.get("body") or item.get("preview"), ensure_ascii=False)

    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    guard = validate_evidence_use(
        [
            {
                "claim": "NVDA quote data was retrieved.",
                "evidence_ids": ["action_result_market_quotes"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == [item["id"]]

    call_id_guard = validate_evidence_use(
        [
            {
                "claim": "Price evidence",
                "evidence_ids": ["call-quotes"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert call_id_guard["valid"] is True
    assert call_id_guard["normalized_evidence_use"][0]["evidence_ids"] == [item["id"]]


def test_access_blocked_action_preview_is_unavailability_evidence_only():
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "browse",
                    "status": "success",
                    "action_call_id": "call-waf",
                    "result_preview": ("为了更好的访问体验，请进行验证。" "appkey: \"CF_APP_WAF\""),
                }
            ],
            "route_logs": {},
        },
    }

    ledger = AgentTask._evidence_ledger_from_execution_meta(execution_meta)
    item = next(evidence for evidence in ledger["items"] if evidence.get("kind") == "agent_task.action.result")

    assert item["status"] == "failed"
    assert item["body_state"] == "bounded"
    assert item["supports"]["content"] is False
    assert item["supports"]["unavailability"] is True
    assert item["diagnostics"][0]["code"] == "agent_task.action_result.access_blocked_preview"

    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    guard = validate_evidence_use(
        [
            {
                "claim": "The source page contains the requested syllabus.",
                "evidence_ids": ["action_result_browse"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is False
    assert guard["blocking_count"] == 1


def test_block_carrier_output_policy_selects_schema_and_body_transport():
    flat_text = WorkUnitIntent(
        id="flat-text",
        origin="flat_step",
        objective="Return separately addressable prose fields.",
        delivery_contract={
            "execution_prompt": {
                "output": {"summary": (str,), "notes": (str,)},
                "output_format": "auto",
            }
        },
    )
    mixed = WorkUnitIntent(
        id="mixed",
        origin="flat_step",
        objective="Return prose plus typed status.",
        delivery_contract={
            "execution_prompt": {
                "output": {"summary": (str,), "accepted": (bool,)},
                "output_format": "auto",
            }
        },
    )
    workspace_artifact = WorkUnitIntent(
        id="workspace-artifact",
        origin="taskboard_card",
        objective="Create a trusted file-backed deliverable.",
        delivery_contract={"deliverable_mode": "sectioned_workspace_artifact"},
    )
    plain_text = WorkUnitIntent(
        id="plain-text",
        origin="flat_step",
        objective="Write one natural-language body.",
        runtime_preferences={"deliverable_mode": "freeform_text"},
    )

    assert select_carrier_output_policy(flat_text).control_format == "xml_field"
    assert select_carrier_output_policy(mixed).control_format == "hybrid"

    artifact_policy = select_carrier_output_policy(workspace_artifact)
    assert artifact_policy.control_format == "json"
    assert artifact_policy.body_transport == "workspace_artifact"
    assert artifact_policy.body_uses_output is False
    assert artifact_policy.requires_workspace_readback is True

    plain_text_policy = select_carrier_output_policy(plain_text)
    assert plain_text_policy.control_format is None
    assert plain_text_policy.body_transport == "plain_text"
    assert plain_text_policy.body_uses_output is False
    assert plain_text_policy.requires_structured_judge is True


def test_block_carrier_exposes_compact_scoped_retrieval_policy():
    intent = WorkUnitIntent(
        id="retrieval-policy",
        origin="flat_step",
        objective="Find scoped evidence before reading large files.",
    )

    policy = scoped_retrieval_policy()
    serialized = intent.to_dict()

    assert serialized["retrieval_policy"] == policy
    assert policy["schema_version"] == "agent_task_scoped_retrieval/v1"
    assert policy["roles"]["locator_ref"] == "discovered target; content not read"
    assert policy["roles"]["evidence_snippet"] == "bounded readable excerpt"
    assert policy["query_owner"] == "planner_or_control_model"
    assert (
        policy["executor_owner"]
        == "Workspace.retrieve through Blocks workspace_operation, plus bounded readback when needed"
    )


def test_flat_step_plan_preserves_scoped_retrieval_query_groups(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-plan").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-plan",
        goal="Use scoped search before reading large files.",
        success_criteria=["Evidence is grounded."],
    )

    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Search scoped notes.",
            "scoped_retrieval": {
                "queries": [
                    {
                        "query": "deadline",
                        "expected_role": "evidence_snippet",
                        "path": "notes",
                        "pattern": "*.md",
                    },
                    {
                        "query": "final.md",
                        "expected_role": "locator_ref",
                    },
                ],
                "fallback_order": ["next_query", "bounded_read"],
            },
        }
    )

    assert plan["scoped_retrieval"] == {
        "query_groups": [
            {
                "query": "deadline",
                "expected_role": "evidence_snippet",
                "path": "notes",
                "pattern": "*.md",
            },
            {
                "query": "final.md",
                "expected_role": "locator_ref",
            },
        ],
        "fallback_order": ["next_query", "bounded_read"],
    }


def test_scoped_retrieval_normalizes_structured_content_contains_and_globs(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-structured-fields").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-structured-fields",
        goal="Use structured retrieval fields.",
        success_criteria=["Evidence is grounded."],
    )

    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Find Atlas evidence.",
            "expected_evidence": "Atlas evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "read all retained files that mention Atlas or owner",
                        "expected_role": "content retrieval for source facts",
                        "search_surface": "workspace_files",
                        "path": "retained/",
                        "pattern": "*.txt,*.md,*.json",
                        "filters": {"content_contains": ["Atlas", "owner"]},
                    }
                ]
            },
        }
    )

    query_groups = plan["scoped_retrieval"]["query_groups"]
    assert [group["query"] for group in query_groups] == ["Atlas", "owner"]
    assert all(group["pattern"] == "**" for group in query_groups)
    assert all("content_contains" not in group.get("filters", {}) for group in query_groups)
    assert query_groups[0]["search_surface"] == "workspace_files"


def test_taskboard_source_ref_policy_reuses_scoped_retrieval_policy():
    policy = AgentTask._taskboard_source_ref_policy()

    assert policy["scoped_retrieval_policy"] == scoped_retrieval_policy()
    assert "locator_ref" in policy["scoped_retrieval_policy"]["roles"]
    assert "evidence_snippet" in policy["scoped_retrieval_policy"]["roles"]
    assert any("filters.collection" in rule for rule in policy["scoped_retrieval_policy"]["rules"])
    assert any("never infer a generic kind" in rule for rule in policy["scoped_retrieval_policy"]["rules"])
    assert any("truncated evidence snippets" in rule for rule in policy["scoped_retrieval_policy"]["rules"])
    assert any("filters.collection" in rule for rule in policy["rules"])
    assert any("never infer a generic kind" in rule for rule in policy["rules"])
    assert any("truncated evidence snippets" in rule for rule in policy["rules"])


def test_block_carrier_compiles_scoped_retrieval_before_agent_step(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-block-plan").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-block-plan",
        goal="Use scoped search before reading large files.",
        success_criteria=["Evidence is grounded."],
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use the retrieved evidence.",
            "expected_evidence": "deadline evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "alpha deadline",
                        "expected_role": "evidence_snippet",
                        "filters": {"scope.case_id": "alpha"},
                        "search_surface": "workspace_files",
                        "path": "notes",
                        "pattern": "*.md",
                        "snippet_limit": 64,
                        "max_file_bytes": 4096,
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))

    execution_plan = task._build_blocks_execution_plan(work_unit, plan, cast(Any, context_pack))

    assert [block.kind for block in execution_plan.plan_blocks] == ["workspace_operation", "agent_step"]
    assert execution_plan.plan_blocks[0].bound_inputs["operation"] == "search"
    assert execution_plan.plan_blocks[0].bound_inputs["query"] == "alpha deadline"
    assert execution_plan.plan_blocks[0].bound_inputs["include_snippets"] is True
    assert execution_plan.plan_blocks[0].bound_inputs["search_surface"] == "workspace_files"
    assert execution_plan.plan_blocks[0].bound_inputs["path"] == "notes"
    assert execution_plan.plan_blocks[0].bound_inputs["pattern"] == "*.md"
    assert execution_plan.plan_blocks[0].bound_inputs["max_file_bytes"] == 4096
    compact_plan = task._compact_execution_plan_for_meta(execution_plan)
    assert compact_plan["plan_blocks"][0]["bound_inputs"]["operation"] == "search"
    assert compact_plan["plan_blocks"][0]["bound_inputs"]["query"] == "alpha deadline"
    assert compact_plan["plan_blocks"][0]["bound_inputs"]["filters"] == {
        "scope.case_id": "alpha",
    }
    assert execution_plan.edges[0].from_plan_block == execution_plan.plan_blocks[0].id
    assert execution_plan.edges[0].to_plan_block == execution_plan.plan_blocks[1].id
    assert execution_plan.edges[0].binding["target_input"] == "scoped_retrieval_results"


def test_block_carrier_keeps_file_path_out_of_record_filters_for_mixed_retrieval(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-mixed-path").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-mixed-path",
        goal="Search records and files without suppressing records by file path.",
        success_criteria=["Evidence is grounded."],
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use mixed Workspace evidence.",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "alpha deadline",
                        "expected_role": "evidence_snippet",
                        "search_surface": "workspace_index_and_files",
                        "collection": "observations",
                        "path": "notes",
                        "pattern": "*.md",
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))

    execution_plan = task._build_blocks_execution_plan(work_unit, plan, cast(Any, context_pack))
    inputs = execution_plan.plan_blocks[0].bound_inputs

    assert inputs["path"] == "notes"
    assert inputs["pattern"] == "*.md"
    assert inputs["filters"] == {"collection": "observations"}


def test_block_carrier_model_hot_snippets_preserve_projection_metadata():
    snippets = AgentTask._model_hot_evidence_snippets(
        [
            {
                "role": "evidence_snippet",
                "content_state": "projected_from_raw_record",
                "record_id": "rec_123",
                "collection": "support-intel",
                "kind": "credit_policy",
                "content": "Credit policy: service credits may not exceed 15 percent.",
                "raw_chars": 1200,
                "projected_chars": 64,
                "projection": {
                    "strategy": "deterministic_structured_projection",
                    "raw_chars": 1200,
                    "projected_chars": 64,
                    "omitted_keys": ["audit", "source_system"],
                    "raw_content_state": "raw_readback_available",
                },
                "original_ref": {
                    "record_id": "rec_123",
                    "collection": "support-intel",
                    "kind": "credit_policy",
                    "path": "support-intel/rec_123.json",
                    "size": 1200,
                    "content_state": "raw_readback_available",
                },
            }
        ]
    )

    assert snippets[0]["content_state"] == "projected_from_raw_record"
    assert snippets[0]["projection"]["strategy"] == "deterministic_structured_projection"
    assert snippets[0]["projection"]["omitted_keys"] == ["audit", "source_system"]
    assert snippets[0]["original_ref"]["record_id"] == "rec_123"
    assert snippets[0]["original_ref"]["content_state"] == "raw_readback_available"


def test_block_carrier_passes_workspace_retrieve_options(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-options").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-options",
        goal="Use Workspace retrieve options.",
        success_criteria=["Evidence is grounded."],
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use reranked tagged Workspace evidence.",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "alpha deadline",
                        "expected_role": "evidence_snippet",
                        "filters": {"collection": "observations"},
                        "tags": ["alpha", "deadline"],
                        "method": "hybrid",
                        "selection": "top_n",
                        "top_n": 2,
                        "rerank": False,
                        "max_candidates": 9,
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))

    execution_plan = task._build_blocks_execution_plan(work_unit, plan, cast(Any, context_pack))
    inputs = execution_plan.plan_blocks[0].bound_inputs

    assert inputs["tags"] == ["alpha", "deadline"]
    assert inputs["method"] == "hybrid"
    assert inputs["selection"] == "top_n"
    assert inputs["top_n"] == 2
    assert inputs["rerank"] is False
    assert inputs["max_candidates"] == 9


def test_block_carrier_normalizes_singleton_record_filters(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-filter-normalization").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-filter-normalization",
        goal="Use record filters.",
        success_criteria=["Evidence is grounded."],
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Search retained notes.",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "Project Atlas",
                        "expected_role": "evidence_snippet",
                        "search_surface": "workspace_index",
                        "filters": {"collection": ["retained-notes"]},
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))

    execution_plan = task._build_blocks_execution_plan(work_unit, plan, cast(Any, context_pack))

    assert execution_plan.plan_blocks[0].bound_inputs["filters"]["collection"] == "retained-notes"


@pytest.mark.asyncio
async def test_block_carrier_executes_scoped_retrieval_and_injects_results(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-block-exec").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-block-exec",
        goal="Use scoped search before reading large files.",
        success_criteria=["Evidence is grounded."],
    )
    await task.workspace.put(
        content="Alpha deadline is 2026-07-01. Use this bounded evidence.",
        collection="observations",
        kind="note",
        summary="alpha deadline note",
        scope={"case_id": "alpha"},
    )
    await task.workspace.put(
        content="Beta deadline is unrelated.",
        collection="observations",
        kind="note",
        summary="beta deadline note",
        scope={"case_id": "beta"},
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use the retrieved evidence.",
            "expected_evidence": "deadline evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "deadline",
                        "expected_role": "evidence_snippet",
                        "filters": {"scope.case_id": "alpha"},
                        "snippet_limit": 48,
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))
    seen: dict[str, Any] = {}

    async def handler(block_context: Mapping[str, Any]) -> dict[str, Any]:
        scoped_results = task._scoped_retrieval_results_from_block_context(block_context)
        evidence_ledger = task._evidence_ledger_from_block_context(block_context)
        seen["scoped_results"] = scoped_results
        seen["evidence_ledger"] = evidence_ledger
        return {
            "execution_result": {
                "candidate_final_result": "Alpha deadline found.",
                "scoped_retrieval_results": scoped_results,
                "evidence_use": [
                        {
                            "claim": "Alpha deadline found.",
                            "evidence_ids": [evidence_ledger["items"][2]["reference_id"]],
                            "support_type": "content",
                        }
                ],
            },
            "execution_meta": {
                "execution_id": "scoped-retrieval-child",
                "status": "completed",
                "route": {"selected_route": "test", "status": "completed"},
                "logs": {"action_logs": [], "route_logs": {}, "errors": []},
            },
        }

    execution_result, execution_meta, _work_unit_result = await task._run_work_unit_through_blocks(
        work_unit=work_unit,
        plan=plan,
        context_pack=cast(Any, context_pack),
        execution_id="scoped-retrieval-block-exec-run",
        handler=handler,
        start_payload={"test": True},
    )

    scoped_results = seen["scoped_results"]
    evidence_ledger = seen["evidence_ledger"]
    assert len(scoped_results) == 1
    assert scoped_results[0]["query"] == "deadline"
    assert scoped_results[0]["bounded"]["retrieval_strategy"] == "workspace.retrieve"
    assert scoped_results[0]["bounded"]["returned_results"] == 1
    assert [item["kind"] for item in evidence_ledger["items"][:3]] == [
        "workspace_operation.search",
        "locator_ref",
        "evidence_snippet",
    ]
    assert evidence_ledger["items"][1]["body_state"] == "ref_only"
    assert evidence_ledger["items"][2]["body_state"] in {"bounded", "truncated"}
    snippet = scoped_results[0]["evidence_snippets"][0]
    assert snippet["role"] == "evidence_snippet"
    assert snippet["content"].startswith("Alpha deadline")
    assert "semantically_relevant" not in scoped_results[0]
    assert execution_result["scoped_retrieval_results"][0]["evidence_snippets"][0]["content"].startswith(
        "Alpha deadline"
    )
    block_kinds = [block["kind"] for block in execution_meta["blocks"]["execution_block_graph"]["execution_blocks"]]
    assert block_kinds == ["workspace_operation", "agent_step"]
    block_evidence_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    assert evidence_ledger["items"][2]["reference_id"].startswith("ref_")
    assert any(item["id"] for item in block_evidence_items)
    compact_search_output = execution_meta["blocks"]["evidence"]["execution_block_results"][0]["output"]
    assert compact_search_output["operation"] == "search"
    assert compact_search_output["query"] == "deadline"
    assert compact_search_output["bounded"]["returned_results"] == 1
    assert compact_search_output["evidence_snippet_count"] == 1


@pytest.mark.asyncio
async def test_flat_agent_step_receives_cumulative_and_current_block_reference_ids(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("flat-step-cumulative-reference-projection").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="flat-step-cumulative-reference-projection",
        goal="Use earlier evidence in the current bounded step.",
        success_criteria=["The final result cites canonical task references."],
        execution="flat",
    )
    captured: dict[str, Any] = {}
    prior_ledger = {
        "items": [
            {
                "id": "agent_task_action_result:market:prior",
                "reference_id": "ref_prior",
                "kind": "action_result",
                "status": "ok",
                "body_state": "bounded",
                "action_id": "market",
                "body": "Prior bounded market evidence.",
            }
        ]
    }
    current_block_item = {
        "id": "blocks:snippet:current",
        "kind": "evidence_snippet",
        "status": "ok",
        "body_state": "bounded",
        "path": "notes/current.md",
        "body": "Current bounded Workspace evidence.",
    }
    monkeypatch.setattr(
        cast(Any, task),
        "_cumulative_evidence_ledger",
        lambda _meta: prior_ledger,
    )

    async def run_bounded_step(
        _iteration_index,
        _plan,
        _context_pack,
        *,
        evidence_ledger,
        **_kwargs,
    ):
        captured["evidence_ledger"] = evidence_ledger
        return (
            {"candidate_final_result": "Bounded result."},
            {
                "execution_id": "flat-step-cumulative-reference-projection:child",
                "status": "completed",
                "route": {"selected_route": "model_request", "status": "completed"},
                "logs": {"action_logs": {}, "route_logs": {}, "errors": []},
            },
        )

    async def run_work_unit(*, handler, **_kwargs):
        output = await handler({"state": {"evidence_items": [current_block_item]}})
        return output["execution_result"], output["execution_meta"], None

    monkeypatch.setattr(cast(Any, task), "_run_bounded_agent_execution_step", run_bounded_step)
    monkeypatch.setattr(cast(Any, task), "_run_work_unit_through_blocks", run_work_unit)
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }

    await task._execute_step(
        2,
        {
            "execution_shape": "direct",
            "step_instruction": "Synthesize the final result from prior and current evidence.",
        },
        cast(Any, context_pack),
    )

    offered = captured["evidence_ledger"]["items"]
    reference_ids = [item["reference_id"] for item in offered]
    assert "ref_prior" in reference_ids
    assert any(
        item["kind"] == "evidence_snippet"
        and item["path"] == "notes/current.md"
        and item["reference_id"].startswith("ref_")
        for item in offered
    )
    assert all(set(item).isdisjoint({"id", "evidence_id"}) for item in offered)


@pytest.mark.asyncio
async def test_block_carrier_executes_file_scoped_retrieval_and_injects_results(tmp_path):
    agent = _create_agent("agent-task-file-scoped-retrieval").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="file-scoped-retrieval-block-exec",
        goal="Use scoped file search before reading broad files.",
        success_criteria=["Evidence is grounded."],
    )
    await task.workspace.write_file("notes/alpha.md", "alpha\nrelease deadline is 2026-07-01\n")
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use the retrieved file evidence.",
            "expected_evidence": "deadline evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "deadline",
                        "expected_role": "evidence_snippet",
                        "search_surface": "workspace_files",
                        "path": "notes",
                        "pattern": "*.md",
                        "max_file_bytes": 1024,
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))
    seen: dict[str, Any] = {}

    async def handler(block_context: Mapping[str, Any]) -> dict[str, Any]:
        scoped_results = task._scoped_retrieval_results_from_block_context(block_context)
        seen["scoped_results"] = scoped_results
        return {
            "execution_result": {
                "candidate_final_result": "File deadline found.",
                "scoped_retrieval_results": scoped_results,
            },
            "execution_meta": {
                "execution_id": "file-scoped-retrieval-child",
                "status": "completed",
                "route": {"selected_route": "test", "status": "completed"},
                "logs": {"action_logs": [], "route_logs": {}, "errors": []},
            },
        }

    execution_result, execution_meta, _work_unit_result = await task._run_work_unit_through_blocks(
        work_unit=work_unit,
        plan=plan,
        context_pack=cast(Any, context_pack),
        execution_id="file-scoped-retrieval-block-exec-run",
        handler=handler,
        start_payload={"test": True},
    )

    scoped_results = seen["scoped_results"]
    assert scoped_results[0]["bounded"]["search_surface"] == "workspace_files"
    assert scoped_results[0]["bounded"]["retrieval_strategy"] == "workspace.retrieve"
    assert "search_engines" not in scoped_results[0]["bounded"]
    assert scoped_results[0]["bounded"]["file_returned_results"] == 1
    assert scoped_results[0]["bounded"]["context_lines"] == 3
    assert scoped_results[0]["evidence_snippets"][0]["content"] == "alpha\nrelease deadline is 2026-07-01"
    assert scoped_results[0]["locator_refs"][0]["content_state"] == "ref_only"
    assert execution_result["scoped_retrieval_results"][0]["bounded"]["returned_results"] == 1
    compact_search_output = execution_meta["blocks"]["evidence"]["execution_block_results"][0]["output"]
    assert compact_search_output["operation"] == "search"
    assert compact_search_output["bounded"]["search_surface"] == "workspace_files"
    assert compact_search_output["evidence_snippet_count"] == 1
    compact_operations = execution_meta["block_carrier"]["workspace_operations"]
    assert compact_operations[0]["kind"] == "workspace_operation"
    assert compact_operations[0]["output"]["operation"] == "search"
    assert compact_operations[0]["output"]["bounded"]["returned_results"] == 1
    assert compact_operations[0]["output"]["bounded"]["search_engines"] in (
        ["workspace_file_grep"],
        ["workspace_file_scan"],
    )


@pytest.mark.asyncio
async def test_taskboard_card_scoped_retrieval_uses_block_carrier(tmp_path):
    agent = _create_agent("agent-task-taskboard-scoped-retrieval").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-scoped-retrieval-block-exec",
        goal="Use scoped retrieval inside a TaskBoard card.",
        success_criteria=["Evidence is grounded."],
    )
    await task.workspace.write_file("retained/ops-note.md", "Project Atlas owner is Priya Shah.\n")
    card = TaskBoardCard(
        id="collect",
        objective="Find the Atlas owner evidence.",
        allowed_execution_shape="actions",
        metadata={
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "Priya Shah",
                        "expected_role": "evidence_snippet",
                        "search_surface": "workspace_files",
                        "path": "retained",
                        "pattern": "**",
                        "max_results": 2,
                    }
                ]
            }
        },
    )
    plan = task._taskboard_card_carrier_plan(card)
    assert plan["execution_shape"] == "taskboard_card"
    assert plan["step_scope"]["allowed_capability_ids"] == []
    work_unit = WorkUnitIntent(
        id="taskboard:collect:attempt:1",
        origin="taskboard_card",
        objective=card.objective,
        input_payload={
            "card": card.to_dict(),
            "scoped_retrieval": task._taskboard_card_scoped_retrieval(card),
            "retrieval_policy": scoped_retrieval_policy(),
        },
        delivery_contract={"card": card.to_dict()},
        runtime_preferences={
            "handler": "agent_task_bounded_step",
            "preferred_execution_shape": "taskboard_card",
            "strategy": "taskboard",
        },
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    seen: dict[str, Any] = {}

    async def handler(block_context: Mapping[str, Any]) -> dict[str, Any]:
        payload = task._taskboard_card_payload_with_scoped_retrieval_results(
            work_unit.input_payload,
            block_context,
        )
        seen["payload"] = payload
        return {
            "execution_result": {
                "answer": "TaskBoard card used scoped retrieval.",
                "scoped_retrieval_results": payload.get("scoped_retrieval_results", []),
            },
            "execution_meta": {
                "execution_id": "taskboard-scoped-retrieval-child",
                "status": "completed",
                "route": {"selected_route": "test", "status": "completed"},
                "logs": {"action_logs": [], "route_logs": {}, "errors": []},
            },
        }

    execution_result, execution_meta, _work_unit_result = await task._run_work_unit_through_blocks(
        work_unit=work_unit,
        plan=plan,
        context_pack=cast(Any, context_pack),
        execution_id="taskboard-scoped-retrieval-block-exec-run",
        handler=handler,
        start_payload={"test": True},
    )

    assert plan["scoped_retrieval"]["query_groups"][0]["pattern"] == "**"
    scoped_results = seen["payload"]["scoped_retrieval_results"]
    assert scoped_results[0]["bounded"]["search_surface"] == "workspace_files"
    assert scoped_results[0]["bounded"]["returned_results"] == 1
    assert scoped_results[0]["evidence_snippets"][0]["content"] == "Project Atlas owner is Priya Shah."
    assert execution_result["scoped_retrieval_results"][0]["bounded"]["returned_results"] == 1
    block_kinds = [block["kind"] for block in execution_meta["blocks"]["execution_block_graph"]["execution_blocks"]]
    assert block_kinds == ["workspace_operation", "agent_step"]
    compact_operations = execution_meta["block_carrier"]["workspace_operations"]
    assert compact_operations[0]["kind"] == "workspace_operation"
    assert compact_operations[0]["output"]["bounded"]["returned_results"] == 1
    taskboard_compact = task._compact_block_carrier_for_taskboard_meta(
        execution_meta["block_carrier"],
        blocks=execution_meta["blocks"],
    )
    assert taskboard_compact["workspace_operations"][0]["kind"] == "workspace_operation"
    assert taskboard_compact["workspace_operations"][0]["output"]["bounded"]["returned_results"] == 1
    prompt_view = task._compact_taskboard_evidence_view_for_prompt(
        {
            "cards": [
                {
                    "card_id": "collect",
                    "status": "completed",
                    "diagnostics": [{"block_carrier": taskboard_compact}],
                }
            ]
        }
    )
    prompt_operation = prompt_view["cards"][0]["workspace_operations"][0]
    assert "Project Atlas owner is Priya Shah." in prompt_operation["output"]["first_evidence_snippet"]["content"]


def test_workspace_artifact_bounded_step_schema_excludes_long_body_fields():
    schema = AgentTask._bounded_step_output_schema(
        {
            "body_transport": "workspace_artifact",
            "body_uses_output": False,
            "control_format": "json",
        }
    )

    assert "artifact_manifest" in schema
    assert schema["artifact_manifest"][2] is False
    assert schema["evidence"][2] is False
    assert "candidate_final_result" not in schema
    assert "artifact_markdown" not in schema
    assert "file_refs" not in schema
    keys = list(schema)
    assert keys.index("self_check") > keys.index("acceptance_points")
    assert keys.index("short_summary") > keys.index("self_check")
    assert keys.index("progress_message") > keys.index("short_summary")


def test_agent_task_defaults_do_not_apply_resource_caps(tmp_path):
    agent = _create_agent("agent-task-default-resource-caps").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="default-resource-caps",
        goal="Complete the task.",
        success_criteria=["The task is complete."],
    )

    assert task.max_iterations is None
    assert task.limits == {}
    assert task._taskboard_max_ticks() is None
    assert task._taskboard_max_ticks_source() == "unbounded_default"


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="local timezone switching requires time.tzset")
def test_task_context_contract_includes_utc_and_local_time_when_timezone_known(tmp_path):
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        agent = _create_agent("agent-task-context-local-time").use_workspace(tmp_path / "workspace")
        task = AgentTask(
            agent,
            task_id="context-local-time",
            goal="Complete the task.",
            success_criteria=["The task is complete."],
        )
        task.created_at = 0
        task.started_at = None

        contract = task._task_context_contract()

        current_time = contract["current_time"]
        assert current_time["utc"] == "1970-01-01T00:00:00Z"
        assert current_time["local"] == "1970-01-01T08:00:00+08:00"
        assert current_time["timezone"] == "Asia/Shanghai"
        assert "run_date_utc" not in contract
        assert "run_time_utc" not in contract
        assert "run_date_local" not in contract
        assert "run_time_local" not in contract
        assert "model decisions broadly" in contract["temporal_policy"]["general_decision_context"]
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


@pytest.mark.asyncio
async def test_record_observation_projects_action_logs_to_normalized_action_events(tmp_path):
    agent = _create_agent("agent-task-action-observation-events").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="action-observation-events",
        goal="Inspect Workspace evidence with actions.",
        success_criteria=["Action facts are observable."],
    )
    decision_ref = await task._record_decision(
        1,
        {"step_instruction": "Search and read scoped evidence."},
        {
            "goal": task.goal,
            "items": [],
            "omitted": [],
            "diagnostics": {},
            "profile": "test",
        },
    )
    execution_meta = {
        "execution_id": "exec-action-events",
        "status": "completed",
        "route": {"selected_route": "actions"},
        "block_carrier": {
            "work_unit": {
                "id": "iter-1:flat-step",
                "origin": "flat_step",
                "runtime_preferences": {"strategy": "flat"},
            }
        },
        "logs": {
            "action_logs": [
                {
                    "action_id": "grep_workspace",
                    "status": "success",
                    "action_call_id": "call-grep",
                    "kind": "shell_search",
                    "raw": {
                        "kwargs": {"query": "deadline", "scope": "workspace"},
                        "error": "one search backend failed after another backend returned results",
                    },
                    "elapsed_ms": 12,
                    "model_digest": {
                        "result_preview": {
                            "path": "notes.md",
                            "content": "deadline is 2026-07-01",
                        },
                        "result_preview_meta": {"bytes": 24, "truncated": False},
                        "file_refs": [{"path": "notes.md", "sha256": "abc"}],
                    },
                },
                {
                    "action_id": "read_file",
                    "status": "failed",
                    "action_call_id": "call-read",
                    "raw": {"kwargs": {"path": "missing.md"}},
                    "error": "file not found",
                    "retryable": False,
                },
            ],
            "route_logs": {},
        },
    }

    await task._record_observation(
        1,
        plan={"step_instruction": "Search and read scoped evidence."},
        decision_ref=decision_ref,
        execution_result={"step_result": "searched workspace"},
        execution_meta=execution_meta,
    )
    await task._record_observation(
        1,
        plan={"step_instruction": "Search and read scoped evidence."},
        decision_ref=decision_ref,
        execution_result={"step_result": "searched workspace"},
        execution_meta=execution_meta,
    )

    action_items = [item for item in task._stream_items if item.path.startswith("agent_task.action.")]
    started_items = [item for item in action_items if item.path == "agent_task.action.started"]
    completed_items = [item for item in action_items if item.path == "agent_task.action.completed"]
    failed_items = [item for item in action_items if item.path == "agent_task.action.failed"]

    assert len(started_items) == 2
    assert len(completed_items) == 1
    assert len(failed_items) == 1
    assert all((item.meta or {}).get("stream_kind") == "action_observation" for item in action_items)
    grep_started = next(item for item in started_items if item.value["action_id"] == "grep_workspace")
    assert grep_started.value["input_summary"] == {"query": "deadline", "scope": "workspace"}
    assert grep_started.value["work_unit_id"] == "iter-1:flat-step"
    grep_completed = completed_items[0]
    assert grep_completed.value["output_summary"]["path"] == "notes.md"
    assert grep_completed.value["file_refs"][0]["path"] == "notes.md"
    assert grep_completed.value["success"] is True
    assert "error" not in grep_completed.value
    assert any(ref["value"] == "notes.md" for ref in grep_completed.value["source_refs"])
    read_failed = failed_items[0]
    assert read_failed.value["action_id"] == "read_file"
    assert read_failed.value["error"] == "file not found"
    assert read_failed.value["failure_category"] == "execution"
    assert read_failed.value["retryable"] is False


def test_agent_task_explicit_resource_caps_remain_effective(tmp_path):
    agent = _create_agent("agent-task-explicit-resource-caps").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="explicit-resource-caps",
        goal="Complete the task.",
        success_criteria=["The task is complete."],
        max_iterations=2,
        limits={"max_model_requests": 1},
    )
    taskboard_task = AgentTask(
        agent,
        task_id="explicit-taskboard-tick-cap",
        goal="Complete the board task.",
        success_criteria=["The task is complete."],
        max_iterations=2,
        options={"agent_task": {"taskboard_max_ticks": 4}},
    )
    batch_taskboard_task = AgentTask(
        agent,
        task_id="explicit-taskboard-batch-scheduler",
        goal="Complete the batch board task.",
        success_criteria=["The task is complete."],
        options={"agent_task": {"taskboard_scheduler": "batch"}},
    )
    frontier_taskboard_task = AgentTask(
        agent,
        task_id="explicit-taskboard-frontier-scheduler",
        goal="Complete the frontier board task.",
        success_criteria=["The task is complete."],
        options={"agent_task": {"taskboard_scheduler": "frontier"}},
    )

    assert task.max_iterations == 2
    assert task.limits["max_model_requests"] == 1
    assert task._taskboard_max_ticks() == 2
    assert task._taskboard_max_ticks_source() == "explicit_max_iterations"
    assert task._taskboard_scheduler() == "frontier"
    assert taskboard_task._taskboard_max_ticks() == 4
    assert taskboard_task._taskboard_max_ticks_source() == "taskboard_option"
    assert taskboard_task._taskboard_scheduler() == "frontier"
    assert batch_taskboard_task._taskboard_scheduler() == "batch"
    assert frontier_taskboard_task._taskboard_scheduler() == "frontier"


def test_flat_step_plan_preserves_explicit_inline_result_for_required_workspace_deliverable():
    task = AgentTask.__new__(AgentTask)
    task.options = {
        "execution_prompt_snapshot": {
            "input": {
                "case": {
                    "output_contract": {
                        "required_deliverables": [{"path": "final.md"}],
                    }
                }
            }
        }
    }

    plan = task._normalize_step_plan(
        {
            "execution_shape": "direct",
            "step_instruction": "write the final file",
            "expected_evidence": "final.md exists",
            "rationale": "caller requires a file deliverable",
            "deliverable_mode": "inline_final",
        }
    )

    assert plan["deliverable_mode"] == "inline_final"
    assert plan["deliverable_mode_source"] == "planner"
    assert "required_workspace_deliverables" not in plan
    assert "prefer_stream_draft" not in plan


def test_flat_step_plan_infers_workspace_artifact_mode_when_planner_omits_carrier():
    task = AgentTask.__new__(AgentTask)
    task.options = {
        "execution_prompt_snapshot": {
            "input": {
                "case": {
                    "output_contract": {
                        "required_deliverables": [{"path": "final.md"}],
                    }
                }
            }
        }
    }

    plan = task._normalize_step_plan(
        {
            "execution_shape": "direct",
            "step_instruction": "write the final file",
            "expected_evidence": "final.md exists",
            "rationale": "caller requires a file deliverable",
            "deliverable_mode": "",
        }
    )

    assert plan["deliverable_mode"] == "sectioned_workspace_artifact"
    assert plan["deliverable_mode_source"] == "required_workspace_deliverables"
    assert plan["required_workspace_deliverables"] == ["final.md"]
    assert plan["prefer_stream_draft"] is True


def test_flat_step_plan_normalizes_expected_evidence_duplicate_prefix_alias():
    task = AgentTask.__new__(AgentTask)
    task.options = {}

    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "write the final file",
            "expected_expected_evidence": "final.md exists after the Workspace write action",
            "rationale": "the previous step gathered the evidence",
        }
    )

    assert plan["expected_evidence"] == "final.md exists after the Workspace write action"
    assert "expected_expected_evidence" not in plan
    assert plan["normalization_diagnostics"][0]["code"] == "agent_task.flat_plan.expected_evidence_alias"


@pytest.mark.asyncio
async def test_carrier_control_policy_reaches_child_execution_output_format():
    task = AgentTask.__new__(AgentTask)
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, payload: dict[str, Any]) -> None:
        emitted.append((event, payload))

    setattr(task, "_emit", emit)

    class FakeExecution:
        id = "fake-carrier-format-execution"

        def __init__(self, data: Any | None = None) -> None:
            self.data = {"summary": "ok"} if data is None else data
            self.output_format: str | None = None
            self.output_called = False

        def input(self, payload: dict[str, Any]) -> None:
            self.input_payload = payload

        def info(self, payload: dict[str, Any]) -> None:
            self.info_payload = payload

        def language(self, language: str) -> None:
            self.language_value = language

        def instruct(self, instruction: str) -> None:
            self.instruction = instruction

        def output(self, schema: dict[str, Any], *, format: str) -> None:
            self.output_called = True
            self.output_schema = schema
            self.output_format = format

        async def async_get_data(self) -> Any:
            return self.data

        async def async_get_meta(self) -> dict[str, Any]:
            return {"status": "success"}

    execution = FakeExecution()
    carrier_policy = task._carrier_output_policy_from_block_context(
        {"input": {"carrier_output_policy": {"control_format": "xml_field"}}}
    )
    result, meta = await task._run_bounded_child_execution(
        execution=execution,
        language_policy={"language": "en"},
        input_payload={"task_id": "carrier-format"},
        info_payload={"goal": "global orientation"},
        instruction="Return a bounded summary.",
        output_schema={"summary": (str, "bounded summary", True)},
        output_format=task._carrier_control_output_format(carrier_policy),
        started_event="agent_task.test.execution.started",
        started_payload={},
        stream_bridge=lambda _execution: asyncio.sleep(0),
    )

    assert execution.output_format == "xml_field"
    assert execution.output_called is True
    assert execution.input_payload["task_id"] == "carrier-format"
    assert execution.info_payload == {"goal": "global orientation"}
    assert result == {"summary": "ok"}
    assert meta["status"] == "success"
    assert emitted[0][0] == "agent_task.test.execution.started"

    free_text_execution = FakeExecution("natural-language body")
    free_text_policy = {"control_format": None, "body_uses_output": False, "body_transport": "plain_text"}
    result, meta = await task._run_bounded_child_execution(
        execution=free_text_execution,
        language_policy={"language": "en"},
        input_payload={"task_id": "carrier-free-text"},
        instruction="Write the report body.",
        output_schema={"summary": (str, "bounded summary", True)},
        output_format="json",
        use_output=task._carrier_uses_control_output(free_text_policy),
        carrier_output_policy=free_text_policy,
        started_event="agent_task.test.free_text.started",
        started_payload={},
        stream_bridge=lambda _execution: asyncio.sleep(0),
    )

    assert free_text_execution.output_called is False
    assert free_text_execution.output_format is None
    assert free_text_execution.input_payload["carrier_output_policy"]["body_transport"] == "plain_text"
    assert "return the natural-language body directly as plain text" in free_text_execution.instruction
    assert result == "natural-language body"
    assert meta["status"] == "success"


@pytest.mark.asyncio
async def test_taskboard_action_card_separates_work_unit_input_from_task_orientation(
    tmp_path,
    monkeypatch,
):
    requirement = {
        "capability_id": "required_probe_action",
        "capability_kind": "action",
        "kind": "action_succeeded",
        "required": True,
        "source": "criterion",
    }
    agent = _create_agent("agent-taskboard-card-boundary").use_workspace(tmp_path / "workspace")

    @agent.action_func
    def required_probe_action() -> dict[str, bool]:
        return {"ok": True}

    task = AgentTask(
        agent,
        task_id="taskboard-card-boundary",
        goal="Analyze a three-stock portfolio and write a final risk brief.",
        success_criteria=["The final brief applies the portfolio mandate."],
        execution="taskboard",
        options={
            "execution_prompt_snapshot": {"input": {"portfolio": ["NVDA", "AMD", "AVGO"]}},
            "planner_capabilities": [
                {
                    "id": "required_probe_action",
                    "kind": "action",
                    "route": "model_request",
                    "guidance_access": "none",
                    "description": "Produce required probe evidence.",
                }
            ],
            "capability_evidence_requirements": [requirement],
        },
    )
    dependency = TaskBoardCard.from_value(
        {
            "id": "source-context",
            "objective": "Provide the mandate source reference.",
        }
    )
    card = TaskBoardCard.from_value(
        {
            "id": "portfolio-mandate",
            "objective": "Read the portfolio mandate",
            "depends_on": ["source-context"],
            "input_refs": ["skills/portfolio-analysis/SKILL.md"],
            "required_outputs": ["Mandate constraints are available as evidence"],
            "allowed_execution_shape": "actions",
            "evidence_contract": {
                "done_when": "Mandate constraints are available as evidence",
                "requires_skill_refs": ["skills/portfolio-analysis/SKILL.md"],
                "capability_evidence_requirements": [requirement],
                "requires_capability_ids": ["required_probe_action"],
            },
            "metadata": {
                "done_when": "Mandate constraints are available as evidence",
                "requires_capability_ids": ["required_probe_action"],
            },
        }
    )
    dependency_result = TaskBoardCardResult(
        card_id="source-context",
        status="completed",
        output_digest="Mandate source is available.",
        preview={
            "status": "completed",
            "answer": "Mandate source is available.",
            "evidence": ["portfolio mandate source"],
        },
        metadata={
            "evidence_ledger": {
                "items": [
                    {
                        "id": "source-context:mandate",
                        "kind": "taskboard_evidence",
                        "status": "ok",
                        "body_state": "bounded",
                        "body": "Portfolio mandate source facts.",
                    }
                ]
            }
        },
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-boundary",
            "graph": {
                "graph_id": "taskboard-card-boundary-graph",
                "cards": [dependency.to_dict(), card.to_dict()],
            },
            "card_results": {
                dependency.id: dependency_result.to_dict(),
            },
        }
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={dependency.id: dependency_result},
        planning_policy=None,
    )
    context_pack = {
        "goal": task.goal,
        "profile": "normal",
        "items": [{"id": "global-orientation", "preview": "whole task context"}],
        "omitted": [],
        "diagnostics": {},
        "skills_context_pack": {
            "skills": [
                {
                    "skill_id": "portfolio-analysis",
                    "guidance": {
                        "path": "SKILL.md",
                        "citation": "skills/portfolio-analysis/SKILL.md",
                        "excerpt": "Read the mandate before analyzing portfolio risk.",
                    },
                    "selected_resources": [],
                }
            ]
        },
    }
    captured: dict[str, Any] = {}

    class FakeExecution:
        id = "card-boundary-child"

        def __init__(self) -> None:
            self.used_action_ids: list[str] = []
            self.required_action_ids: list[str] = []
            self.route_policies: list[dict[str, Any]] = []
            self.request = SimpleNamespace(settings=Settings(name="taskboard-action-card-settings"))

        def use_actions(self, action_ids: list[str]) -> None:
            self.used_action_ids.extend(action_ids)

        def require_actions(self, action_ids: list[str]) -> None:
            self.required_action_ids.extend(action_ids)

        def route_policy(self, policy: dict[str, Any]) -> None:
            self.route_policies.append(policy)

    child_execution = FakeExecution()

    async def capture_bounded_child(**kwargs):
        captured.update(kwargs)
        return (
            {
                "status": "completed",
                "answer": "Mandate constraints are available.",
                "evidence": ["portfolio mandate source"],
                "remaining_work": [],
            },
            {
                "execution_id": "card-boundary-child",
                "status": "success",
                "route": {"selected_route": "model_request", "status": "completed"},
                "logs": {"action_logs": {}, "route_logs": {}, "errors": []},
                "diagnostics": [],
            },
        )

    async def pass_through_blocks(*_args, **kwargs):
        captured["work_unit"] = kwargs["work_unit"]
        captured["carrier_plan"] = kwargs["plan"]
        handler_result = await kwargs["handler"]({"input": {}})
        return (
            handler_result["execution_result"],
            handler_result["execution_meta"],
            {},
        )

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        cast(Any, task),
        "_create_bounded_child_execution",
        lambda **_kwargs: child_execution,
    )
    monkeypatch.setattr(
        cast(Any, task),
        "_run_bounded_child_execution",
        capture_bounded_child,
    )
    monkeypatch.setattr(
        cast(Any, task),
        "_run_work_unit_through_blocks",
        pass_through_blocks,
    )
    monkeypatch.setattr(cast(Any, task), "_emit", noop)
    monkeypatch.setattr(cast(Any, task), "_emit_action_observation_events", noop)
    # This test isolates the fallback child-carrier prompt boundary. Direct and
    # one-request Action lowering have dedicated tests above.
    monkeypatch.setattr(
        cast(Any, task),
        "_try_taskboard_narrow_action_command_request",
        noop,
    )

    result = await task._run_taskboard_agent_card(context, cast(Any, context_pack))

    assert result.status == "completed"
    assert "goal" not in captured["input_payload"]
    assert "success_criteria" not in captured["input_payload"]
    assert "context_pack" not in captured["input_payload"]
    assert "execution_prompt" not in captured["input_payload"]
    assert captured["input_payload"]["card"]["id"] == "portfolio-mandate"
    assert captured["input_payload"]["work_unit_boundary"] == {
        "card_id": "portfolio-mandate",
        "objective": "Read the portfolio mandate",
        "done_when": ["Mandate constraints are available as evidence"],
        "whole_task_completion_out_of_scope": True,
    }
    assert captured["info_payload"]["goal"] == task.goal
    assert captured["info_payload"]["success_criteria"] == task.success_criteria
    assert "context_pack" in captured["info_payload"]
    assert "execution_prompt" in captured["info_payload"]
    assert captured["input_payload"]["dependency_results"]
    assert captured["input_payload"]["evidence_ledger"]
    assert captured["input_payload"]["evidence_ledger"]["items"]
    for evidence_item in captured["input_payload"]["evidence_ledger"]["items"]:
        assert evidence_item["reference_id"].startswith("ref_")
        assert "id" not in evidence_item
        assert "evidence_id" not in evidence_item
        assert "cite_as" not in evidence_item
        assert "aliases" not in evidence_item
    assert captured["input_payload"]["skill_context_readbacks"][0]["skill_id"] == ("portfolio-analysis")
    skill_evidence = [
        item
        for item in captured["input_payload"]["evidence_ledger"]["items"]
        if item.get("kind") == "skill_context.readback"
    ]
    assert len(skill_evidence) == 1
    assert skill_evidence[0]["reference_id"].startswith("ref_")
    assert "Read the mandate" in skill_evidence[0]["body_preview"]
    work_unit = cast(WorkUnitIntent, captured["work_unit"])
    carrier_plan = cast(dict[str, Any], captured["carrier_plan"])
    assert list(work_unit.evidence_requirements) == [
        {
            "required_output": "Mandate constraints are available as evidence",
            "source": "taskboard_card",
        },
        requirement,
    ]
    assert list(work_unit.capability_scope) == [
        {
            "capability_id": "required_probe_action",
            "capability_kind": "action",
            "source": "taskboard_card",
        }
    ]
    assert carrier_plan["execution_shape"] == "actions"
    assert carrier_plan["effective_execution_shape"] == "actions"
    assert carrier_plan["step_scope"]["allowed_capability_ids"] == ["required_probe_action"]
    assert carrier_plan["required_action_ids"] == ["required_probe_action"]
    assert child_execution.used_action_ids == ["required_probe_action"]
    assert child_execution.required_action_ids == ["required_probe_action"]
    assert child_execution.request.settings.get("action.loop.max_rounds") == 1
    assert child_execution.request.settings.get("tool.loop.max_rounds") == 1
    assert child_execution.route_policies[-1]["allowed_routes"] == ["model_request"]


@pytest.mark.asyncio
async def test_taskboard_workspace_artifact_action_card_dispatches_without_actionloop(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspace"
    agent = _create_agent("agent-taskboard-direct-artifact-action").use_workspace(
        workspace_root,
        mode="read_write",
    )
    agent.enable_workspace_file_actions(
        root=workspace_root,
        read=True,
        write=True,
        search=False,
        list_files=False,
        expose_to_model=True,
    )
    task = AgentTask(
        agent,
        task_id="taskboard-direct-artifact-action",
        goal="Write the completed report to final.md.",
        success_criteria=["final.md contains the completed report."],
        execution="taskboard",
        options={
            "agent_task": {"required_deliverables": [{"path": "final.md"}]},
            "capability_evidence_requirements": [
                {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"},
                {"capability_id": "read_file", "capability_kind": "action", "kind": "action_succeeded"},
            ],
        },
    )
    source_path = "working/taskboard/synthesize/final.md"
    source_body = "# Final report\n\nCanonical synthesized body.\n"
    await task.workspace.write_file(source_path, source_body)
    dependency = TaskBoardCard.from_value(
        {"id": "synthesize", "objective": "Synthesize the final report."}
    )
    dependency_result = TaskBoardCardResult(
        card_id=dependency.id,
        status="completed",
        file_refs=(
            {
                "path": source_path,
                "role": "workspace_artifact",
                "available": True,
            },
        ),
    )
    card = TaskBoardCard.from_value(
        {
            "id": "write_output",
            "objective": "Materialize the synthesized report.",
            "depends_on": [dependency.id],
            "allowed_execution_shape": "actions",
            "required_outputs": ["final.md is written and read back."],
            "evidence_contract": {
                "requires_capability_ids": ["write_file"],
                "capability_evidence_requirements": [
                    {
                        "capability_id": "write_file",
                        "capability_kind": "action",
                        "kind": "action_succeeded",
                    }
                ],
            },
            "metadata": {"final_workspace_deliverables": ["final.md"]},
        }
    )
    revision = TaskBoardRevision.create(
        board_id=task.id,
        graph=TaskBoardGraph.from_value(
            {"graph_id": f"{task.id}.graph", "cards": [dependency.to_dict(), card.to_dict()]}
        ),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={dependency.id: dependency_result},
        planning_policy=None,
    )
    assert task._set_taskboard_planned_workspace_deliverables(revision) == ["final.md"]
    assert task._required_workspace_deliverables() == ["final.md"]
    assert task._taskboard_card_action_requirements(card)[0]["capability_id"] == "write_file"
    assert task.agent.action.action_registry.get_spec("write_file")["meta"]["write"] is True
    assert task._task_contract_required_action_ids() >= {"write_file", "read_file"}

    action_events: list[Any] = []

    async def capture_action_event(event):
        if event.event_type in {"action.started", "action.completed", "action.failed"}:
            action_events.append(event)

    hook_name = "test.taskboard.workspace_artifact_action_events"
    Agently.event_center.register_hook(capture_action_event, hook_name=hook_name)
    try:
        direct = await task._try_taskboard_workspace_artifact_action_transfer(context)
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert direct is not None
    card_output, execution_meta = direct
    assert card_output["status"] == "completed"
    assert card_output["artifact_manifest"]["path"] == "final.md"
    assert task.workspace.inspect_file("final.md")["sha256"] == task.workspace.inspect_file(source_path)["sha256"]
    action_logs = execution_meta["logs"]["action_logs"]
    assert [record["action_id"] for record in action_logs] == ["write_file", "read_file"]
    assert all(record["status"] in {"success", "succeeded"} for record in action_logs)
    assert execution_meta["route"]["selected_route"] == "action_call"
    assert execution_meta["diagnostics"][0]["action_planning_model_requests"] == 0
    assert [event.event_type for event in action_events] == [
        "action.started",
        "action.completed",
        "action.started",
        "action.completed",
    ]
    assert [event.payload["action_name"] for event in action_events] == [
        "write_file",
        "write_file",
        "read_file",
        "read_file",
    ]

    async def fail_if_child_agent_runs(*_args, **_kwargs):
        raise AssertionError("direct artifact transfer must not start a child AgentExecution")

    monkeypatch.setattr(cast(Any, task), "_run_bounded_child_execution", fail_if_child_agent_runs)
    result = await task._run_taskboard_agent_card(
        context,
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    assert task.workspace.inspect_file("final.md")["sha256"] == task.workspace.inspect_file(source_path)["sha256"]
    assert result.metadata["block_carrier"]["work_unit"]["runtime_preferences"]["plan_block_kind"] == (
        "action_call"
    )
    assert result.metadata["block_carrier"]["block_graph"]["execution_block_kinds"] == [
        "action_call"
    ]


def test_taskboard_planned_workspace_deliverables_reject_escape_paths(tmp_path):
    task = AgentTask(
        _create_agent("agent-taskboard-invalid-final-path").use_workspace(
            tmp_path / "workspace",
            mode="read_write",
        ),
        task_id="taskboard-invalid-final-path",
        goal="Prepare the requested report.",
        success_criteria=["The report is delivered."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.create(
        board_id=task.id,
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "write-output",
                        "objective": "Write the final report.",
                        "metadata": {
                            "final_workspace_deliverables": ["../escape.md", "/tmp/escape.md"]
                        },
                    }
                ],
            }
        ),
    )

    assert task._set_taskboard_planned_workspace_deliverables(revision) == []
    assert task._required_workspace_deliverables() == []
    assert len(task.diagnostics["taskboard_invalid_final_deliverables"]) == 2


@pytest.mark.asyncio
async def test_taskboard_preplanned_action_commands_dispatch_without_actionloop(tmp_path, monkeypatch):
    calls: list[str] = []
    agent = _create_agent("agent-taskboard-preplanned-actions").use_workspace(
        tmp_path / "workspace",
        mode="read_write",
    )

    @agent.action_func
    def market_snapshot(ticker: str) -> dict[str, str]:
        calls.append(ticker)
        return {"ticker": ticker, "status": "ok"}

    task = AgentTask(
        agent,
        task_id="taskboard-preplanned-actions",
        goal="Collect market snapshots.",
        success_criteria=["Snapshots exist for NVDA and AVGO."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "market-data",
            "objective": "Collect the two market snapshots.",
            "allowed_execution_shape": "actions",
            "evidence_contract": {"requires_capability_ids": ["market_snapshot"]},
            "metadata": {
                "action_commands": [
                    {
                        "purpose": "Collect NVDA snapshot.",
                        "action_id": "market_snapshot",
                        "action_input": {"ticker": "NVDA"},
                    },
                    {
                        "purpose": "Collect AVGO snapshot.",
                        "action_id": "market_snapshot",
                        "action_input": {"ticker": "AVGO"},
                    },
                ]
            },
        }
    )

    revision = TaskBoardRevision.create(
        board_id=task.id,
        graph=TaskBoardGraph.from_value(
            {"graph_id": f"{task.id}.graph", "cards": [card.to_dict()]}
        ),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )
    action_events: list[Any] = []

    async def capture_action_event(event):
        if event.event_type in {"action.started", "action.completed", "action.failed"}:
            action_events.append(event)

    hook_name = "test.taskboard.preplanned_action_events"
    Agently.event_center.register_hook(capture_action_event, hook_name=hook_name)
    try:
        direct = await task._try_taskboard_preplanned_action_calls(context)
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert direct is not None
    card_output, execution_meta = direct
    assert card_output["status"] == "completed"
    assert calls == ["NVDA", "AVGO"]
    assert [record["action_id"] for record in execution_meta["logs"]["action_logs"]] == [
        "market_snapshot",
        "market_snapshot",
    ]
    assert execution_meta["route"]["selected_route"] == "action_call"
    assert execution_meta["diagnostics"][0]["action_planning_model_requests"] == 0
    assert [event.event_type for event in action_events] == [
        "action.started",
        "action.started",
        "action.completed",
        "action.completed",
    ]
    assert [event.payload["action_name"] for event in action_events if event.event_type == "action.completed"] == [
        "market_snapshot",
        "market_snapshot",
    ]

    async def fail_if_child_agent_runs(*_args, **_kwargs):
        raise AssertionError("preplanned Action commands must not start a child AgentExecution")

    monkeypatch.setattr(cast(Any, task), "_run_bounded_child_execution", fail_if_child_agent_runs)
    calls.clear()
    result = await task._run_taskboard_agent_card(
        context,
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    assert calls == ["NVDA", "AVGO"]


@pytest.mark.asyncio
async def test_taskboard_preplanned_action_commands_fail_closed_for_unknown_action(tmp_path):
    task = AgentTask(
        _create_agent("agent-taskboard-preplanned-action-unknown").use_workspace(
            tmp_path / "workspace",
            mode="read_write",
        ),
        task_id="taskboard-preplanned-action-unknown",
        goal="Execute the planned Action.",
        success_criteria=["The exact Action succeeds."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "unknown-action",
            "objective": "Execute an unavailable Action.",
            "allowed_execution_shape": "actions",
            "metadata": {
                "action_commands": [
                    {
                        "purpose": "Must fail closed.",
                        "action_id": "missing_action",
                        "action_input": {},
                    }
                ]
            },
        }
    )

    direct = await task._try_taskboard_preplanned_action_calls(
        SimpleNamespace(card=card, dependency_results={})
    )

    assert direct is not None
    card_output, execution_meta = direct
    assert card_output["status"] == "failed"
    assert execution_meta["status"] == "failed"
    assert execution_meta["diagnostics"][0]["code"] == "taskboard.action_commands.unknown_action"
    assert execution_meta["logs"]["action_logs"] == []


@pytest.mark.asyncio
async def test_taskboard_known_action_unknown_args_uses_one_narrow_command_request(
    tmp_path,
    monkeypatch,
):
    calls: list[str] = []
    agent = _create_agent("agent-taskboard-narrow-action-command").use_workspace(
        tmp_path / "workspace",
        mode="read_write",
    )

    @agent.action_func
    def market_snapshot(ticker: str) -> dict[str, str]:
        calls.append(ticker)
        return {"ticker": ticker, "status": "ok"}

    task = AgentTask(
        agent,
        task_id="taskboard-narrow-action-command",
        goal="Collect the ticker supplied by the upstream card.",
        success_criteria=["The market snapshot is collected."],
        execution="taskboard",
    )
    dependency = TaskBoardCard.from_value(
        {"id": "select-ticker", "objective": "Select the ticker."}
    )
    card = TaskBoardCard.from_value(
        {
            "id": "market-data",
            "objective": "Collect the selected ticker's market snapshot.",
            "depends_on": [dependency.id],
            "allowed_execution_shape": "actions",
            "evidence_contract": {"requires_capability_ids": ["market_snapshot"]},
        }
    )
    revision = TaskBoardRevision.create(
        board_id=task.id,
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": f"{task.id}.graph",
                "cards": [dependency.to_dict(), card.to_dict()],
            }
        ),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={
            dependency.id: TaskBoardCardResult(
                card_id=dependency.id,
                status="completed",
                preview={"ticker": "AVGO"},
            )
        },
        planning_policy=None,
    )

    class FakeNarrowRequest:
        def __init__(self):
            self.prompts: dict[str, Any] = {}

        def input(self, value):
            self.prompts["input"] = value
            return self

        def info(self, value):
            self.prompts["info"] = value
            return self

        def instruct(self, value):
            self.prompts["instruct"] = value
            return self

        def output(self, value, *, format=None):
            self.prompts["output"] = value
            self.prompts["output_format"] = format
            return self

        async def async_get_data(self):
            return {
                "action_commands": [
                    {
                        "purpose": "Collect the selected market snapshot.",
                        "action_id": "market_snapshot",
                        "action_input": {"ticker": "AVGO"},
                    }
                ]
            }

    requests: list[FakeNarrowRequest] = []

    def create_fake_request():
        request = FakeNarrowRequest()
        requests.append(request)
        return request

    monkeypatch.setattr(agent, "create_temp_request", create_fake_request)
    monkeypatch.setattr(cast(Any, task), "_apply_language_policy_to_request", lambda *_args, **_kwargs: None)

    async def fail_if_child_agent_runs(*_args, **_kwargs):
        raise AssertionError("known TaskBoard Actions must not start a child AgentExecution")

    monkeypatch.setattr(cast(Any, task), "_run_bounded_child_execution", fail_if_child_agent_runs)
    result = await task._run_taskboard_agent_card(
        context,
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    assert calls == ["AVGO"]
    assert len(requests) == 1
    assert requests[0].prompts["info"]["required_action_ids"] == ["market_snapshot"]
    assert requests[0].prompts["info"]["available_actions"][0]["kwargs"]["ticker"]
    assert requests[0].prompts["output_format"] == "json"


@pytest.mark.asyncio
async def test_taskboard_required_action_unavailable_fails_before_model_request(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-missing-required-action").use_workspace(
        tmp_path / "workspace",
        mode="read_write",
    )
    task = AgentTask(
        agent,
        task_id="taskboard-missing-required-action",
        goal="Collect a required market snapshot.",
        success_criteria=["The required Action succeeds."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "market-data",
            "objective": "Use the required market Action.",
            "allowed_execution_shape": "actions",
            "evidence_contract": {"requires_capability_ids": ["missing_market_snapshot"]},
        }
    )
    context = SimpleNamespace(card=card, dependency_results={}, planning_policy=None)

    def unexpected_request():
        raise AssertionError("Unavailable required Actions must fail before a ModelRequest.")

    monkeypatch.setattr(agent, "create_temp_request", unexpected_request)

    direct = await task._try_taskboard_narrow_action_command_request(
        context,
        card_input_payload={"dependency_results": {}},
    )

    assert direct is not None
    card_output, execution_meta = direct
    assert card_output["status"] == "failed"
    assert execution_meta["status"] == "failed"
    assert execution_meta["diagnostics"][0]["code"] == (
        "taskboard.action_commands.required_action_unavailable"
    )
    assert execution_meta["diagnostics"][0]["action_planning_model_requests"] == 0


@pytest.mark.asyncio
async def test_flat_preplanned_action_commands_dispatch_without_actionloop(tmp_path, monkeypatch):
    calls: list[str] = []
    prepared: dict[str, str] = {}
    agent = _create_agent("agent-flat-preplanned-actions").use_workspace(
        tmp_path / "workspace",
        mode="read_write",
    )

    @agent.action_func
    async def prepare_policy(path: str) -> dict[str, str]:
        await asyncio.sleep(0.02)
        prepared[path] = "bounded policy"
        calls.append(f"prepare:{path}")
        return {"path": path, "status": "prepared"}

    @agent.action_func
    def read_policy(path: str) -> dict[str, str]:
        calls.append(f"read:{path}")
        return {"path": path, "content": prepared[path]}

    task = AgentTask(
        agent,
        task_id="flat-preplanned-actions",
        goal="Read the required policy file.",
        success_criteria=["The prepare_policy and read_policy Actions succeed in order."],
        execution="flat",
    )

    async def fail_if_child_agent_runs(*_args, **_kwargs):
        raise AssertionError("preplanned Flat Action commands must not start a child AgentExecution")

    monkeypatch.setattr(cast(Any, task), "_run_bounded_child_execution", fail_if_child_agent_runs)
    result, execution_meta = await task._execute_step(
        1,
        {
            "execution_shape": "actions",
            "effective_execution_shape": "actions",
            "step_instruction": "Read the required policy file.",
            "required_action_ids": ["prepare_policy", "read_policy"],
            "action_commands": [
                {
                    "purpose": "Prepare the bounded policy file.",
                    "action_id": "prepare_policy",
                    "action_input": {"path": "policy.md"},
                },
                {
                    "purpose": "Read the bounded policy file.",
                    "action_id": "read_policy",
                    "action_input": {"path": "policy.md"},
                }
            ],
        },
        cast(
            Any,
            {
                "goal": task.goal,
                "items": [],
                "omitted": [],
                "profile": "none",
                "diagnostics": {},
            },
        ),
    )

    assert calls == ["prepare:policy.md", "read:policy.md"]
    assert result["status"] == "completed"
    assert execution_meta["route"]["selected_route"] == "action_call"
    assert execution_meta["diagnostics"][0]["action_planning_model_requests"] == 0
    assert execution_meta["diagnostics"][0]["command_source"] == "flat_plan"
    assert execution_meta["diagnostics"][0]["command_concurrency"] == 1
    assert execution_meta["block_carrier"]["work_unit"]["runtime_preferences"][
        "plan_block_kind"
    ] == "action_call"
    assert [
        block["kind"]
        for block in execution_meta["blocks"]["execution_block_graph"]["execution_blocks"]
    ] == ["action_call"]


@pytest.mark.asyncio
async def test_flat_known_action_unknown_args_uses_one_narrow_command_request(
    tmp_path,
    monkeypatch,
):
    calls: list[str] = []
    agent = _create_agent("agent-flat-narrow-action-command").use_workspace(
        tmp_path / "workspace",
        mode="read_write",
    )

    @agent.action_func
    def read_policy(path: str) -> dict[str, str]:
        calls.append(path)
        return {"path": path, "content": "bounded policy"}

    task = AgentTask(
        agent,
        task_id="flat-narrow-action-command",
        goal="Read the policy path selected by the planner.",
        success_criteria=["The read_policy Action succeeds."],
        execution="flat",
    )

    class FakeNarrowRequest:
        def __init__(self):
            self.prompts: dict[str, Any] = {}

        def input(self, value):
            self.prompts["input"] = value
            return self

        def info(self, value):
            self.prompts["info"] = value
            return self

        def instruct(self, value):
            self.prompts["instruct"] = value
            return self

        def output(self, value, *, format=None):
            self.prompts["output"] = value
            self.prompts["output_format"] = format
            return self

        async def async_get_data(self):
            return {
                "action_commands": [
                    {
                        "purpose": "Read the selected policy file.",
                        "action_id": "read_policy",
                        "action_input": {"path": "policy.md"},
                    }
                ]
            }

    requests: list[FakeNarrowRequest] = []

    def create_fake_request():
        request = FakeNarrowRequest()
        requests.append(request)
        return request

    monkeypatch.setattr(agent, "create_temp_request", create_fake_request)
    monkeypatch.setattr(cast(Any, task), "_apply_language_policy_to_request", lambda *_args, **_kwargs: None)

    async def fail_if_child_agent_runs(*_args, **_kwargs):
        raise AssertionError("known Flat Actions must not start a child AgentExecution")

    monkeypatch.setattr(cast(Any, task), "_run_bounded_child_execution", fail_if_child_agent_runs)
    result, execution_meta = await task._execute_step(
        2,
        {
            "execution_shape": "actions",
            "effective_execution_shape": "actions",
            "step_instruction": "Call read_policy with path='policy.md'.",
            "required_action_ids": ["read_policy"],
        },
        cast(
            Any,
            {
                "goal": task.goal,
                "items": [],
                "omitted": [],
                "profile": "none",
                "diagnostics": {},
            },
        ),
    )

    assert calls == ["policy.md"]
    assert result["status"] == "completed"
    assert len(requests) == 1
    assert requests[0].prompts["info"]["required_action_ids"] == ["read_policy"]
    assert [item["name"] for item in requests[0].prompts["info"]["available_actions"]] == [
        "read_policy"
    ]
    assert requests[0].prompts["output_format"] == "json"
    assert execution_meta["route"]["selected_route"] == "action_call"
    assert execution_meta["diagnostics"][0]["action_planning_model_requests"] == 1


@pytest.mark.asyncio
async def test_flat_required_action_unavailable_fails_before_model_request(tmp_path, monkeypatch):
    agent = _create_agent("agent-flat-missing-required-action").use_workspace(
        tmp_path / "workspace",
        mode="read_write",
    )
    task = AgentTask(
        agent,
        task_id="flat-missing-required-action",
        goal="Run the required Action.",
        success_criteria=["The required Action succeeds."],
        execution="flat",
    )

    def unexpected_request():
        raise AssertionError("Unavailable Flat Actions must fail before a ModelRequest")

    monkeypatch.setattr(agent, "create_temp_request", unexpected_request)
    result, execution_meta = await task._execute_step(
        1,
        {
            "execution_shape": "actions",
            "effective_execution_shape": "actions",
            "step_instruction": "Run the unavailable Action.",
            "required_action_ids": ["missing_action"],
        },
        cast(
            Any,
            {
                "goal": task.goal,
                "items": [],
                "omitted": [],
                "profile": "none",
                "diagnostics": {},
            },
        ),
    )

    assert result["status"] == "failed"
    assert execution_meta["status"] == "failed"
    assert execution_meta["diagnostics"][0]["code"] == (
        "agent_task.flat.action_commands.required_action_unavailable"
    )
    assert execution_meta["diagnostics"][0]["action_planning_model_requests"] == 0


def test_blocks_carrier_preserves_work_unit_capability_contract(tmp_path):
    requirement = {
        "capability_id": "required_probe_action",
        "capability_kind": "action",
        "kind": "action_succeeded",
        "required": True,
        "source": "criterion",
    }
    task = AgentTask(
        _create_agent("agent-blocks-work-unit-capability").use_workspace(tmp_path / "workspace"),
        task_id="blocks-work-unit-capability",
        goal="Produce exact Action evidence.",
        success_criteria=["The required Action succeeds."],
    )
    work_unit = WorkUnitIntent(
        id="work-unit-capability",
        origin="taskboard_card",
        objective="Call the required Action.",
        evidence_requirements=(requirement,),
        capability_scope=(
            {
                "capability_id": "required_probe_action",
                "capability_kind": "action",
                "source": "taskboard_card",
            },
        ),
        runtime_preferences={"preferred_execution_shape": "actions"},
    )
    plan = {
        "execution_shape": "actions",
        "effective_execution_shape": "actions",
        "step_scope": {},
    }

    execution_plan = task._build_blocks_execution_plan(
        work_unit,
        plan,
        cast(Any, {"items": [], "profile": "normal"}),
    )
    resolution = task._blocks_capability_resolution(plan, work_unit=work_unit)

    assert list(execution_plan.evidence_requirements) == [requirement]
    assert list(resolution.allowed_capabilities) == ["required_probe_action"]
    assert list(resolution.scoped_action_candidates) == [
        {
            "action_id": "required_probe_action",
            "capability_id": "required_probe_action",
            "source": "AgentTask.work_unit",
        }
    ]


def test_taskboard_prompt_compaction_drops_recursive_block_carrier_payload():
    huge = "x" * 20000
    block_carrier = {
        "work_unit": {
            "id": "taskboard:deliver:attempt:1",
            "origin": "taskboard_card",
            "objective": huge,
            "input_payload": {
                "taskboard_evidence_view": {
                    "cards": [{"diagnostics": [{"block_carrier": {"recursive": huge}}]}],
                }
            },
            "input_refs": [{"artifact_id": "a1", "bytes": 100}],
            "expected_deliverable": {"required_outputs": ["final.md"]},
            "evidence_requirements": [{"required_output": "final.md"}],
        },
        "work_unit_result": {
            "id": "taskboard:deliver:attempt:1",
            "status": "completed",
            "summary": huge,
            "carrier_meta": {
                "snapshot_status": "completed",
                "execution_plan": {"id": "plan", "large": huge},
                "execution_block_graph": {"id": "graph", "large": huge},
            },
        },
        "output_policy": {
            "body_transport": "structured_control",
            "control_format": "json",
        },
    }
    compact_carrier = AgentTask._compact_block_carrier_for_taskboard_meta(block_carrier)
    compact_carrier_text = json.dumps(compact_carrier, ensure_ascii=False)

    assert compact_carrier["work_unit"]["origin"] == "taskboard_card"
    assert compact_carrier["work_unit_result"]["id"] == compact_carrier["work_unit"]["id"]
    assert "input_payload" not in compact_carrier_text
    assert len(compact_carrier_text) < 8000

    evidence_view = {
        "schema_version": "task_board_evidence_view/v1",
        "revision_id": "rev-1",
        "status_counts": {"completed": 1},
        "source_refs": [
            {
                "source_url": "https://example.test/source",
                "title": "Official source",
                "content": huge,
            }
        ],
        "file_refs": [
            {
                "path": "final.md",
                "sha256": "abc123",
                "preview": huge,
                "bytes": len(huge),
            }
        ],
        "cards": [
            {
                "card_id": "deliver",
                "status": "completed",
                "preview": {"answer": huge},
                "source_refs": [
                    {
                        "url": "https://example.test/card-source",
                        "label": "Card source",
                        "content": huge,
                    }
                ],
                "file_refs": [
                    {
                        "path": "evidence/source.md",
                        "sha256": "def456",
                        "preview": huge,
                    }
                ],
                "diagnostics": [{"block_carrier": block_carrier}],
                "metadata": {"block_carrier": block_carrier},
            }
        ],
    }
    compact_view = AgentTask._compact_taskboard_evidence_view_for_prompt(evidence_view)
    compact_view_text = json.dumps(compact_view, ensure_ascii=False)

    assert len(compact_view_text) < 10000
    assert len(huge) > len(compact_view_text)
    assert "taskboard:deliver:attempt:1" in compact_view_text
    assert "https://example.test/source" in compact_view_text
    assert "https://example.test/card-source" in compact_view_text
    assert "final.md" in compact_view_text
    assert "evidence/source.md" in compact_view_text
    assert "input_payload" not in compact_view_text
    assert huge not in compact_view_text


def test_block_carrier_hot_metadata_compacts_recursive_payload():
    huge = "x" * 50000
    work_unit = WorkUnitIntent(
        id="iter-1:flat-step",
        origin="flat_step",
        objective=huge,
        input_payload={"recursive": {"execution_meta": {"block_carrier": {"large": huge}}}},
        delivery_contract={
            "deliverable_mode": "workspace_artifact",
            "execution_prompt": {"output": {"final": (str,)}, "output_format": "json"},
        },
        runtime_preferences={"handler": "agent_task_bounded_step", "strategy": "flat", "step_plan": "direct"},
    )
    work_unit_result = WorkUnitResult(
        id=work_unit.id,
        status="completed",
        summary={"large": huge},
        artifact_manifest={"path": "final.md", "sha256": "abc"},
        evidence=(huge,),
        carrier_meta={
            "execution_plan": {"large": huge},
            "execution_block_graph": {"large": huge},
            "snapshot": {"large": huge},
        },
    )
    output_policy = select_carrier_output_policy(work_unit)
    snapshot = {
        "status": "completed",
        "blocks": {
            "status": "completed",
            "replan_signals": [],
            "execution_block_results": [{"output": {"execution_meta": {"recursive": huge}}}],
        },
    }
    block_result = {"semantic_outputs": {"step": {"large": huge}}, "status": "completed"}
    block_carrier = AgentTask._compact_block_carrier_for_meta(
        work_unit=work_unit,
        work_unit_result=work_unit_result,
        output_policy=output_policy,
        block_result=block_result,
        snapshot=snapshot,
    )

    class FakeExecutionPlan:
        def to_dict(self) -> dict[str, Any]:
            return {
                "plan_id": "plan-1",
                "task_frame_id": "frame-1",
                "plan_blocks": [
                    {
                        "id": "agent-step",
                        "plan_block_id": "agent_step",
                        "kind": "agent_step",
                        "intent": huge,
                        "bound_inputs": {
                            "task_id": "task-1",
                            "step_plan": "direct",
                            "work_unit": work_unit.to_dict(),
                            "plan": {"large": huge},
                        },
                    }
                ],
            }

    class FakeExecutionGraph:
        def to_dict(self) -> dict[str, Any]:
            return {
                "execution_id": "exec-1",
                "execution_blocks": [
                    {
                        "id": "agent-step",
                        "kind": "agent_step",
                        "bound_inputs": {"large": huge},
                    }
                ],
            }

    class FakeEvidence:
        def to_dict(self) -> dict[str, Any]:
            return {
                "execution_block_results": [
                    {
                        "id": "agent-step",
                        "kind": "agent_step",
                        "status": "completed",
                        "output": {
                            "execution_result": {"large": huge},
                            "execution_meta": {
                                "execution_id": "child-1",
                                "status": "completed",
                                "route": {"selected_route": "model_request", "status": "completed"},
                                "block_carrier": {"recursive": huge},
                            },
                        },
                    }
                ],
            }

    execution_meta: dict[str, Any] = {}
    AgentTask._attach_blocks_evidence(
        execution_meta,
        execution_plan=FakeExecutionPlan(),
        execution_graph=FakeExecutionGraph(),
        evidence=FakeEvidence(),
        block_result=block_result,
        snapshot=snapshot,
    )
    execution_meta["block_carrier"] = block_carrier
    hot_text = json.dumps(execution_meta, ensure_ascii=False)

    assert execution_meta["block_carrier"]["work_unit"]["origin"] == "flat_step"
    assert execution_meta["block_carrier"]["work_unit_result"]["id"] == work_unit.id
    assert execution_meta["blocks"]["execution_plan"]["plan_blocks"][0]["kind"] == "agent_step"
    assert execution_meta["blocks"]["execution_plan"]["plan_blocks"][0]["bound_inputs"]["step_plan"] == "direct"
    assert execution_meta["blocks"]["execution_block_graph"]["execution_blocks"][0]["kind"] == "agent_step"
    assert execution_meta["blocks"]["evidence"]["execution_block_results"][0]["kind"] == "agent_step"
    assert execution_meta["blocks"]["result"]["semantic_outputs"]
    assert "input_payload" not in hot_text
    assert "carrier_meta" not in hot_text
    assert len(hot_text) < 20000


def test_agent_task_hot_path_compaction_omits_provider_request_payloads():
    secret = "SECRET_REQUEST_DATA_SHOULD_STAY_COLD"
    request_payload = {
        "messages": [{"role": "user", "content": secret}],
        "tools": [{"name": "oversized_tool_schema", "description": secret}],
    }

    meta_value = AgentTask._compact_value_for_meta(
        {
            "status": "failed",
            "request_data": request_payload,
            "nested": {
                "provider_request": {
                    "request_payload": request_payload,
                }
            },
        }
    )
    verifier_value = AgentTask._compact_verifier_prompt_value(
        {
            "status": "failed",
            "raw_request": request_payload,
            "message": f"Status Code: 403\nRequest Data: {json.dumps(request_payload)}",
        }
    )
    action_preview = AgentTask._compact_action_preview_value(
        {
            "ok": False,
            "prompt_data": request_payload,
        },
        max_chars=1200,
    )
    hot_text = json.dumps(
        {
            "meta": meta_value,
            "verifier": verifier_value,
            "action_preview": action_preview,
        },
        ensure_ascii=False,
    )

    assert secret not in hot_text
    assert "messages" not in json.dumps(meta_value["request_data"], ensure_ascii=False)
    assert meta_value["request_data"]["reason"] == "provider_request_payload_hot_path"
    assert meta_value["nested"]["provider_request"]["reason"] == "provider_request_payload_hot_path"
    assert verifier_value["raw_request"]["reason"] == "provider_request_payload_hot_path"
    assert verifier_value["message"]["reason"] == "provider_request_payload_hot_path"
    assert action_preview["prompt_data"]["reason"] == "provider_request_payload_hot_path"


def test_agent_task_compacts_grounding_guard_for_verifier_prompt():
    refs = [
        {
            "id": f"evidence-{index}",
            "cite_as": f"e{index}",
            "kind": "workspace_artifact.acceptance_locator",
            "status": "ok",
            "body_state": "ref_only",
            "path": "final.md",
            "aliases": [f"alias-{index}-{alias}" for alias in range(10)],
        }
        for index in range(30)
    ]
    valid_guard = {
        "schema_version": "evidence_use_guard/v1",
        "valid": True,
        "blocking_count": 0,
        "diagnostics": [],
        "checked_claims": 4,
        "available_evidence_ids": [ref["id"] for ref in refs],
        "available_evidence_refs": refs,
        "normalized_evidence_use": [{"claim": "supported", "evidence_ids": ["evidence-1"], "support_type": "content"}],
    }

    compact_valid = AgentTask._compact_grounding_guard_for_verifier(valid_guard)

    assert compact_valid["valid"] is True
    assert compact_valid["available_evidence_count"] == 30
    assert "available_evidence_id_sample" not in compact_valid
    assert "available_evidence_refs" not in compact_valid
    assert "normalized_evidence_use" not in compact_valid

    blocking_guard = dict(valid_guard)
    blocking_guard.update(
        {
            "valid": False,
            "blocking_count": 1,
            "diagnostics": [{"code": "evidence_ledger.invalid_evidence_id", "blocking": True}],
        }
    )
    compact_blocking = AgentTask._compact_grounding_guard_for_verifier(blocking_guard)

    assert compact_blocking["valid"] is False
    assert compact_blocking["available_evidence_count"] == 30
    assert compact_blocking["diagnostics"] == [
        {"code": "evidence_ledger.invalid_evidence_id", "blocking": True}
    ]
    assert "available_evidence_refs" not in compact_blocking


def test_agent_task_verifier_evidence_summary_strips_source_selection_ids():
    compact = AgentTask._compact_verifier_evidence_summary(
        {
            "source_refs": [
                {
                    "reference_id": "ref_A",
                    "id": "source-A",
                    "evidence_id": "evidence-A",
                    "cite_as": "e1",
                    "selection_key": "candidate-1",
                    "path": "sources/a.md",
                    "status": "ok",
                    "body_state": "bounded",
                }
            ]
        }
    )

    assert compact == {
        "source_refs": [
            {
                "path": "sources/a.md",
                "status": "ok",
                "body_state": "bounded",
            }
        ]
    }


class MockAgentTaskRequester:
    name = "MockAgentTaskRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}
    calls: list[str] = []
    verification_calls = 0

    def __init_subclass__(cls, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("request_model")
        if original is None:
            return

        async def request_model_with_semantic_contract(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" not in text:
                async for event in original(self, request_data):
                    yield event
                return
            events = [event async for event in original(self, request_data)]
            response_text = "".join(str(data) for event, data in events if event == "message")
            try:
                payload = json.loads(response_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                for event in events:
                    yield event
                return
            if isinstance(payload, Mapping):
                payload = self._complete_semantic_verifier_fixture(text, dict(payload))
            yield "message", json.dumps(payload, ensure_ascii=False)

        cls.request_model = request_model_with_semantic_contract

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def reset():
        MockAgentTaskRequester.calls = []
        MockAgentTaskRequester.verification_calls = 0

    @staticmethod
    def _on_register():
        MockAgentTaskRequester.reset()

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
            request_options={"stream": True},
            request_url="mock://agent-task",
        )

    @staticmethod
    def _complete_semantic_verifier_fixture(
        text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Complete old mock verdicts with the current structural contract.

        This is a deterministic test transport fixture, not semantic proof. The
        dedicated verifier tests provide explicit supported/derived/failed
        claim judgments; lifecycle tests use an empty material-claim audit.
        """

        criterion_ids = list(
            dict.fromkeys(re.findall(r"criterion:\d+", text))
        )
        satisfied = payload.get("is_complete") is True and not payload.get("missing_criteria")
        raw_checks = payload.get("criterion_checks")
        if not isinstance(raw_checks, list) or len(raw_checks) != len(criterion_ids):
            payload["criterion_checks"] = [
                {
                    "criterion_id": criterion_id,
                    "satisfied": satisfied,
                    "summary": str(payload.get("reason") or "Synthetic lifecycle verifier fixture."),
                    "gaps": list(payload.get("missing_criteria") or []) if not satisfied else [],
                    "evidence_ids": [],
                }
                for criterion_id in criterion_ids
            ]
        else:
            normalized_checks: list[dict[str, Any]] = []
            for index, raw_check in enumerate(raw_checks):
                check = dict(raw_check) if isinstance(raw_check, Mapping) else {}
                check["criterion_id"] = criterion_ids[index]
                check.setdefault("satisfied", satisfied)
                check.setdefault("summary", str(payload.get("reason") or "Synthetic lifecycle verifier fixture."))
                check.setdefault("evidence_ids", [])
                normalized_checks.append(check)
            payload["criterion_checks"] = normalized_checks
        payload.setdefault("material_claim_coverage_complete", True)
        payload.setdefault("material_claim_checks", [])
        return payload

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentTaskRequester.calls.append(text)
        if "Summarize AgentTask progress" in text:
            payload = {
                "message": "Progress model summarized the current snapshot.",
            }
            payload_text = json.dumps(payload, ensure_ascii=False)
            midpoint = max(1, len(payload_text) // 2)
            yield "message", payload_text[:midpoint]
            yield "message", payload_text[midpoint:]
            return
        elif "Analyze this task's execution shape for AgentTask strategy resolution" in text:
            payload = {
                "analysis": "This mock task is linear and can stay in the flat loop.",
                "execution_hint": {
                    "recommended_shape": "flat",
                    "confidence": "medium",
                    "reasons": ["one bounded repair loop is enough"],
                    "linear_evidence": ["single deliverable"],
                    "branching_evidence": [],
                    "uncertainty": "",
                },
            }
        elif "Verify the task against every success criterion" in text:
            MockAgentTaskRequester.verification_calls += 1
            if MockAgentTaskRequester.verification_calls == 1:
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "verification evidence is incomplete",
                    "missing_criteria": ["script does not run yet"],
                    "replan_instruction": "run the repair step again with the recorded failure evidence",
                    "final_result": "",
                }
            else:
                final_result = "legacy script upgraded and verified"
                if "summary" in text:
                    final_result = json.dumps(
                        {"summary": "Operator summary for INC-4242."},
                        ensure_ascii=False,
                    )
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "all success criteria are now satisfied",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": final_result,
                }
        elif "Plan the next bounded AgentExecution step" in text:
            payload = {
                "step_instruction": "repair the legacy script using current Agently APIs",
                "expected_evidence": "script execution succeeds",
                "rationale": "the prior failure must be fixed before final verification",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "patched script and ran verification",
                "evidence": ["python legacy_script.py exited with status 0"],
                "remaining_work": [],
            }
        else:
            payload = {"answer": "ok"}
        if "Verify the task against every success criterion" in text:
            payload = self._complete_semantic_verifier_fixture(text, payload)
        yield "message", json.dumps(payload, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text


def _create_agent(name: str = "agent-task-loop-test"):
    settings = Settings(name=f"{name}-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-plugins")
    plugin_manager.register("ModelRequester", MockAgentTaskRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def test_agent_task_terminal_final_result_is_bounded_and_file_body_free(tmp_path):
    agent = _create_agent("agent-task-terminal-result-bounds").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="agent-task-terminal-result-bounds",
        goal="Return a bounded terminal result.",
        success_criteria=["The result remains useful without duplicating file bodies."],
        execution="flat",
    )
    body = "FILE_BODY_MUST_NOT_REACH_TERMINAL_RESULT\n" + ("section body\n" * 800)
    file_ref: WorkspaceFileRef = {
        "path": "reports/final.md",
        "bytes": len(body.encode("utf-8")),
        "sha256": "a" * 64,
        "media_type": "text/markdown",
        "content_kind": "text",
        "role": "workspace_artifact",
    }
    compact = getattr(task, "_compact_terminal_final_result", None)
    assert callable(compact), "AgentTask must own one bounded terminal final_result compactor."

    file_result = cast(Any, compact)(body, trusted_file_refs=[file_ref])
    summary_result = cast(Any, compact)(
        "Compact summary returned separately from the file body.",
        trusted_file_refs=[file_ref],
        preserve_value=True,
    )
    natural_result = cast(Any, compact)("BUSINESS_RESULT_START\n" + ("useful detail\n" * 800))

    assert isinstance(file_result, str)
    assert file_result.startswith("Workspace artifact delivered at reports/final.md")
    assert "FILE_BODY_MUST_NOT_REACH_TERMINAL_RESULT" not in file_result
    assert summary_result == "Compact summary returned separately from the file body."
    assert isinstance(natural_result, Mapping)
    assert natural_result["truncated"] is True
    assert str(natural_result["preview"]).startswith("BUSINESS_RESULT_START")
    assert len(json.dumps(natural_result, ensure_ascii=False)) < 2200


def test_agent_task_file_backed_final_response_ignores_model_body_and_is_byte_bounded(tmp_path):
    agent = _create_agent("agent-task-terminal-response-bounds").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="agent-task-terminal-response-bounds",
        goal="Return a file-backed terminal response.",
        success_criteria=["The response points to the canonical file."],
        execution="flat",
    )
    model_body = "MODEL_FILE_BODY_MUST_NOT_REACH_FINAL_RESPONSE\n" + ("section\n" * 200000)
    file_ref = {
        "path": "reports/final.md",
        "bytes": len(model_body.encode()),
        "sha256": "a" * 64,
        "media_type": "text/markdown",
        "content_kind": "text",
        "role": "workspace_artifact",
    }

    response = task._agent_task_user_final_response(
        final={"final_response": model_body, "final_result": model_body},
        accepted=True,
        artifact_status="accepted",
        final_refs=[file_ref],
        final_result=model_body,
    )

    assert response == "Completed. Deliverable artifact: reports/final.md."
    assert len(response.encode("utf-8")) <= 4096
    assert "MODEL_FILE_BODY_MUST_NOT_REACH_FINAL_RESPONSE" not in response


@pytest.mark.asyncio
async def test_agent_task_terminal_retention_accepts_verified_zero_byte_file(tmp_path):
    agent = _create_agent("agent-task-zero-byte-terminal-file").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="agent-task-zero-byte-terminal-file",
        goal="Retain the intentionally empty marker file.",
        success_criteria=["The empty marker is retained by exact digest."],
        execution="flat",
    )
    write_result = await task.workspace.write_file("reports/empty.txt", "")
    file_ref = cast(
        WorkspaceFileRef,
        {**write_result["file_refs"][0], "role": "workspace_artifact"},
    )

    assert task._trusted_terminal_refs({"artifact_refs": [{**file_ref, "bytes": True}]}) == []
    selected = task._trusted_terminal_refs({"artifact_refs": [file_ref]})
    assert selected == [file_ref]
    promoted = await task._register_terminal_deliverables(selected)

    assert len(promoted) == 1
    assert promoted[0]["path"] == file_ref["path"]
    assert promoted[0]["sha256"] == file_ref["sha256"]
    assert str(promoted[0].get("locator_id") or "").startswith("loc_")
    assert str(promoted[0].get("content_version_id") or "").startswith("cv_")
    assert not (task.workspace.root / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_agent_task_guidance_stays_in_memory_without_workspace_records(tmp_path):
    agent = _create_agent("agent-task-guidance-record").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="guidance-record-task",
        goal="Prepare the incident summary.",
        success_criteria=["The summary uses the operator's latest context."],
        execution="flat",
        max_iterations=1,
    )

    guidance = await task.async_add_guidance(
        "Use the operator's newly uploaded incident note as the primary context.",
        author="operator",
        target="task",
        meta={"source": "test"},
    )

    assert guidance["kind"] == "guidance"
    assert guidance["status"] == "received"
    assert guidance["storage"] == "memory"
    assert "workspace_ref" not in guidance
    assert task.workspace_refs["guidance"] == []
    assert task.guidance_items[0]["id"] == guidance["id"]
    assert task.guidance_items[0]["content"] == (
        "Use the operator's newly uploaded incident note as the primary context."
    )
    assert not (task.workspace.root / ".agently" / "workspace.db").exists()

    guidance_events = [item for item in task._stream_items if item.path == "agent_task.guidance.received"]
    assert guidance_events
    assert isinstance(guidance_events[0].meta, dict)
    assert guidance_events[0].meta["stream_kind"] == "guidance"


@pytest.mark.asyncio
async def test_taskboard_guidance_safe_boundary_keeps_checkpoint_projection_in_memory(tmp_path):
    agent = _create_agent("agent-taskboard-guidance-boundary").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-guidance-boundary",
        goal="Draft the board-backed response.",
        success_criteria=["The board-backed response uses operator context."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the response.",
            "done_when": ["The response reflects the latest operator context."],
        }
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-guidance-boundary",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "taskboard-guidance-boundary-graph",
                "cards": [card.to_dict()],
            }
        ),
        revision_id="rev-0",
    )

    guidance = await task.async_add_guidance(
        "Use the operator's late clarification when drafting the response.",
        author="operator",
        target={"card_id": "draft"},
    )
    applied = await task._apply_guidance_boundary(iteration_index=1, boundary="taskboard_tick")
    record_ref, _checkpoint_ref = await task._record_taskboard_checkpoint(
        stage="tick",
        tick_index=1,
        revision=revision,
    )

    assert applied[0]["id"] == guidance["id"]
    assert task.guidance_items[0]["status"] == "applied"
    assert len(revision.graph.cards) == 1
    assert record_ref is None
    assert _checkpoint_ref is None
    assert task._latest_taskboard_acceptance_index is not None
    assert task._latest_taskboard_acceptance_index["schema_version"]
    assert any(item.path == "agent_task.taskboard.tick_recorded" for item in task._stream_items)
    assert not (task.workspace.root / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_writes_and_readbacks(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-helper")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-helper"
    task.workspace = workspace
    task.diagnostics = {}

    execution_meta = {"logs": {}}
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": "# Actual Report\n\nThis content must exist on disk.",
            "artifact_manifest": {
                "path": "reports/final.md",
                "file_refs": [{"path": "fake-final.md", "sha256": "fake"}],
            },
            "file_refs": [{"path": "model-claimed.md", "sha256": "fake"}],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact",
    )

    assert workspace.resolve_file_path("reports/final.md").read_text(encoding="utf-8").startswith("# Actual Report")
    assert task._workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == "reports/final.md"
    assert delivered["file_refs"][0]["source"] == "test.workspace_artifact"
    assert delivered["artifact_manifest"]["sha256"] == delivered["file_refs"][0]["sha256"]
    assert delivered["diagnostics"][0]["code"] == "agent_task.workspace_artifact.untrusted_model_file_refs"
    assert delivered["artifact_markdown"].startswith("Workspace artifact delivered at reports/final.md")
    assert delivered["artifact_preview"].startswith("# Actual Report")
    assert delivered["workspace_artifact_content_omitted"][0]["field"] == "artifact_markdown"
    assert (
        task._workspace_artifact_display_path(execution_meta["logs"]["artifact_refs"][0]["path"]) == "reports/final.md"
    )
    assert (
        task._workspace_artifact_display_path(execution_meta["workspace_refs"]["agent_task_artifacts"][0]["path"])
        == "reports/final.md"
    )
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    artifact_item = next(item for item in ledger_items if item["kind"] == "workspace_artifact.readback")
    assert artifact_item["status"] == "ok"
    assert task._workspace_artifact_display_path(artifact_item["path"]) == "reports/final.md"
    assert artifact_item["body"].startswith("# Actual Report")


@pytest.mark.asyncio
async def test_workspace_artifact_delivery_records_acceptance_locators_from_actual_artifact(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-locator")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-locator"
    task.workspace = workspace
    task.diagnostics = {}
    task.success_criteria = ["The final report includes the middle evidence section."]

    execution_meta = {"logs": {}}
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": "# Report\n\nIntro.\n\n## Middle Evidence\n\nActual middle content.",
            "artifact_manifest": {"path": "reports/final.md"},
            "acceptance_points": [
                {
                    "criterion": "Middle evidence section is present.",
                    "expected_anchor": "Middle Evidence",
                    "line_start": 999,
                    "evidence_ids": ["source.evidence"],
                }
            ],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact",
    )

    assert delivered["workspace_artifact_delivery"]["acceptance_locator_count"] >= 1
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    locator = next(
        item
        for item in ledger_items
        if item["kind"] == "workspace_artifact.acceptance_locator" and item.get("anchor_text") == "Middle Evidence"
    )
    assert locator["status"] == "ok"
    assert locator["body_state"] == "ref_only"
    assert locator["line_start"] == 5
    assert locator["byte_offset"] < locator["byte_end"]
    assert "body" not in locator
    assert any("workspace_artifact_readback" in item for item in locator["source_evidence_ids"])


@pytest.mark.asyncio
async def test_verifier_workspace_artifact_readback_targets_required_sections(tmp_path):
    agent = _create_agent("agent-task-verifier-targeted-artifact").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="verifier-targeted-artifact",
        goal="Produce a long source-grounded final artifact.",
        success_criteria=["The final artifact includes a source list with concrete refs."],
        workspace=tmp_path / "workspace",
        options={
            "execution_prompt_snapshot": {
                "input": {
                    "case": {
                        "output_contract": {
                            "deliverables": [{"path": "final.md", "media_type": "text/markdown"}],
                            "sections": ["overview", "analysis", "source list"],
                        }
                    }
                }
            }
        },
    )
    filler = "\n".join(f"## Analysis {index}\n\nFiller paragraph {index}." for index in range(650))
    source_section = "\n\n## Source List\n\n- https://example.test/source-a\n- workspace://evidence/ref-a\n"
    body = "# Long Artifact\n\n" + filler + source_section
    write_result = await task.workspace.write_file("final.md", body)
    read_result = await task.workspace.read_file("final.md", max_bytes=4000)
    ref = {
        "path": "final.md",
        "bytes": int(read_result["bytes"]),
        "sha256": read_result["sha256"],
        "media_type": write_result.get("media_type"),
        "content_kind": "text",
        "role": "workspace_artifact",
        "source": "test",
        "preview": str(read_result["content"]),
        "truncated": True,
        "read_bytes": int(read_result["read_bytes"]),
    }

    artifacts = await task._trusted_workspace_artifacts_for_verifier({"artifact_refs": [ref]})

    assert artifacts[0]["readback"]["truncated"] is True
    assert "https://example.test/source-a" not in artifacts[0]["readback"]["content"]
    targeted = artifacts[0]["targeted_readbacks"]
    assert any(item["kind"] == "section_search" and item["query"] == "source list" for item in targeted)
    assert any("https://example.test/source-a" in item.get("content", "") for item in targeted)

    execution_meta = {
        "blocks": {
            "evidence": {
                "evidence_items": [task._workspace_artifact_readback_evidence_item(ref)],
            }
        }
    }
    await task._ensure_workspace_artifact_targeted_readback_evidence(
        execution_meta,
        task._cumulative_evidence_ledger(execution_meta),
    )
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    targeted_items = [item for item in ledger_items if item["kind"] == "workspace_artifact.targeted_readback"]
    assert targeted_items
    assert any("https://example.test/source-a" in item.get("body", "") for item in targeted_items)

    small_body = "# Small Artifact\n\n## Source List\n\n- https://example.test/source-a\n"
    small_write_result = await task.workspace.write_file("small.md", small_body)
    full_read_result = await task.workspace.read_file("small.md", max_bytes=12000)
    full_ref = {
        "path": "small.md",
        "bytes": int(full_read_result["bytes"]),
        "sha256": full_read_result["sha256"],
        "media_type": small_write_result.get("media_type"),
        "content_kind": "text",
        "role": "workspace_artifact",
        "source": "test",
        "truncated": False,
        "read_bytes": int(full_read_result["read_bytes"]),
    }
    full_artifacts = await task._trusted_workspace_artifacts_for_verifier({"artifact_refs": [full_ref]})
    assert "targeted_readbacks" not in full_artifacts[0]


@pytest.mark.asyncio
async def test_verifier_workspace_artifact_readback_uses_acceptance_locator_for_middle_section(tmp_path):
    agent = _create_agent("agent-task-verifier-acceptance-locator").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="verifier-acceptance-locator",
        goal="Produce a long final artifact with a verifiable middle section.",
        success_criteria=["The final artifact includes the target middle section."],
        workspace=tmp_path / "workspace",
    )
    filler = "\n".join(f"Filler paragraph {index}: " + ("x" * 80) for index in range(220))
    marker = "TARGET_MIDDLE_SECTION_MARKER"
    body = f"# Long Report\n\n{filler}\n\n## Target Middle Section\n\n{marker}\n"
    write_result = await task.workspace.write_file("final.md", body)
    read_result = await task.workspace.read_file("final.md", max_bytes=4000)
    ref = {
        "path": "final.md",
        "bytes": int(read_result["bytes"]),
        "sha256": read_result["sha256"],
        "media_type": write_result.get("media_type"),
        "content_kind": "text",
        "role": "workspace_artifact",
        "source": "test",
        "preview": str(read_result["content"]),
        "truncated": True,
        "read_bytes": int(read_result["read_bytes"]),
    }
    locator_items = await task._workspace_artifact_acceptance_locator_evidence_items(
        ref=ref,
        result={
            "acceptance_points": [
                {
                    "criterion": "Target middle section is present.",
                    "expected_anchor": "Target Middle Section",
                }
            ]
        },
        manifest={"path": "final.md"},
        source="test",
        content=body,
    )
    locator = next(item for item in locator_items if item["status"] == "ok")
    assert locator["byte_offset"] > 4000

    execution_meta = {
        "blocks": {
            "evidence": {
                "evidence_items": [
                    task._workspace_artifact_readback_evidence_item(ref),
                    locator,
                ],
            }
        }
    }
    await task._ensure_workspace_artifact_targeted_readback_evidence(
        execution_meta,
        task._cumulative_evidence_ledger(execution_meta),
    )
    targeted_items = [
        item
        for item in execution_meta["blocks"]["evidence"]["evidence_items"]
        if item["kind"] == "workspace_artifact.targeted_readback"
    ]
    assert any(
        item.get("provenance", {}).get("source_evidence_id") == locator["id"] and marker in item.get("body", "")
        for item in targeted_items
    )


@pytest.mark.asyncio
async def test_taskboard_final_artifact_readback_reads_small_tail_sections_for_verifier(tmp_path):
    agent = _create_agent("agent-taskboard-final-small-tail-readback").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-small-tail-readback",
        goal="Verify a small final artifact with required tail sections.",
        success_criteria=["The final artifact includes tail sections."],
        execution="taskboard",
    )
    filler = "\n".join(f"Filler {index}: " + ("x" * 80) for index in range(70))
    body = (
        "# Final Report\n\n"
        f"{filler}\n\n"
        "## Tail Coverage\n\nCoverage table content.\n\n"
        "## Tail Self Check\n\nSelf-check content.\n"
    )
    write_result = await task.workspace.write_file("final.md", body)
    preview_read = await task.workspace.read_file("final.md", max_bytes=4000)
    assert preview_read["truncated"] is True
    ref = {
        "path": "final.md",
        "bytes": int(preview_read["bytes"]),
        "sha256": preview_read["sha256"],
        "media_type": write_result.get("media_type"),
        "content_kind": "text",
        "role": "workspace_artifact",
        "source": "test",
        "preview": str(preview_read["content"]),
        "truncated": True,
        "read_bytes": int(preview_read["read_bytes"]),
    }

    items = await task._taskboard_final_artifact_verification_evidence_items(
        [ref],
        final={
            "artifact_manifest": {
                "path": "final.md",
                "sections": [
                    {"title": "Tail Coverage"},
                    {"title": "Tail Self Check"},
                ],
            }
        },
    )

    readback = next(item for item in items if item["kind"] == "workspace_artifact.readback")
    assert readback["body_state"] == "full"
    assert "Tail Self Check" in readback["body"]
    targeted = [item for item in items if item["kind"] == "workspace_artifact.targeted_readback"]
    assert any("Tail Coverage" in item.get("body", "") for item in targeted)
    assert any("Tail Self Check" in item.get("body", "") for item in targeted)


@pytest.mark.asyncio
async def test_taskboard_required_final_deliverable_promotion_replaces_stale_target(tmp_path):
    agent = _create_agent("agent-taskboard-final-deliverable-promotion").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-deliverable-promotion",
        goal="Produce final.md at the required deliverable path.",
        success_criteria=["final.md contains the complete deliverable body."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    await task.workspace.write_file("final.md", "Summary only.")
    await task.workspace.write_file("working/taskboard/design-questions/final.md", "# Draft\n\nShorter draft.")
    full_body = "# Complete Deliverable\n\n" + "\n".join(
        f"Section {index}: " + ("complete body " * 20) for index in range(80)
    )
    await task.workspace.write_file("working/taskboard/coverage-and-finalize/final.md", full_body)

    async def ref_for(path: str) -> dict[str, Any]:
        read_result = await task.workspace.read_file(path, max_bytes=4000)
        return {
            "path": path,
            "bytes": int(read_result["bytes"]),
            "sha256": str(read_result["sha256"]),
            "media_type": read_result.get("media_type"),
            "content_kind": "text",
            "role": "workspace_artifact",
            "source": f"test.{path}",
            "preview": str(read_result.get("content") or ""),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "truncated": bool(read_result.get("truncated")),
        }

    refs = [
        await ref_for("final.md"),
        await ref_for("working/taskboard/design-questions/final.md"),
        await ref_for("working/taskboard/coverage-and-finalize/final.md"),
    ]

    promoted_refs = await task._taskboard_materialize_required_final_deliverable_refs([*refs, dict(refs[0])])

    final_read = await task.workspace.read_file("final.md", max_bytes=len(full_body.encode("utf-8")) + 1)
    assert final_read["content"] == full_body
    assert task._workspace_artifact_display_path(promoted_refs[0]["path"]) == "final.md"
    assert promoted_refs[0]["source_path"] == "working/taskboard/coverage-and-finalize/final.md"
    assert promoted_refs[0]["sha256"] == final_read["sha256"]
    assert task.diagnostics["taskboard_final_deliverable_promotion"][0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_taskboard_required_final_deliverable_promotion_uses_unique_trusted_source(tmp_path):
    agent = _create_agent("agent-taskboard-final-deliverable-unique-source").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-deliverable-unique-source",
        goal="Produce support_reply.md at the required deliverable path.",
        success_criteria=["support_reply.md contains the completed customer reply."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "support_reply.md"}]}},
    )
    body = "# Support Reply\n\nCompleted customer reply body."
    await task.workspace.write_file("final.md", body)
    read_result = await task.workspace.read_file("final.md", max_bytes=4000)
    refs = [
        {
            "path": "final.md",
            "bytes": int(read_result["bytes"]),
            "sha256": str(read_result["sha256"]),
            "media_type": read_result.get("media_type"),
            "content_kind": "text",
            "role": "workspace_artifact",
            "source": "test.final-md",
            "preview": str(read_result.get("content") or ""),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "truncated": bool(read_result.get("truncated")),
        }
    ]

    promoted_refs = await task._taskboard_materialize_required_final_deliverable_refs([*refs, dict(refs[0])])

    target_read = await task.workspace.read_file("support_reply.md", max_bytes=4000)
    assert target_read["content"] == body
    assert task._workspace_artifact_display_path(promoted_refs[0]["path"]) == "support_reply.md"
    assert promoted_refs[0]["source_path"] == "final.md"
    assert task.diagnostics["taskboard_final_deliverable_promotion"][0]["status"] == "selected"
    assert task.diagnostics["taskboard_final_deliverable_promotion"][1]["status"] == "delivered"


@pytest.mark.asyncio
async def test_taskboard_required_final_deliverable_promotion_uses_repair_source_over_existing_target(tmp_path):
    agent = _create_agent("agent-taskboard-final-deliverable-repair-source").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-deliverable-repair-source",
        goal="Repair incident_learning.md at the required deliverable path.",
        success_criteria=["incident_learning.md contains only grounded incident facts."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "incident_learning.md"}]}},
    )
    stale_body = (
        "# Incident Learning Note\n\n"
        "**Date:** 2026-07-07\n\n"
        "## Open Risks\n\n"
        "The long-term prevention work is not yet implemented. No other risks are currently known.\n"
        + "\n".join(f"Unsupported stale detail {index}." for index in range(20))
    )
    repaired_body = (
        "# Incident Learning Note\n\n"
        "## Open Risks\n\n"
        "The long-term prevention work is identified but not yet implemented.\n"
    )
    await task.workspace.write_file("incident_learning.md", stale_body)
    await task.workspace.write_file("final.md", repaired_body)

    async def ref_for(path: str, source: str) -> dict[str, Any]:
        read_result = await task.workspace.read_file(path, max_bytes=4000)
        return {
            "path": path,
            "bytes": int(read_result["bytes"]),
            "sha256": str(read_result["sha256"]),
            "media_type": read_result.get("media_type"),
            "content_kind": "text",
            "role": "workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "truncated": bool(read_result.get("truncated")),
        }

    promoted_refs = await task._taskboard_materialize_required_final_deliverable_refs(
        [
            await ref_for("incident_learning.md", "agent_task.taskboard.card.write_incident_note.workspace_artifact"),
            await ref_for("final.md", "agent_task.taskboard.card.final-verification-repair.workspace_artifact"),
        ]
    )

    target_read = await task.workspace.read_file("incident_learning.md", max_bytes=4000)
    assert target_read["content"] == repaired_body
    assert task._workspace_artifact_display_path(promoted_refs[0]["path"]) == "incident_learning.md"
    assert promoted_refs[0]["source_path"] == "final.md"
    assert task.diagnostics["taskboard_final_deliverable_promotion"][0]["reason"] == (
        "unique_final_verification_repair_source_for_required_deliverable"
    )
    assert task.diagnostics["taskboard_final_deliverable_promotion"][1]["status"] == "delivered"


@pytest.mark.asyncio
async def test_workspace_intermediate_artifact_stays_ref_backed_without_satisfying_final_contract(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-intermediate")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-intermediate"
    task.workspace = workspace
    task.diagnostics = {}
    task.options = {"agent_task": {"required_deliverables": [{"path": "deliverables/final.md"}]}}

    notes_body = "# Search Notes\n\n" + "\n".join(
        f"- Source note {index}: keep this large intermediate evidence cold." for index in range(20)
    )
    execution_meta = {"logs": {}}
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": notes_body,
            "artifact_manifest": {"path": "working/search-notes.md"},
            "evidence": ["Downloaded source snapshot and summarized it into working notes."],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact.intermediate",
    )

    bounded_read = await workspace.read_file("working/search-notes.md", max_bytes=80)

    assert task._workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == "working/search-notes.md"
    assert (
        task._workspace_artifact_display_path(execution_meta["logs"]["artifact_refs"][0]["path"])
        == "working/search-notes.md"
    )
    assert (
        task._workspace_artifact_display_path(execution_meta["workspace_refs"]["agent_task_artifacts"][0]["path"])
        == "working/search-notes.md"
    )
    assert bounded_read["ok"] is True
    assert bounded_read["truncated"] is True
    assert bounded_read["content"].startswith("# Search Notes")
    assert task._required_workspace_deliverables() == ["deliverables/final.md"]
    assert await task._missing_required_workspace_deliverables() == ["deliverables/final.md"]


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_reports_readback_failure(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-readback-failure")

    class ReadbackFailingWorkspace:
        root = workspace.root

        async def write_file(self, *args: Any, **kwargs: Any) -> Any:
            return await workspace.write_file(*args, **kwargs)

        async def read_file(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("readback unavailable")

    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-readback-failure"
    cast(Any, task).workspace = ReadbackFailingWorkspace()
    task.diagnostics = {}
    execution_meta = {"logs": {}}

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": "# Actual Report\n\nThis content was written but cannot be read back.",
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact",
    )

    assert workspace.resolve_file_path("reports/final.md").is_file()
    assert delivered["file_refs"] == []
    assert "artifact_refs" not in execution_meta["logs"]
    assert delivered["workspace_artifact_delivery"]["status"] == "readback_failed"
    diagnostic = delivered["diagnostics"][0]
    assert diagnostic["code"] == "agent_task.workspace_artifact.readback_failed"
    assert "readback failed" in diagnostic["message"]
    assert "write_failed" not in json.dumps(DataFormatter.sanitize(delivered), ensure_ascii=False)
    assert task.diagnostics["workspace_artifact_delivery"][0]["status"] == "readback_failed"


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_prefers_complete_body(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-complete-body")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-complete-body"
    task.workspace = workspace
    task.diagnostics = {}

    full_body = "# Complete Report\n\n" + "\n".join(f"Section {index}: complete content." for index in range(20))
    delivered = await task._deliver_workspace_artifact(
        {
            "answer": full_body,
            "artifact_markdown": "# Short Report\n\nSee candidate_final_result for details.",
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    written = workspace.resolve_file_path("reports/final.md").read_text(encoding="utf-8")
    assert written == full_body
    assert delivered["workspace_artifact_delivery"]["content_key"] == "answer"
    assert delivered["file_refs"][0]["bytes"] == len(full_body.encode("utf-8"))
    assert delivered["answer"].startswith("Workspace artifact delivered at reports/final.md")
    assert delivered["artifact_preview"].startswith("# Complete Report")


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_compacts_manifest_sections(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-compact-manifest")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-compact-manifest"
    task.workspace = workspace
    task.diagnostics = {}

    long_section = "Section body.\n" * 500
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {
                "path": "reports/final.md",
                "sections": [
                    {"id": "overview", "title": "Overview", "content": long_section},
                    "raw section text\n" * 200,
                ],
            },
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    written = workspace.resolve_file_path("reports/final.md").read_text(encoding="utf-8")
    assert "Section body." in written
    first_section = delivered["artifact_manifest"]["sections"][0]
    second_section = delivered["artifact_manifest"]["sections"][1]
    assert "content" not in first_section
    assert first_section["omitted_content"][0]["field"] == "content"
    assert second_section["content_omitted"] is True
    assert delivered["artifact_preview"].startswith("## Overview")


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_does_not_write_plain_answer_without_mode(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-no-implicit-answer")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-no-implicit-answer"
    task.workspace = workspace
    task.diagnostics = {}

    delivered = await task._deliver_workspace_artifact(
        {"answer": "This is a control-card summary, not a deliverable body."},
        plan={},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    assert delivered["answer"] == "This is a control-card summary, not a deliverable body."
    assert delivered.get("file_refs") == []
    assert not workspace.resolve_file_path("final.md").exists()


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_waits_for_remaining_work(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-remaining-work")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-remaining-work"
    task.workspace = workspace
    task.diagnostics = {}

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "reports/final.md"},
            "remaining_work": ["Read README.md before writing the final report."],
            "step_result": "Repository cloned; detailed source reading remains.",
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    assert delivered["artifact_manifest"]["path"] == "reports/final.md"
    assert delivered["remaining_work"] == ["Read README.md before writing the final report."]
    assert delivered.get("file_refs") == []
    assert "workspace_artifact_delivery" not in delivered
    assert not workspace.resolve_file_path("reports/final.md").exists()


@pytest.mark.asyncio
async def test_taskboard_completed_sufficient_leaf_materializes_declared_path_before_verifier_handoff(tmp_path):
    agent = _create_agent("taskboard-leaf-delivery-handoff").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-leaf-delivery-handoff",
        goal="Write the final report.",
        success_criteria=["The report is available through trusted Workspace readback."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "synthesize",
            "objective": "Synthesize and deliver the final report.",
            "required_outputs": ["final.md"],
            "allowed_execution_shape": "control",
        }
    )
    revision = TaskBoardRevision.create(
        board_id=task.id,
        graph=TaskBoardGraph.from_value(
            {"graph_id": f"{task.id}.graph", "cards": [card.to_dict()]}
        ),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )
    body = "# Final Report\n\nComplete candidate body ready for delivery.\n"
    output = {
        "status": "completed",
        "sufficient": True,
        "next_board_action": "finalize",
        "candidate_final_result": body,
        "artifact_manifest": {"path": "final.md"},
        "remaining_work": ["Materialize and read back final.md before terminal verification."],
        "ready_for_final_verification": False,
    }

    assert task._taskboard_control_output_allows_workspace_delivery(output) is True
    prepared, plan = task._prepare_taskboard_workspace_artifact_delivery(
        output,
        context,
        deliverable_mode="workspace_artifact",
    )
    assert prepared["artifact_manifest"]["path"] == "final.md"

    delivered = await task._deliver_workspace_artifact(
        prepared,
        plan=plan,
        execution_meta={"logs": {}},
        source="test.taskboard.completed_leaf",
        card_context=context,
    )

    assert task.workspace.resolve_file_path("final.md").read_text(encoding="utf-8") == body.strip()
    assert not task.workspace.resolve_file_path("working/taskboard/synthesize/final.md").exists()
    assert task._workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == "final.md"
    assert delivered["remaining_work"] == []
    assert delivered["ready_for_final_verification"] is True
    assert delivered["workspace_artifact_delivery"]["remaining_work_handoff"]["status"] == (
        "handed_to_terminal_verification"
    )


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_adopts_successful_action_written_file(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-action-adopt")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-action-adopt"
    task.workspace = workspace
    task.diagnostics = {}
    task.success_criteria = ["The final artifact is available through trusted Workspace readback."]
    task.options = {}

    body = "# Existing Action Report\n\nThis file was written by a Workspace action before execution stalled.\n"
    await workspace.write_file("final.md", body, append=False)
    execution_meta = {
        "status": "failed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": {
                        "ok": True,
                        "mode": "write",
                        "path": "final.md",
                        "file_refs": [{"path": "final.md", "role": "output"}],
                    },
                    "file_refs": [{"path": "final.md", "role": "output"}],
                }
            ]
        },
        "diagnostics": {
            "errors": [
                {
                    "error_type": "RuntimeStageStallError",
                    "message": "AgentExecution made no progress before idle deadline.",
                    "stage": "action_loop_close",
                }
            ]
        },
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "final.md"},
            "remaining_work": ["Retry or replan after execution failure."],
            "ready_for_final_verification": False,
            "step_result": "",
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="agent_task.iteration.2.workspace_artifact",
    )

    assert delivered["workspace_artifact_delivery"]["status"] == "adopted_existing"
    assert delivered["workspace_artifact_delivery"]["content_key"] == "action_file_ref"
    assert (
        task._workspace_artifact_display_path(delivered["workspace_artifact_delivery"]["readback"]["path"])
        == "final.md"
    )
    assert task._workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == "final.md"
    assert delivered["file_refs"][0]["sha256"]
    assert delivered["artifact_preview"].startswith("# Existing Action Report")
    assert delivered["remaining_work"] == []
    assert delivered["ready_for_final_verification"] is True
    assert delivered["workspace_artifact_remaining_work_handoff"]["status"] == "handed_to_terminal_verification"
    assert delivered["diagnostics"][-1]["code"] == "agent_task.workspace_artifact.action_file_adopted"
    assert task._workspace_artifact_display_path(execution_meta["logs"]["artifact_refs"][0]["path"]) == "final.md"
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    assert any(
        item["kind"] == "workspace_artifact.readback"
        and task._workspace_artifact_display_path(item["path"]) == "final.md"
        for item in ledger_items
    )


@pytest.mark.asyncio
async def test_workspace_artifact_action_write_owns_target_over_model_returned_body(
    tmp_path,
):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-action-owner")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-action-owner"
    task.workspace = workspace
    task.diagnostics = {}
    task.success_criteria = ["The Action-written final artifact is preserved."]
    task.options = {}

    action_body = "# Action-owned report\n\nThe write_file Action produced this body.\n"
    model_body = "# Conflicting model body\n\nThis must not silently overwrite the Action result.\n"
    write_result = await workspace.write_file("final.md", action_body, append=False)
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": write_result,
                    "file_refs": write_result["file_refs"],
                }
            ]
        },
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "status": "completed",
            "candidate_final_result": model_body,
            "artifact_manifest": {"path": "final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact.action_owner",
    )

    assert workspace.resolve_file_path("final.md").read_text(encoding="utf-8") == action_body
    assert delivered["workspace_artifact_delivery"]["status"] == "adopted_existing"
    assert delivered["file_refs"][0]["sha256"] == write_result["sha256"]
    assert any(
        item.get("code")
        == "agent_task.workspace_artifact.action_file_preferred_over_model_body"
        for item in delivered["diagnostics"]
    )


@pytest.mark.asyncio
async def test_agent_task_inline_result_adopts_explicit_action_written_artifact_without_overwrite(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-inline-action-adopt")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-inline-action-adopt"
    task.workspace = workspace
    task.diagnostics = {}
    task.success_criteria = ["The final artifact is available through trusted Workspace readback."]
    task.options = {}

    body = "# Existing Action Report\n\nFile-backed body written by the Action.\n"
    summary = "Compact inline summary returned separately."
    await workspace.write_file("final.md", body, append=False)
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": {
                        "ok": True,
                        "mode": "write",
                        "path": "final.md",
                        "file_refs": [{"path": "final.md", "role": "output"}],
                    },
                    "file_refs": [{"path": "final.md", "role": "output"}],
                }
            ]
        },
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "candidate_final_result": summary,
            "artifact_manifest": {"path": "final.md"},
        },
        plan={"deliverable_mode": "inline_final"},
        execution_meta=execution_meta,
        source="agent_task.iteration.1.workspace_artifact",
    )

    written = workspace.resolve_file_path("final.md").read_text(encoding="utf-8")
    assert written == body
    assert delivered["candidate_final_result"] == summary
    assert delivered["workspace_artifact_delivery"]["status"] == "adopted_existing"
    assert task._workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == "final.md"
    assert delivered["file_refs"][0]["sha256"]


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_does_not_adopt_missing_action_file(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-action-missing")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-action-missing"
    task.workspace = workspace
    task.diagnostics = {}
    task.options = {}

    execution_meta = {
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": {
                        "ok": True,
                        "mode": "write",
                        "path": "final.md",
                        "file_refs": [{"path": "final.md", "role": "output"}],
                    },
                }
            ]
        }
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "final.md"},
            "remaining_work": ["Retry or replan after execution failure."],
            "step_result": "",
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="agent_task.iteration.2.workspace_artifact",
    )

    assert delivered.get("file_refs") == []
    assert delivered["remaining_work"] == ["Retry or replan after execution failure."]
    assert delivered["status"] == "blocked"
    assert delivered["workspace_artifact_delivery"]["status"] == "failed"
    diagnostic_codes = [item["code"] for item in delivered["diagnostics"]]
    assert "agent_task.workspace_artifact.action_file_readback_failed" in diagnostic_codes
    assert (
        "agent_task.workspace_artifact.action_file_owner_readback_failed"
        in diagnostic_codes
    )


@pytest.mark.asyncio
async def test_action_owned_manifest_path_does_not_fall_back_to_another_action_file(
    tmp_path,
):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-exact-owner")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-exact-owner"
    task.workspace = workspace
    task.diagnostics = {}
    task.success_criteria = ["Deliver final.md from its owning file Action."]
    task.options = {}

    other_result = await workspace.write_file(
        "notes.md",
        "# Notes\n\nThis is not the declared deliverable.\n",
        append=False,
    )
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": {
                        "path": "final.md",
                        "bytes": 100,
                        "sha256": "f" * 64,
                    },
                },
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": other_result,
                    "file_refs": other_result["file_refs"],
                },
            ]
        },
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "status": "completed",
            "candidate_final_result": "# Model fallback\n\nMust not be used.\n",
            "artifact_manifest": {"path": "final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact.exact_action_owner",
    )

    assert delivered["status"] == "blocked"
    assert delivered["file_refs"] == []
    assert delivered["workspace_artifact_delivery"]["status"] == "failed"
    assert delivered["artifact_manifest"]["path"] == "final.md"


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_uses_full_markdown_body_from_evidence(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-evidence-body")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-evidence-body"
    task.workspace = workspace
    task.diagnostics = {}

    full_body = "# Corrected Report\n\nThis is the complete file body from structured evidence.\n"
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "reports/final.md", "sections": [{"id": "report"}]},
            "evidence": [
                "Source facts were gathered.",
                f"Corrected reports/final.md content (full body):\n\n{full_body}",
            ],
            "remaining_work": [
                "Write the corrected reports/final.md Workspace artifact using the full body content provided in evidence."
            ],
            "ready_for_final_verification": False,
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.evidence_body",
    )

    written = workspace.resolve_file_path("reports/final.md").read_text(encoding="utf-8")
    assert written == full_body.strip()
    assert delivered["workspace_artifact_delivery"]["status"] == "delivered"
    assert delivered["workspace_artifact_delivery"]["content_key"] == "evidence[1]"
    assert delivered["workspace_artifact_delivery"]["remaining_work_handoff"]["status"] == (
        "handed_to_terminal_verification"
    )
    assert delivered["remaining_work"] == []
    assert delivered["ready_for_final_verification"] is True
    assert delivered["evidence"][1].startswith("Workspace artifact delivered at reports/final.md")
    assert delivered["workspace_artifact_content_omitted"][0]["field"] == "evidence[1]"
    assert delivered["diagnostics"][-1]["code"] == ("agent_task.workspace_artifact.remaining_work_handed_to_verifier")


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_ignores_non_body_evidence_snippets(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-evidence-snippet")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-evidence-snippet"
    task.workspace = workspace
    task.diagnostics = {}

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "reports/final.md", "sections": [{"id": "report"}]},
            "evidence": [
                "Source excerpt:\n\n# Not The Deliverable\n\nThis is a source page title, not an artifact body.",
                {"content": "# Still only an untyped snippet\n\nNo artifact role or path marks this as a body."},
            ],
            "remaining_work": ["Read README.md before writing the final report."],
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.evidence_snippet",
    )

    assert delivered["artifact_manifest"]["path"] == "reports/final.md"
    assert delivered["remaining_work"] == ["Read README.md before writing the final report."]
    assert delivered.get("file_refs") == []
    assert "workspace_artifact_delivery" not in delivered
    assert not workspace.resolve_file_path("reports/final.md").exists()


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_preserves_existing_full_body(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-preserve-body")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-preserve-body"
    task.workspace = workspace
    task.diagnostics = {}

    full_body = "# Complete Report\n\n" + "\n".join(f"Section {index}: complete content." for index in range(80))
    first = await task._deliver_workspace_artifact(
        {
            "answer": full_body,
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.initial",
    )
    short_control_note = "Existing final.md already satisfies the output contract."
    second = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": short_control_note,
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.followup",
    )

    written = workspace.resolve_file_path("reports/final.md").read_text(encoding="utf-8")
    assert written == full_body
    assert first["file_refs"][0]["sha256"] == second["file_refs"][0]["sha256"]
    assert second["workspace_artifact_delivery"]["status"] == "preserved_existing"
    assert second["workspace_artifact_delivery"]["content_key"] == "artifact_markdown"
    assert second["diagnostics"][0]["code"] == "agent_task.workspace_artifact.preserved_existing"


@pytest.mark.asyncio
async def test_agent_task_inline_final_does_not_overwrite_workspace_artifact(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-inline-result")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-inline-result"
    task.workspace = workspace
    task.diagnostics = {}

    report = "# Complete Report\n\nThis is the file-backed deliverable."
    summary = "Compact returned summary; this is not the file-backed report body."
    await workspace.write_file("final.md", report)

    delivered = await task._deliver_workspace_artifact(
        {
            "candidate_final_result": summary,
            "artifact_manifest": {"path": "final.md"},
        },
        plan={"deliverable_mode": "inline_final"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.inline_result",
    )

    written = workspace.resolve_file_path("final.md").read_text(encoding="utf-8")
    assert written == report
    assert delivered["candidate_final_result"] == summary
    assert delivered["file_refs"] == []
    assert "workspace_artifact_delivery" not in delivered


@pytest.mark.asyncio
async def test_agent_task_inline_final_preserves_summary_when_explicit_artifact_body_is_written(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-inline-body")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-inline-body"
    task.workspace = workspace
    task.diagnostics = {}

    report = "# Complete Report\n\nThis is the explicit file-backed deliverable body."
    summary = "Compact returned summary for the completed report."
    delivered = await task._deliver_workspace_artifact(
        {
            "candidate_final_result": summary,
            "artifact_markdown": report,
            "artifact_manifest": {"path": "final.md"},
        },
        plan={"deliverable_mode": "inline_final"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.inline_body",
    )

    written = workspace.resolve_file_path("final.md").read_text(encoding="utf-8")
    assert written == report
    assert delivered["candidate_final_result"] == summary
    assert delivered["workspace_artifact_delivery"]["status"] == "delivered"
    assert task._workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == "final.md"


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_outline_sections_trigger_stream_draft(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-outline-sections")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-outline-sections"
    task.goal = "Revise a source-grounded artifact."
    task.success_criteria = ["The final artifact cites only supported sources."]
    task.execution_strategy = "flat"
    task.workspace = workspace
    task.diagnostics = {}

    old_body = "# Complete Report\n\nUnsupported source: https://example.test/old\n" + ("Existing body.\n" * 200)
    replacement_body = "# Complete Report\n\nSupported source: https://example.test/source\n"
    await workspace.write_file("reports/final.md", old_body)

    async def fake_stream_workspace_artifact_draft(**kwargs: Any) -> dict[str, Any]:
        path = kwargs["path"]
        await workspace.write_file(path, replacement_body, append=False)
        readback = await workspace.read_file(path)
        ref = {
            "path": readback["path"],
            "bytes": readback["bytes"],
            "sha256": readback["sha256"],
            "media_type": readback.get("media_type"),
            "content_kind": readback.get("content_kind", "text"),
            "role": "workspace_artifact",
            "source": kwargs.get("source"),
            "preview": readback["content"],
            "truncated": False,
            "read_bytes": readback["bytes"],
            "handler_id": readback.get("handler_id"),
        }
        return {
            "source": kwargs.get("source"),
            "path": path,
            "status": "delivered",
            "mode": "streamed_workspace_artifact",
            "file_refs": [ref],
        }

    task._stream_workspace_artifact_draft = fake_stream_workspace_artifact_draft

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {
                "path": "reports/final.md",
                "sections": [
                    "official source references",
                    "syllabus boundary",
                    "mock questions",
                    "answer key",
                ],
            },
            "evidence": ["Outline is ready; framework should draft the body."],
            "remaining_work": [],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.outline",
    )

    written = workspace.resolve_file_path("reports/final.md").read_text(encoding="utf-8")
    assert written == replacement_body
    assert delivered["workspace_artifact_delivery"]["mode"] == "streamed_workspace_artifact"
    assert delivered["workspace_artifact_delivery"]["status"] == "delivered"
    assert delivered["file_refs"][0]["bytes"] == len(replacement_body.encode("utf-8"))


@pytest.mark.asyncio
async def test_workspace_artifact_outline_with_remaining_verification_still_drafts(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-outline-remaining")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-outline-remaining"
    task.goal = "Write a file-backed report."
    task.success_criteria = ["The final artifact is written and read back."]
    task.execution_strategy = "flat"
    task.workspace = workspace
    task.diagnostics = {}

    body = "# Final Report\n\nThe body was drafted from the section outline.\n"

    async def fake_stream_workspace_artifact_draft(**kwargs: Any) -> dict[str, Any]:
        path = kwargs["path"]
        await workspace.write_file(path, body, append=False)
        readback = await workspace.read_file(path)
        return {
            "source": kwargs.get("source"),
            "path": path,
            "status": "delivered",
            "mode": "streamed_workspace_artifact",
            "file_refs": [
                {
                    "path": readback["path"],
                    "bytes": readback["bytes"],
                    "sha256": readback["sha256"],
                    "media_type": readback.get("media_type"),
                    "content_kind": readback.get("content_kind", "text"),
                    "role": "workspace_artifact",
                    "source": kwargs.get("source"),
                    "preview": readback["content"],
                    "truncated": False,
                    "read_bytes": readback["bytes"],
                    "handler_id": readback.get("handler_id"),
                }
            ],
        }

    task._stream_workspace_artifact_draft = fake_stream_workspace_artifact_draft

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {
                "path": "final.md",
                "section_outline": [
                    "data boundary",
                    "ticker snapshot",
                    "source list",
                ],
            },
            "evidence": ["The required evidence has already been collected."],
            "remaining_work": ["Verify final.md content via readback."],
            "ready_for_final_verification": False,
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.outline_remaining",
    )

    written = workspace.resolve_file_path("final.md").read_text(encoding="utf-8")
    assert written == body
    assert delivered["workspace_artifact_delivery"]["mode"] == "streamed_workspace_artifact"
    assert delivered["workspace_artifact_delivery"]["remaining_work_handoff"]["status"] == (
        "handed_to_terminal_verification"
    )
    assert delivered["remaining_work"] == []
    assert delivered["ready_for_final_verification"] is True


@pytest.mark.asyncio
async def test_agent_task_flat_workspace_artifact_delivery_before_verification(tmp_path):
    class WorkspaceArtifactRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactRequester"
        verify_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                self.__class__.verify_text = text
                assert "reports/final.md" in text
                assert "file_refs" in text
                assert "artifact_preview" in text
                assert "Delivered Report" in text
                assert "capability_evidence" in text
                assert "artifacts" in text
                assert "readback" in text
                assert "sha256" not in text
                assert "agent_task.workspace_artifact.untrusted_model_file_refs" in text
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "trusted Workspace readback evidence is present",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "Delivered final report at reports/final.md.",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "produce the final report body for framework Workspace delivery",
                    "expected_evidence": "trusted Workspace artifact readback",
                    "rationale": "the task requires a file deliverable",
                    "deliverable_mode": "workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "prepared final report body",
                    "artifact_markdown": "# Delivered Report\n\nThe framework must write this report.",
                    "artifact_manifest": {
                        "path": "reports/final.md",
                        "file_refs": [{"path": "test fake ref"}],
                    },
                    "file_refs": [{"path": "test fake ref"}],
                    "evidence": ["report body is ready for framework delivery"],
                    "remaining_work": [],
                }
            else:
                payload = {
                    "step_result": "Direct bounded step returned value ok.",
                    "candidate_final_result": "Bounded step completed with value ok.",
                    "evidence": ["value ok was produced by the direct bounded step."],
                    "remaining_work": [],
                }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings, parent=Agently.plugin_manager, name="agent-task-workspace-artifact-plugins"
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-workspace-artifact")
    task = agent.create_task(
        task_id="workspace-artifact-flat",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=["A final report file is written and read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    task_scoped_workspace = Agently.create_workspace(tmp_path / "task-workspace")._bind_execution(
        "workspace-artifact-flat"
    )
    assert (await task_scoped_workspace.read_file("reports/final.md"))["content"].startswith("# Delivered Report")
    delivery = meta["diagnostics"]["workspace_artifact_delivery"][0]
    assert delivery["status"] == "delivered"
    assert AgentTask._workspace_artifact_display_path(delivery["file_refs"][0]["path"]) == "reports/final.md"
    assert delivery["file_refs"][0]["bytes"] > 0
    assert delivery["file_refs"][0]["sha256"]
    assert delivery["file_refs"][0]["preview"].startswith("# Delivered Report")
    assert meta["iterations"][0]["verification"]["reason"] == "trusted Workspace readback evidence is present"
    assert delivery["file_refs"][0]["sha256"][:12] not in WorkspaceArtifactRequester.verify_text
    assert "reports/final.md#" not in WorkspaceArtifactRequester.verify_text


def test_artifact_readback_evidence_ids_omit_sha_from_model_hot_key():
    readback_ids = AgentTask._artifact_readback_evidence_ids(
        [
            {
                "path": "reports/final.md",
                "bytes": 120,
                "sha256": "a" * 64,
                "role": "workspace_artifact",
            }
        ]
    )

    assert readback_ids == ["reports/final.md"]


@pytest.mark.asyncio
async def test_verification_accepts_trusted_workspace_artifact_without_inline_final_result(tmp_path):
    class WorkspaceArtifactPointerRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactPointerRequester"
        tail_marker = "TRUSTED_WORKSPACE_ARTIFACT_TAIL_MARKER"
        verify_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                self.__class__.verify_text = text
                assert "trusted_workspace_artifacts" in text
                assert "source-grounded Workspace artifacts" in text
                assert "body-light Workspace location/status index" in text
                assert "trusted_workspace_artifacts.readback.content" not in text
                assert "acceptance-point evidence review" in text
                assert "whole-document editorial review" in text
                assert "evidence_ledger" in text
                assert "reports/final.md" in text
                assert "https://example.test/source" in text
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "trusted Workspace artifact readback satisfies the deliverable",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "",
                    "criterion_checks": [
                        {
                            "criterion_id": "criterion:1",
                            "satisfied": True,
                            "summary": "The report file was written and read back.",
                            "evidence_ids": [],
                        },
                        {
                            "criterion_id": "criterion:2",
                            "satisfied": True,
                            "summary": "The artifact includes its concrete source URL.",
                            "evidence_ids": [],
                        },
                    ],
                    "material_claim_coverage_complete": True,
                    "material_claim_checks": [],
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "produce the long, sectioned deliverable as a Workspace artifact",
                    "expected_evidence": "trusted Workspace artifact readback",
                    "rationale": "the task requires a file-backed deliverable",
                    "deliverable_mode": "workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                long_body = (
                    "# Delivered Report\n\n"
                    + "Source: https://example.test/source\n\n"
                    + ("This section is intentionally long so the default hot preview is insufficient.\n" * 90)
                    + f"\n{self.__class__.tail_marker}\n"
                )
                payload = {
                    "step_result": "prepared long, sectioned deliverable body",
                    "artifact_markdown": long_body,
                    "artifact_manifest": {"path": "reports/final.md"},
                    "evidence": ["long, sectioned deliverable body is ready for framework delivery"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-pointer-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-pointer-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactPointerRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-pointer",
    )
    task = agent.create_task(
        task_id="workspace-artifact-pointer",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=[
            "A final report file is written and read back.",
            "The final artifact includes concrete source URLs for source-grounded claims.",
        ],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["final_result"].startswith("Workspace artifact delivered at reports/final.md")
    assert len(result["artifact_refs"]) == 1
    assert result["artifact_refs"][0]["type"] == "file"
    assert AgentTask._workspace_artifact_display_path(result["artifact_refs"][0]["path"]) == "reports/final.md"
    assert not (tmp_path / "task-workspace" / ".agently" / "workspace.db").exists()
    assert "verification" not in result
    assert "iterations" not in result
    assert "# Delivered Report" not in json.dumps(result, ensure_ascii=False)
    assert "final_response" in result
    assert "Completed" in result["final_response"]
    assert "reports/final.md" in result["final_response"]
    assert await task.async_get_text() == result["final_response"]
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is True
    assert verification["final_result_via_workspace_artifact"] is True
    assert "final_result_missing" not in verification.get("guard_reasons", [])
    assert WorkspaceArtifactPointerRequester.tail_marker in WorkspaceArtifactPointerRequester.verify_text
    assert "material_claim_candidates" in WorkspaceArtifactPointerRequester.verify_text


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_refs_survive_incomplete_verification(tmp_path):
    class WorkspaceArtifactPartialRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactPartialRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                assert "reports/partial.md" in text
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "real Workspace artifact exists but source coverage is incomplete",
                    "failure_analysis": "The artifact was written, but verification still needs stronger evidence.",
                    "acceptance_delta": ["Add stronger cited evidence before final acceptance."],
                    "missing_criteria": ["stronger cited evidence"],
                    "replan_instruction": "Gather stronger cited evidence and update the artifact.",
                    "final_result_required": True,
                    "final_result": "",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "draft the report as a sectioned Workspace artifact manifest",
                    "expected_evidence": "Workspace write/readback and cited evidence",
                    "rationale": "the task requires a file deliverable",
                    "deliverable_mode": "sectioned_workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "prepared a sectioned artifact manifest",
                    "artifact_manifest": {
                        "path": "reports/partial.md",
                        "sections": [
                            {
                                "title": "概览",
                                "content": "这是一份仍需补证据的报告草稿。",
                            },
                            {
                                "title": "待补充证据",
                                "content": "后续步骤需要补充真实引用后才能验收。",
                            },
                        ],
                        "file_refs": [{"path": "fake-partial.md", "sha256": "fake"}],
                    },
                    "file_refs": [{"path": "model-claimed.md", "sha256": "fake"}],
                    "evidence": ["draft content is ready for framework delivery"],
                    "remaining_work": ["stronger cited evidence"],
                    "ready_for_final_verification": True,
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-partial-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings, parent=Agently.plugin_manager, name="agent-task-workspace-artifact-partial-plugins"
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactPartialRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-workspace-artifact-partial")
    task = agent.create_task(
        task_id="workspace-artifact-partial",
        goal="Create a report file and verify it against cited evidence.",
        success_criteria=["A final report file is written.", "The report has stronger cited evidence."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert "final_response" in result
    assert "Partial result available" in result["final_response"]
    assert "reports/partial.md" in result["final_response"]
    assert "stronger cited evidence" in result["final_response"]
    assert await task.async_get_text() == result["final_response"]
    delivery = meta["diagnostics"]["workspace_artifact_delivery"][0]
    assert delivery["status"] == "delivered"
    assert AgentTask._workspace_artifact_display_path(delivery["file_refs"][0]["path"]) == "reports/partial.md"
    assert (
        meta["iterations"][0]["verification"]["reason"]
        == "real Workspace artifact exists but source coverage is incomplete"
    )
    task_scoped_workspace = Agently.create_workspace(tmp_path / "task-workspace")._bind_execution(
        "workspace-artifact-partial"
    )
    readback = await task_scoped_workspace.read_file("reports/partial.md")
    assert "这是一份仍需补证据的报告草稿" in readback["content"]
    assert "## 待补充证据" in readback["content"]


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_stream_draft_when_step_returns_no_body(tmp_path):
    class WorkspaceArtifactStreamDraftRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactStreamDraftRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                yield "message", "# Partial attempt that must be discarded\n\n"
                yield "status", {
                    "status": "failed",
                    "attempt_index": 1,
                    "retry": True,
                    "next_attempt_index": 2,
                    "reason": "transient provider disconnect",
                }
                yield "message", "# Streamed Report\n\n"
                yield "message", "This body was written through the framework artifact draft stream.\n"
                return
            if "Verify the task against every success criterion" in text:
                assert "reports/final.md" in text
                assert "agent_task.workspace_artifact.stream_drafted" in text
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "trusted streamed Workspace artifact readback is present",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "Delivered final report at reports/final.md.",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "prepare the final report; framework may stream-draft the file body if needed",
                    "expected_evidence": "trusted Workspace artifact readback",
                    "rationale": "the task requires a file deliverable",
                    "deliverable_mode": "sectioned_workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "ready to generate reports/final.md as a Workspace artifact",
                    "answer": "A short control summary; the artifact_manifest describes the real deliverable.",
                    "candidate_final_result": "STRUCTURED BODY SHOULD NOT BE WRITTEN",
                    "artifact_manifest": {
                        "path": "final.md",
                        "sections": [
                            {"id": "report", "title": "Final report", "intent": "Write the complete deliverable."}
                        ],
                    },
                    "evidence": ["source evidence has been collected"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

        async def broadcast_response(
            self,
            response_generator: AsyncGenerator[tuple[str, object], None],
        ):
            response_text = ""
            async for event, data in response_generator:
                if event == "message":
                    response_text += str(data)
                    yield "delta", str(data)
                elif event == "status":
                    yield "status", data
            yield "done", response_text

    settings = Settings(name="agent-task-workspace-artifact-stream-draft-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings, parent=Agently.plugin_manager, name="agent-task-workspace-artifact-stream-draft-plugins"
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactStreamDraftRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager, parent_settings=settings, name="agent-task-workspace-artifact-stream-draft"
    )
    task = agent.create_task(
        task_id="workspace-artifact-stream-draft",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=["A final report file is written and read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    task_scoped_workspace = Agently.create_workspace(tmp_path / "task-workspace")._bind_execution(
        "workspace-artifact-stream-draft"
    )
    readback = await task_scoped_workspace.read_file("final.md")
    assert "Streamed Report" in readback["content"]
    assert "<$retry>" not in readback["content"]
    assert "Partial attempt" not in readback["content"]
    assert "STRUCTURED BODY SHOULD NOT BE WRITTEN" not in readback["content"]
    delivery = meta["diagnostics"]["workspace_artifact_delivery"][0]
    assert delivery["status"] == "delivered"
    assert delivery["mode"] == "streamed_workspace_artifact"
    assert delivery["retry_boundaries"][0]["source"] == "structured_status"
    assert delivery["retry_boundaries"][0]["reason"] == "transient provider disconnect"
    assert AgentTask._workspace_artifact_display_path(delivery["file_refs"][0]["path"]) == "final.md"
    assert meta["iterations"][0]["verification"]["reason"] == "trusted streamed Workspace artifact readback is present"


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_consumes_public_retry_marker_without_writing(tmp_path):
    class WorkspaceArtifactDraftPublicMarkerRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftPublicMarkerRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                yield "message", "# Partial attempt that must be discarded\n\n"
                yield "message", "<$retry>transient provider disconnect</$retry>"
                yield "message", "# Streamed Report\n\n"
                yield "message", "This body was written after the public retry marker.\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-public-marker-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-public-marker-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftPublicMarkerRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-public-marker",
    )
    task = AgentTask(
        agent,
        task_id="workspace-artifact-public-marker",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=["A final report file is written and read back."],
        workspace=tmp_path / "task-workspace",
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={"deliverable_mode": "workspace_artifact"},
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.public_marker",
        context_pack=None,
        iteration_index=1,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert delivery.get("retry_boundaries", []) == []
    assert delivery["public_replay_markers"][0]["source"] == "delta_replay_marker"
    assert delivery["public_replay_markers"][0]["reason"] == "transient provider disconnect"
    readback = await task.workspace.read_file("final.md")
    assert "Streamed Report" in readback["content"]
    assert "This body was written after the public retry marker." in readback["content"]
    assert "<$retry>" not in readback["content"]
    assert "Partial attempt" not in readback["content"]


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_uses_bounded_dependency_projection(tmp_path):
    class WorkspaceArtifactDraftBoundedDependencyRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftBoundedDependencyRequester"
        draft_request_chars = 0
        draft_request_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                self.__class__.draft_request_chars = len(text)
                self.__class__.draft_request_text = text
                yield "message", "# Bounded Draft\n\nThe current report uses compact dependency evidence.\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-bounded-dependency-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-bounded-dependency-plugins",
    )
    plugin_manager.register(
        "ModelRequester",
        WorkspaceArtifactDraftBoundedDependencyRequester,
        activate=True,
    )
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-bounded-dependency",
    )
    task = AgentTask(
        agent,
        task_id="workspace-artifact-bounded-dependency",
        goal="Write the final report from bounded dependency evidence.",
        success_criteria=["The final report is written."],
        workspace=tmp_path / "task-workspace",
    )
    huge = "recursive-dependency-payload-" * 4000
    dependency_result = TaskBoardCardResult(
        card_id="collect",
        status="completed",
        output_digest="Collected bounded evidence.",
        preview={
            "status": "completed",
            "answer": "Collected bounded evidence.",
            "short_summary": "Use the stable source refs.",
            "evidence_ledger": {f"item-{index}": huge for index in range(20)},
            "execution_meta": {f"record-{index}": huge for index in range(20)},
        },
    )
    card = TaskBoardCard.from_value(
        {
            "id": "final",
            "objective": "Write final.md.",
            "depends_on": ["collect"],
            "required_outputs": ["final.md"],
        }
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={"deliverable_mode": "workspace_artifact"},
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.bounded_dependency",
        context_pack=None,
        iteration_index=1,
        card_context=SimpleNamespace(
            card=card,
            dependency_results={"collect": dependency_result},
        ),
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert WorkspaceArtifactDraftBoundedDependencyRequester.draft_request_chars < 50_000
    assert huge not in WorkspaceArtifactDraftBoundedDependencyRequester.draft_request_text
    assert "Collected bounded evidence." in WorkspaceArtifactDraftBoundedDependencyRequester.draft_request_text


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_receives_stable_source_references(tmp_path):
    class WorkspaceArtifactDraftEvidenceRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftEvidenceRequester"
        draft_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                self.__class__.draft_text = text
                assert "offered_source_references" in text
                match = re.search(r"reference_id:\s*(ref_[0-9A-Za-z]+)", text)
                assert "https://example.test/exact-source" in text
                reference_id = match.group(1) if match is not None else "ref_missing"
                yield "message", f"# Evidence Draft\n\nSource: [[ref:{reference_id}]]\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-draft-evidence-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-draft-evidence-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftEvidenceRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-draft-evidence",
    )
    task = AgentTask(
        agent,
        task_id="workspace-artifact-draft-evidence",
        goal="Write a source-grounded Workspace artifact.",
        success_criteria=["The final artifact cites exact source URLs."],
        workspace=tmp_path / "task-workspace",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "web_search",
                            "status": "success",
                            "action_call_id": "call-source",
                            "model_digest": {
                                "result_preview": [
                                    {
                                        "title": "Exact source",
                                        "href": "https://example.test/exact-source",
                                        "body": "Evidence-backed source snippet.",
                                    }
                                ],
                                "result_preview_meta": {"truncated": False},
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
        }
    )
    preflight_ledger = task._cumulative_evidence_ledger(
        {"status": "completed", "logs": {"action_logs": [], "route_logs": {}}}
    )
    assert preflight_ledger["items"]
    assert preflight_ledger["items"][0]["reference_id"].startswith("ref_")
    assert preflight_ledger["source_refs"][0]["reference_id"].startswith("ref_")

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={"deliverable_mode": "workspace_artifact"},
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.evidence",
        context_pack=None,
        iteration_index=2,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert "reference_id: ref_" in WorkspaceArtifactDraftEvidenceRequester.draft_text
    assert "https://example.test/exact-source" in WorkspaceArtifactDraftEvidenceRequester.draft_text
    readback = await task.workspace.read_file("final.md")
    assert "[[ref:ref_" in readback["content"]


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_disables_action_loop(tmp_path, monkeypatch):
    class WorkspaceArtifactDraftNoActionRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftNoActionRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                yield "message", "# Draft Without Actions\n\nThe draft used existing evidence only.\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-draft-no-action-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-draft-no-action-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftNoActionRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-draft-no-action",
    )

    def browse(query: str = "") -> str:
        return f"unexpected browse: {query}"

    agent.use_actions(browse, always=True)
    calls = 0

    async def fail_if_action_loop_runs(**kwargs: Any):
        nonlocal calls
        _ = kwargs
        calls += 1
        raise AssertionError("workspace artifact draft must not run the action loop")

    monkeypatch.setattr(agent.action, "async_plan_and_execute", fail_if_action_loop_runs)

    task = AgentTask(
        agent,
        task_id="workspace-artifact-draft-no-action",
        goal="Write a Workspace artifact from existing evidence.",
        success_criteria=["The final artifact is written without extra actions."],
        workspace=tmp_path / "task-workspace",
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={"deliverable_mode": "workspace_artifact"},
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.no_action_loop",
        context_pack=None,
        iteration_index=1,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert calls == 0
    assert agent.settings.get("action.loop.enabled", True) is True
    readback = await task.workspace.read_file("final.md")
    assert "Draft Without Actions" in readback["content"]


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_receives_active_repair_context(tmp_path):
    class WorkspaceArtifactDraftRepairRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftRepairRequester"
        draft_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                self.__class__.draft_text = text
                assert "repair_context" in text
                assert "Replace the unsupported legacy claim." in text
                assert "https://example.test/exact-source" in text
                yield "message", "# Repaired Draft\n\nSource: https://example.test/exact-source\n\nNo unsupported claim remains.\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-draft-repair-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-draft-repair-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftRepairRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-draft-repair",
    )
    task = AgentTask(
        agent,
        task_id="workspace-artifact-draft-repair",
        goal="Repair a source-grounded Workspace artifact.",
        success_criteria=["The final artifact removes unsupported claims."],
        workspace=tmp_path / "task-workspace",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "plan": {"step_instruction": "Draft the first artifact.", "execution_shape": "direct"},
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "browse",
                            "status": "success",
                            "action_call_id": "call-source",
                            "model_digest": {
                                "result_preview": {
                                    "selected_url": "https://example.test/exact-source",
                                    "content": "Exact source says the corrected claim.",
                                },
                                "result_preview_meta": {"truncated": False},
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
            "verification": {
                "is_complete": False,
                "reason": "Unsupported claim remains.",
                "failure_analysis": "The draft kept a claim that the source does not support.",
                "acceptance_delta": ["Replace the unsupported legacy claim."],
                "missing_criteria": ["Unsupported claim remains."],
                "repair_constraints": ["Use exact source URLs from available evidence."],
                "next_step_requirements": ["Rewrite the affected artifact section."],
                "replan_instruction": "Repair the artifact using verifier feedback.",
            },
        }
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={
            "deliverable_mode": "workspace_artifact",
            "step_instruction": "Repair the artifact.",
        },
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.repair",
        context_pack=None,
        iteration_index=2,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert "active correction contract" in WorkspaceArtifactDraftRepairRequester.draft_text
    readback = await task.workspace.read_file("final.md")
    assert "No unsupported claim remains." in readback["content"]


def test_agent_language_policy_normalizes_and_reaches_execution_prompt():
    agent = _create_agent("agent-language-policy")

    agent.language("简体中文")
    execution = agent.create_execution().language("chinese")

    agent_policy = agent.agent_prompt.get("options.language_policy")
    execution_policy = execution.prompt_snapshot.get("options", {}).get("language_policy")

    assert agent_policy is not None
    assert execution_policy is not None
    assert agent_policy["language"] == "zh-CN"
    assert "search_region" not in agent_policy
    assert execution_policy["language"] == "zh-CN"
    assert "search_region" not in execution_policy
    assert execution_policy["accept_language"].startswith("zh-CN")
    assert "Language policy" in execution.request.prompt.to_text()


@pytest.mark.asyncio
async def test_agent_create_task_exposes_scoped_workspace_readback_actions(tmp_path):
    task_id = "workspace-readback-task"
    agent = _create_agent("agent-task-scoped-workspace-actions")

    execution = agent.create_task(
        task_id=task_id,
        goal="Create and verify a workspace deliverable.",
        success_criteria=["final.md can be read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"enable_workspace_readback_actions": True}},
    )

    assert {"list_files", "read_file", "search_files"}.issubset(set(execution.local_action_ids))

    scoped_workspace = agent.workspace._bind_execution(
        task_id,
        scope={"task_id": task_id},
        search_scope={"task_id": task_id},
    )
    await scoped_workspace.write_file("final.md", "# Scoped Deliverable\n")

    read_result = await agent.action.async_execute_action("read_file", {"path": "final.md"})

    data = read_result.get("data")
    assert read_result.get("status") == "success"
    assert isinstance(data, dict)
    assert data.get("path") == "final.md"
    assert "Scoped Deliverable" in str(data.get("content") or "")


@pytest.mark.asyncio
async def test_agent_create_task_exposes_scoped_workspace_coding_actions(tmp_path):
    task_id = "workspace-coding-task"
    agent = _create_agent("agent-task-scoped-workspace-coding-actions")

    execution = agent.create_task(
        task_id=task_id,
        goal="Create and repair a workspace deliverable.",
        success_criteria=["final.md can be edited and read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={
            "agent_task": {
                "enable_workspace_readback_actions": True,
                "enable_workspace_coding_actions": True,
            }
        },
    )

    expected_actions = {
        "list_files",
        "read_file",
        "search_files",
        "glob_files",
        "grep_files",
        "write_file",
        "edit_file",
        "apply_patch",
    }
    assert expected_actions.issubset(set(execution.local_action_ids))

    scoped_workspace = agent.workspace._bind_execution(
        task_id,
        scope={"task_id": task_id},
        search_scope={"task_id": task_id},
    )
    await scoped_workspace.write_file("final.md", "# Draft\nold wording\n")

    read_result = await agent.action.async_execute_action("read_file", {"path": "final.md"})
    assert read_result.get("status") == "success"
    expected_sha = read_result.get("data", {}).get("sha256")

    edit_result = await agent.action.async_execute_action(
        "edit_file",
        {
            "path": "final.md",
            "old_string": "old wording",
            "new_string": "corrected wording",
            "expected_sha256": expected_sha,
        },
    )
    assert edit_result.get("status") == "success"

    updated = await scoped_workspace.read_file("final.md")
    assert "corrected wording" in str(updated.get("content") or "")


def test_agent_task_child_execution_sets_task_local_action_loop_guard(tmp_path):
    agent = _create_agent("agent-task-action-loop-guard").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Use actions inside a bounded task step.",
        success_criteria=["The child action loop has a task-local safety guard."],
        execution="flat",
    )

    execution = task._create_bounded_child_execution(
        lineage={
            "task_id": task.id,
            "iteration_id": "iter-1",
            "step_id": "execute",
        }
    )

    assert agent.settings.get("action.loop.max_rounds") is None
    assert execution.request.settings.get("action.loop.max_rounds") == 2
    assert execution.request.settings.get("tool.loop.max_rounds") == 2


def test_agent_task_child_execution_respects_explicit_action_loop_guard(tmp_path):
    agent = _create_agent("agent-task-action-loop-explicit").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Use actions inside a bounded task step.",
        success_criteria=["The child action loop honors explicit task options."],
        execution="flat",
        options={"agent_task": {"action_loop_max_rounds": 3}},
    )
    disabled_task = AgentTask(
        agent,
        goal="Use actions without task-local loop guard.",
        success_criteria=["The explicit None option disables the task guard."],
        execution="flat",
        options={"agent_task": {"action_loop_max_rounds": None}},
    )

    execution = task._create_bounded_child_execution(
        lineage={"task_id": task.id, "iteration_id": "iter-1", "step_id": "execute"}
    )
    disabled_execution = disabled_task._create_bounded_child_execution(
        lineage={"task_id": disabled_task.id, "iteration_id": "iter-1", "step_id": "execute"}
    )

    assert execution.request.settings.get("action.loop.max_rounds") == 3
    assert execution.request.settings.get("tool.loop.max_rounds") == 3
    assert disabled_execution.request.settings.get("action.loop.max_rounds") is None
    assert disabled_execution.request.settings.get("tool.loop.max_rounds") is None


@pytest.mark.asyncio
async def test_agent_goal_success_criteria_uses_task_execution_path(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-goal-task-path").use_workspace(tmp_path / "task-workspace")

    execution = agent.goal(
        "Repair a legacy Agently script so it runs on the current API.",
        ["The script runs successfully."],
    ).strategy("task", max_iterations=2)

    result = await execution.async_start()
    meta = await execution.async_get_meta()

    assert result["status"] == "completed"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["task_id"]
    assert meta["task_refs"]["status"] == "completed"
    assert meta["success_criteria"] == ["The script runs successfully."]


@pytest.mark.asyncio
async def test_agent_task_loop_receives_execution_prompt_snapshot(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-execution-prompt").use_workspace(tmp_path / "task-workspace")

    execution = (
        agent.goal(
            "Prepare an operator summary from caller-provided facts.",
            ["The summary uses the supplied incident id."],
        )
        .effort("low", budget={"iteration_limit": 2})
        .input({"incident_id": "INC-4242", "severity": "SEV2"})
        .output({"summary": (str, "Operator summary that includes the incident id.", True)}, format="json")
        .strategy("task", max_iterations=2)
    )

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    calls = "\n".join(MockAgentTaskRequester.calls)
    task = cast(Any, execution).task_record

    assert result["status"] == "completed"
    assert meta["route"]["selected_route"] == "agent_task"
    assert "INC-4242" in calls
    assert "severity" in calls
    assert "summary" in calls
    assert task.options["execution_prompt_snapshot"]["input"]["incident_id"] == "INC-4242"


@pytest.mark.asyncio
async def test_public_task_strategy_spellings_share_agent_task_lifecycle(tmp_path):
    for label, build in (
        (
            "create_task_loop",
            lambda agent, workspace: agent.create_task_loop(
                task_id="task-loop-spelling",
                goal="Repair a legacy Agently script so it runs on the current API.",
                success_criteria=["The script runs successfully."],
                workspace=workspace,
                max_iterations=2,
                limits={"max_model_requests": 1},
            ),
        ),
        (
            "strategy_task_loop",
            lambda agent, workspace: (
                agent.create_execution(
                    options={
                        "task": {
                            "task_id": "strategy-task-loop-spelling",
                            "workspace": workspace,
                            "max_iterations": 2,
                            "limits": {"max_model_requests": 1},
                        }
                    }
                )
                .goal(
                    "Repair a legacy Agently script so it runs on the current API.",
                    ["The script runs successfully."],
                )
                .strategy("task_loop")
            ),
        ),
    ):
        MockAgentTaskRequester.reset()
        agent = _create_agent(f"agent-{label}").use_workspace(tmp_path / label)
        execution = build(agent, tmp_path / label)

        result = await execution.async_start()
        meta = await execution.async_get_meta()

        assert result["status"] == "completed"
        assert meta["route"]["selected_route"] == "agent_task"
        assert meta["route"]["options"]["strategy"] == "task_loop"
        assert meta["task_refs"]["status"] == "completed"


@pytest.mark.asyncio
async def test_task_shape_analysis_uses_exact_taskboard_initial_plan_schema(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-task-shape-exact-schema").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="task-shape-exact-schema",
        goal="Collect independent evidence and synthesize a report.",
        success_criteria=["The report is supported by collected evidence."],
    )
    captured: dict[str, Any] = {}

    class FakeRequest:
        def input(self, value):
            captured["input"] = value
            return self

        def instruct(self, value):
            captured["instruct"] = value
            return self

        def output(self, value, *, format):
            captured["output"] = value
            captured["format"] = format
            return self

        async def async_get_data(self):
            return {
                "analysis": "The work has independent evidence branches.",
                "execution_hint": {
                    "recommended_shape": "taskboard",
                    "confidence": "high",
                },
            }

    monkeypatch.setattr(agent, "create_temp_request", lambda: FakeRequest())
    monkeypatch.setattr(
        cast(Any, task),
        "_apply_language_policy_to_request",
        lambda *_args, **_kwargs: None,
    )

    await task._request_task_shape_analysis()

    assert captured["output"]["initial_taskboard_plan"][0] == (task_board_planning_output_schema())
    assert captured["format"] == "json"


@pytest.mark.asyncio
async def test_strategy_shape_analysis_is_hint_not_hard_route(tmp_path):
    agent = _create_agent("agent-task-shape-analysis").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="shape-analysis-policy-gate",
        goal="Plan a multi-angle launch review.",
        success_criteria=["The review is planned."],
        execution="auto",
        options={"agent_task": {"execution_strategy_policy": {"allow_taskboard": False}}},
    )

    async def taskboard_hint():
        return {
            "analysis": "Several workstreams could benefit from a board.",
            "execution_hint": {
                "recommended_shape": "taskboard",
                "confidence": "high",
                "reasons": ["parallel evidence streams"],
                "linear_evidence": [],
                "branching_evidence": ["marketing, operations, and finance perspectives"],
                "uncertainty": "",
            },
        }

    cast(Any, task)._request_task_shape_analysis = taskboard_hint

    effective = await task._resolve_effective_execution_strategy()

    assert effective == "flat"
    assert task.execution_strategy == "auto"
    assert task.task_shape_analysis["execution_hint"]["recommended_shape"] == "taskboard"
    assert task.diagnostics["execution_strategy"]["effective"] == "flat"
    assert task.workspace_refs["strategy"] == []
    assert not (task.workspace.root / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_auto_strategy_can_select_taskboard_when_policy_allows(tmp_path):
    agent = _create_agent("agent-task-shape-taskboard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="shape-analysis-taskboard",
        goal="Plan multiple parallel research tracks.",
        success_criteria=["Each track has evidence."],
        execution="auto",
        options={"agent_task": {"execution_strategy_policy": {"allow_taskboard": True}}},
    )

    async def taskboard_hint():
        return {
            "analysis": "The task has multiple independent tracks.",
            "execution_hint": {
                "recommended_shape": "taskboard",
                "confidence": "medium",
                "reasons": ["independent evidence tracks"],
                "linear_evidence": [],
                "branching_evidence": ["track A", "track B"],
                "uncertainty": "",
            },
        }

    cast(Any, task)._request_task_shape_analysis = taskboard_hint

    assert await task._resolve_effective_execution_strategy() == "taskboard"
    assert task.diagnostics["execution_strategy"]["source"] == "task_shape_analysis"


@pytest.mark.asyncio
async def test_explicit_flat_strategy_skips_shape_analysis(tmp_path):
    agent = _create_agent("agent-explicit-flat-strategy").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="explicit-flat-no-analysis",
        goal="Run a linear task.",
        success_criteria=["The task is done."],
        execution="flat",
    )

    async def fail_if_called():
        raise AssertionError("explicit flat must not request task-shape analysis")

    cast(Any, task)._request_task_shape_analysis = fail_if_called

    assert await task._resolve_effective_execution_strategy() == "flat"
    assert task.task_shape_analysis == {}


def test_strategy_method_maps_execution_shapes_and_nested_inheritance(tmp_path):
    from agently.core.application.AgentExecution import AgentExecutionContext
    from agently.core.runtime.RuntimeContext import bind_runtime_context

    agent = _create_agent("agent-strategy-method").use_workspace(tmp_path / "workspace")

    flat_execution = agent.goal("Do a flat task.", ["Done."]).strategy("flat", max_iterations=1)
    assert flat_execution.strategy_name == "flat"
    assert flat_execution.task_options["execution"] == "flat"
    assert flat_execution.task_strategy_options()["execution"] == "flat"

    parent_context = AgentExecutionContext(
        execution_id="parent-exec",
        lineage={},
        limits={},
        task_execution_strategy="auto",
        effective_task_execution_strategy="taskboard",
        strategy_context_source="task_shape_analysis",
    )
    with bind_runtime_context(agent_execution_context=parent_context):
        inherited = agent.create_execution().goal("Nested task.", ["Done."])
        overridden = agent.create_execution().goal("Nested override.", ["Done."]).strategy("flat")

    assert inherited.task_strategy_options()["execution"] == "taskboard"
    assert inherited.task_strategy_options()["_execution_strategy_source"] == "inherited_agent_execution_context"
    assert overridden.task_strategy_options()["execution"] == "flat"

    inherited_task = AgentTask(
        agent,
        task_id="nested-inherited-taskboard",
        goal="Nested inherited task.",
        success_criteria=["Done."],
        execution=inherited.task_strategy_options()["execution"],
    )

    async def fail_if_called():
        raise AssertionError("inherited effective strategy must not request task-shape analysis")

    cast(Any, inherited_task)._request_task_shape_analysis = fail_if_called
    assert inherited_task.execution_strategy == "taskboard"
    assert inherited_task.effective_execution_strategy == "taskboard"
    assert asyncio.run(inherited_task._resolve_effective_execution_strategy()) == "taskboard"


@pytest.mark.asyncio
async def test_agent_execution_runtime_observes_flat_agent_task_stream(tmp_path):
    MockAgentTaskRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_agent_task_loop.agent_execution_runtime_stream_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        agent = _create_agent("agent-flat-runtime-observation").use_workspace(tmp_path / "workspace")
        execution = agent.goal(
            "Repair a legacy Agently script.",
            [
                "The original failure is recorded.",
                "The script runs successfully.",
            ],
        ).strategy("flat", max_iterations=2)

        result = await execution.async_get_full_data()

        assert result["status"] == "completed"
        execution_events = [
            event
            for event in captured
            if event.run is not None
            and event.run.run_kind == "agent_execution"
            and event.run.execution_id == execution.id
        ]
        event_types = [event.event_type for event in execution_events]
        assert event_types[0] == "agent_execution.started"
        assert event_types[-1] == "agent_execution.completed"
        stream_events = [event for event in execution_events if event.event_type == "agent_execution.stream"]
        stream_paths = [event.payload.get("path") for event in stream_events if isinstance(event.payload, dict)]
        assert "route.selected" in stream_paths
        assert "agent_task.created" in stream_paths
        assert any(str(path).startswith("agent_task.iteration.") for path in stream_paths)
        assert any(str(path).endswith(".execution.completed") for path in stream_paths)
        task_stream_event = next(
            event
            for event in stream_events
            if isinstance(event.payload, dict) and event.payload.get("path") == "agent_task.created"
        )
        assert task_stream_event.payload["execution_id"] == execution.id
        assert task_stream_event.payload["task_id"] == execution.task_refs["task_id"]
        assert task_stream_event.payload["execution_strategy"] == "flat"
        assert task_stream_event.payload["effective_execution_strategy"] == "flat"
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_effort_reflection_density_records_expected_points(tmp_path):
    async def run_with_effort(label: str, effort: dict[str, Any]):
        agent = _create_agent(f"agent-reflection-{label}").use_workspace(tmp_path / label)
        task = AgentTask(
            agent,
            task_id=f"reflection-{label}",
            goal="Complete one bounded step.",
            success_criteria=["Verifier accepts the result."],
            execution="flat",
            max_iterations=1,
            options={"agent_task": {"effort": effort}},
        )

        async def request_plan(_iteration_index, _context_pack):
            return {
                "execution_shape": "direct",
                "step_instruction": "produce evidence",
                "expected_evidence": "evidence",
                "rationale": "one step",
            }

        async def execute_step(_iteration_index, _plan, _context_pack):
            return (
                {"step_result": "done", "evidence": ["ok"], "remaining_work": []},
                {"execution_id": f"exec-{label}", "status": "success", "route": {"selected_route": "model_request"}},
            )

        async def request_verification(_iteration_index, **_kwargs):
            return {
                "is_complete": True,
                "requires_block": False,
                "reason": "accepted",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "done",
            }

        terminal_calls: list[str] = []
        original_terminal_verification = task._run_terminal_verification

        async def run_terminal_verification(iteration_index, **kwargs):
            terminal_calls.append(str(kwargs["plan"].get("execution_shape") or ""))
            return await original_terminal_verification(iteration_index, **kwargs)

        cast(Any, task)._request_plan = request_plan
        cast(Any, task)._execute_step = execute_step
        cast(Any, task)._request_verification = request_verification
        cast(Any, task)._run_terminal_verification = run_terminal_verification
        await task.async_run()
        return [item["phase"] for item in task.reflections], terminal_calls

    low, low_terminal_calls = await run_with_effort(
        "low", {"name": "low", "reflection_density": "final"}
    )
    medium, medium_terminal_calls = await run_with_effort(
        "medium", {"name": "medium", "reflection_density": "major_node"}
    )
    high, high_terminal_calls = await run_with_effort(
        "high", {"name": "high", "reflection_density": "action"}
    )

    assert low == ["final"]
    assert "major_node" in medium and "bounded_step" not in medium and "final" in medium
    assert {"bounded_step", "major_node", "final"}.issubset(set(high))
    assert low_terminal_calls == medium_terminal_calls == high_terminal_calls == [
        "direct"
    ]


@pytest.mark.asyncio
async def test_acp_recovery_policy_uses_registered_action_after_exhaustion(tmp_path, monkeypatch):
    agent = _create_agent("agent-acp-recovery").use_workspace(tmp_path / "workspace")
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute_action(action_id: str, payload: dict[str, Any]):
        calls.append((action_id, payload))
        return {
            "ok": True,
            "status": "success",
            "agent_id": payload.get("agent_id"),
            "result": {"final_result": "recovered by acp"},
            "diagnostics": [],
        }

    monkeypatch.setattr(agent.action, "async_execute_action", fake_execute_action)

    task = AgentTask(
        agent,
        task_id="acp-recovery-task",
        goal="Recover a failed bounded step.",
        success_criteria=["ACP recovery evidence is accepted."],
        execution="flat",
        max_iterations=1,
        options={"agent_task": {"acp_recovery": {"enabled": True, "agent_id": "codex"}}},
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "direct",
            "step_instruction": "fail first",
            "expected_evidence": "failure",
            "rationale": "exercise recovery",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {"step_result": "", "evidence": ["failed"], "remaining_work": ["recover"]},
            {"execution_id": "failed-step", "status": "failed", "route": {"selected_route": "model_request"}},
        )

    async def request_verification(_iteration_index, *, execution_meta, **_kwargs):
        assert execution_meta["route"]["selected_route"] == "acp_recovery"
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "ACP recovery accepted",
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "recovered by acp",
        }

    cast(Any, task)._request_plan = request_plan
    cast(Any, task)._execute_step = execute_step
    cast(Any, task)._request_verification = request_verification

    result = await task.async_run()

    assert result["status"] == "completed"
    assert calls and calls[0][0] == "acp_run_task"
    assert calls[0][1]["agent_id"] == "codex"
    assert task.workspace_refs["acp_recovery"] == []
    assert not (task.workspace.root / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_taskboard_card_failure_uses_acp_recovery_after_card_attempts(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-acp-recovery").use_workspace(tmp_path / "workspace")
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute_action(action_id: str, payload: dict[str, Any]):
        calls.append((action_id, payload))
        return {
            "ok": True,
            "status": "success",
            "agent_id": payload.get("agent_id"),
            "result": {"final_result": "taskboard card recovered by acp"},
            "diagnostics": [],
        }

    monkeypatch.setattr(agent.action, "async_execute_action", fake_execute_action)
    task = AgentTask(
        agent,
        task_id="taskboard-acp-recovery",
        goal="Recover a failed TaskBoard card.",
        success_criteria=["ACP recovery evidence is accepted."],
        execution="taskboard",
        max_iterations=1,
        options={"agent_task": {"acp_recovery": {"enabled": True, "agent_id": "codex"}}},
    )
    card = TaskBoardCard.from_value(
        {
            "id": "collect",
            "objective": "Collect evidence for the final answer.",
            "required_outputs": ["Recovered evidence"],
        }
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-acp-recovery",
        graph=TaskBoardGraph.from_value({"graph_id": "taskboard-acp-recovery-graph", "cards": [card.to_dict()]}),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )

    async def failing_card(_context, _context_pack):
        return TaskBoardCardResult(
            card_id=card.id,
            status="failed",
            preview={"error": "missing dynamic handler"},
            diagnostics=({"code": "taskboard.card.missing_handler", "card_id": card.id},),
            metadata={"execution_kind": "taskboard_agent_card"},
        )

    monkeypatch.setattr(cast(Any, task), "_run_taskboard_agent_card", failing_card)
    monkeypatch.setattr(cast(Any, task), "_should_record_process_reflection", lambda *_args, **_kwargs: False)

    result = await task._run_taskboard_card(
        context,
        {
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result.status == "completed"
    assert result.metadata["acp_recovery"] is True
    assert result.metadata["acp_recovered"] is True
    assert any(item.get("code") == "taskboard.card.acp_recovery" for item in result.diagnostics)
    assert calls and calls[0][0] == "acp_run_task"
    payload = calls[0][1]
    assert payload["agent_id"] == "codex"
    assert payload["context"]["plan"]["taskboard_card_id"] == "collect"
    assert payload["context"]["failed_execution_result"]["taskboard_card_result"]["status"] == "failed"
    assert task.workspace_refs["acp_recovery"] == []
    assert not (task.workspace.root / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_taskboard_action_card_retries_retryable_result_protocol_failure(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-card-result-retry").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-card-result-retry",
        goal="Write the final report.",
        success_criteria=["final.md is written."],
        execution="taskboard",
        options={
            "agent_task": {
                "required_deliverables": [{"path": "final.md", "media_type": "text/markdown"}],
                "taskboard_card_max_attempts": 2,
            }
        },
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft final.md.",
            "allowed_execution_shape": "actions",
            "required_outputs": ["final.md exists with a non-empty body"],
        }
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-card-result-retry",
        graph=TaskBoardGraph.from_value({"graph_id": "taskboard-card-result-retry-graph", "cards": [card.to_dict()]}),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )
    attempts: list[int] = []

    async def fake_run_work_unit_through_blocks(*_args, **kwargs):
        attempt_index = kwargs["start_payload"]["attempt_index"]
        attempts.append(attempt_index)
        meta = {
            "execution_id": f"exec-{attempt_index}",
            "status": "success",
            "route": {"selected_route": "model_request", "status": "completed"},
            "logs": {"action_logs": {}, "route_logs": {}, "errors": []},
            "diagnostics": [],
        }
        if attempt_index == 1:
            return (
                {
                    "status": "completed",
                    "answer": "A final.md artifact was produced.",
                    "artifact_manifest": {"path": "final.md", "sections": [{}]},
                    "remaining_work": [],
                },
                meta,
                {},
            )
        return (
            {
                "status": "completed",
                "artifact_markdown": "# Final Report\n\nRecovered body.",
                "artifact_manifest": {"path": "final.md", "sections": [{"title": "Final Report"}]},
                "remaining_work": [],
            },
            meta,
            {},
        )

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_run_work_unit_through_blocks", fake_run_work_unit_through_blocks)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)
    monkeypatch.setattr(cast(Any, task), "_emit_action_observation_events", noop)

    result = await task._run_taskboard_agent_card(
        context,
        {
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert attempts == [1, 2]
    assert result.status == "completed"
    retry_diagnostics = task.diagnostics["taskboard_card_retries"]
    assert retry_diagnostics[0]["code"] == "taskboard.card.result_protocol_retry"
    assert "agent_task.workspace_artifact.empty_body" in retry_diagnostics[0]["retryable_codes"]
    assert result.metadata["attempt_index"] == 2
    readback = await task.workspace.read_file("final.md")
    assert readback["content"] == "# Final Report\n\nRecovered body."


@pytest.mark.asyncio
async def test_taskboard_workspace_file_copy_patch_materializes_target_ref(tmp_path):
    agent = _create_agent("agent-taskboard-file-copy-patch").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-file-copy-patch",
        goal="Copy a trusted workspace artifact to final.md.",
        success_criteria=["final.md matches the source artifact."],
        execution="taskboard",
    )
    source_path = "working/taskboard/coverage-and-finalize/final.md"
    source_body = "# Final\n\n" + "\n".join(f"Paragraph {index}: " + ("body " * 30) for index in range(40))
    await task.workspace.write_file(source_path, source_body)
    await task.workspace.write_file("final.md", "Summary only.")
    context = SimpleNamespace(card=SimpleNamespace(id="final-verification-repair"))

    patched = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "patch_proposal": {
                "kind": "workspace_file_copy",
                "source": source_path,
                "target": "final.md",
            },
        },
    )

    final_read = await task.workspace.read_file("final.md", max_bytes=len(source_body.encode("utf-8")) + 1)
    assert final_read["content"] == source_body
    assert "patch_proposal" not in patched
    assert patched["workspace_patch_delivery"]["status"] == "completed"
    assert patched["workspace_patch_delivery"]["source_path"] == source_path
    assert (
        task._workspace_artifact_display_path(patched["workspace_patch_delivery"]["file_refs"][0]["path"]) == "final.md"
    )
    assert task._workspace_artifact_display_path(patched["file_refs"][0]["path"]) == "final.md"
    assert patched["diagnostics"][0]["code"] == "taskboard.control.workspace_patch_applied"


@pytest.mark.asyncio
async def test_taskboard_grounding_workspace_patch_is_claim_scoped_and_rejects_full_rewrite(tmp_path):
    agent = _create_agent("agent-taskboard-grounding-workspace-patch").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-grounding-workspace-patch",
        goal="Repair only the unsupported claim.",
        success_criteria=["The final artifact remains otherwise unchanged."],
        execution="taskboard",
    )
    original = "# Report\n\nSupported paragraph.\n\nUpside is already priced.\n"
    await task.workspace.write_file("final.md", original)
    promoted = await task.workspace._promote_file_identity("final.md", role="grounding_candidate")
    grounding_contract = {
        "gate_kind": "factual_grounding",
        "issue_code": "unsupported_material_claim",
        "contract_subject": "artifact:factual_integrity",
        "requirements": [
            {
                "claim_key": "candidate_segment:1:claim:1",
                "claim": "Upside is already priced.",
                "artifact_quote": "Upside is already priced.",
                "segment_id": "seg_upside_claim",
                "carrier_id": promoted.get("content_version_id"),
                "content_version_id": promoted.get("content_version_id"),
                "state": "unsupported",
            }
        ],
    }
    context = SimpleNamespace(
        card=SimpleNamespace(
            id="final-verification-repair",
            evidence_contract={
                "material_claim_repair_contract": grounding_contract,
                "material_claim_patch_paths": ["final.md"],
            },
        )
    )

    rejected = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "patch",
            "patch_proposal": {
                "path": "final.md",
                "operations": [{"op": "write", "content": "# Rewritten report"}],
            },
        },
    )
    unchanged = await task.workspace.read_file("final.md")

    assert rejected["status"] == "blocked"
    assert rejected["sufficient"] is False
    assert rejected["workspace_patch_delivery"]["status"] == "failed"
    assert rejected["diagnostics"][-1]["code"] == "taskboard.control.grounding_patch_out_of_scope"
    assert unchanged["content"] == original

    patched = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "patch",
            "patch_proposal": {
                "path": "final.md",
                "operations": [
                    {
                        "claim_key": "candidate_segment:1:claim:1",
                        "op": "replace",
                        "old_string": "Upside is already priced.",
                        "new_string": "Data-center demand remains the primary growth driver.",
                    }
                ],
            },
        },
    )
    readback = await task.workspace.read_file("final.md")

    assert patched["status"] == "completed"
    assert patched["workspace_patch_delivery"]["status"] == "completed"
    assert readback["content"] == (
        "# Report\n\nSupported paragraph.\n\nData-center demand remains the primary growth driver.\n"
    )


@pytest.mark.asyncio
async def test_taskboard_grounding_workspace_patch_scope_ignores_markdown_emphasis(tmp_path):
    agent = _create_agent("agent-taskboard-grounding-markdown-patch").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-grounding-markdown-patch",
        goal="Repair only the unsupported claim.",
        success_criteria=["The final artifact remains otherwise unchanged."],
        execution="taskboard",
    )
    original = (
        "# Report\n\n"
        "**Valuation multiples:** AVGO's multiple expansion is a specific watchpoint; "
        "NVDA and AMD trade at elevated multiples relative to history.\n"
    )
    await task.workspace.write_file("final.md", original)
    promoted = await task.workspace._promote_file_identity("final.md", role="grounding_candidate")
    quote = (
        "Valuation multiples: AVGO's multiple expansion is a specific watchpoint; "
        "NVDA and AMD trade at elevated multiples relative to history."
    )
    context = SimpleNamespace(
        card=SimpleNamespace(
            id="final-verification-repair",
            evidence_contract={
                "material_claim_repair_contract": {
                    "requirements": [
                        {
                            "claim_key": "candidate_segment:1:claim:1",
                            "claim": quote,
                            "artifact_quote": quote,
                            "segment_id": "seg_valuation_claim",
                            "carrier_id": promoted.get("content_version_id"),
                            "content_version_id": promoted.get("content_version_id"),
                            "state": "unsupported",
                        }
                    ]
                },
                "material_claim_patch_paths": ["final.md"],
            },
        )
    )

    patched = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "patch",
            "patch_proposal": {
                "path": "final.md",
                "operations": [
                    {
                        "claim_key": "candidate_segment:1:claim:1",
                        "op": "replace",
                        "old_string": (
                            "**Valuation multiples:** AVGO's multiple expansion is a specific watchpoint; "
                            "NVDA and AMD trade at elevated multiples relative to history."
                        ),
                        "new_string": "**Valuation multiples:** AVGO's multiple expansion is a specific watchpoint.",
                    }
                ],
            },
        },
    )
    readback = await task.workspace.read_file("final.md")

    assert patched["status"] == "completed"
    assert patched["workspace_patch_delivery"]["status"] == "completed"
    assert readback["content"] == (
        "# Report\n\n**Valuation multiples:** AVGO's multiple expansion is a specific watchpoint.\n"
    )


@pytest.mark.asyncio
async def test_taskboard_grounding_workspace_patch_uses_host_validated_artifact_quote(tmp_path):
    agent = _create_agent("agent-taskboard-grounding-artifact-quote").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-grounding-artifact-quote",
        goal="Repair only the unsupported claim.",
        success_criteria=["The final artifact remains otherwise unchanged."],
        execution="taskboard",
    )
    quote = "Integration complexity and elevated valuation multiples could amplify drawdowns."
    original = f"# Report\n\n- **Downside risks:** {quote}\n"
    await task.workspace.write_file("final.md", original)
    promoted = await task.workspace._promote_file_identity("final.md", role="grounding_candidate")
    context = SimpleNamespace(
        card=SimpleNamespace(
            id="final-verification-repair",
            evidence_contract={
                "material_claim_repair_contract": {
                    "requirements": [
                        {
                            "claim_key": "candidate_segment:1:claim:1",
                            "claim": "AVGO downside risks overstate drawdown amplification.",
                            "artifact_quote": quote,
                            "segment_id": "seg_claim_1",
                            "carrier_id": promoted.get("content_version_id"),
                            "content_version_id": promoted.get("content_version_id"),
                            "state": "unsupported",
                        }
                    ]
                },
                "material_claim_patch_paths": ["final.md"],
            },
        )
    )

    patched = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "patch",
            "patch_proposal": {
                "path": "final.md",
                "operations": [
                    {
                        "claim_key": "candidate_segment:1:claim:1",
                        "op": "replace",
                        "old_string": quote,
                        "new_string": "Integration workload and valuation multiple expansion require monitoring.",
                    }
                ],
            },
        },
    )
    readback = await task.workspace.read_file("final.md")

    assert patched["status"] == "completed"
    assert patched["workspace_patch_delivery"]["status"] == "completed"
    assert readback["content"] == (
        "# Report\n\n- **Downside risks:** Integration workload and valuation multiple expansion require monitoring.\n"
    )


@pytest.mark.asyncio
async def test_taskboard_grounding_workspace_patch_requires_exact_claim_coverage_and_current_version(tmp_path):
    agent = _create_agent("agent-taskboard-grounding-exact-coverage").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-grounding-exact-coverage",
        goal="Repair every rejected claim without rewriting unrelated content.",
        success_criteria=["Every rejected claim is repaired against the current artifact version."],
        execution="taskboard",
    )
    first_quote = "Margin gaps add downside watchpoints."
    second_quote = "Execution ramp is already de-risked."
    original = f"# Report\n\n{first_quote}\n\n{second_quote}\n"
    await task.workspace.write_file("final.md", original)
    promoted = await task.workspace._promote_file_identity("final.md", role="grounding_candidate")
    requirements = [
        {
            "claim_key": "candidate_segment:1:claim:1",
            "claim": first_quote,
            "artifact_quote": first_quote,
            "segment_id": "seg_margin_claim",
            "carrier_id": promoted.get("content_version_id"),
            "content_version_id": promoted.get("content_version_id"),
            "state": "unsupported",
        },
        {
            "claim_key": "candidate_segment:1:claim:2",
            "claim": second_quote,
            "artifact_quote": second_quote,
            "segment_id": "seg_ramp_claim",
            "carrier_id": promoted.get("content_version_id"),
            "content_version_id": promoted.get("content_version_id"),
            "state": "unsupported",
        },
    ]
    context = SimpleNamespace(
        card=SimpleNamespace(
            id="final-verification-repair",
            evidence_contract={
                "material_claim_repair_contract": {"requirements": requirements},
                "material_claim_patch_paths": ["final.md"],
            },
        )
    )

    partial = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "patch",
            "patch_proposal": {
                "path": "final.md",
                "operations": [
                    {
                        "claim_key": "candidate_segment:1:claim:2",
                        "op": "replace",
                        "old_string": second_quote,
                        "new_string": "Execution remains a monitored risk.",
                    }
                ],
            },
        },
    )
    unchanged = await task.workspace.read_file("final.md")

    assert partial["status"] == "blocked"
    assert partial["workspace_patch_delivery"]["status"] == "failed"
    assert "exactly one" in partial["workspace_patch_delivery"]["reason"]
    assert unchanged["content"] == original

    exact = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "patch",
            "patch_proposal": {
                "path": "final.md",
                "operations": [
                    {
                        "claim_key": "candidate_segment:1:claim:1",
                        "op": "replace",
                        "old_string": first_quote,
                        "new_string": "Margin gaps remain investor watchpoints.",
                    },
                    {
                        "claim_key": "candidate_segment:1:claim:2",
                        "op": "replace",
                        "old_string": second_quote,
                        "new_string": "Execution remains a monitored risk.",
                    },
                ],
            },
        },
    )
    exact_readback = await task.workspace.read_file("final.md")

    assert exact["status"] == "completed"
    assert exact["workspace_patch_delivery"]["status"] == "completed"
    assert exact["workspace_patch_delivery"]["base_content_version_id"] == promoted.get("content_version_id")
    assert exact["workspace_patch_delivery"]["content_version_id"] != promoted.get("content_version_id")
    assert exact_readback["content"] == (
        "# Report\n\nMargin gaps remain investor watchpoints.\n\nExecution remains a monitored risk.\n"
    )

    externally_changed = original.replace("# Report", "# Updated Report")
    await task.workspace.write_file("final.md", externally_changed)
    stale = await task._materialize_taskboard_workspace_patch(
        context,
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "patch",
            "patch_proposal": {
                "path": "final.md",
                "operations": [
                    {
                        "claim_key": "candidate_segment:1:claim:1",
                        "op": "replace",
                        "old_string": first_quote,
                        "new_string": "Margin gaps remain investor watchpoints.",
                    },
                    {
                        "claim_key": "candidate_segment:1:claim:2",
                        "op": "replace",
                        "old_string": second_quote,
                        "new_string": "Execution remains a monitored risk.",
                    },
                ],
            },
        },
    )
    stale_readback = await task.workspace.read_file("final.md")

    assert stale["status"] == "blocked"
    assert stale["workspace_patch_delivery"]["status"] == "failed"
    assert "changed since the repair contract" in stale["workspace_patch_delivery"]["reason"]
    assert stale_readback["content"] == externally_changed


@pytest.mark.asyncio
async def test_flat_grounding_file_repair_uses_host_patch_without_action_full_rewrite(tmp_path, monkeypatch):
    agent = _create_agent("agent-flat-grounding-host-patch").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="flat-grounding-host-patch",
        goal="Repair only the unsupported claim.",
        success_criteria=["The final artifact remains otherwise unchanged."],
        execution="flat",
        options={"required_deliverables": ["final.md"]},
    )
    original = (
        "# Portfolio brief\n\n"
        "Supported allocation paragraph.\n\n"
        "NVDA's dominance in data-center drives growth.\n"
    )
    write_result = await task.workspace.write_file("final.md", original)
    await task._replace_terminal_carriers(
        execution_result={"file_refs": write_result["file_refs"], "final_result": "final.md"},
        execution_evidence_summary={},
        source_work_result_id="work:grounding-host-patch",
    )
    inventory = task._lifecycle_state.carrier_inventory
    assert inventory is not None
    current_carrier = inventory.carriers[0]
    quote = "NVDA's dominance in data-center drives growth."
    repair_context = {
        "source_iteration": 1,
        "material_claim_repair_contract": {
            "gate_kind": "factual_grounding",
            "issue_code": "unsupported_material_claim",
            "contract_subject": "artifact:factual_integrity",
            "requirements": [
                {
                    "claim_key": "candidate_segment:2:claim:1",
                    "claim": quote,
                    "artifact_quote": quote,
                    "segment_id": "seg_nvda_claim",
                    "carrier_id": current_carrier.carrier_id,
                    "content_version_id": current_carrier.content_version_id,
                    "state": "unsupported",
                    "reason": "The sources support data-center demand as a growth driver, not dominance.",
                }
            ],
        },
        "available_evidence_anchors": {
            "source_refs": [
                {
                    "reference_id": "ref_nvda_market",
                    "body_preview": "Data-center demand remains the primary growth driver.",
                }
            ]
        },
    }
    monkeypatch.setattr(cast(Any, task), "_active_repair_context", lambda: repair_context)

    captured: dict[str, Any] = {}

    class FakeRequest:
        def input(self, value):
            captured["input"] = value
            return self

        def info(self, value):
            captured["info"] = value
            return self

        def instruct(self, value):
            captured["instruct"] = value
            return self

        def output(self, value, *, format):
            captured["output"] = value
            captured["format"] = format
            return self

        async def async_get_data(self):
            return {
                "step_result": "Replaced only the unsupported NVDA wording.",
                "patch_proposal": {
                    "path": "final.md",
                    "operations": [
                        {
                            "claim_key": "candidate_segment:2:claim:1",
                            "op": "replace",
                            "old_string": quote,
                            "new_string": "Data-center demand remains the primary growth driver.",
                        }
                    ],
                },
                "remaining_work": [],
                "ready_for_final_verification": True,
            }

    monkeypatch.setattr(agent, "create_temp_request", lambda: FakeRequest())
    monkeypatch.setattr(cast(Any, task), "_apply_language_policy_to_request", lambda *_args, **_kwargs: None)

    def forbid_action_execution(*_args, **_kwargs):
        raise AssertionError("Grounding-only file repair must not open AgentExecution/ActionRuntime.")

    monkeypatch.setattr(agent, "create_execution", forbid_action_execution)

    async def run_work_unit(*, work_unit, plan, context_pack, execution_id, handler, start_payload):
        _ = plan, context_pack, execution_id
        captured["work_unit"] = work_unit.to_dict()
        captured["start_payload"] = start_payload
        block_output = await handler({"carrier_output_policy": {"control_format": "json"}})
        return block_output["execution_result"], block_output["execution_meta"], None

    monkeypatch.setattr(cast(Any, task), "_run_work_unit_through_blocks", run_work_unit)

    result, meta = await task._execute_step(
        2,
        {
            "execution_shape": "actions",
            "effective_execution_shape": "actions",
            "step_instruction": "Repair the final report and write it back.",
            "expected_evidence": "A corrected final.md readback.",
            "rationale": "The grounding gate rejected one claim.",
            "deliverable_mode": "workspace_artifact",
        },
        cast(
            Any,
            {
                "goal": task.goal,
                "items": [],
                "omitted": [],
                "profile": "balanced",
                "diagnostics": {},
            },
        ),
    )
    readback = await task.workspace.read_file("final.md")

    assert readback["content"] == (
        "# Portfolio brief\n\n"
        "Supported allocation paragraph.\n\n"
        "Data-center demand remains the primary growth driver.\n"
    )
    assert result["workspace_patch_delivery"]["status"] == "completed"
    assert result["workspace_patch_delivery"]["operation_count"] == 1
    assert result["file_refs"][0]["content_version_id"] != current_carrier.content_version_id
    assert meta["status"] == "completed"
    assert meta["logs"]["action_logs"] == {}
    assert captured["work_unit"]["runtime_preferences"]["handler"] == "agent_task_material_claim_patch"
    assert captured["work_unit"]["capability_scope"] == []
    assert "plan" not in captured["work_unit"]["input_payload"]
    assert "repair_context" not in captured["work_unit"]["input_payload"]
    assert "plan" not in captured["start_payload"]
    assert "context_pack" not in captured["start_payload"]
    assert "plan" not in captured["input"]
    assert "repair_context" not in captured["input"]
    assert captured["info"]["material_claim_repair_contract"] == repair_context[
        "material_claim_repair_contract"
    ]
    assert "candidate_final_result" not in captured["output"]
    patch_schema = captured["output"]["patch_proposal"][0]
    assert patch_schema["operations"][0][0]["claim_key"][2] is True
    assert "full-file" in captured["instruct"]


@pytest.mark.asyncio
async def test_flat_grounding_host_patch_fails_closed_for_rewrite_scope_and_stale_version(tmp_path):
    agent = _create_agent("agent-flat-grounding-patch-fail-closed").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="flat-grounding-patch-fail-closed",
        goal="Repair only one unsupported claim.",
        success_criteria=["Unrelated artifact content remains unchanged."],
        execution="flat",
        options={"required_deliverables": ["final.md"]},
    )
    quote = "Unsupported dominance claim."
    original = f"# Report\n\nSupported paragraph.\n\n{quote}\n"
    await task.workspace.write_file("final.md", original)
    promoted = await task.workspace._promote_file_identity("final.md", role="grounding_candidate")
    contract = {
        "requirements": [
            {
                "claim_key": "claim:dominance",
                "claim": quote,
                "artifact_quote": quote,
                "segment_id": "seg_dominance",
                "carrier_id": promoted.get("content_version_id"),
                "content_version_id": promoted.get("content_version_id"),
                "state": "unsupported",
            }
        ]
    }

    rewrite = await task._apply_grounding_workspace_patch(
        {
            "path": "final.md",
            "operations": [{"op": "write", "content": "# Rewritten"}],
        },
        contract,
        allowed_patch_paths=["final.md"],
        source="test.flat.grounding_patch",
    )
    unauthorized = await task._apply_grounding_workspace_patch(
        {
            "path": "other.md",
            "operations": [
                {
                    "claim_key": "claim:dominance",
                    "op": "replace",
                    "old_string": quote,
                    "new_string": "Supported narrower claim.",
                }
            ],
        },
        contract,
        allowed_patch_paths=["final.md"],
        source="test.flat.grounding_patch",
    )
    unchanged = await task.workspace.read_file("final.md")

    assert rewrite["status"] == "failed"
    assert "forbids full writes" in rewrite["reason"]
    assert unauthorized["status"] == "failed"
    assert "authorized" in unauthorized["reason"]
    assert unchanged["content"] == original

    externally_changed = original.replace("Supported paragraph.", "Updated supported paragraph.")
    await task.workspace.write_file("final.md", externally_changed)
    stale = await task._apply_grounding_workspace_patch(
        {
            "path": "final.md",
            "operations": [
                {
                    "claim_key": "claim:dominance",
                    "op": "replace",
                    "old_string": quote,
                    "new_string": "Supported narrower claim.",
                }
            ],
        },
        contract,
        allowed_patch_paths=["final.md"],
        source="test.flat.grounding_patch",
    )
    stale_readback = await task.workspace.read_file("final.md")

    assert stale["status"] == "failed"
    assert "changed since the repair contract" in stale["reason"]
    assert stale_readback["content"] == externally_changed


def test_taskboard_control_continue_preserves_current_card_status():
    agent = _create_agent("agent-taskboard-control-continue-status")
    task = AgentTask(
        agent,
        task_id="taskboard-control-continue-status",
        goal="Complete synthesis before downstream delivery.",
        success_criteria=["The synthesis card completes once."],
        execution="taskboard",
    )

    completed = task._taskboard_control_card_status(
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "continue",
            "remaining_work": ["A downstream delivery card will write final.md."],
        }
    )
    setback = task._taskboard_control_card_status(
        {
            "status": "setback",
            "sufficient": False,
            "next_board_action": "continue",
            "gaps": ["Current-card evidence is incomplete."],
        }
    )

    assert completed == "completed"
    assert setback == "setback"


def test_taskboard_control_stop_preserves_completed_card_status():
    agent = _create_agent("agent-taskboard-control-stop-status")
    task = AgentTask(
        agent,
        task_id="taskboard-control-stop-status",
        goal="Return the completed compact summary and stop the board.",
        success_criteria=["The summary card completes without a repair card."],
        execution="taskboard",
    )

    status = task._taskboard_control_card_status(
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "stop",
            "gaps": [],
            "remaining_work": [],
        }
    )

    assert status == "completed"


@pytest.mark.asyncio
async def test_taskboard_action_card_repairs_binding_without_repeating_business_actions(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-card-evidence-use-retry").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-card-evidence-use-retry",
        goal="Collect source evidence.",
        success_criteria=["The card reports collected evidence without invalid evidence bindings."],
        execution="taskboard",
        options={"agent_task": {"taskboard_card_max_attempts": 2}},
    )
    card = TaskBoardCard.from_value(
        {
            "id": "collect",
            "objective": "Collect source evidence for the final answer.",
            "allowed_execution_shape": "actions",
            "required_outputs": ["Collected source evidence summary"],
        }
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-card-evidence-use-retry",
        graph=TaskBoardGraph.from_value(
            {"graph_id": "taskboard-card-evidence-use-retry-graph", "cards": [card.to_dict()]}
        ),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )
    attempts: list[int] = []
    binding_requests: list[list[dict[str, Any]]] = []

    async def fake_run_work_unit_through_blocks(*_args, **kwargs):
        attempt_index = kwargs["start_payload"]["attempt_index"]
        attempts.append(attempt_index)
        meta = {
            "execution_id": f"exec-{attempt_index}",
            "status": "success",
            "route": {"selected_route": "model_request", "status": "completed"},
            "logs": {
                "action_logs": [
                    {
                        "action_id": "market_news",
                        "status": "success",
                        "action_call_id": "act_call_nvda",
                        "input": {"ticker": "NVDA", "limit": 5},
                        "model_digest": {
                            "result_preview": {"count": 5, "summary": "Recent company coverage collected."}
                        },
                    },
                    {
                        "action_id": "market_news",
                        "status": "success",
                        "action_call_id": "act_call_avgo",
                        "input": {"ticker": "AVGO", "limit": 5},
                        "model_digest": {
                            "result_preview": {"count": 5, "summary": "Recent company coverage collected."}
                        },
                    },
                ],
                "route_logs": {},
                "errors": [],
            },
            "diagnostics": [],
        }
        return (
            {
                "status": "completed",
                "answer": "The requested NVDA news retrieval completed.",
                "evidence_use": [
                    {
                        "claim": "The requested NVDA news retrieval completed.",
                        "evidence_ids": ["agent_task_action_result:market_news:act_call_nvd4"],
                        "support_type": "content",
                    }
                ],
                "remaining_work": [],
            },
            meta,
            {},
        )

    async def fake_request_evidence_binding_repair(grounding_guard, evidence_ledger, *, language_policy):
        candidates = task._evidence_binding_repair_candidate_refs(evidence_ledger)
        binding_requests.append(candidates)
        nvda = next(item for item in candidates if item.get("input_preview", {}).get("ticker") == "NVDA")
        current = grounding_guard["normalized_evidence_use"][0]
        return [
            {
                "claim_index": 0,
                "claim": current["claim"],
                "evidence_ids": [nvda["reference_id"]],
                "support_type": current["support_type"],
            }
        ]

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_run_work_unit_through_blocks", fake_run_work_unit_through_blocks)
    monkeypatch.setattr(
        cast(Any, task),
        "_request_evidence_binding_repair",
        fake_request_evidence_binding_repair,
    )
    monkeypatch.setattr(cast(Any, task), "_emit", noop)
    monkeypatch.setattr(cast(Any, task), "_emit_action_observation_events", noop)

    result = await task._run_taskboard_agent_card(
        context,
        {
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert attempts == [1]
    assert len(binding_requests) == 1
    assert {
        item["input_preview"]["ticker"]
        for item in binding_requests[0]
        if item.get("action_id") == "market_news" and isinstance(item.get("input_preview"), Mapping)
    } == {"NVDA", "AVGO"}
    assert result.status == "completed"
    assert "taskboard_card_retries" not in task.diagnostics
    assert result.metadata["attempt_index"] == 1
    assert result.metadata["evidence_use_guard"]["valid"] is True
    assert any(
        item.get("code") == "taskboard.card.model_evidence_binding_repair"
        for item in result.diagnostics
    )


@pytest.mark.asyncio
async def test_taskboard_finalizer_repairs_binding_before_terminal_verifier(tmp_path, monkeypatch):
    from agently.core.application.AgentTask.EvidenceLedger import collect_evidence_use, validate_evidence_use

    task = AgentTask(
        _create_agent("taskboard-finalizer-binding-repair").use_workspace(tmp_path / "workspace"),
        task_id="taskboard-finalizer-binding-repair",
        goal="Produce an NVDA evidence-backed result.",
        success_criteria=["The NVDA claim uses the matching stable evidence ref."],
        execution="taskboard",
    )
    ledger = task._stable_evidence_ledger_view(
        {
            "evidence_items": [
                {
                    "id": "agent_task_action_result:market_news:act_call_nvda",
                    "kind": "agent_task.action.result",
                    "status": "ok",
                    "body_state": "bounded",
                    "action_id": "market_news",
                    "action_call_id": "act_call_nvda",
                    "input_preview": {"ticker": "NVDA"},
                    "body": "Recent NVDA company coverage was collected.",
                },
                {
                    "id": "agent_task_action_result:market_news:act_call_avgo",
                    "kind": "agent_task.action.result",
                    "status": "ok",
                    "body_state": "bounded",
                    "action_id": "market_news",
                    "action_call_id": "act_call_avgo",
                    "input_preview": {"ticker": "AVGO"},
                    "body": "Recent AVGO company coverage was collected.",
                },
            ]
        },
        max_items=16,
        body_chars=1200,
    )
    final = {
        "accepted": True,
        "final_result": "NVDA company coverage was collected.",
        "evidence_use": [
            {
                "claim": "NVDA company coverage was collected.",
                "evidence_ids": ["agent_task_action_result:market_news:act_call_nvd4"],
                "support_type": "content",
            }
        ],
    }
    guard = validate_evidence_use(collect_evidence_use(final), ledger)
    model_calls = 0

    async def fake_request_evidence_binding_repair(grounding_guard, evidence_ledger, *, language_policy):
        nonlocal model_calls
        model_calls += 1
        candidates = task._evidence_binding_repair_candidate_refs(evidence_ledger)
        nvda = next(item for item in candidates if item.get("input_preview", {}).get("ticker") == "NVDA")
        current = grounding_guard["normalized_evidence_use"][0]
        return [
            {
                "claim_index": 0,
                "claim": current["claim"],
                "evidence_ids": [nvda["reference_id"]],
                "support_type": current["support_type"],
            }
        ]

    monkeypatch.setattr(
        cast(Any, task),
        "_request_evidence_binding_repair",
        fake_request_evidence_binding_repair,
    )

    repaired_final, repaired_guard = await task._repair_taskboard_final_evidence_use(
        final,
        guard,
        ledger,
        language_policy=task._language_policy(),
    )

    nvda_ref = next(
        item["reference_id"]
        for item in ledger["items"]
        if item.get("input_preview", {}).get("ticker") == "NVDA"
    )
    assert model_calls == 1
    assert repaired_guard["valid"] is True
    assert repaired_final["evidence_use"][0]["evidence_ids"] == [nvda_ref]


def test_taskboard_final_verification_failure_creates_repair_revision(tmp_path):
    agent = _create_agent("agent-taskboard-final-repair").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-repair",
        goal="Produce a source-grounded final deliverable.",
        success_criteria=["Unsupported facts are removed or backed by evidence."],
        execution="taskboard",
        max_iterations=2,
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    collect = TaskBoardCard.from_value(
        {
            "id": "collect",
            "objective": "Collect official source evidence.",
            "required_outputs": ["Official source evidence"],
        }
    )
    draft = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final deliverable.",
            "depends_on": ["collect"],
            "required_outputs": ["Final deliverable"],
            "allowed_execution_shape": "control",
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-repair",
            "revision_id": "rev-1",
            "graph": {
                "graph_id": "taskboard-final-repair-graph",
                "cards": [collect.to_dict(), draft.to_dict()],
            },
            "card_results": {
                "collect": TaskBoardCardResult(card_id="collect", status="completed").to_dict(),
                "draft": TaskBoardCardResult(card_id="draft", status="completed").to_dict(),
            },
        }
    )

    repaired = task._taskboard_final_verification_repair_revision(
        revision,
        final={
            "accepted": False,
            "reason": "unsupported labels remain",
            "final_result": "draft",
            "evidence_use": [
                {
                    "claim": "The official source supports the draft.",
                    "evidence_ids": ["ref_source"],
                    "support_type": "content",
                }
            ],
        },
        final_verification={
            "is_complete": False,
            "requires_block": False,
            "reason": "unsupported sub-section labels are not in evidence",
            "missing_criteria": ["Remove unsupported sub-section labels."],
            "next_step_requirements": ["Use only verifier-visible source titles."],
            "acceptance_delta": ["Preserve source URLs."],
        },
    )

    assert repaired is not None
    cards = repaired.graph.card_by_id()
    repair_ids = [
        card_id
        for card_id, card in cards.items()
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    ]
    assert len(repair_ids) == 1
    repair = cards[repair_ids[0]]
    assert repair.allowed_execution_shape == "auto"
    assert set(repair.depends_on) == {"collect", "draft"}
    assert "Remove unsupported sub-section labels." in repair.evidence_contract["missing_criteria"]
    assert repair.evidence_contract["prior_final_evidence_use"] == [
        {
            "claim": "The official source supports the draft.",
            "evidence_ids": ["ref_source"],
            "support_type": "content",
        }
    ]
    assert repair.metadata["final_workspace_deliverables"] == ["final.md"]
    assert repair.metadata["terminal_convergence_subject"] == "taskboard_final_verification"
    assert any("final.md" in item for item in repair.required_outputs)
    assert any(item.get("code") == "taskboard.final_verification.repair_patch" for item in repaired.diagnostics)
    schedule = TaskBoard(repaired, handler=lambda _context: None).schedule()
    assert schedule.runnable_card_ids == (repair.id,)
    assert task.diagnostics["taskboard_final_repair_patches"][0]["repair_card_id"] == repair.id


def _completed_taskboard_revision_for_final_repair(board_id: str) -> TaskBoardRevision:
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final deliverable.",
            "required_outputs": ["Final deliverable"],
            "allowed_execution_shape": "control",
        }
    )
    return TaskBoardRevision.from_value(
        {
            "board_id": board_id,
            "revision_id": "rev-1",
            "graph": {
                "graph_id": f"{board_id}-graph",
                "cards": [card.to_dict()],
            },
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                ).to_dict()
            },
        }
    )


def test_taskboard_final_verification_capability_repair_uses_exact_action_requirement(
    tmp_path,
):
    requirement = {
        "capability_id": "required_probe_action",
        "capability_kind": "action",
        "kind": "action_succeeded",
        "required": True,
        "source": "criterion",
    }
    agent = _create_agent("agent-taskboard-capability-repair").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-capability-repair",
        goal="Produce the final deliverable with required capability evidence.",
        success_criteria=["The required capability succeeds."],
        execution="taskboard",
        options={
            "planner_capabilities": [
                {
                    "id": "required_probe_action",
                    "kind": "action",
                    "route": "model_request",
                    "guidance_access": "none",
                    "description": "Produce required probe evidence.",
                }
            ],
            "capability_evidence_requirements": [requirement, dict(requirement)],
        },
    )
    revision = _completed_taskboard_revision_for_final_repair(task.id)

    repaired = task._taskboard_final_verification_repair_revision(
        revision,
        final={"accepted": False, "final_result": "draft"},
        final_verification={
            "is_complete": False,
            "requires_block": False,
            "missing_capability_evidence": [
                "required_probe_action",
                "required_probe_action",
            ],
            "missing_criteria": ["Missing required capability evidence."],
        },
    )

    assert repaired is not None
    repairs = [
        card
        for card in repaired.graph.cards
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    ]
    assert len(repairs) == 1
    repair = repairs[0]
    assert repair.allowed_execution_shape == "actions"
    assert repair.evidence_contract["capability_evidence_requirements"] == [requirement]
    assert repair.evidence_contract["requires_capability_ids"] == ["required_probe_action"]
    assert repair.metadata["requires_capability_ids"] == ["required_probe_action"]
    assert repair.depends_on == ("draft",)
    assert any(item.get("code") == "taskboard.final_verification.repair_patch" for item in repaired.diagnostics)
    assert TaskBoard(repaired, handler=lambda _context: None).schedule().runnable_card_ids == (repair.id,)


def test_taskboard_material_claim_repair_uses_structured_claim_contract(tmp_path):
    agent = _create_agent("agent-taskboard-grounding-repair").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-grounding-repair",
        goal="Produce a grounded deliverable.",
        success_criteria=["Every material claim is grounded."],
        execution="taskboard",
    )
    task._lifecycle_state.replace_carriers(
        [
            {
                "carrier_id": "car_grounded_candidate",
                "kind": "workspace_artifact",
                "required": True,
                "path": "final.md",
                "content_version_id": "cv_grounded_candidate",
                "content_digest": hashlib.sha256(
                    b"Unsupported factual claim."
                ).hexdigest(),
                "source_work_result_id": "work:taskboard-repair",
                "status": "materialized",
            }
        ],
        expected_version=task._lifecycle_state.state_version,
    )
    revision = _completed_taskboard_revision_for_final_repair(task.id)
    repair_contract = {
        "gate_kind": "factual_integrity",
        "issue_code": "unsupported_material_claim",
        "contract_subject": "carrier:car_grounded_candidate",
        "requirements": [
            {
                "claim_key": "claim:1",
                "carrier_id": "car_grounded_candidate",
                "path": "final.md",
                "content_version_id": "cv_grounded_candidate",
                "artifact_quote": "Unsupported factual claim.",
                "state": "unsupported",
                "reason": "No eligible source supports it.",
            }
        ],
    }

    repaired = task._taskboard_final_verification_repair_revision(
        revision,
        final={"accepted": False, "final_result": "draft"},
        final_verification={
            "is_complete": False,
            "requires_block": False,
            "reason": "Free-form verifier prose is not the repair contract.",
            "missing_criteria": ["Unrelated prose gap."],
            "material_claim_repair_contract": repair_contract,
        },
    )

    assert repaired is not None
    repair = next(
        card
        for card in repaired.graph.cards
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    )
    assert repair.metadata["repair_source"] == "material_claim_audit"
    assert repair.metadata["terminal_convergence_subject"] == "carrier:car_grounded_candidate"
    assert repair.evidence_contract["material_claim_repair_contract"] == repair_contract
    assert repair.evidence_contract["material_claim_patch_paths"] == ["final.md"]
    assert repair.metadata["final_workspace_deliverables"] == ["final.md"]
    assert "Unsupported factual claim." in repair.objective
    assert "Unrelated prose gap." not in repair.objective
    assert "Change only the implicated claims" in repair.objective
    assert "preserve all unrelated artifact text" in repair.objective
    assert "Workspace replace patch" in repair.objective
    assert "Do not return or rewrite the complete artifact body" in repair.objective
    assert "Produce a complete corrected deliverable" not in repair.objective
    assert repair.allowed_execution_shape == "control"


@pytest.mark.parametrize(
    ("requirement", "final_verification"),
    [
        (
            {
                "capability_id": "required_probe_action",
                "capability_kind": "action",
                "kind": "action_succeeded",
                "required": True,
                "source": "criterion",
            },
            {
                "reason": "Verifier prose mentions required_probe_action.",
                "missing_capability_evidence": [],
                "missing_criteria": ["Repair the final deliverable."],
            },
        ),
        (
            {
                "capability_id": "required_probe_action",
                "capability_kind": "action",
                "kind": "capability_used",
                "required": True,
                "source": "criterion",
            },
            {
                "missing_capability_evidence": ["required_probe_action"],
                "missing_criteria": ["Missing general capability evidence."],
            },
        ),
    ],
)
def test_taskboard_final_verification_non_action_repair_keeps_capabilities_available_without_inference(
    tmp_path,
    requirement,
    final_verification,
):
    agent = _create_agent("agent-taskboard-control-repair").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-control-repair",
        goal="Repair the final deliverable.",
        success_criteria=["The deliverable is complete."],
        execution="taskboard",
        options={
            "planner_capabilities": [{"id": "required_probe_action", "kind": "action"}],
            "capability_evidence_requirements": [requirement],
        },
    )

    repaired = task._taskboard_final_verification_repair_revision(
        _completed_taskboard_revision_for_final_repair(task.id),
        final={"accepted": False, "final_result": "draft"},
        final_verification=final_verification,
    )

    assert repaired is not None
    repair = next(
        card
        for card in repaired.graph.cards
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    )
    assert repair.allowed_execution_shape == "auto"
    assert "capability_evidence_requirements" not in repair.evidence_contract
    assert "requires_capability_ids" not in repair.metadata


def test_taskboard_final_verification_action_repair_fails_closed_when_unavailable(
    tmp_path,
):
    agent = _create_agent("agent-taskboard-unavailable-capability-repair").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-unavailable-capability-repair",
        goal="Produce required Action evidence.",
        success_criteria=["The required Action succeeds."],
        execution="taskboard",
        options={
            "planner_capabilities": [{"id": "different_available_action", "kind": "action"}],
            "capability_evidence_requirements": [
                {
                    "capability_id": "required_probe_action",
                    "capability_kind": "action",
                    "kind": "action_succeeded",
                    "required": True,
                    "source": "criterion",
                }
            ],
        },
    )

    repaired = task._taskboard_final_verification_repair_revision(
        _completed_taskboard_revision_for_final_repair(task.id),
        final={"accepted": False, "final_result": "draft"},
        final_verification={
            "missing_capability_evidence": ["required_probe_action"],
            "missing_criteria": ["Missing required capability evidence."],
        },
    )

    assert repaired is None
    diagnostics = task.diagnostics["taskboard_final_repair_unavailable_capabilities"]
    assert diagnostics == [
        {
            "code": "taskboard.final_verification.repair_capability_unavailable",
            "unavailable_capability_ids": ["required_probe_action"],
            "revision_id": "rev-1",
        }
    ]


def test_workspace_readback_does_not_satisfy_required_action_evidence(tmp_path):
    agent = _create_agent("agent-workspace-readback-action-gate").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="workspace-readback-action-gate",
        goal="Produce evidence through the specified Action.",
        success_criteria=["The specified Action succeeds."],
        execution="taskboard",
        options={
            "planner_capabilities": [{"id": "required_probe_action", "kind": "action"}],
            "capability_evidence_requirements": [
                {
                    "capability_id": "required_probe_action",
                    "capability_kind": "action",
                    "kind": "action_succeeded",
                    "required": True,
                    "source": "criterion",
                }
            ],
        },
    )
    verifier_claim = {
        "is_complete": True,
        "requires_block": False,
        "reason": "The file was read back from Workspace.",
        "missing_criteria": [],
        "final_result": "final.md",
    }
    workspace_only_evidence = {
        "status": "completed",
        "action_ids": [],
        "capabilities_used": [],
        "capability_evidence": {
            "actions": {"succeeded": [], "failed": []},
            "artifacts": {"readback": ["workspace_artifact_readback:required_probe_action:final.md"]},
        },
    }

    blocked = task._normalize_verification(
        verifier_claim,
        execution_evidence_summary=workspace_only_evidence,
    )

    assert blocked["is_complete"] is False
    assert blocked["missing_capability_evidence"] == ["required_probe_action"]
    assert "capability_evidence_missing" in blocked["guard_reasons"]

    satisfied = task._normalize_verification(
        verifier_claim,
        execution_evidence_summary={
            **workspace_only_evidence,
            "action_ids": ["required_probe_action"],
            "capabilities_used": ["required_probe_action"],
            "capability_evidence": {
                "actions": {
                    "succeeded": ["required_probe_action"],
                    "failed": [],
                },
                "artifacts": workspace_only_evidence["capability_evidence"]["artifacts"],
            },
        },
    )

    assert satisfied["is_complete"] is True
    assert satisfied["missing_capability_evidence"] == []


@pytest.mark.asyncio
async def test_terminal_capability_preflight_skips_semantic_verifier_for_missing_action(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-terminal-capability-preflight").use_workspace(
        tmp_path / "workspace"
    )
    requirement = {
        "capability_id": "write_file",
        "capability_kind": "action",
        "kind": "action_succeeded",
        "required": True,
    }
    task = AgentTask(
        agent,
        task_id="terminal-capability-preflight",
        goal="Write final.md through the required Action.",
        success_criteria=["The write_file Action succeeds."],
        execution="taskboard",
        options={
            "planner_capabilities": [{"id": "write_file", "kind": "action"}],
            "capability_evidence_requirements": [requirement],
        },
    )
    request_calls = 0

    def forbidden_request():
        nonlocal request_calls
        request_calls += 1
        raise AssertionError("semantic verifier must not run before deterministic capability evidence is complete")

    async def noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(agent, "create_temp_request", forbidden_request)
    monkeypatch.setattr(
        cast(Any, task),
        "_ensure_workspace_artifact_targeted_readback_evidence",
        noop_async,
    )
    monkeypatch.setattr(
        cast(Any, task),
        "_emit_process_progress_from_output",
        noop_async,
    )

    verification = await task._request_verification(
        1,
        plan={"execution_shape": "control"},
        execution_result={"candidate_final_result": "Draft body."},
        execution_meta={
            "status": "completed",
            "logs": {
                "action_logs": [],
                "capability_evidence_requirements": [requirement],
            },
        },
        context_pack=cast(Any, {}),
    )

    assert request_calls == 0
    assert verification["is_complete"] is False
    assert verification["requires_block"] is False
    assert verification["missing_capability_evidence"] == ["write_file"]
    assert verification["terminal_convergence"]["issue"] == {
        "gate_kind": "capability",
        "issue_code": "action_succeeded_missing",
        "contract_subject": "action:write_file",
    }
    assert verification["terminal_convergence"]["verifier_called"] is False


@pytest.mark.asyncio
async def test_taskboard_finalization_repairs_structured_continuation_verdict(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-final-continuation-repair").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-continuation-repair",
        goal="Return a complete final report.",
        success_criteria=["The final report includes required sections."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final report.",
            "required_outputs": ["Final report"],
            "allowed_execution_shape": "control",
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-continuation-repair",
            "revision_id": "rev-1",
            "graph": {"graph_id": "taskboard-final-continuation-repair-graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": "Draft body missing a required section.",
                        "remaining_work": [],
                    },
                ).to_dict()
            },
        }
    )

    async def verifier_requests_continuation_for_repairable_gap(*_args, **_kwargs):
        return {
            "is_complete": False,
            "requires_block": False,
            "reason": "The final deliverable is missing a required section.",
            "failure_analysis": "The artifact can continue with a localized repair.",
            "acceptance_delta": ["Add the missing required section."],
            "missing_criteria": ["Missing required section."],
            "replan_instruction": "Repair final.md by adding the missing section.",
            "repair_constraints": ["Preserve existing evidence."],
            "next_step_requirements": ["Return verifier-visible readback for final.md."],
            "final_result_required": True,
            "final_result": "",
        }

    async def fail_finalizer(*_args, **_kwargs):
        raise AssertionError("Promotable terminal candidate should skip the model finalizer.")

    async def noop(*_args, **_kwargs):
        return None

    terminal_calls: list[str] = []
    original_terminal_verification = task._run_terminal_verification

    async def run_terminal_verification(iteration_index, **kwargs):
        terminal_calls.append(str(kwargs["plan"].get("execution_shape") or ""))
        return await original_terminal_verification(iteration_index, **kwargs)

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", verifier_requests_continuation_for_repairable_gap)
    monkeypatch.setattr(cast(Any, task), "_run_terminal_verification", run_terminal_verification)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result["terminal"] is False
    assert result["status"] == "repair_requested"
    repair_revision = TaskBoardRevision.from_value(result["revision"])
    repair_cards = [
        card
        for card in repair_revision.graph.cards
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    ]
    assert len(repair_cards) == 1
    assert "Missing required section." in repair_cards[0].evidence_contract["missing_criteria"]
    assert terminal_calls == ["taskboard"]


@pytest.mark.asyncio
async def test_taskboard_early_noncompleted_terminal_preserves_verified_partial_ref(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-partial-terminal-ref").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-partial-terminal-ref",
        goal="Preserve the verified partial report on failure.",
        success_criteria=["The final report is complete."],
        execution="taskboard",
    )
    write_result = await task.workspace.write_file("reports/partial.md", "verified partial body")
    partial_ref = {
        **write_result["file_refs"][0],
        "role": "workspace_artifact",
    }
    card = TaskBoardCard.from_value(
        {"id": "draft", "objective": "Draft the report.", "required_outputs": ["Final report"]}
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-failed",
            "graph": {"graph_id": f"{task.id}.graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="failed",
                    preview={"status": "failed", "remaining_work": ["Complete the report."]},
                    file_refs=(partial_ref,),
                ).to_dict()
            },
        }
    )

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(cast(Any, task), "_emit", noop)
    terminal = await task._finalize_taskboard(
        revision,
        context_pack=cast(Any, {}),
    )
    retention = await task._apply_terminal_workspace_retention(status="failed")

    assert terminal == {"terminal": True, "status": "error"}
    assert len(task.result["artifact_refs"]) == 1
    assert task.result["artifact_refs"][0]["path"] == partial_ref["path"]
    assert task.result["artifact_refs"][0]["sha256"] == partial_ref["sha256"]
    assert str(task.result["artifact_refs"][0].get("locator_id") or "").startswith("loc_")
    assert str(task.result["artifact_refs"][0].get("content_version_id") or "").startswith("cv_")
    assert not (task.workspace.root / ".agently" / "workspace.db").exists()
    assert retention is not None
    assert retention["status"] in {"applied", "noop"}
    assert (await task.workspace.read_file("reports/partial.md"))["content"] == "verified partial body"


def test_taskboard_final_verification_does_not_parse_repairable_reason_text():
    assert (
        AgentTask._taskboard_final_verification_allows_repair(
            {
                "is_complete": False,
                "requires_block": True,
                "reason": "This sounds repairable, retryable, and localized.",
                "failure_analysis": "Please repair final.md.",
            },
            blocking_state_facts=[],
        )
        is False
    )


@pytest.mark.asyncio
async def test_taskboard_finalization_replaces_stale_rejection_reason_after_verifier_accepts(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-final-stale-reason").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-stale-reason",
        goal="Return a complete final report.",
        success_criteria=["The final report includes all required sections."],
        execution="taskboard",
    )
    cards = [
        TaskBoardCard.from_value(
            {
                "id": "draft-a",
                "objective": "Draft the first part.",
                "required_outputs": ["First part"],
            }
        ),
        TaskBoardCard.from_value(
            {
                "id": "draft-b",
                "objective": "Draft the second part.",
                "required_outputs": ["Second part"],
            }
        ),
    ]
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-stale-reason",
            "revision_id": "rev-1",
            "graph": {
                "graph_id": "taskboard-final-stale-reason-graph",
                "cards": [card.to_dict() for card in cards],
            },
            "card_results": {
                "draft-a": TaskBoardCardResult(
                    card_id="draft-a",
                    status="completed",
                    preview={"status": "completed", "final_result": "First part.", "remaining_work": []},
                ).to_dict(),
                "draft-b": TaskBoardCardResult(
                    card_id="draft-b",
                    status="completed",
                    preview={"status": "completed", "final_result": "Second part.", "remaining_work": []},
                ).to_dict(),
            },
        }
    )

    async def stale_rejection_finalizer(*_args, **_kwargs):
        return {
            "accepted": False,
            "reason": "Unable to verify the tail sections from truncated readback.",
            "final_result": "final.md",
            "missing_criteria": ["Tail sections need readback."],
        }

    async def accepting_verifier(*_args, **kwargs):
        execution_result = kwargs["execution_result"]
        assert execution_result["reason"] == "Unable to verify the tail sections from truncated readback."
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Final artifact verified all required sections.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
        }

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", stale_rejection_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", accepting_verifier)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result == {"terminal": True, "status": "completed"}
    assert task.result["accepted"] is True
    assert task.result["reason"] == "Final artifact verified all required sections."
    assert task.result["missing_criteria"] == []


@pytest.mark.asyncio
async def test_taskboard_finalization_promotes_single_terminal_candidate_without_finalizer(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-final-promotion").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-promotion",
        goal="Return the final report.",
        success_criteria=["The completed card result is returned."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final report.",
            "required_outputs": ["Final report"],
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-promotion",
            "revision_id": "rev-1",
            "graph": {"graph_id": "taskboard-final-promotion-graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": "Final report body from the completed terminal card.",
                        "remaining_work": [],
                    },
                ).to_dict()
            },
        }
    )
    calls = {"finalizer": 0, "verifier": 0}

    async def fail_finalizer(*_args, **_kwargs):
        calls["finalizer"] += 1
        raise AssertionError("TaskBoard finalizer should be skipped for promotable terminal candidate.")

    async def complete_verifier(*_args, **kwargs):
        calls["verifier"] += 1
        execution_result = kwargs["execution_result"]
        assert execution_result["final_result"] == "Final report body from the completed terminal card."
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "complete",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
        }

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", complete_verifier)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result == {"terminal": True, "status": "completed"}
    assert calls == {"finalizer": 0, "verifier": 1}
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    assert terminal_state["finalization_source"] == "candidate_promotion"
    assert "taskboard" not in task.result
    assert task.result["artifact_refs"] == []


@pytest.mark.asyncio
async def test_taskboard_verifier_protocol_retry_reuses_prepared_finalizer_result(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("taskboard-verifier-retry-reuses-final").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-verifier-retry-reuses-final",
        goal="Combine two completed parts into one report.",
        success_criteria=["The combined report is returned."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-1",
            "status": "completed",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {"id": "part_a", "objective": "Produce part A."},
                    {"id": "part_b", "objective": "Produce part B."},
                ],
            },
            "card_results": {
                "part_a": TaskBoardCardResult(
                    card_id="part_a",
                    status="completed",
                    preview={"status": "completed", "answer": "Part A."},
                ).to_dict(),
                "part_b": TaskBoardCardResult(
                    card_id="part_b",
                    status="completed",
                    preview={"status": "completed", "answer": "Part B."},
                ).to_dict(),
            },
        }
    )
    calls = {"finalizer": 0, "verifier": 0}

    async def finalizer(*_args, **_kwargs):
        calls["finalizer"] += 1
        return {
            "accepted": True,
            "reason": "Both parts were combined.",
            "final_result": "Combined report from Part A and Part B.",
            "missing_criteria": [],
            "evidence_use": [],
        }

    async def verifier(*_args, **kwargs):
        calls["verifier"] += 1
        execution_result = kwargs["execution_result"]
        assert "evidence_use" not in execution_result
        assert execution_result["final_result"] == (
            "Combined report from Part A and Part B."
        )
        if calls["verifier"] == 1:
            repair_contract = {
                "gate_kind": "output_contract",
                "issue_code": "terminal_verifier_output_invalid",
                "contract_subject": "verification:response",
                "protocol_section": "criterion_checks",
                "requirements": [
                    {
                        "code": "criterion_check_untrusted",
                        "reason": "evidence_ids contains a reference outside the offered set",
                    }
                ],
            }
            return {
                "is_complete": False,
                "requires_block": False,
                "reason": "Verifier response violated its output contract.",
                "missing_criteria": ["Correct the verifier response structure."],
                "final_result_required": True,
                "final_result": "Combined report from Part A and Part B.",
                "criterion_repair_contract": repair_contract,
                "strict_terminal_gates_applied": True,
            }
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "The current combined report is complete.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": "Combined report from Part A and Part B.",
            "strict_terminal_gates_applied": True,
        }

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", verifier)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    first = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )
    assert first["status"] == "verification_retry"
    assert first["prepared_final"]["final_result"] == (
        "Combined report from Part A and Part B."
    )

    second = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
        prepared_outputs={"final_candidate": first["prepared_final"]},
    )

    assert second == {"terminal": True, "status": "completed"}
    assert calls == {"finalizer": 1, "verifier": 2}


@pytest.mark.asyncio
async def test_taskboard_finalization_repairs_missing_declared_leaf_artifact_instead_of_accepting_working_ref(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-taskboard-missing-declared-leaf-artifact").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-missing-declared-leaf-artifact",
        goal="Write the final report to final.md.",
        success_criteria=["The final report is delivered at final.md."],
        execution="taskboard",
    )
    working_write = await task.workspace.write_file(
        "working/taskboard/collect/final.md",
        "# Upstream Evidence\n\nNot the terminal artifact.\n",
    )
    working_ref = {
        **working_write["file_refs"][0],
        "role": "workspace_artifact",
        "source": "agent_task.taskboard.card.collect.workspace_artifact",
    }
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-missing-declared",
            "status": "completed",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "collect",
                        "objective": "Collect evidence.",
                        "required_outputs": ["Working evidence"],
                    },
                    {
                        "id": "synthesize",
                        "objective": "Deliver final.md.",
                        "depends_on": ["collect"],
                        "required_outputs": ["final.md"],
                    },
                ],
            },
            "card_results": {
                "collect": TaskBoardCardResult(
                    card_id="collect",
                    status="completed",
                    preview={"status": "completed", "candidate_final_result": "Upstream evidence."},
                    file_refs=(working_ref,),
                    artifact_refs=(working_ref,),
                ).to_dict(),
                "synthesize": TaskBoardCardResult(
                    card_id="synthesize",
                    status="completed",
                    preview={
                        "status": "completed",
                        "sufficient": True,
                        "candidate_final_result": "# Final Report\n\nComplete candidate body.\n",
                        "artifact_manifest": {"path": "final.md"},
                        "remaining_work": ["Materialize and read back final.md."],
                    },
                ).to_dict(),
            },
        }
    )

    async def fail_finalizer(*_args, **_kwargs):
        raise AssertionError("The unique leaf candidate should skip redundant final synthesis.")

    async def accepting_verifier(*_args, **kwargs):
        execution_result = kwargs["execution_result"]
        assert execution_result["file_refs"] == []
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Semantic content appears complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
        }

    async def preserve_verifier_result(verification, **_kwargs):
        return dict(verification)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", accepting_verifier)
    monkeypatch.setattr(cast(Any, task), "_apply_strict_terminal_gates", preserve_verifier_result)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result["terminal"] is False
    assert result["status"] == "repair_requested"
    assert not task.workspace.resolve_file_path("final.md").exists()
    repair_revision = TaskBoardRevision.from_value(result["revision"])
    repair = next(
        card
        for card in repair_revision.graph.cards
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    )
    assert repair.metadata["final_workspace_deliverables"] == ["final.md"]
    assert any("final.md" in item for item in repair.required_outputs)
    assert "Missing required Workspace deliverable(s): final.md" in result["final_verification"][
        "missing_criteria"
    ]


@pytest.mark.asyncio
async def test_taskboard_finalization_fails_closed_for_model_declared_internal_working_artifact(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-taskboard-internal-working-terminal").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-internal-working-terminal",
        goal="Deliver the final report.",
        success_criteria=["The final report is delivered."],
        execution="taskboard",
    )
    working_path = "working/portfolio_risk_brief/final.md"
    write_result = await task.workspace.write_file(
        working_path,
        "# Working Report\n\nComplete content at an internal evidence path.\n",
    )
    working_ref = {
        **write_result["file_refs"][0],
        "role": "workspace_artifact",
        "source": "agent_task.taskboard.card.synthesize.workspace_artifact",
    }
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-working-terminal",
            "status": "completed",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "synthesize",
                        "objective": "Deliver the final report.",
                        "required_outputs": ["Final report"],
                    }
                ],
            },
            "card_results": {
                "synthesize": TaskBoardCardResult(
                    card_id="synthesize",
                    status="completed",
                    preview={
                        "status": "completed",
                        "sufficient": True,
                        "candidate_final_result": "Workspace artifact delivered.",
                        "artifact_manifest": {"path": working_path},
                        "file_refs": [working_ref],
                        "remaining_work": [],
                    },
                    file_refs=(working_ref,),
                    artifact_refs=(working_ref,),
                ).to_dict()
            },
        }
    )

    async def fail_finalizer(*_args, **_kwargs):
        raise AssertionError("The unique leaf candidate should skip redundant final synthesis.")

    async def accepting_verifier(*_args, **kwargs):
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Semantic content appears complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": kwargs["execution_result"]["final_result"],
        }

    async def preserve_verifier_result(verification, **_kwargs):
        return dict(verification)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", accepting_verifier)
    monkeypatch.setattr(cast(Any, task), "_apply_strict_terminal_gates", preserve_verifier_result)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result == {"terminal": True, "status": "blocked"}
    assert task.result["accepted"] is False
    assert task.result["artifact_status"] == "partial"
    assert task.result["artifact_refs"] == []
    assert "framework-internal working path" in task.result["reason"]
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    assert "taskboard_terminal_workspace_delivery_invalid" in terminal_state["final_verification"][
        "guard_reasons"
    ]


@pytest.mark.asyncio
async def test_taskboard_file_terminal_uses_current_content_version_over_stale_candidate(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-current-file-terminal").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-current-file-terminal",
        goal="Write the verified report to final.md.",
        success_criteria=["final.md contains the grounded current report."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    stale_body = (
        "# Historical Report\n\n"
        "AVGO price $1,208.50 from unsupported historical output.\n\n"
        + "Unsupported historical analysis must not remain authoritative.\n" * 40
    )
    corrected_body = "# Report\n\nAVGO price 238.4 from the canonical current Action evidence."
    await task.workspace.write_file("final.md", stale_body)
    stale_ref = {
        **await task.workspace._promote_file_identity("final.md", role="workspace_artifact"),
        "source": "agent_task.taskboard.card.synthesize.workspace_artifact",
        "preview": stale_body,
        "truncated": False,
        "read_bytes": len(stale_body.encode("utf-8")),
    }
    await task.workspace.write_file("final.md", corrected_body)
    current_ref = {
        **await task.workspace._promote_file_identity("final.md", role="workspace_artifact"),
        "source": "agent_task.taskboard.card.final-verification-repair.workspace_artifact",
        "preview": corrected_body,
        "truncated": False,
        "read_bytes": len(corrected_body.encode("utf-8")),
    }
    assert stale_ref["content_version_id"] != current_ref["content_version_id"]

    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-current-file",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "synthesize",
                        "objective": "Produce the historical report body.",
                        "required_outputs": ["Report body"],
                    },
                    {
                        "id": "final-verification-repair",
                        "objective": "Repair final.md from canonical evidence.",
                        "depends_on": ["synthesize"],
                        "required_outputs": ["Corrected final.md"],
                    },
                ],
            },
            "card_results": {
                "synthesize": TaskBoardCardResult(
                    card_id="synthesize",
                    status="completed",
                    preview={
                        "status": "completed",
                        "candidate_final_result": stale_body,
                        "file_refs": [stale_ref],
                        "remaining_work": [],
                    },
                    file_refs=(stale_ref,),
                    artifact_refs=(stale_ref,),
                ).to_dict(),
                "final-verification-repair": TaskBoardCardResult(
                    card_id="final-verification-repair",
                    status="completed",
                    preview={
                        "status": "completed",
                        "candidate_final_result": (
                            "Workspace artifact delivered at final.md; full content is available through "
                            "file_refs/readback."
                        ),
                        "file_refs": [current_ref],
                        "remaining_work": [],
                    },
                    file_refs=(current_ref,),
                    artifact_refs=(current_ref,),
                ).to_dict(),
            },
        }
    )

    async def fail_finalizer(*_args, **_kwargs):
        raise AssertionError("The current trusted file should be promoted without a model finalizer.")

    async def complete_verifier(*_args, **kwargs):
        execution_result = kwargs["execution_result"]
        assert execution_result["final_result"] == (
            "Workspace artifact delivered at final.md; full content is available through file_refs/readback."
        )
        final_refs = execution_result["file_refs"]
        assert final_refs
        assert {ref.get("content_version_id") for ref in final_refs} == {current_ref["content_version_id"]}
        assert all(ref.get("sha256") == current_ref["sha256"] for ref in final_refs)
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "The current physical content version is grounded.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
            "strict_terminal_gates_applied": True,
        }

    async def noop(*_args, **_kwargs):
        return None

    async def preserve_verifier_result(verification, **_kwargs):
        return dict(verification)

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", complete_verifier)
    monkeypatch.setattr(cast(Any, task), "_apply_strict_terminal_gates", preserve_verifier_result)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result == {"terminal": True, "status": "completed"}
    assert task.result["accepted"] is True
    assert task.result["artifact_refs"]
    assert {ref.get("content_version_id") for ref in task.result["artifact_refs"]} == {
        current_ref["content_version_id"]
    }


@pytest.mark.asyncio
async def test_taskboard_file_terminal_preserves_explicit_leaf_final_result_summary(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-taskboard-file-summary-terminal").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-file-summary-terminal",
        goal="Write final.md and return a compact final summary.",
        success_criteria=["final.md is complete.", "A compact final summary is returned."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    await task.workspace.write_file("final.md", "# Complete report\n\nGrounded artifact body.")
    final_ref = {
        **await task.workspace._promote_file_identity("final.md", role="workspace_artifact"),
        "source": "agent_task.taskboard.card.final-verification-repair.workspace_artifact",
        "preview": "# Complete report\n\nGrounded artifact body.",
        "truncated": False,
    }
    summary = "Compact grounded summary returned by the completed terminal repair card."
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-file-summary",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "final-verification-repair",
                        "objective": "Return the missing compact summary without rewriting final.md.",
                        "required_outputs": ["Compact final summary"],
                        "metadata": {
                            "generated_by": "agent_task.taskboard.final_verification_repair"
                        },
                    }
                ],
            },
            "card_results": {
                "final-verification-repair": TaskBoardCardResult(
                    card_id="final-verification-repair",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": summary,
                        "file_refs": [final_ref],
                        "remaining_work": [],
                    },
                    file_refs=(final_ref,),
                    artifact_refs=(final_ref,),
                ).to_dict()
            },
        }
    )

    async def fail_finalizer(*_args, **_kwargs):
        raise AssertionError("The completed terminal card should be promoted without a model finalizer.")

    async def complete_verifier(*_args, **kwargs):
        execution_result = kwargs["execution_result"]
        assert execution_result["final_result"] == summary
        assert execution_result["file_refs"][0]["content_version_id"] == final_ref[
            "content_version_id"
        ]
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "The file and compact summary are both complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
            "strict_terminal_gates_applied": True,
        }

    async def preserve_verifier_result(verification, **_kwargs):
        return dict(verification)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", complete_verifier)
    monkeypatch.setattr(cast(Any, task), "_apply_strict_terminal_gates", preserve_verifier_result)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result == {"terminal": True, "status": "completed"}
    assert task.result["accepted"] is True
    assert task.result["final_result"] == summary
    assert task.result["artifact_refs"][0]["content_version_id"] == final_ref[
        "content_version_id"
    ]


@pytest.mark.asyncio
async def test_taskboard_finalization_does_not_use_acceptance_cache_as_terminal_semantic_gate(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-taskboard-final-clean-acceptance-cache").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-clean-acceptance-cache",
        goal="Return the final report.",
        success_criteria=["The completed card result is returned."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final report.",
            "required_outputs": ["Final report"],
            "metadata": {"acceptance_criteria": ["The completed card result is returned."]},
        }
    )
    base_revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-clean-acceptance-cache",
            "revision_id": "rev-1",
            "graph": {"graph_id": "taskboard-final-clean-acceptance-cache-graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": "Final report body from the completed terminal card.",
                        "remaining_work": [],
                    },
                ).to_dict()
            },
        }
    )
    previous_index = build_task_board_acceptance_index(
        base_revision,
        success_criteria=task.success_criteria,
        verification={
            "criterion_checks": [
                {
                    "criterion": "The completed card result is returned.",
                    "satisfied": True,
                    "reason": "Prior verifier accepted the terminal card.",
                    "verification_ref": "verification:clean",
                }
            ]
        },
        evidence_view=build_task_board_evidence_view(base_revision).to_dict(),
    )
    revision = TaskBoardRevision.from_value(
        {**base_revision.to_dict(), "metadata": {"taskboard_acceptance_index": previous_index}}
    )
    calls = {"finalizer": 0, "verifier": 0}

    async def fail_finalizer(*_args, **_kwargs):
        calls["finalizer"] += 1
        raise AssertionError("TaskBoard finalizer should be skipped for promotable terminal candidate.")

    async def terminal_verifier(*_args, **_kwargs):
        calls["verifier"] += 1
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "The current terminal candidate passed semantic verification.",
            "acceptance_delta": [],
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": "Final report body from the completed terminal card.",
            "criterion_checks": [
                {
                    "criterion": "The completed card result is returned.",
                    "satisfied": True,
                    "summary": "The current candidate satisfies the criterion.",
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [],
            "material_claim_audit": {
                "valid": True,
                "coverage_complete": True,
                "checks": [],
                "failed_checks": [],
                "structural_errors": [],
                "repair_contract": {},
            },
            "strict_terminal_gates_applied": True,
        }

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", terminal_verifier)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result == {"terminal": True, "status": "completed"}
    assert calls == {"finalizer": 0, "verifier": 1}
    assert task.result["accepted"] is True
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    assert terminal_state["acceptance_verification_plan"]["all_satisfied"] is True
    assert terminal_state["final_verification"]["material_claim_audit"]["valid"] is True


@pytest.mark.asyncio
async def test_taskboard_final_gate_blocks_only_explicit_dirty_state_facts(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-final-dirty-state").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-dirty-state",
        goal="Return the final report.",
        success_criteria=["The completed card result is returned."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final report.",
            "required_outputs": ["Final report"],
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-dirty-state",
            "revision_id": "rev-1",
            "graph": {"graph_id": "taskboard-final-dirty-state-graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": "Final report body from the completed terminal card.",
                        "remaining_work": [],
                    },
                ).to_dict()
            },
            "diagnostics": [
                {
                    "kind": "explicit_state_fact",
                    "code": "task_repo.dirty",
                    "scope": "task",
                    "status": "dirty",
                    "blocking": True,
                    "reason": "Task-scoped repository files are still dirty.",
                    "source": "action:git_status",
                }
            ],
        }
    )
    calls = {"finalizer": 0, "verifier": 0}

    async def fail_finalizer(*_args, **_kwargs):
        calls["finalizer"] += 1
        raise AssertionError("TaskBoard finalizer should be skipped for promotable terminal candidate.")

    async def complete_verifier(*_args, **kwargs):
        calls["verifier"] += 1
        execution_result = kwargs["execution_result"]
        assert execution_result["final_result"] == "Final report body from the completed terminal card."
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "complete before explicit state gate",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
        }

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", complete_verifier)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result == {"terminal": True, "status": "blocked"}
    assert calls == {"finalizer": 0, "verifier": 1}
    assert task.result["accepted"] is False
    assert task.result["artifact_status"] == "partial"
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    assert terminal_state["explicit_state_facts"][0]["code"] == "task_repo.dirty"
    assert "Task-scoped repository files are still dirty." in task.result["reason"]


def test_taskboard_auto_reuses_initial_plan_and_falls_back_for_small_linear_board(tmp_path):
    agent = _create_agent("agent-taskboard-auto-plan-reuse").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-auto-plan-reuse",
        goal="Answer a simple question.",
        success_criteria=["Return the answer."],
        execution="auto",
    )
    task.task_shape_analysis = task._normalize_task_shape_analysis(
        {
            "analysis": "A tiny board would be enough.",
            "execution_hint": {"recommended_shape": "taskboard", "confidence": "medium"},
            "initial_taskboard_plan": {
                "board_goal": "Answer a simple question.",
                "cards": [
                    {
                        "id": "answer",
                        "action_block": "Answer directly.",
                        "objective": "Return the answer.",
                        "depends_on": [],
                        "done_when": "Answer is returned.",
                        "allowed_execution_shape": "auto",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The answer is returned.",
                "why_this_effort_shape": "Single card.",
            },
        }
    )

    planning_result = task._initial_taskboard_plan_from_shape_analysis()

    assert planning_result is not None
    assert [card.id for card in planning_result.revision.graph.cards] == ["answer"]
    assert task._taskboard_should_fallback_to_flat(planning_result.revision) is True

    readback_revision = TaskBoardRevision.from_value(
        {
            "board_id": "readback-board",
            "revision_id": "rev-readback",
            "graph": {
                "graph_id": "readback-graph",
                "cards": [
                    TaskBoardCard.from_value(
                        {
                            "id": "readback",
                            "objective": "Read a required artifact.",
                            "allowed_execution_shape": "readback",
                        }
                    ).to_dict()
                ],
            },
        }
    )
    assert task._taskboard_should_fallback_to_flat(readback_revision) is False

    explicit_task = AgentTask(
        agent,
        task_id="taskboard-explicit-no-fallback",
        goal="Answer a simple question.",
        success_criteria=["Return the answer."],
        execution="taskboard",
    )
    assert explicit_task._taskboard_should_fallback_to_flat(planning_result.revision) is False


def test_output_contract_guards_invalid_final_result_after_json_fallback(tmp_path):
    agent = _create_agent("agent-output-final-guard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="output-final-guard",
        goal="Return structured output.",
        success_criteria=["Final result must be valid JSON."],
        options={
            "execution_prompt_snapshot": {
                "output": {"answer": (str, "Answer", True)},
                "output_format": "hybrid",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": '{"answer": "bad quote”}',
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is False
    assert "final_result_output_parse_failed" in verification["guard_reasons"]
    assert "parse as a dict" in verification["missing_criteria"][0]
    assert "hybrid -> json" in verification["missing_criteria"][0]


def test_verification_accepts_file_backed_result_despite_soft_liveness_failure(tmp_path):
    agent = _create_agent("agent-soft-liveness-final").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="soft-liveness-final",
        goal="Produce a file-backed report.",
        success_criteria=["The report exists and satisfies every criterion."],
    )
    idle_error = "AgentExecution made no progress before idle deadline: max_no_progress_seconds=90.0."

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All success criteria are satisfied.",
            "failure_analysis": "All success criteria are satisfied.",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "Task complete, no replan needed.",
            "next_step_requirements": ["Task complete, no replan needed."],
            "progress_message": "Verification successful: deliverable complete and accepted.",
            "final_result_required": True,
            "final_result": "final.md",
            "criterion_checks": [
                {
                    "criterion": "sections",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "All required sections are present.",
                },
                {
                    "criterion": "grounding",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "Grounding guard is clear.",
                },
            ],
        },
        execution_evidence_summary={
            "status": "failed",
            "errors": [
                {
                    "error_type": "RuntimeStageStallError",
                    "stage": "action_planning",
                    "status": "stalled",
                    "message": idle_error,
                    "last_progress_event": "action_planning.started",
                }
            ],
            "action_ids": ["list_files", "read_file"],
            "action_statuses": {"list_files": "success", "read_file": "success"},
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "artifact_refs": [
                {
                    "path": "final.md",
                    "role": "workspace_artifact",
                    "source": "agent_task.workspace_artifact.stream_drafted",
                    "readback": {"content": "# Report\n\nDone.", "truncated": False},
                }
            ],
        },
        grounding_guard={
            "valid": True,
            "blocking_count": 0,
            "checked_claims": [],
            "diagnostics": [],
        },
    )

    assert verification["is_complete"] is True
    assert "execution_status_failed" not in verification.get("guard_reasons", [])
    assert "Execution step status is failed" not in " ".join(verification.get("missing_criteria", []))
    assert "Execution step status is failed" not in " ".join(verification.get("repair_constraints", []))
    assert verification["final_result_via_workspace_artifact"] is True
    assert verification["non_blocking_execution_status"]["error_type"] == "RuntimeStageStallError"


def test_verification_criterion_checks_require_structured_satisfied_boolean():
    assert (
        AgentTask._verification_criteria_are_satisfied(
            [
                {
                    "criterion": "grounding",
                    "status": "satisfied",
                    "summary": "Display-only model status text is not enough.",
                }
            ]
        )
        is False
    )
    assert (
        AgentTask._verification_criteria_are_satisfied(
            [
                {
                    "criterion": "grounding",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "Structured boolean is the completion signal.",
                }
            ]
        )
        is True
    )


def test_terminal_verification_rejects_missing_or_unknown_criterion_joins(tmp_path):
    agent = _create_agent("agent-criterion-join-validation").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="criterion-join-validation",
        goal="Return a concise reformatted result.",
        success_criteria=["The result is concise.", "The result is reformatted."],
    )
    carrier_id = "inline:" + "f" * 64
    candidate: dict[str, Any] = {
        "carrier_id": carrier_id,
        "text": "Concise reformatted result.",
        "content_version_id": carrier_id,
    }
    candidate["carriers"] = [dict(candidate)]

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "Complete.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:unknown",
                    "satisfied": True,
                    "summary": "Copied an untrusted criterion id.",
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [],
        },
        execution_evidence_summary={},
        terminal_candidate=candidate,
    )

    assert verification["is_complete"] is False
    assert "criterion_audit_invalid" in verification["guard_reasons"]
    assert verification["criterion_audit"]["valid"] is False
    assert {error["code"] for error in verification["criterion_audit"]["structural_errors"]} == {
        "criterion_id_unknown",
        "criterion_checks_missing",
    }


def test_terminal_verifier_validates_the_exact_model_visible_reference_snapshot(tmp_path):
    agent = _create_agent("agent-verifier-reference-snapshot").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="verifier-reference-snapshot",
        goal="Return one grounded fact.",
        success_criteria=["The fact is grounded in Action evidence."],
    )
    source = task._task_reference_catalog.add_evidence(
        {
            "id": "market:avgo",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "body": "AVGO closed at 238.4, up 0.9%.",
        }
    )
    carrier_readback = task._task_reference_catalog.add_evidence(
        {
            "id": "artifact:final",
            "kind": "workspace_artifact.targeted_readback",
            "status": "ok",
            "body_state": "bounded",
            "path": "final.md",
            "body": "AVGO closed at 238.4, up 0.9%.",
        }
    )
    eligible_reference_ids = set(task._task_reference_catalog.offered_references())
    projection = task._model_evidence_ledger_projection(
        {"items": [source, carrier_readback]},
        offered_reference_ids=eligible_reference_ids,
    )
    offered_snapshot = {
        str(item["reference_id"])
        for item in projection["items"]
    }

    assert offered_snapshot == {source["reference_id"]}
    assert carrier_readback["reference_id"] not in offered_snapshot
    assert projection["omitted_count"] == 0

    carrier_id = "inline:" + "9" * 64
    candidate = {
        "carrier_id": carrier_id,
        "text": "AVGO closed at 238.4, up 0.9%.",
        "content_version_id": carrier_id,
        "carriers": [
            {
                "carrier_id": carrier_id,
                "kind": "inline_final_result",
                "text": "AVGO closed at 238.4, up 0.9%.",
                "content_version_id": carrier_id,
            }
        ],
    }
    raw_verification = {
        "is_complete": True,
        "requires_block": False,
        "reason": "Complete.",
        "missing_criteria": [],
        "criterion_checks": [
            {
                "criterion_id": "criterion:1",
                "satisfied": True,
                "summary": "The Action result grounds the fact.",
                "evidence_ids": [source["reference_id"]],
            }
        ],
        "material_claim_coverage_complete": True,
        "material_claim_checks": [
            {
                "claim_key": "claim_1",
                "claim_kind": "external_fact",
                "state": "supported",
                "evidence_ids": [source["reference_id"]],
                "reason": "The offered Action evidence contains the exact fact.",
            }
        ],
    }
    accepted = task._normalize_verification(
        raw_verification,
        execution_evidence_summary={},
        terminal_candidate=candidate,
        offered_reference_ids=offered_snapshot,
    )
    invalid_transport_join = {
        **raw_verification,
        "criterion_checks": [
            {
                **raw_verification["criterion_checks"][0],
                "evidence_ids": [carrier_readback["reference_id"]],
            }
        ],
    }
    rejected = task._normalize_verification(
        invalid_transport_join,
        execution_evidence_summary={},
        terminal_candidate=candidate,
        offered_reference_ids=offered_snapshot,
    )

    assert accepted["is_complete"] is True
    assert rejected["criterion_audit"]["valid"] is False
    assert {
        error["code"]
        for error in rejected["criterion_audit"]["structural_errors"]
    } == {"criterion_evidence_unknown"}


def test_terminal_verifier_material_claim_keys_reconstruct_host_owned_identity(tmp_path):
    agent = _create_agent("agent-verifier-claim-selection").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="verifier-claim-selection",
        goal="Return a grounded two-part brief.",
        success_criteria=["Every material claim is grounded."],
    )
    source = task._task_reference_catalog.add_evidence(
        {
            "id": "market:avgo",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "body": "AVGO closed at 238.4, up 0.9%.",
        }
    )
    carrier_id = "car_current"
    candidate: dict[str, Any] = {
        "carrier_id": carrier_id,
        "kind": "workspace_artifact",
        "path": "final.md",
        "text": "# Brief\n\nAVGO closed at 238.4, up 0.9%.\n\nPortfolio conclusion.",
        "content_version_id": "cv_current",
    }
    candidate["carriers"] = [dict(candidate)]

    offered_candidates = task._material_claim_candidates_for_verifier(candidate)

    assert [item["claim_key"] for item in offered_candidates] == [
        "claim_1",
        "claim_2",
        "claim_3",
    ]
    assert all(
        not {"carrier_id", "content_version_id", "artifact_quote"}.intersection(item)
        for item in offered_candidates
    )
    selected = offered_candidates[1]
    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "Complete.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "The material claim is supported.",
                    "evidence_ids": [source["reference_id"]],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [
                {
                    "claim_key": selected["claim_key"],
                    "claim_kind": "external_fact",
                    "state": "supported",
                    "evidence_ids": [source["reference_id"]],
                    "reason": "The Action result directly supports the selected claim.",
                }
            ],
        },
        execution_evidence_summary={},
        terminal_candidate=candidate,
        offered_reference_ids={source["reference_id"]},
    )

    assert verification["is_complete"] is True
    assert verification["material_claim_checks"] == [
        {
            "claim_key": selected["claim_key"],
            "carrier_id": carrier_id,
            "path": "final.md",
            "content_version_id": "cv_current",
            "artifact_quote": selected["text"],
            "claim_kind": "external_fact",
            "state": "supported",
            "evidence_ids": [source["reference_id"]],
            "reason": "The Action result directly supports the selected claim.",
        }
    ]


def test_terminal_verifier_claim_candidates_keep_adjacent_markdown_lines_separate(
    tmp_path,
):
    agent = _create_agent("agent-verifier-line-claim-selection").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="verifier-line-claim-selection",
        goal="Return a grounded portfolio brief.",
        success_criteria=["Every ticker fact is grounded."],
    )
    candidate: dict[str, Any] = {
        "carrier_id": "car_current",
        "kind": "workspace_artifact",
        "path": "final.md",
        "text": (
            "## Market facts\n"
            "- NVDA closed at 170.2.\n"
            "- AVGO closed at 238.4.\n"
        ),
        "content_version_id": "cv_current",
    }
    candidate["carriers"] = [dict(candidate)]

    offered = task._material_claim_candidates_for_verifier(candidate)

    assert [(item["claim_key"], item["text"]) for item in offered] == [
        ("claim_1", "## Market facts"),
        ("claim_2", "- NVDA closed at 170.2."),
        ("claim_3", "- AVGO closed at 238.4."),
    ]


def test_terminal_verifier_rejects_unknown_material_claim_key(tmp_path):
    agent = _create_agent("agent-verifier-unknown-claim").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="verifier-unknown-claim",
        goal="Return a grounded fact.",
        success_criteria=["The fact is grounded."],
    )
    candidate: dict[str, Any] = {
        "carrier_id": "car_current",
        "kind": "inline_final_result",
        "text": "Grounded fact.",
        "content_version_id": "cv_current",
    }
    candidate["carriers"] = [dict(candidate)]

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "Complete.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "Checked.",
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [
                {
                    "claim_key": "claim_unknown",
                    "claim_kind": "external_fact",
                    "state": "unsupported",
                    "evidence_ids": [],
                    "reason": "Unknown selection key.",
                }
            ],
        },
        execution_evidence_summary={},
        terminal_candidate=candidate,
        offered_reference_ids=set(),
    )

    assert verification["is_complete"] is False
    assert verification["material_claim_audit"]["valid"] is False
    assert any(
        "claim_key is not one of the current offered material claim candidates"
        in message
        for error in verification["material_claim_audit"]["structural_errors"]
        for message in error.get("messages", [])
    )


def test_terminal_verifier_rejects_duplicate_material_claim_key(tmp_path):
    agent = _create_agent("agent-verifier-duplicate-claim").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="verifier-duplicate-claim",
        goal="Return a grounded fact.",
        success_criteria=["The fact is grounded."],
    )
    candidate: dict[str, Any] = {
        "carrier_id": "car_current",
        "kind": "inline_final_result",
        "text": "Grounded fact.",
        "content_version_id": "cv_current",
    }
    candidate["carriers"] = [dict(candidate)]
    duplicate_check = {
        "claim_key": "claim_1",
        "claim_kind": "external_fact",
        "state": "unsupported",
        "evidence_ids": [],
        "reason": "No offered evidence supports the claim.",
    }

    verification = task._normalize_verification(
        {
            "is_complete": False,
            "requires_block": False,
            "reason": "Repair is required.",
            "missing_criteria": ["The fact is grounded."],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": False,
                    "summary": "Unsupported.",
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [duplicate_check, dict(duplicate_check)],
        },
        execution_evidence_summary={},
        terminal_candidate=candidate,
        offered_reference_ids=set(),
    )

    assert verification["is_complete"] is False
    assert verification["material_claim_audit"]["valid"] is False
    assert any(
        "claim_key is duplicated" in message
        for error in verification["material_claim_audit"]["structural_errors"]
        for message in error.get("messages", [])
    )


@pytest.mark.asyncio
async def test_terminal_verifier_join_errors_share_one_protocol_owner_and_stop_on_third(
    tmp_path,
):
    agent = _create_agent("agent-verifier-protocol-convergence").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="verifier-protocol-convergence",
        goal="Return one grounded fact.",
        success_criteria=["The fact is grounded."],
    )
    carrier_id = "inline:" + "8" * 64
    candidate = {
        "carrier_id": carrier_id,
        "text": "Grounded fact.",
        "content_version_id": carrier_id,
        "carriers": [
            {
                "carrier_id": carrier_id,
                "kind": "inline_final_result",
                "text": "Grounded fact.",
                "content_version_id": carrier_id,
            }
        ],
    }
    base = {
        "is_complete": True,
        "requires_block": False,
        "reason": "Complete.",
        "missing_criteria": [],
        "criterion_checks": [
            {
                "criterion_id": "criterion:1",
                "satisfied": True,
                "summary": "Grounded.",
                "evidence_ids": [],
            }
        ],
        "material_claim_coverage_complete": True,
        "material_claim_checks": [],
    }
    criterion_join_error = {
        **base,
        "criterion_checks": [
            {
                **base["criterion_checks"][0],
                "evidence_ids": ["ref_not_offered"],
            }
        ],
    }
    material_join_error = {
        **base,
        "material_claim_checks": [
            {
                "claim_key": "claim_1",
                "claim_kind": "external_fact",
                "state": "supported",
                "evidence_ids": ["ref_not_offered"],
                "reason": "Copied an unavailable reference.",
            }
        ],
    }

    normalized = [
        task._normalize_verification(
            raw,
            execution_evidence_summary={},
            terminal_candidate=candidate,
            offered_reference_ids=set(),
        )
        for raw in (criterion_join_error, material_join_error, criterion_join_error)
    ]
    decisions = [
        await task._apply_strict_terminal_gates(
            item,
            candidate=candidate,
            execution_evidence_summary={},
            verifier_called=True,
        )
        for item in normalized
    ]

    assert {
        tuple(
            decision["terminal_convergence"]["issue"][field]
            for field in ("gate_kind", "issue_code", "contract_subject")
        )
        for decision in decisions
    } == {
        (
            "output_contract",
            "terminal_verifier_output_invalid",
            "verification:response",
        )
    }
    assert [
        decision["terminal_convergence"]["occurrence"] for decision in decisions
    ] == [1, 2, 3]
    assert decisions[-1]["requires_block"] is True
    assert (
        AgentTask._taskboard_final_verification_allows_repair(
            decisions[0],
            blocking_state_facts=[],
        )
        is False
    )


@pytest.mark.asyncio
async def test_terminal_verifier_merges_all_response_contract_failures_into_retry_contract(
    tmp_path,
):
    agent = _create_agent("agent-verifier-merged-protocol-repair").use_workspace(
        tmp_path / "workspace"
    )
    task = AgentTask(
        agent,
        task_id="verifier-merged-protocol-repair",
        goal="Return one grounded fact.",
        success_criteria=["The fact is grounded."],
    )
    carrier_id = "inline:" + "9" * 64
    candidate = {
        "carrier_id": carrier_id,
        "text": "Grounded fact.",
        "content_version_id": carrier_id,
        "carriers": [
            {
                "carrier_id": carrier_id,
                "kind": "inline_final_result",
                "text": "Grounded fact.",
                "content_version_id": carrier_id,
            }
        ],
    }
    normalized = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "Complete.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "Grounded.",
                    "evidence_ids": ["ref_stale_criterion"],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [
                {
                    "claim_key": "claim_1",
                    "claim_kind": "external_fact",
                    "state": "supported",
                    "evidence_ids": ["ref_stale_material"],
                    "reason": "Copied stale response-local values.",
                }
            ],
        },
        execution_evidence_summary={},
        terminal_candidate=candidate,
        offered_reference_ids=set(),
    )

    decision = await task._apply_strict_terminal_gates(
        normalized,
        candidate=candidate,
        execution_evidence_summary={},
        verifier_called=True,
    )
    repair_contract = decision["terminal_convergence"]["repair_contract"]

    assert repair_contract["protocol_sections"] == [
        "criterion_checks",
        "material_claim_checks",
    ]
    assert {
        requirement["protocol_section"]
        for requirement in repair_contract["requirements"]
    } == {"criterion_checks", "material_claim_checks"}
    assert {
        invalid_id
        for requirement in repair_contract["requirements"]
        for invalid_id in requirement.get("invalid_reference_ids", [])
    } == {"ref_stale_criterion", "ref_stale_material"}


def test_verification_material_claim_audit_blocks_unsupported_external_fact(tmp_path):
    agent = _create_agent("agent-material-claim-unsupported").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="material-claim-unsupported",
        goal="Produce a source-grounded portfolio brief.",
        success_criteria=["Material external facts are supported."],
    )
    source = task._task_reference_catalog.add_evidence(
        {
            "id": "market:avgo",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "body": "AVGO closed at 238.4, up 0.9%.",
        }
    )
    carrier_id = "inline:" + "a" * 64
    candidate = {
        "carrier_id": carrier_id,
        "text": "AVGO revenue rose 99%.",
        "content_version_id": carrier_id,
        "carriers": [
            {
                "kind": "inline_final_result",
                "carrier_id": carrier_id,
                "text": "AVGO revenue rose 99%.",
                "content_version_id": carrier_id,
            }
        ],
    }

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All criteria passed.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": candidate["text"],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "The external-fact support criterion is checked by the material claim audit.",
                    "evidence_ids": [source["reference_id"]],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [
                {
                    "claim_key": "claim_1",
                    "claim_kind": "external_fact",
                    "state": "unsupported",
                    "evidence_ids": [source["reference_id"]],
                    "reason": "The offered market source contains price movement, not revenue growth.",
                }
            ],
        },
        execution_evidence_summary={},
        candidate_final_result=candidate["text"],
        terminal_candidate=candidate,
    )

    assert verification["is_complete"] is False
    assert "material_claim_audit_failed" in verification["guard_reasons"]
    assert verification["material_claim_audit"]["valid"] is False
    assert verification["material_claim_repair_contract"]["issue_code"] == (
        "unsupported_material_claim"
    )


def test_verification_material_claim_audit_allows_reasonable_derived_analysis(tmp_path):
    agent = _create_agent("agent-material-claim-derived").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="material-claim-derived",
        goal="Produce a source-grounded portfolio brief.",
        success_criteria=["Material facts and analysis are clearly distinguished."],
    )
    source = task._task_reference_catalog.add_evidence(
        {
            "id": "mandate:diversification",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "body": "AVGO diversifies exposure across custom silicon and infrastructure software.",
        }
    )
    carrier_id = "inline:" + "b" * 64
    text = "In this portfolio, AVGO can reduce ticker-specific concentration risk."
    candidate = {
        "carrier_id": carrier_id,
        "text": text,
        "content_version_id": carrier_id,
        "carriers": [
            {
                "kind": "inline_final_result",
                "carrier_id": carrier_id,
                "text": text,
                "content_version_id": carrier_id,
            }
        ],
    }

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All criteria passed.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": text,
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "Facts and bounded analysis are distinguished.",
                    "evidence_ids": [source["reference_id"]],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [
                {
                    "claim_key": "claim_1",
                    "claim_kind": "derived_analysis",
                    "state": "reasonable_derived",
                    "evidence_ids": [source["reference_id"]],
                    "reason": "This is a bounded portfolio inference from the offered diversification fact.",
                }
            ],
        },
        execution_evidence_summary={},
        candidate_final_result=text,
        terminal_candidate=candidate,
    )

    assert verification["is_complete"] is True
    assert verification["material_claim_audit"]["valid"] is True
    assert verification.get("guard_reasons") in (None, [])


@pytest.mark.parametrize(
    "claim_check",
    [
        {
            "claim_key": "claim_unknown",
            "claim_kind": "external_fact",
            "state": "supported",
            "evidence_ids": [],
            "reason": "Unknown claim selection.",
        },
        {
            "claim_key": "claim_1",
            "claim_kind": "external_fact",
            "state": "supported",
            "evidence_ids": ["ref_unknown"],
            "reason": "Unknown evidence ref.",
        },
    ],
)
def test_verification_material_claim_audit_rejects_untrusted_model_joins(tmp_path, claim_check):
    agent = _create_agent("agent-material-claim-invalid-join").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="material-claim-invalid-join",
        goal="Reformat the supplied output.",
        success_criteria=["The supplied output is reformatted."],
    )
    carrier_id = "inline:" + "c" * 64
    candidate = {
        "carrier_id": carrier_id,
        "text": "Reformatted output.",
        "content_version_id": carrier_id,
        "carriers": [
            {
                "kind": "inline_final_result",
                "carrier_id": carrier_id,
                "text": "Reformatted output.",
                "content_version_id": carrier_id,
            }
        ],
    }

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "Complete.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": candidate["text"],
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "The output was reformatted.",
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [claim_check],
        },
        execution_evidence_summary={},
        candidate_final_result=candidate["text"],
        terminal_candidate=candidate,
    )

    assert verification["is_complete"] is False
    assert "material_claim_audit_invalid" in verification["guard_reasons"]
    assert verification["material_claim_audit"]["structural_errors"]


def test_verification_material_claim_audit_allows_empty_complete_audit_for_non_factual_output(tmp_path):
    agent = _create_agent("agent-material-claim-empty").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="material-claim-empty",
        goal="Reformat the supplied output.",
        success_criteria=["The supplied output is reformatted."],
    )
    carrier_id = "inline:" + "e" * 64
    text = "Reformatted output."
    candidate = {
        "carrier_id": carrier_id,
        "text": text,
        "content_version_id": carrier_id,
        "carriers": [
            {
                "kind": "inline_final_result",
                "carrier_id": carrier_id,
                "text": text,
                "content_version_id": carrier_id,
            }
        ],
    }

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "Complete.",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": text,
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "The output was reformatted.",
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [],
        },
        execution_evidence_summary={},
        candidate_final_result=text,
        terminal_candidate=candidate,
    )

    assert verification["is_complete"] is True
    assert verification["material_claim_audit"]["valid"] is True


@pytest.mark.asyncio
async def test_terminal_verification_uses_one_semantic_request_without_grounding_subflow(
    tmp_path,
    monkeypatch,
):
    verification_module = importlib.import_module("agently.core.application.AgentTask.Verification")
    agent = _create_agent("agent-single-terminal-verifier").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="single-terminal-verifier",
        goal="Reformat the supplied output.",
        success_criteria=["The supplied output is reformatted."],
        execution="flat",
    )
    calls = {"requests": 0}
    captured: dict[str, Any] = {}

    class FakeRequest:
        def input(self, value):
            captured["input"] = value
            return self

        def instruct(self, value):
            captured["instruct"] = value
            return self

        def output(self, value, *, format):
            captured["output"] = value
            captured["format"] = format
            return self

        async def async_get_data(self):
            return {
                "is_complete": True,
                "requires_block": False,
                "reason": "Complete.",
                "failure_analysis": "",
                "acceptance_delta": [],
                "missing_criteria": [],
                "replan_instruction": "",
                "repair_constraints": [],
                "next_step_requirements": [],
                "final_result_required": True,
                "final_result": "Reformatted output.",
                "criterion_checks": [
                    {
                        "criterion_id": "criterion:1",
                        "satisfied": True,
                        "summary": "The output is reformatted.",
                        "evidence_ids": [],
                    }
                ],
                "material_claim_coverage_complete": True,
                "material_claim_checks": [],
            }

    def create_request():
        calls["requests"] += 1
        return FakeRequest()

    async def noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(agent, "create_temp_request", create_request)
    monkeypatch.setattr(cast(Any, task), "_apply_language_policy_to_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cast(Any, task), "_ensure_workspace_artifact_targeted_readback_evidence", noop_async)
    monkeypatch.setattr(cast(Any, task), "_emit_process_progress_from_output", noop_async)
    assert not hasattr(verification_module, "run_grounding_subflow")

    verification = await task._request_verification(
        1,
        plan={"deliverable_mode": "inline_final"},
        execution_result={
            "candidate_final_result": "Reformatted output.",
            "remaining_work": [],
        },
        execution_meta={"status": "completed", "logs": {}},
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert verification["is_complete"] is True
    assert calls == {"requests": 1}
    assert "material_claim_checks" in captured["output"]
    assert "material_claim_candidates" in captured["input"]
    material_check_schema = captured["output"]["material_claim_checks"][0][0]
    assert "claim_key" in material_check_schema
    assert "carrier_id" not in material_check_schema
    assert "artifact_quote" not in material_check_schema


def test_verification_keeps_liveness_failure_blocking_without_criterion_checks(tmp_path):
    agent = _create_agent("agent-soft-liveness-needs-checks").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="soft-liveness-needs-checks",
        goal="Produce a file-backed report.",
        success_criteria=["The report exists and satisfies every criterion."],
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All success criteria are satisfied.",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "Task complete, no replan needed.",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "failed",
            "errors": [
                {
                    "error_type": "RuntimeStageStallError",
                    "stage": "action_planning",
                    "status": "stalled",
                    "message": "AgentExecution made no progress before idle deadline.",
                }
            ],
            "action_statuses": {"read_file": "success"},
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "artifact_refs": [
                {
                    "path": "final.md",
                    "role": "workspace_artifact",
                    "source": "agent_task.workspace_artifact.stream_drafted",
                    "readback": {"content": "# Report\n\nDone.", "truncated": False},
                }
            ],
        },
        grounding_guard={
            "valid": True,
            "blocking_count": 0,
            "checked_claims": [],
            "diagnostics": [],
        },
    )

    assert verification["is_complete"] is False
    assert "execution_status_failed" in verification["guard_reasons"]
    assert "non_blocking_execution_status" not in verification


def test_verification_guard_rewrites_conflicting_completion_fields(tmp_path):
    agent = _create_agent("agent-guard-field-alignment").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="guard-field-alignment",
        goal="Produce a grounded report.",
        success_criteria=["Every claim is grounded in bounded readback evidence."],
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All success criteria are satisfied.",
            "failure_analysis": "All success criteria are satisfied.",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "Task complete, no replan needed.",
            "next_step_requirements": ["Task complete, no replan needed."],
            "progress_message": "Verification successful: deliverable complete and accepted.",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={"status": "completed"},
        grounding_guard={
            "valid": False,
            "blocking_count": 1,
            "checked_claims": [
                {
                    "claim": "A factual claim.",
                    "evidence_ids": ["action_evidence:ref-only"],
                    "support_type": "content",
                }
            ],
            "diagnostics": [
                {
                    "blocking": True,
                    "code": "evidence_ledger.ref_only_item_used_as_content_support",
                    "message": "ref_only evidence supports only discovery/ref-pointer claims until readback evidence exists.",
                }
            ],
        },
    )

    assert verification["is_complete"] is False
    assert "evidence_ledger_grounding_guard_failed" in verification["guard_reasons"]
    assert "Verification successful" not in verification.get("progress_message", "")
    assert "Task complete" not in verification.get("replan_instruction", "")
    assert "Task complete" not in " ".join(verification.get("next_step_requirements", []))
    assert "ref_only evidence" in " ".join(verification.get("missing_criteria", []))


def test_output_contract_accepts_declared_hybrid_final_result(tmp_path):
    agent = _create_agent("agent-hybrid-final-guard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="hybrid-final-guard",
        goal="Return structured output.",
        success_criteria=["Final result must match the declared hybrid output contract."],
        options={
            "execution_prompt_snapshot": {
                "output": {"answer": (str, "Answer", True), "items": ([str], "Items", True)},
                "output_format": "hybrid",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": '### answer\nDone.\n\n### items\n```json\n["a", "b"]\n```',
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is True
    assert "guard_reasons" not in verification


def test_output_contract_accepts_declared_xml_field_final_result(tmp_path):
    agent = _create_agent("agent-xml-field-final-guard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="xml-field-final-guard",
        goal="Return XML-like structured output.",
        success_criteria=["Final result must match the declared xml_field output contract."],
        options={
            "execution_prompt_snapshot": {
                "output": {
                    "lesson_script": (str, "Long lesson script", True),
                    "review_note": (str, "Review note", True),
                },
                "output_format": "xml_field",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": (
                "<agently_output>"
                '<field name="lesson_script" type="text"># Lesson\n\nUse natural prose.</field>'
                '<field name="review_note" type="text">Structured review is separate.</field>'
                "</agently_output>"
            ),
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is True
    assert "guard_reasons" not in verification


def test_output_contract_falls_back_to_json_when_declared_format_fails(tmp_path):
    agent = _create_agent("agent-hybrid-json-final-fallback").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="hybrid-json-final-fallback",
        goal="Return structured output.",
        success_criteria=["Final result must match the declared output contract."],
        options={
            "execution_prompt_snapshot": {
                "output": {"answer": (str, "Answer", True), "items": ([str], "Items", True)},
                "output_format": "hybrid",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": '{"answer": "Done.", "items": ["a", "b"]}',
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is True
    assert "guard_reasons" not in verification
    diagnostics = task.diagnostics["final_result_output_contract"][0]
    assert diagnostics["declared_format"] == "hybrid"
    assert diagnostics["resolved_format"] == "json"


@pytest.mark.asyncio
async def test_agent_task_loop_replans_without_workspace_audit_records(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent()

    task = agent.create_task(
        task_id="legacy-script-upgrade",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=[
            "The original failure is recorded.",
            "The script runs successfully.",
            "Verification evidence is stored.",
        ],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
        limits={"max_model_requests": 1},
        options={"agent_task": {"stream_progress": True}},
    )

    result_facade = task.get_result()
    stream_items = [item async for item in result_facade.get_async_generator(type="instant")]
    result = await result_facade.async_get_full_data()
    execution_meta = await result_facade.async_get_meta()
    meta = await task.meta()
    delta_text = "".join([chunk async for chunk in task.get_async_generator(type="delta")])

    assert result["status"] == "completed"
    assert len(meta["iterations"]) == 2
    assert "final_response" in result
    assert await result_facade.async_get_text() == result["final_response"]
    assert result_facade.task_refs["task_id"] == "legacy-script-upgrade"
    assert result_facade.task_refs["status"] == "completed"
    assert execution_meta["task_refs"]["task_id"] == "legacy-script-upgrade"
    assert execution_meta["task_refs"]["status"] == "completed"
    assert meta["status"] == "completed"
    assert len(meta["iterations"]) == 2
    for iteration in meta["iterations"]:
        block_carrier = iteration["execution_meta"]["block_carrier"]
        assert block_carrier["work_unit"]["origin"] == "flat_step"
        assert block_carrier["work_unit_result"]["id"] == block_carrier["work_unit"]["id"]
        assert block_carrier["output_policy"]["body_transport"] == "structured_control"
        blocks = iteration["execution_meta"]["blocks"]
        assert blocks["execution_plan"]["plan_blocks"][0]["kind"] == "agent_step"
        assert blocks["execution_block_graph"]["execution_blocks"][0]["kind"] == "agent_step"
        assert blocks["evidence"]["execution_block_results"][0]["kind"] == "agent_step"
        assert blocks["result"]["semantic_outputs"]
    assert MockAgentTaskRequester.verification_calls == 2
    assert any(item.path == "agent_task.started" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "progress" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    progress_messages = [
        item.value.get("message")
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "progress" and isinstance(item.value, dict)
    ]
    snapshot_values = [
        item.value
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "snapshot" and isinstance(item.value, dict)
    ]
    assert any("building a Workspace context pack" in str(message) for message in progress_messages)
    assert any(value.get("stage") == "plan" for value in snapshot_values)
    assert any(value.get("stage") == "verification" for value in snapshot_values)
    child_execution_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "child_execution"]
    assert any(item.path == "agent_task.iteration.1.execution.route.selected" for item in child_execution_items)
    assert any(item.path.endswith(".execution.step_result") for item in child_execution_items)
    assert all((item.meta or {}).get("child_execution_id") for item in child_execution_items)
    assert any(item.path.endswith(".replan") for item in stream_items)
    assert any(item.path == "result" for item in stream_items)
    assert "building a Workspace context pack" in delta_text
    assert "plan ready" in delta_text
    assert "execution evidence was captured" in delta_text
    assert "all success criteria are satisfied" in delta_text
    assert result["final_response"] in delta_text
    assert "Operator summary for INC-4242." in delta_text
    phase_names = [item["phase"] for item in meta["diagnostics"]["phases"]]
    assert "configured" in phase_names
    assert "planned" in phase_names
    assert "executing" in phase_names
    assert "evidence_recorded" in phase_names
    assert "verified" in phase_names
    assert "guarded" in phase_names
    assert "replanned" in phase_names
    assert "terminal" in phase_names
    assert any(item.path == "agent_task.phase.verified" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "phase" for item in stream_items)
    assert all(not refs for refs in meta["workspace_refs"].values())
    workspace = agent.workspace
    assert workspace is not None
    assert not (workspace.root / ".agently" / "workspace.db").exists()
    assert meta["diagnostics"]["workspace_retention"]["status"] in {"applied", "noop"}


@pytest.mark.asyncio
async def test_agent_task_loop_progress_stream_is_opt_in(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-opt-in")

    task = agent.create_task(
        task_id="progress-opt-in",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]

    assert not any((item.meta or {}).get("stream_kind") == "progress" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_uses_snapshot_background(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-model")

    task = agent.create_task(
        task_id="progress-model",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"]
    progress_delta_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress_delta"]

    assert progress_items
    assert all((item.meta or {}).get("progress_source") == "model" for item in progress_items)
    assert any("Progress model summarized" in item.value.get("message", "") for item in progress_items)
    assert progress_delta_items
    assert all(item.event_type == "delta" for item in progress_delta_items)
    assert all(item.is_complete is False for item in progress_delta_items)
    assert "Progress model summarized" in "".join(item.delta or "" for item in progress_delta_items)
    assert not any("building a Workspace context pack" in item.value.get("message", "") for item in progress_items)
    assert any("Summarize AgentTask progress" in call for call in MockAgentTaskRequester.calls)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_uses_configured_language(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-language")
    agent.settings.set("agent_task.progress.language", "zh-CN")

    task = agent.create_task(
        task_id="progress-language",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"]

    assert any("progress_language: zh-CN" in call for call in MockAgentTaskRequester.calls)
    assert progress_items
    assert all((item.meta or {}).get("progress_language") == "zh-CN" for item in progress_items)


@pytest.mark.asyncio
async def test_agent_task_loop_uses_agent_language_policy(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-language-policy")
    agent.language("中文")

    task = agent.create_task(
        task_id="language-policy-task",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"]

    assert any("language_policy" in call and "output_language: zh-CN" in call for call in MockAgentTaskRequester.calls)
    assert all("search_region" not in call for call in MockAgentTaskRequester.calls)
    assert progress_items
    assert all((item.meta or {}).get("progress_language") == "zh-CN" for item in progress_items)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_omits_developer_diagnostics(tmp_path, monkeypatch):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-safe-diagnostics")

    task = agent.create_task(
        task_id="progress-safe-diagnostics",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    async def noisy_context_pack(**_kwargs):
        return {
            "goal": "Repair a legacy Agently script so it runs on the current API.",
            "profile": "auto",
            "items": [],
            "omitted": [],
            "diagnostics": {
                "fallback_reason": {
                    "type": "OperationalError",
                    "message": 'fts5: syntax error near "."; no such column: question',
                },
                "builder": "default",
            },
        }

    assert task.workspace is not None
    monkeypatch.setattr(task.workspace, "build_context", noisy_context_pack)

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_calls = [call for call in MockAgentTaskRequester.calls if "Summarize AgentTask progress" in call]
    meta = await task.meta()

    assert progress_calls
    assert not any("fts5" in call for call in progress_calls)
    assert not any("no such column" in call for call in progress_calls)
    assert not any("fallback_reason" in call for call in progress_calls)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    assert "progress_errors" not in meta["diagnostics"]


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_does_not_delay_stream_close(tmp_path):
    class SlowProgressRequester(MockAgentTaskRequester):
        name = "SlowProgressRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Summarize AgentTask progress" in text:
                await asyncio.sleep(10)
                yield "message", json.dumps(
                    {"message": "late progress summary"},
                    ensure_ascii=False,
                )
                return
            async for event in super().request_model(request_data):
                yield event

    settings = Settings(name="agent-task-slow-progress-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-slow-progress-plugins")
    plugin_manager.register("ModelRequester", SlowProgressRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-slow-progress")

    task = agent.create_task(
        task_id="slow-progress",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "slow-progress-narrator",
                "progress_timeout_seconds": 30,
            },
        },
    )

    stream_items = await asyncio.wait_for(
        _collect_stream(task),
        timeout=8,
    )

    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    assert not any((item.meta or {}).get("progress_source") == "model" for item in stream_items)


def test_agent_execution_dynamic_task_candidate_route_is_removed():
    agent = Agently.create_agent("execution-local-dynamic-task")
    execution = agent.create_execution().input("run a local dynamic task graph")

    with pytest.raises(ValueError, match=r"AgentExecution\.use_dynamic_task.*independent DAG workflows"):
        execution.use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "execution-local-dynamic-task",
                "task_schema_version": "task_dag/v1",
                "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
            },
            handlers={"local_handler": lambda context: context.task.id},
        )

    assert not hasattr(execution, "dynamic_task_candidates")
    assert not hasattr(agent, "_dynamic_task_candidates")


@pytest.mark.asyncio
async def test_agent_task_loop_rejects_dag_shaped_step_without_global_candidate_leak(tmp_path):
    class DagStepVerificationRequester(MockAgentTaskRequester):
        name = "DagStepVerificationRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "the bounded step returned the required evidence",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "Bounded step completed with value ok.",
                }
            else:
                payload = {
                    "step_result": "Direct bounded step returned value ok.",
                    "candidate_final_result": "Bounded step completed with value ok.",
                    "evidence": ["value ok was produced by the direct bounded step."],
                    "remaining_work": [],
                }
            yield "message", json.dumps(payload, ensure_ascii=False)

    async def run_task(context):
        return {"task_id": context.task.id, "value": context.graph_input["value"]}

    settings = Settings(name="agent-task-dag-step-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-dag-step-plugins")
    plugin_manager.register("ModelRequester", DagStepVerificationRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-dag-step")
    graph = {
        "graph_id": "agent-task-loop-dag-step",
        "task_schema_version": "task_dag/v1",
        "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
        "semantic_outputs": {"final": "extract"},
    }
    task = agent.create_task(
        task_id="dag-shaped-step",
        goal="Return the final DAG result.",
        success_criteria=["The final result includes value ok."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"effort": {"execution": {"step_plan": "dag"}}}},
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "dynamic_task",
            "step_instruction": "Run the DAG-shaped extraction step.",
            "expected_evidence": "TaskDAG semantic output includes value ok.",
            "rationale": "The step has a clear bounded DAG contract.",
            "dynamic_task": {
                "mode": "submitted",
                "plan": graph,
                "handlers": {"local_handler": run_task},
                "graph_input": {"value": "ok"},
            },
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": request_plan}

    result = await task.async_run()
    meta = await task.async_meta()
    first_iteration = meta["iterations"][0]

    assert result["status"] == "completed"
    assert first_iteration["plan"]["execution_shape"] == "dynamic_task"
    assert first_iteration["plan"]["effective_execution_shape"] == "direct"
    assert first_iteration["plan"]["step_execution"]["dag_shape_degraded"] is True
    assert first_iteration["plan"]["step_execution"]["warning"] == "dag_shape_not_agent_execution_strategy"
    assert first_iteration["plan"]["step_execution"]["policy"]["step_plan"] == "direct"
    assert first_iteration["plan"]["step_execution"]["policy"]["step_plan_degraded_from"] == "dag"
    assert first_iteration["plan"]["step_execution"]["policy"]["allow_dag_steps"] is False
    assert first_iteration["execution_meta"]["route_plan"]["selected_route"] == "model_request"
    blocks = first_iteration["execution_meta"]["blocks"]
    assert blocks["execution_plan"]["plan_blocks"][0]["kind"] == "agent_step"
    assert blocks["execution_plan"]["plan_blocks"][0]["bound_inputs"]["step_plan"] == "direct"
    assert blocks["execution_block_graph"]["execution_blocks"][0]["kind"] == "agent_step"
    assert not hasattr(agent, "_dynamic_task_candidates")


@pytest.mark.asyncio
async def test_agent_task_loop_actions_step_route_policy_prevents_skill_takeover(tmp_path):
    class ActionStepRequester(MockAgentTaskRequester):
        name = "ActionStepRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "the action-shaped step returned bounded evidence",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "source evidence collected",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "collected repository source evidence",
                    "evidence": ["fetch_agently_architecture_sources returned bounded excerpts"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-action-step-route-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-action-step-route-plugins")
    plugin_manager.register("ModelRequester", ActionStepRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-action-step-route")

    def fetch_agently_architecture_sources():
        return {"status": "ok", "sources": ["architecture evidence"]}

    skill_source = tmp_path / "skill-source" / "architecture-diagram"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text(
        """---
name: architecture-diagram
description: Use for architecture diagram rendering.
---

# architecture-diagram

Render the final diagram only after source evidence is collected.
""",
        encoding="utf-8",
    )
    Agently.skills_executor.configure(
        registry_root=tmp_path / "skills-registry",
        allowed_trust_levels=["local"],
    )
    Agently.skills_executor.install_skills(skill_source, trust_level="local")

    agent.use_actions(fetch_agently_architecture_sources, always=True)
    agent.use_skills(["architecture-diagram"], mode="model_decision", always=True)

    task = agent.create_task(
        task_id="action-step-route-policy",
        goal="Gather source evidence before rendering with a skill.",
        success_criteria=["Source evidence is collected."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "actions",
            "step_instruction": "Call fetch_agently_architecture_sources before using any Skill.",
            "expected_evidence": "Repository source evidence is collected.",
            "rationale": "The task must gather source evidence before rendering.",
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": request_plan}

    result = await task.async_run()
    meta = await task.async_meta()
    first_iteration = meta["iterations"][0]

    assert result["status"] == "completed"
    assert first_iteration["plan"]["execution_shape"] == "actions"
    assert first_iteration["plan"]["step_execution"]["route_policy"]["allowed_routes"] == ["model_request"]
    assert first_iteration["execution_meta"]["route_plan"]["selected_route"] == "model_request"
    assert first_iteration["execution_meta"]["route_plan"]["candidates"]["skills"]["model_decision"] is True


@pytest.mark.asyncio
async def test_flat_intermediate_work_unit_skips_independent_verifier(tmp_path):
    agent = _create_agent("agent-task-flat-consumer-driven-sufficiency").use_workspace(tmp_path / "workspace")
    task = agent.create_task(
        task_id="flat-consumer-driven-sufficiency",
        goal="Gather evidence before writing the final answer.",
        success_criteria=["The final answer uses gathered evidence."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "direct",
            "step_instruction": "Gather intermediate evidence.",
            "expected_evidence": "Intermediate evidence for a later answer.",
            "rationale": "The next step should consume this evidence.",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {
                "step_result": "Evidence note was captured.",
                "evidence": ["source note"],
                "remaining_work": ["Use the evidence to write the final answer."],
                "ready_for_final_verification": False,
            },
            {
                "execution_id": "exec-intermediate",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("intermediate Flat work unit should not call independent verifier")

    cast(Any, task)._agent_task_step_overrides = {
        "_request_plan": request_plan,
        "_execute_step": execute_step,
    }
    cast(Any, task)._request_verification = fail_if_called

    result = await task.async_run()
    meta = await task.async_meta()
    iteration = meta["iterations"][0]

    assert result["status"] == "max_iterations"
    assert iteration["verification_source"] == "consumer_driven_continuation"
    assert iteration["verification"]["is_complete"] is False
    assert iteration["verification"]["consumer_driven_sufficiency"]["consumer"] == "next_flat_iteration"
    assert "Use the evidence" in " ".join(iteration["verification"]["next_step_requirements"])
    assert meta["workspace_refs"]["verification"] == []
    assert not (tmp_path / "task-workspace" / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_flat_remaining_work_without_ready_flag_skips_independent_verifier(tmp_path):
    agent = _create_agent("agent-task-flat-remaining-work-consumer").use_workspace(tmp_path / "workspace")
    task = agent.create_task(
        task_id="flat-remaining-work-consumer",
        goal="Gather evidence before writing the final answer.",
        success_criteria=["The final answer uses gathered evidence."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "direct",
            "step_instruction": "Gather intermediate evidence.",
            "expected_evidence": "Intermediate evidence for a later answer.",
            "rationale": "The next step should consume this evidence.",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {
                "step_result": "Evidence note was captured.",
                "evidence": ["source note"],
                "remaining_work": ["Use the evidence to write the final answer."],
            },
            {
                "execution_id": "exec-remaining-work",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("non-empty remaining_work should defer independent verifier")

    cast(Any, task)._agent_task_step_overrides = {
        "_request_plan": request_plan,
        "_execute_step": execute_step,
    }
    cast(Any, task)._request_verification = fail_if_called

    result = await task.async_run()
    meta = await task.async_meta()
    iteration = meta["iterations"][0]

    assert result["status"] == "max_iterations"
    assert iteration["verification_source"] == "consumer_driven_continuation"
    assert iteration["verification"]["consumer_driven_sufficiency"]["decision"]["reason"] == (
        "work_unit_reports_remaining_work"
    )
    assert "Use the evidence" in " ".join(iteration["verification"]["next_step_requirements"])
    assert meta["workspace_refs"]["verification"] == []
    assert not (tmp_path / "task-workspace" / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_agent_task_loop_stops_at_max_iterations(tmp_path):
    class NeverCompleteRequester(MockAgentTaskRequester):
        name = "NeverCompleteRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload: dict[str, Any] = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "still incomplete",
                    "missing_criteria": ["final answer missing"],
                    "replan_instruction": "try one more step",
                    "final_result": "",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "continue analysis",
                    "expected_evidence": "final answer",
                    "rationale": "more evidence needed",
                }
            else:
                payload = {"step_result": "partial", "evidence": ["partial"], "remaining_work": ["final"]}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-max-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-max-plugins")
    plugin_manager.register("ModelRequester", NeverCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-max")

    task = agent.create_task(
        task_id="survey-analysis",
        goal="Analyze customer interview responses.",
        success_criteria=["pain points are identified"],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert meta["status"] == "max_iterations"
    assert len(meta["iterations"]) == 1
    assert all(not refs for refs in meta["workspace_refs"].values())
    assert not (tmp_path / "task-workspace" / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_agent_task_loop_blocks_when_verifier_requires_block(tmp_path):
    class BlockedRequester(MockAgentTaskRequester):
        name = "BlockedRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload: dict[str, Any] = {
                    "is_complete": True,
                    "requires_block": True,
                    "reason": "external approval is required before continuing",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "draft result should not be accepted",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "prepare the approval-bound change",
                    "expected_evidence": "approval state",
                    "rationale": "the task cannot safely continue without approval",
                }
            else:
                payload = {
                    "step_result": "approval is still pending",
                    "evidence": ["approval_required"],
                    "remaining_work": ["wait for approval"],
                    "ready_for_final_verification": True,
                }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-blocked-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-blocked-plugins")
    plugin_manager.register("ModelRequester", BlockedRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-blocked")

    task = agent.create_task(
        task_id="blocked-approval",
        goal="Produce the final remediation report after external approval.",
        success_criteria=["The final report is returned only after approval is available."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "blocked"
    assert result["accepted"] is False
    assert result["artifact_status"] == "blocked"
    assert meta["status"] == "blocked"
    assert len(meta["iterations"]) == 1
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is False
    assert verification["requires_block"] is True
    assert "requires_block_true" in verification["guard_reasons"]
    assert meta["diagnostics"]["verification_guards"][0]["guard_reasons"] == ["requires_block_true"]
    assert meta["diagnostics"]["phases"][-1]["phase"] == "terminal"
    assert meta["diagnostics"]["phases"][-1]["diagnostics"]["artifact_status"] == "blocked"


def test_agent_task_verifier_block_continues_when_untried_read_action_exists(tmp_path):
    agent = _create_agent("agent-task-block-continuation").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Collect official source evidence and produce a report.",
        success_criteria=["The report is grounded in source evidence."],
        execution="flat",
        max_iterations=2,
        options={
            "planner_capabilities": [
                {
                    "id": "web_search",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                },
                {
                    "id": "browse",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                },
            ]
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": False,
            "requires_block": True,
            "reason": "Search failed to locate source evidence.",
            "failure_analysis": "The current evidence-gathering step failed.",
            "acceptance_delta": ["Official source evidence is still missing."],
            "missing_criteria": ["Official source evidence is missing."],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["web_search"],
            "failed_actions": ["web_search"],
            "blocked_actions": [],
            "approval_required_actions": [],
        },
    )

    assert verification["is_complete"] is False
    assert verification["requires_block"] is False
    assert "untried_read_action_available" in verification["guard_reasons"]
    assert "requires_block_true" not in verification["guard_reasons"]
    assert verification["continuation_opportunities"]["untried_action_ids"] == ["browse"]
    assert verification["replan_instruction"] == "Plan another bounded evidence-gathering step before blocking."
    assert task.diagnostics["verification_continuations"][0]["untried_action_ids"] == ["browse"]


def test_agent_task_verifier_block_does_not_expand_artifact_readback_into_all_read_actions(tmp_path):
    agent = _create_agent("agent-task-artifact-block-no-generic-continuation").use_workspace(
        tmp_path / "task-workspace"
    )
    task = AgentTask(
        agent,
        goal="Verify the final Workspace artifact.",
        success_criteria=["The final report has verifier-readable evidence."],
        execution="taskboard",
        max_iterations=2,
        options={
            "planner_capabilities": [
                {"id": "read_file", "kind": "action", "side_effect_level": "read", "replay_safe": True},
                {"id": "search_files", "kind": "action", "side_effect_level": "read", "replay_safe": True},
                {"id": "write_file", "kind": "action", "side_effect_level": "write", "replay_safe": True},
            ]
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": False,
            "requires_block": True,
            "reason": "The artifact evidence is still insufficient.",
            "failure_analysis": "A specific section needs scoped readback.",
            "acceptance_delta": ["Scoped artifact evidence is missing."],
            "missing_criteria": ["Scoped artifact evidence is missing."],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "Workspace artifact delivered at final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": [],
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "missing_required_actions": [],
            "capability_evidence": {
                "actions": {"succeeded": [], "failed": []},
                "artifacts": {"readback": ["workspace_artifact_readback:test:final.md"]},
            },
        },
    )

    assert verification["requires_block"] is True
    assert "untried_read_action_available" not in verification.get("guard_reasons", [])
    assert "continuation_opportunities" not in verification
    assert "write_file" not in " ".join(verification.get("missing_criteria", []))


def test_optional_failed_read_action_does_not_force_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-optional-read-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a source-grounded brief.",
        success_criteria=["The brief is complete and cites available evidence."],
        execution="flat",
        options={
            "planner_capabilities": [
                {
                    "id": "read_skill_guidance",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                }
            ]
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final brief is complete; optional guidance was unavailable and disclosed.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["read_skill_guidance"],
            "failed_actions": ["read_skill_guidance"],
            "blocked_actions": [],
            "approval_required_actions": [],
            "required_actions": [],
        },
    )

    assert verification["is_complete"] is True
    assert "execution_risk_actions_present" not in verification.get("guard_reasons", [])
    assert verification["non_blocking_failed_actions"] == ["read_skill_guidance"]
    assert "Unresolved execution risk actions" not in " ".join(verification.get("missing_criteria", []))


def test_required_failed_read_action_still_blocks_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-required-read-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a source-grounded brief.",
        success_criteria=["The required read action succeeds."],
        execution="flat",
        options={
            "planner_capabilities": [
                {
                    "id": "read_skill_guidance",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                }
            ],
            "capability_evidence_requirements": [{"capability_id": "read_skill_guidance", "kind": "action_succeeded"}],
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final brief is complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["read_skill_guidance"],
            "failed_actions": ["read_skill_guidance"],
            "blocked_actions": [],
            "approval_required_actions": [],
            "required_actions": [],
        },
    )

    assert verification["is_complete"] is False
    assert "execution_risk_actions_present" in verification["guard_reasons"]
    assert "read_skill_guidance" in " ".join(verification.get("missing_criteria", []))
    assert verification.get("non_blocking_failed_actions") in (None, [])


def test_framework_action_loop_guard_diagnostic_does_not_force_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-action-loop-diagnostic").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a verified report.",
        success_criteria=["The report is complete and grounded."],
        execution="flat",
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final report is complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["read_file"],
            "failed_actions": [],
            "blocked_actions": ["action_loop"],
            "approval_required_actions": [],
            "required_actions": [],
        },
    )

    assert verification["is_complete"] is True
    assert "execution_risk_actions_present" not in verification.get("guard_reasons", [])
    assert verification["non_blocking_failed_actions"] == ["action_loop"]
    assert "Unresolved execution risk actions" not in " ".join(verification.get("missing_criteria", []))


def test_blocked_step_with_only_nonblocking_read_failures_can_accept_completed_artifact(tmp_path):
    agent = _create_agent("agent-task-blocked-read-failure-completed-artifact").use_workspace(
        tmp_path / "task-workspace"
    )
    task = AgentTask(
        agent,
        goal="Produce a source-grounded brief.",
        success_criteria=["The brief is complete and cites available evidence."],
        execution="flat",
        options={
            "planner_capabilities": [
                {"id": "browse", "kind": "action", "side_effect_level": "read", "replay_safe": True}
            ]
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final brief is complete; a source page was unavailable and disclosed.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
            "criterion_checks": [
                {"criterion": "The brief is complete and cites available evidence.", "satisfied": True}
            ],
        },
        execution_evidence_summary={
            "status": "blocked",
            "action_ids": ["browse", "action_loop"],
            "failed_actions": ["browse"],
            "blocked_actions": ["action_loop"],
            "approval_required_actions": [],
            "required_actions": [],
            "artifact_refs": [
                {
                    "path": "final.md",
                    "role": "workspace_artifact",
                    "sha256": "abc123",
                    "readback": {"content": "Complete brief with disclosed source limitation."},
                }
            ],
        },
        grounding_guard={"valid": True, "blocking_count": 0, "diagnostics": []},
    )

    assert verification["is_complete"] is True
    assert "execution_status_failed" not in verification.get("guard_reasons", [])
    assert "execution_risk_actions_present" not in verification.get("guard_reasons", [])
    assert verification["non_blocking_failed_actions"] == ["browse", "action_loop"]
    assert verification["non_blocking_execution_status"]["status"] == "blocked"


def test_step_local_required_read_action_is_scoped_without_task_required_guard(tmp_path):
    agent = _create_agent("agent-task-step-local-required-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a source-grounded brief.",
        success_criteria=["The brief is complete and cites available evidence."],
        execution="flat",
        options={
            "planner_capabilities": [
                {"id": "browse", "kind": "action", "side_effect_level": "read", "replay_safe": True}
            ]
        },
    )

    class FakeExecution:
        def __init__(self):
            self.used_actions: list[list[str]] = []
            self.required_actions: list[list[str]] = []
            self.route_policies: list[dict[str, object]] = []

        def use_actions(self, action_ids):
            self.used_actions.append(list(action_ids))

        def require_actions(self, action_ids):
            self.required_actions.append(list(action_ids))

        def route_policy(self, value):
            self.route_policies.append(dict(value))

    execution = FakeExecution()
    plan = {
        "execution_shape": "actions",
        "step_scope": {"allowed_capability_ids": ["browse"]},
        "required_action_ids": ["browse"],
    }

    step_execution = task._configure_step_execution(execution, plan)

    assert execution.used_actions == [["browse"]]
    assert execution.required_actions == []
    assert step_execution["step_required_action_ids"] == ["browse"]
    assert step_execution["task_required_action_ids"] == []
    assert step_execution["action_scope_source"] == "step_required_action_ids"


def test_task_contract_required_read_action_still_uses_required_guard(tmp_path):
    agent = _create_agent("agent-task-contract-required-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a source-grounded brief.",
        success_criteria=["The required source action succeeds."],
        execution="flat",
        options={
            "planner_capabilities": [
                {"id": "browse", "kind": "action", "side_effect_level": "read", "replay_safe": True}
            ],
            "capability_constraints": {"actions": {"required": ["browse"]}},
        },
    )

    class FakeExecution:
        def __init__(self):
            self.used_actions: list[list[str]] = []
            self.required_actions: list[list[str]] = []
            self.route_policies: list[dict[str, object]] = []

        def use_actions(self, action_ids):
            self.used_actions.append(list(action_ids))

        def require_actions(self, action_ids):
            self.required_actions.append(list(action_ids))

        def route_policy(self, value):
            self.route_policies.append(dict(value))

    execution = FakeExecution()
    plan = {
        "execution_shape": "actions",
        "step_scope": {"allowed_capability_ids": ["browse"]},
        "required_action_ids": ["browse"],
    }

    step_execution = task._configure_step_execution(execution, plan)

    assert execution.required_actions == [["browse"]]
    assert step_execution["task_required_action_ids"] == ["browse"]
    assert step_execution["step_required_action_ids"] == []


def test_unknown_failed_action_still_blocks_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-unsafe-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a report.",
        success_criteria=["The report is complete."],
        execution="flat",
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final report is complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["write_file"],
            "failed_actions": ["write_file"],
            "blocked_actions": [],
            "approval_required_actions": [],
        },
    )

    assert verification["is_complete"] is False
    assert "execution_risk_actions_present" in verification["guard_reasons"]
    assert "write_file" in " ".join(verification.get("missing_criteria", []))


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_when_missing_criteria_is_present(tmp_path):
    class CompleteWithMissingRequester(MockAgentTaskRequester):
        name = "CompleteWithMissingRequester"
        verification_calls = 0

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                CompleteWithMissingRequester.verification_calls += 1
                if CompleteWithMissingRequester.verification_calls == 1:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "looks complete but readback is missing",
                        "missing_criteria": ["file readback missing"],
                        "replan_instruction": "",
                        "final_result": "done",
                    }
                else:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "readback evidence is now present",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result": "legacy script upgraded and verified",
                    }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "repair the legacy script using current Agently APIs",
                    "expected_evidence": "script execution succeeds and file is read back",
                    "rationale": "the prior verification gap must be closed",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "patched script and ran verification",
                    "evidence": ["python legacy_script.py exited with status 0", "file readback succeeded"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-guard-missing-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-guard-missing-plugins")
    plugin_manager.register("ModelRequester", CompleteWithMissingRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-guard-missing")

    task = agent.create_task(
        task_id="guard-missing",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The final file readback evidence is included."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert len(meta["iterations"]) == 2
    assert any(item.path.endswith(".replan") for item in stream_items)
    assert meta["iterations"][0]["verification"]["is_complete"] is False
    assert "missing_criteria_present" in meta["iterations"][0]["verification"]["guard_reasons"]
    assert meta["diagnostics"]["verification_guards"]


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_when_final_result_missing(tmp_path):
    class CompleteWithoutFinalResultRequester(MockAgentTaskRequester):
        name = "CompleteWithoutFinalResultRequester"
        verification_calls = 0

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                CompleteWithoutFinalResultRequester.verification_calls += 1
                if CompleteWithoutFinalResultRequester.verification_calls == 1:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "all evidence is present but no final deliverable was returned",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result_required": True,
                        "final_result": "",
                    }
                else:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "final deliverable is now included",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result_required": True,
                        "final_result": "Final remediation report with verification evidence.",
                    }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "produce the final report artifact",
                    "expected_evidence": "final report text",
                    "rationale": "the verifier requires the final deliverable before acceptance",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "prepared final report artifact",
                    "evidence": ["final report content is present"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-final-result-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-final-result-plugins")
    plugin_manager.register("ModelRequester", CompleteWithoutFinalResultRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-final-result")

    task = agent.create_task(
        task_id="guard-final-result",
        goal="Return the final remediation report.",
        success_criteria=["The final report artifact is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["artifact_status"] == "accepted"
    assert len(meta["iterations"]) == 2
    assert any(item.path.endswith(".replan") for item in stream_items)
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert "final_result_missing" in first_verification["guard_reasons"]
    assert "Final result is missing." in first_verification["missing_criteria"]
    assert first_verification["replan_instruction"]
    assert (
        meta["iterations"][1]["verification"]["final_result"] == "Final remediation report with verification evidence."
    )


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_on_failed_action_evidence(tmp_path):
    class AlwaysCompleteRequester(MockAgentTaskRequester):
        name = "AlwaysCompleteRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "all criteria are satisfied",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "legacy script upgraded and verified",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "run the verification command",
                    "expected_evidence": "command succeeds",
                    "rationale": "the task needs command evidence",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-guard-action-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-guard-action-plugins")
    plugin_manager.register("ModelRequester", AlwaysCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-guard-action")
    task = agent.create_task(
        task_id="guard-action",
        goal="Repair a legacy Agently script and return the final verified result.",
        success_criteria=["The verification command succeeds.", "The final result is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    async def fake_execute(iteration_index, plan, context_pack):
        _ = (plan, context_pack)
        status = "failed" if iteration_index == 1 else "success"
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": [status], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {
                    "action_logs": {
                        "run_task_command": {
                            "name": "run_task_command",
                            "status": status,
                            "action_type": "shell",
                        }
                    }
                },
            },
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": fake_execute}

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert len(meta["iterations"]) == 2
    assert any(phase["phase"] == "replanned" for phase in meta["diagnostics"]["phases"])
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert "execution_risk_actions_present" in first_verification["guard_reasons"]
    second_verification = meta["iterations"][1]["verification"]
    assert second_verification["is_complete"] is True
    assert "execution_risk_actions_present" not in second_verification.get("guard_reasons", [])
    second_logs = meta["iterations"][1]["execution_meta"]["logs"]["action_logs"]
    assert second_logs["run_task_command"]["status"] == "success"


@pytest.mark.asyncio
async def test_agent_task_loop_replans_on_structured_blocks_replan_signal(tmp_path):
    class AlwaysCompleteRequester(MockAgentTaskRequester):
        name = "AlwaysCompleteForReplanSignalRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "model thinks the task is complete",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "final report",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "collect structured evidence",
                    "expected_evidence": "valid upstream evidence",
                    "rationale": "the step needs trusted evidence",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-replan-signal-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-replan-signal-plugins")
    plugin_manager.register("ModelRequester", AlwaysCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-replan-signal")
    task = agent.create_task(
        task_id="structured-replan-signal",
        goal="Produce a final report only after structured execution evidence is valid.",
        success_criteria=["The upstream evidence is valid.", "The final report is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    async def fake_execute(iteration_index, plan, context_pack):
        _ = (plan, context_pack)
        replan_diagnostics = []
        if iteration_index == 1:
            replan_diagnostics.append(
                {
                    "kind": "replan_signal",
                    "status": "replan_goal",
                    "reason": "upstream evidence invalidates the current goal plan",
                    "affected_plan_block_ids": ["collect"],
                    "affected_execution_block_ids": ["collect:model_request"],
                }
            )
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["candidate"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {"action_logs": {}},
                "blocks": {
                    "evidence": {"diagnostics": replan_diagnostics},
                    "snapshot": {"blocks": {"replan_signals": replan_diagnostics}},
                },
            },
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": fake_execute}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert len(meta["iterations"]) == 2
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert first_verification["replan_signals"][0]["status"] == "replan_goal"
    assert "structured_replan_signal" in first_verification["guard_reasons"]
    assert any(
        phase["phase"] == "replanned" and phase["diagnostics"]["replan_signals"][0]["status"] == "replan_goal"
        for phase in meta["diagnostics"]["phases"]
    )


@pytest.mark.asyncio
async def test_required_capabilities_satisfied_cumulatively_across_iterations(tmp_path):
    """ISSUE-012: required actions and skills can be satisfied in different steps."""

    class CumulativeRequester(MockAgentTaskRequester):
        name = "CumulativeRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "evidence is present",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": False,
                    "final_result": "done",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "produce capability evidence",
                    "expected_evidence": "x",
                    "rationale": "y",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-cumulative-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-cumulative-plugins")
    plugin_manager.register("ModelRequester", CumulativeRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-cumulative")

    constraints = {"capability_constraints": {"actions": {"required": ["act_x"]}, "skills": {"required": ["skill_y"]}}}

    async def step_with_capability(iteration_index, plan, context_pack):
        # iteration 1 satisfies the required action, iteration 2 the required skill.
        if iteration_index == 1:
            logs = {"action_logs": {"act_x": {"name": "act_x", "status": "success"}}, "route_logs": {}}
        else:
            logs = {"action_logs": {}, "route_logs": {"plan": {"selected_skills": [{"skill_id": "skill_y"}]}}}
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request" if iteration_index == 1 else "skills"},
                "logs": logs,
                "effective_options": constraints,
            },
        )

    task = agent.create_task(
        task_id="cumulative-capabilities",
        goal="Satisfy required action and skill across steps.",
        success_criteria=["The required action and skill are both used."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        options={"capability_constraints": constraints["capability_constraints"]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step_with_capability}

    result = await task.async_run()
    meta = await task.async_meta()

    # Iteration 1 cannot complete (skill_y still missing); iteration 2 completes.
    assert result["status"] == "completed"
    assert len(meta["iterations"]) == 2
    first = meta["iterations"][0]["verification"]
    assert first["is_complete"] is False
    assert "skill_y" in " ".join(first.get("missing_required_capabilities", []))


@pytest.mark.asyncio
async def test_agent_task_resumes_from_checkpoint_after_crash(tmp_path):
    """ISSUE-005: a task continues from its last durable snapshot in a fresh task object."""
    MockAgentTaskRequester.reset()
    workspace_dir = tmp_path / "task-workspace"

    # First run: iteration 1 replans (verifier incomplete), then a simulated crash
    # before iteration 2 by raising inside the step of iteration 2.
    agent = _create_agent("agent-task-resume-1").use_workspace(workspace_dir)
    task = agent.create_task(
        task_id="resumable-task",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=workspace_dir,
        max_iterations=3,
        options={"agent_task": {"workspace_recovery": True}},
    )

    async def crash_on_second_iteration(iteration_index, plan, context_pack):
        if iteration_index >= 2:
            raise RuntimeError("simulated process crash")
        cast(Any, task).task_record._task_reference_catalog.add_evidence(
            {
                "id": "resume.seed",
                "kind": "action_evidence",
                "status": "ok",
                "body_state": "bounded",
                "body": "stable before checkpoint",
            }
        )
        return (
            {"step_result": "iteration 1 partial", "evidence": ["progress"], "remaining_work": ["finish"]},
            {"execution_id": "exec-1", "status": "completed", "route": {"selected_route": "model_request"}, "logs": {}},
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": crash_on_second_iteration}
    with pytest.raises(RuntimeError):
        await task.async_run()

    # A resume snapshot for iteration 1 must have been persisted (namespaced so
    # it does not mix with the task's per-step observation checkpoints).
    snapshot = await agent.workspace.get_snapshot("resumable-task::resume")
    assert snapshot is not None
    assert snapshot["iteration"] == 1
    assert snapshot["manifest"]["goal"].startswith("Repair a legacy")
    assert snapshot["task_reference_catalog"]["task_id"] == "resumable-task"
    assert snapshot["task_reference_catalog"]["references"]
    assert snapshot["terminal_convergence"]["task_id"] == "resumable-task"
    seeded_reference = next(iter(snapshot["task_reference_catalog"]["references"]))
    seeded_target = snapshot["task_reference_catalog"]["references"][seeded_reference]["evidence_id"]
    # Failed-run cleanup discards ordinary process checkpoints while the
    # explicitly anchored compact resume snapshot remains available.
    assert await agent.workspace.checkpoint_history("resumable-task") == []

    # Second run: a fresh AgentExecution resumes from the snapshot and completes.
    MockAgentTaskRequester.reset()
    agent2 = _create_agent("agent-task-resume-2").use_workspace(workspace_dir)
    resumed = await agent2.async_resume("resumable-task", workspace=workspace_dir)

    async def finish_step(iteration_index, plan, context_pack):
        return (
            {"step_result": "completed", "evidence": ["done"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "repair_step",
                            "action_call_id": f"repair-call-{iteration_index}",
                            "status": "success",
                            "result": f"repair evidence version {iteration_index}",
                        }
                    ]
                },
            },
        )

    cast(Any, resumed)._agent_task_step_overrides = {"_execute_step": finish_step}
    result = await resumed.async_start()
    assert (
        cast(Any, resumed).task_record._task_reference_catalog.resolve(seeded_reference)["evidence_id"] == seeded_target
    )
    execution_meta = await resumed.async_get_meta()
    meta = execution_meta["logs"]["route_logs"]["agent_task"]

    assert resumed.task_refs["resume"] is True
    assert resumed.task_refs["resumed_from_iteration"] == 1
    assert meta["resumed_from_iteration"] == 1
    # Continued from iteration 2 (did not re-run iteration 1).
    assert meta["iterations"][0]["iteration"] == 2
    assert cast(Any, resumed).task_record._terminal_convergence_state.snapshot()["task_id"] == "resumable-task"
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_task_resume_without_snapshot_raises(tmp_path):
    """ISSUE-005: resuming an unknown task id is an explicit error."""
    agent = _create_agent("agent-task-resume-missing").use_workspace(tmp_path / "task-workspace")
    with pytest.raises(ValueError):
        await agent.async_resume("does-not-exist", workspace=tmp_path / "task-workspace")


@pytest.mark.asyncio
async def test_task_wall_clock_budget_surfaces_timed_out(tmp_path):
    """ISSUE-010: max_seconds is a task wall-clock deadline across task stages."""
    agent = _create_agent("agent-task-deadline").use_workspace(tmp_path / "task-workspace")
    task = agent.create_task(
        task_id="deadline",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        execution="flat",
        max_iterations=3,
        limits={"max_seconds": 0.2},
    )

    async def slow_request_plan(_iteration_index, _context_pack):
        await asyncio.sleep(0.6)
        return {
            "step_instruction": "repair the script",
            "expected_evidence": "script execution succeeds",
            "rationale": "the task should not reach this plan after the deadline",
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": slow_request_plan}

    result = await task.async_run()
    assert result["status"] == "timed_out"
    assert task.status == "timed_out"
    assert "plan stage" in result["reason"]


def test_action_final_status_exempts_recovered_actions():
    """ISSUE-012: an action that failed then succeeded is not a risk action."""
    from agently.core.application.AgentTask.Task import AgentTask

    statuses = {"act_a": "success", "act_b": "failed"}
    failed = AgentTask._action_ids_by_final_status(statuses, {"failed", "failure", "error"})
    assert failed == ["act_b"]
    assert "act_a" not in failed


@pytest.mark.asyncio
async def test_interview_semantic_judge_returns_structured_rule_fields():
    class SemanticJudgeRequester(MockAgentTaskRequester):
        name = "SemanticJudgeRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            assert "Judge the candidate interview brief semantically" in text
            payload = {
                "accepted": False,
                "source_specificity_ok": False,
                "target_coverage_ok": True,
                "conflict_handling_ok": False,
                "low_evidence_handling_ok": False,
                "blog_interview_quality_ok": True,
                "not_hiring_framed_ok": True,
                "reason": "Sources are too generic and uncertainty is not handled.",
                "rule_evidence": [
                    {
                        "rule": "source_specificity",
                        "ok": False,
                        "evidence": "The brief says sources exist but gives no URL or title.",
                    }
                ],
            }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="interview-semantic-judge-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="interview-semantic-judge-plugins")
    plugin_manager.register("ModelRequester", SemanticJudgeRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="interview-semantic-judge")

    result = await judge_interview_semantics(
        agent,
        file_text="# Interview brief\n\nSources: public web.\n\nQuestions?\n",
        interview_input={
            "targets": [
                {
                    "raw_input": "Karpathy from Anthropic",
                    "original_name": "Karpathy",
                    "organization_or_work": "Anthropic",
                    "aliases": [],
                }
            ],
            "interview_goal": "Prepare a blog interview brief.",
        },
        success_criteria=["The brief handles source evidence and target ambiguity."],
        action_summary={"action_log_count": 1, "action_log_ids": ["web_search"]},
    )

    assert result["accepted"] is False
    assert result["source_specificity_ok"] is False
    assert result["target_coverage_ok"] is True
    assert result["rule_evidence"]


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_failure_is_side_channel(tmp_path):
    MockAgentTaskRequester.reset()

    class FailingProgressRequester(MockAgentTaskRequester):
        name = "FailingProgressRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Summarize AgentTask progress" in text:
                raise RuntimeError("progress model unavailable")
            async for event in super().request_model(request_data):
                yield event

    settings = Settings(name="agent-task-failing-progress-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-failing-progress-plugins")
    plugin_manager.register("ModelRequester", FailingProgressRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-failing-progress")
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_agent_task_loop_progress_model_failure_is_side_channel.capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        task = agent.create_task(
            task_id="failing-progress",
            goal="Repair a legacy Agently script so it runs on the current API.",
            success_criteria=["The script runs successfully."],
            workspace=tmp_path / "task-workspace",
            max_iterations=1,
            options={
                "agent_task": {
                    "stream_progress": True,
                    "progress_model_key": "progress-narrator",
                    "progress_timeout_seconds": 5,
                },
            },
        )

        result = await task.async_run()
        meta = await task.async_meta()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    event_types = [event.event_type for event in captured]
    side_channel_events = [
        event
        for event in captured
        if event.event_type in {"model.side_channel_request_failed", "request.side_channel_failed"}
    ]

    assert result["status"] == "max_iterations"
    assert side_channel_events
    assert "model.request_failed" not in event_types
    assert "request.failed" not in event_types
    assert all(event.level == "WARNING" for event in side_channel_events)
    assert meta["diagnostics"]["progress_errors"]


async def _collect_stream(task) -> list[Any]:
    return [item async for item in task.get_async_generator(type="instant")]


def _capability_gate_agent(name: str):
    """Agent whose verifier always claims completion (drives the gate tests)."""

    class AlwaysCompleteVerifier(MockAgentTaskRequester):
        name = "CapabilityGateVerifier"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            MockAgentTaskRequester.calls.append(text)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "looks done from the verifier's view",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": False,
                    "final_result": "done",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "produce the artifact directly",
                    "expected_evidence": "x",
                    "rationale": "y",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "produced the artifact",
                    "evidence": ["artifact written"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name=f"{name}-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-plugins")
    plugin_manager.register("ModelRequester", AlwaysCompleteVerifier, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


@pytest.mark.asyncio
async def test_capability_evidence_gate_blocks_bypass_when_capability_unused(tmp_path):
    """AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC (load-bearing gate).

    The verifier claims completion, but the required capability never appears in
    execution evidence. The structured capability-evidence requirement must turn
    this into a hard verification failure, so 'accepted with the capability
    bypassed' becomes impossible — regardless of the capability kind.
    """
    agent = _capability_gate_agent("agent-task-capability-bypass")

    async def bypass_step(iteration_index, plan, context_pack):
        # model_request route, no skill selected -> the capability was bypassed.
        return (
            {"step_result": "did it without the capability", "evidence": ["wrote a file"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {
                    "action_logs": {"write_file": {"name": "write_file", "status": "success"}},
                    "route_logs": {},
                },
            },
        )

    task = agent.create_task(
        task_id="capability-evidence-bypass",
        goal="Produce the artifact using the intended capability.",
        success_criteria=["The artifact reflects the intended capability."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={
            "capability_evidence_requirements": [
                {"capability_id": "design-skill", "capability_kind": "skill", "kind": "capability_used"}
            ]
        },
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": bypass_step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] != "completed"
    assert result["accepted"] is False
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is False
    assert "capability_evidence_missing" in verification.get("guard_reasons", [])
    assert "design-skill" in " ".join(verification.get("missing_capability_evidence", []))
    assert "design-skill" in " ".join(verification.get("missing_required_capabilities", []))


@pytest.mark.asyncio
async def test_capability_evidence_gate_reads_execution_effective_options(tmp_path):
    """Agent.create_task routes may carry task-owned evidence requirements
    through execution effective_options. The deterministic guard must still
    enforce them against real action logs, not verifier prose."""
    agent = _capability_gate_agent("agent-task-execution-option-evidence-gate")

    async def prompt_only_step(iteration_index, plan, context_pack):
        return (
            {
                "step_result": "claimed final.md was written",
                "evidence": ["write_file action_succeeded"],
                "remaining_work": [],
            },
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "skills"},
                "logs": {
                    "action_logs": {},
                    "route_logs": {
                        "plan": {"selected_skills": [{"skill_id": "report-skill"}]},
                        "skill_logs": [{"skill_id": "report-skill", "status": "success"}],
                    },
                },
                "effective_options": {
                    "capability_evidence_requirements": [
                        {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"}
                    ]
                },
            },
        )

    task = agent.create_task(
        task_id="execution-option-evidence-gate",
        goal="Write a report to final.md.",
        success_criteria=["The report is written to final.md."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": prompt_only_step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] != "completed"
    assert result["accepted"] is False
    verification = meta["iterations"][0]["verification"]
    assert "capability_evidence_missing" in verification.get("guard_reasons", [])
    assert "write_file" in " ".join(verification.get("missing_capability_evidence", []))


def test_pending_action_evidence_requirement_escalates_direct_step_to_actions(tmp_path):
    agent = _capability_gate_agent("agent-task-action-evidence-shape")
    task = AgentTask(
        agent,
        task_id="pending-action-evidence-shape",
        goal="Write a report to final.md.",
        success_criteria=["The report is written to final.md."],
        workspace=tmp_path / "task-workspace",
        options={
            "capability_evidence_requirements": [
                {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"}
            ],
            "planner_capabilities": [
                {
                    "id": "write_file",
                    "kind": "action",
                    "route": "model_request",
                    "guidance_access": "none",
                    "description": "write",
                }
            ],
        },
    )
    used_actions: list[str] = []
    route_policies: list[dict[str, Any]] = []

    class DummyExecution:
        local_action_ids: list[str] = []

        def use_actions(self, action_ids):
            used_actions.extend(action_ids)

        def route_policy(self, policy):
            route_policies.append(policy)

        def record_consumed_option(self, *args, **kwargs):
            return None

    plan: dict[str, Any] = {
        "execution_shape": "direct",
        "step_instruction": "Write the file.",
        "expected_evidence": "final.md",
        "rationale": "the task asks for a file",
    }

    step_execution = task._configure_step_execution(DummyExecution(), plan)

    assert step_execution["effective_shape"] == "actions"
    assert plan["effective_execution_shape"] == "actions"
    assert plan["execution_shape_adjustment"]["reason"] == "pending_action_succeeded_evidence"
    assert used_actions == ["write_file"]
    assert route_policies and route_policies[0]["allowed_routes"] == ["model_request"]


def test_pending_action_evidence_requirement_escalates_skills_step_to_actions(tmp_path):
    agent = _capability_gate_agent("agent-task-skill-step-action-evidence-shape")
    task = AgentTask(
        agent,
        task_id="pending-action-evidence-skills-shape",
        goal="Write a report to final.md using configured Skill guidance.",
        success_criteria=["The report is written to final.md."],
        workspace=tmp_path / "task-workspace",
        options={
            "capability_evidence_requirements": [
                {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"}
            ],
            "planner_capabilities": [
                {
                    "id": "write_file",
                    "kind": "action",
                    "route": "model_request",
                    "guidance_access": "none",
                    "description": "write",
                },
                {
                    "id": "report-skill",
                    "kind": "skill",
                    "route": "model_request",
                    "guidance_access": "prompt_bound",
                    "description": "report guidance",
                },
            ],
        },
    )
    used_actions: list[str] = []
    route_policies: list[dict[str, Any]] = []

    class DummyExecution:
        local_action_ids: list[str] = []

        def use_actions(self, action_ids):
            used_actions.extend(action_ids)

        def route_policy(self, policy):
            route_policies.append(policy)

        def record_consumed_option(self, *args, **kwargs):
            return None

    plan: dict[str, Any] = {
        "execution_shape": "skills",
        "step_instruction": "Write the file with the configured Skill guidance.",
        "expected_evidence": "final.md",
        "rationale": "the task asks for a file and Skill guidance",
    }

    step_execution = task._configure_step_execution(DummyExecution(), plan)

    assert step_execution["effective_shape"] == "actions"
    assert plan["execution_shape_adjustment"]["from"] == "skills"
    assert plan["execution_shape_adjustment"]["reason"] == "pending_action_succeeded_evidence"
    assert used_actions == ["write_file"]
    assert route_policies and route_policies[0]["allowed_routes"] == ["model_request"]


@pytest.mark.asyncio
async def test_execution_exception_becomes_verifier_visible_failed_evidence(tmp_path, monkeypatch):
    """A bounded step runtime error must not crash AgentTask before the
    observation/verifier path can see it. The deterministic guard blocks a
    verifier that incorrectly claims completion over failed execution evidence."""
    agent = _capability_gate_agent("agent-task-execution-failure-evidence")

    class FailingExecution:
        id = "exec-failed-route"

        def __init__(self):
            self.local_action_ids: list[str] = []

        def input(self, value):
            return self

        def instruct(self, value):
            return self

        def output(self, value, *, format=None):
            return self

        def language(self, value):
            return self

        async def async_get_data(self):
            raise RuntimeError("runtime placeholder path is invalid")

        async def async_get_meta(self):
            return {"execution_id": self.id, "status": "failed", "route": {"selected_route": "direct"}, "logs": {}}

    task = agent.create_task(
        task_id="execution-failure-evidence",
        goal="Produce the artifact using a bounded execution step.",
        success_criteria=["The artifact is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )
    monkeypatch.setattr(agent, "create_execution", lambda **kwargs: FailingExecution())

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    execution_meta = meta["iterations"][0]["execution_meta"]
    assert execution_meta["status"] == "failed"
    assert execution_meta["logs"]["errors"][0]["message"] == "runtime placeholder path is invalid"
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is False
    assert "execution_status_failed" in verification.get("guard_reasons", [])
    assert "runtime placeholder path is invalid" in " ".join(verification.get("missing_criteria", []))
    assert meta["diagnostics"]["execution_errors"][0]["message"] == "runtime placeholder path is invalid"


@pytest.mark.asyncio
async def test_execution_exception_compacts_provider_request_payload_for_hot_paths(tmp_path, monkeypatch):
    """Provider errors may mention request payloads, but AgentTask hot paths
    must keep only a compact error fact for verifier/planner input."""
    agent = _capability_gate_agent("agent-task-provider-error-compaction")
    huge_request_payload = (
        'Request Data: {"messages": ["SECRET_PROMPT_SHOULD_NOT_ENTER_VERIFIER", "'
        + ("large-token " * 500)
        + '"], "tools": ["SECRET_TOOL_SCHEMA_SHOULD_NOT_ENTER_VERIFIER"]}'
    )
    provider_error = (
        "Status Code: 403\n"
        "Response: {'code': 'AllocationQuota.FreeTierOnly', 'message': 'quota exhausted'}\n" + huge_request_payload
    )

    class FailingExecution:
        id = "exec-provider-error"

        def __init__(self):
            self.local_action_ids: list[str] = []

        def input(self, value):
            return self

        def instruct(self, value):
            return self

        def output(self, value, *, format=None):
            return self

        def language(self, value):
            return self

        async def async_get_data(self):
            raise RuntimeError(provider_error)

        async def async_get_meta(self):
            return {"execution_id": self.id, "status": "failed", "route": {"selected_route": "direct"}, "logs": {}}

    task = agent.create_task(
        task_id="provider-error-compaction",
        goal="Produce the artifact using a bounded execution step.",
        success_criteria=["The artifact is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )
    monkeypatch.setattr(agent, "create_execution", lambda **kwargs: FailingExecution())

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "max_iterations"
    hot_path_text = json.dumps(
        {
            "diagnostics": meta["diagnostics"],
            "execution_meta": meta["iterations"][0]["execution_meta"],
            "verification": meta["iterations"][0]["verification"],
        },
        ensure_ascii=False,
    )
    assert "Status Code: 403" in hot_path_text
    assert "AllocationQuota.FreeTierOnly" in hot_path_text
    assert "Request Data" not in hot_path_text
    assert "SECRET_PROMPT_SHOULD_NOT_ENTER_VERIFIER" not in hot_path_text
    assert "SECRET_TOOL_SCHEMA_SHOULD_NOT_ENTER_VERIFIER" not in hot_path_text
    error_messages = [
        meta["diagnostics"]["execution_errors"][0]["message"],
        meta["iterations"][0]["execution_meta"]["logs"]["errors"][0]["message"],
        meta["iterations"][0]["execution_meta"]["diagnostics"]["execution_error"]["message"],
    ]
    assert all(len(message) < 2500 for message in error_messages)


@pytest.mark.asyncio
async def test_capability_evidence_gate_passes_when_capability_used(tmp_path):
    """The same requirement passes when the capability genuinely shows up in
    execution evidence (here, a skills-route selected_skills record)."""
    agent = _capability_gate_agent("agent-task-capability-present")

    async def skills_step(iteration_index, plan, context_pack):
        return (
            {"step_result": "used the capability", "evidence": ["rendered via capability"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "skills"},
                "logs": {
                    "action_logs": {},
                    "route_logs": {"plan": {"selected_skills": [{"skill_id": "design-skill"}]}},
                },
            },
        )

    task = agent.create_task(
        task_id="capability-evidence-present",
        goal="Produce the artifact using the intended capability.",
        success_criteria=["The artifact reflects the intended capability."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
        options={"capability_evidence_requirements": ["design-skill"]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": skills_step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is True
    assert verification.get("missing_capability_evidence", []) == []


@pytest.mark.asyncio
async def test_capability_evidence_satisfied_cumulatively_across_iterations(tmp_path):
    """Capability evidence accumulates across iterations: iteration 1 lacks it
    (gate fails -> replan), iteration 2 supplies it (task accepted)."""
    agent = _capability_gate_agent("agent-task-capability-cumulative")

    async def step(iteration_index, plan, context_pack):
        if iteration_index == 1:
            logs = {"action_logs": {"prep": {"name": "prep", "status": "success"}}, "route_logs": {}}
        else:
            logs = {"action_logs": {}, "route_logs": {"plan": {"selected_skills": [{"skill_id": "design-skill"}]}}}
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request" if iteration_index == 1 else "skills"},
                "logs": logs,
            },
        )

    task = agent.create_task(
        task_id="capability-evidence-cumulative",
        goal="Produce the artifact using the intended capability.",
        success_criteria=["The artifact reflects the intended capability."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        options={"capability_evidence_requirements": ["design-skill"]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert len(meta["iterations"]) == 2
    first = meta["iterations"][0]["verification"]
    assert first["is_complete"] is False
    assert "design-skill" in " ".join(first.get("missing_capability_evidence", []))


@pytest.mark.asyncio
async def test_step_planner_prompt_exposes_capability_candidates_of_all_kinds(tmp_path):
    """Planner visibility: action, skill, and skill_pack capability candidates
    reach the planner prompt as one typed snapshot in options, with guidance_access
    so the planner knows which capabilities need their own route."""
    MockAgentTaskRequester.reset()
    agent = _capability_gate_agent("agent-task-capability-visibility")

    task = agent.create_task(
        task_id="capability-visibility",
        goal="Produce the artifact, choosing the right execution shape.",
        success_criteria=["An artifact is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={
            "planner_capabilities": [
                {
                    "id": "fetch_sources",
                    "kind": "action",
                    "route": "model_request",
                    "guidance_access": "none",
                    "description": "gather",
                },
                {
                    "id": "design-skill",
                    "kind": "skill",
                    "route": "skills",
                    "guidance_access": "route_context",
                    "mode": "model_decision",
                    "description": "design",
                },
                {
                    "id": "report-pack",
                    "kind": "skill_pack",
                    "route": "skills",
                    "guidance_access": "route_context",
                    "description": "pack",
                },
            ]
        },
    )

    await task.async_run()

    plan_calls = [text for text in MockAgentTaskRequester.calls if "Plan the next bounded AgentExecution step" in text]
    assert plan_calls, "planner was never invoked"
    plan_text = plan_calls[0]
    assert "planner_capabilities" in plan_text
    for capability_id in ("fetch_sources", "design-skill", "report-pack"):
        assert capability_id in plan_text
    for kind in ("action", "skill", "skill_pack"):
        assert kind in plan_text
    assert "guidance_access" in plan_text


def test_step_scope_restricts_step_actions_from_structured_field(tmp_path):
    """Step scope comes from the structured step_scope field (not prose): an
    allowed_capability_ids list narrows the bounded step's action candidates via
    the execution-local action-id seam."""
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-step-scope")
    task = AgentTask(
        agent,
        goal="Gather evidence in a scoped step.",
        success_criteria=["Evidence gathered."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []
            self.applied_route_policy: dict[str, Any] | None = None

        def route_policy(self, value):
            self.applied_route_policy = value

        def record_consumed_option(self, *args, **kwargs):
            pass

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "direct",
            "step_instruction": "gather only",
            "expected_evidence": "x",
            "rationale": "y",
            "step_scope": {"allowed_capability_ids": ["fetch_sources"]},
        }
    )
    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert execution.local_action_ids == ["fetch_sources"]
    assert step_execution["step_scope"]["allowed_capability_ids"] == ["fetch_sources"]


def test_step_scope_uses_agent_execution_use_actions_when_available(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-step-scope-use-actions")
    task = AgentTask(
        agent,
        goal="Gather evidence in a scoped step.",
        success_criteria=["Evidence gathered."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    class _FakeExecution:
        def __init__(self):
            self.used_actions: list[list[str]] = []
            self.applied_route_policy: dict[str, Any] | None = None

        def use_actions(self, action_ids):
            self.used_actions.append(list(action_ids))

        def route_policy(self, value):
            self.applied_route_policy = value

        def record_consumed_option(self, *args, **kwargs):
            pass

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "gather only",
            "expected_evidence": "x",
            "rationale": "y",
            "step_scope": {"allowed_capability_ids": ["fetch_sources"]},
        }
    )
    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert execution.used_actions == [["fetch_sources"]]
    assert step_execution["action_scope_source"] == "step_scope"


def test_step_required_action_ids_scope_actions_without_contract_guard(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-step-required-actions")
    task = AgentTask(
        agent,
        goal="Run a required action.",
        success_criteria=["Required action evidence exists."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    class _FakeExecution:
        def __init__(self):
            self.required_actions: list[list[str]] = []
            self.used_actions: list[list[str]] = []
            self.applied_route_policy: dict[str, Any] | None = None

        def use_actions(self, action_ids):
            self.used_actions.append(list(action_ids))

        def require_actions(self, action_ids):
            self.required_actions.append(list(action_ids))

        def route_policy(self, value):
            self.applied_route_policy = value

        def record_consumed_option(self, *args, **kwargs):
            pass

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "run required probe",
            "expected_evidence": "action record",
            "rationale": "user required the action",
            "required_action_ids": ["probe_action"],
        }
    )
    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert execution.used_actions == [["probe_action"]]
    assert execution.required_actions == []
    assert step_execution["required_action_ids"] == ["probe_action"]
    assert step_execution["step_required_action_ids"] == ["probe_action"]
    assert step_execution["task_required_action_ids"] == []
    assert step_execution["action_scope_source"] == "step_required_action_ids"


def test_auto_step_plan_suppresses_dag_after_prior_dag_failure(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-auto-dag-suppression")
    task = AgentTask(
        agent,
        goal="Run the next useful step.",
        success_criteria=["The result is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
        options={"agent_task": {"effort": {"execution": {"step_plan": "auto"}}}},
    )
    cast(Any, task)._failed_execution_shapes.add("dynamic_task")

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "execution_dag",
            "step_instruction": "try a DAG",
            "expected_evidence": "result",
            "rationale": "has substeps",
            "dynamic_task": {"plan": {"tasks": []}},
        }
    )

    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert step_execution["effective_shape"] == "direct"
    assert step_execution["warning"] == "dag_shape_not_agent_execution_strategy"
    assert step_execution["policy"]["allow_dag_steps"] is False
    assert step_execution["policy"]["suppressed_execution_shapes"] == ["dynamic_task"]
    assert not hasattr(execution, "_add_dynamic_task_candidate")


def test_direct_step_plan_rejects_model_generated_dynamic_task_shape(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-direct-dag-rejected")
    task = AgentTask(
        agent,
        goal="Run one bounded AgentExecution step.",
        success_criteria=["The result is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"effort": {"execution": {"step_plan": "direct"}}}},
    )

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "execution_dag",
            "step_instruction": "try a DAG anyway",
            "expected_evidence": "result",
            "rationale": "model proposed a DAG",
            "dynamic_task": {"plan": {"tasks": []}},
        }
    )

    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert step_execution["effective_shape"] == "direct"
    assert step_execution["warning"] == "dag_shape_not_agent_execution_strategy"
    assert not hasattr(execution, "_add_dynamic_task_candidate")


def test_explicit_dag_step_plan_is_not_agent_task_strategy(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-explicit-dag-kept")
    task = AgentTask(
        agent,
        goal="Run the requested DAG step.",
        success_criteria=["The result is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"effort": {"execution": {"step_plan": "dag"}}}},
    )
    cast(Any, task)._failed_execution_shapes.add("dynamic_task")

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "dynamic_task",
            "step_instruction": "run the explicit DAG",
            "expected_evidence": "result",
            "rationale": "caller asked for dag",
            "dynamic_task": {"plan": {"tasks": []}},
        }
    )

    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert step_execution["effective_shape"] == "direct"
    assert step_execution["dag_shape_degraded"] is True
    assert step_execution["warning"] == "dag_shape_not_agent_execution_strategy"
    assert step_execution["policy"]["step_plan"] == "direct"
    assert step_execution["policy"]["step_plan_degraded_from"] == "dag"
    assert step_execution["policy"]["allow_dag_steps"] is False
    assert not hasattr(execution, "_add_dynamic_task_candidate")


def test_execution_log_summary_infers_action_success_from_route_history():
    """Older or nested route histories may expose action result records without
    a status field. AgentTask still needs a deterministic evidence projection."""
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "success",
            "logs": {
                "route_logs": {
                    "output": {
                        "history": [
                            {"name": "write_file", "result": {"path": "out.html"}},
                            {"name": "read_file", "error": "not found"},
                        ]
                    }
                }
            },
        }
    )

    assert summary["action_statuses"]["write_file"] == "success"
    assert summary["action_statuses"]["read_file"] == "failed"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["write_file"]
    assert summary["capability_evidence"]["actions"]["failed"] == ["read_file"]


def test_execution_log_summary_treats_partial_success_search_as_succeeded_action():
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "completed",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "web_search",
                        "status": "partial_success",
                        "action_call_id": "call-search",
                        "model_digest": {
                            "result_preview": [
                                {
                                    "title": "Official release note",
                                    "href": "https://example.test/release",
                                    "body": "A successful backend returned useful source evidence.",
                                }
                            ],
                            "result_preview_meta": {"truncated": False},
                        },
                        "diagnostics": [
                            {
                                "code": "search_backend_failed",
                                "backend": "yahoo",
                                "message": "transient backend failure",
                            }
                        ],
                    }
                ],
                "route_logs": {},
            },
        }
    )

    assert summary["failed_actions"] == []
    assert summary["action_statuses"]["web_search"] == "partial_success"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["web_search"]
    assert summary["capability_evidence"]["actions"]["failed"] == []


def test_execution_log_summary_infers_nested_partial_success_result_as_succeeded_action():
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "completed",
            "logs": {
                "route_logs": {
                    "output": {
                        "history": [
                            {
                                "name": "web_search",
                                "result": {
                                    "status": "partial_success",
                                    "ok": True,
                                    "success": True,
                                    "data": [
                                        {
                                            "title": "Official release note",
                                            "href": "https://example.test/release",
                                            "body": "A recovered backend returned usable evidence.",
                                        }
                                    ],
                                    "diagnostics": [
                                        {
                                            "code": "search_backend_failed",
                                            "backend": "yahoo",
                                            "message": "transient backend failure",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                }
            },
        }
    )

    assert summary["failed_actions"] == []
    assert summary["action_statuses"]["web_search"] == "partial_success"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["web_search"]
    assert summary["capability_evidence"]["actions"]["failed"] == []


def test_execution_log_summary_includes_nested_action_result_previews():
    """AgentTask verifier evidence must include bounded Action observations
    produced inside the TriggerFlow/Blocks bounded-step execution wrapper."""
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "completed",
            "logs": {"action_logs": {}, "route_logs": {}},
            "blocks": {
                "evidence": {
                    "execution_block_results": [
                        {
                            "output": {
                                "execution_meta": {
                                    "status": "completed",
                                    "logs": {
                                        "action_logs": [
                                            {
                                                "action_id": "browse",
                                                "status": "success",
                                                "action_call_id": "call-1",
                                                "model_digest": {
                                                    "result_preview": {
                                                        "selected_url": "https://example.test/syllabus",
                                                        "content": (
                                                            ("Navigation link " * 160)
                                                            + "Official syllabus sections: "
                                                            + "1. Foundations; 2. Model architecture."
                                                        ),
                                                    },
                                                    "result_preview_meta": {
                                                        "original_size": 120,
                                                        "preview_size": 80,
                                                        "truncated": False,
                                                    },
                                                },
                                            }
                                        ],
                                        "route_logs": {},
                                    },
                                },
                                "execution_result": {"step_result": "ok"},
                            }
                        }
                    ]
                }
            },
        }
    )

    assert summary["action_ids"] == ["browse"]
    assert summary["action_statuses"]["browse"] == "success"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["browse"]
    action = summary["actions"][0]
    assert action["action_call_id"] == "call-1"
    assert action["result_preview"]["selected_url"] == "https://example.test/syllabus"
    assert "Official syllabus sections" in action["result_preview"]["content"]
    assert action["result_preview_meta"]["truncated"] is False

    verifier_summary = AgentTask._compact_verifier_evidence_summary(
        summary,
        include_body_previews=True,
    )
    verifier_action = verifier_summary["actions"][0]
    assert verifier_action["result_preview"]["selected_url"] == "https://example.test/syllabus"
    assert "Official syllabus sections" in verifier_action["result_preview"]["content"]
    assert "Model architecture" in verifier_action["result_preview"]["content"]


def test_cumulative_verifier_evidence_keeps_previous_iteration_action_previews(tmp_path):
    agent = _create_agent("agent-task-cumulative-evidence").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded report.",
        success_criteria=["The report uses the most specific official source evidence."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {"action_logs": {}, "route_logs": {}},
                "blocks": {
                    "evidence": {
                        "execution_block_results": [
                            {
                                "output": {
                                    "execution_meta": {
                                        "status": "completed",
                                        "logs": {
                                            "action_logs": [
                                                {
                                                    "action_id": "browse",
                                                    "status": "success",
                                                    "action_call_id": "call-specific",
                                                    "model_digest": {
                                                        "result_preview": {
                                                            "selected_url": "https://example.test/specific",
                                                            "content": (
                                                                "Specific official syllabus: "
                                                                "1. Foundations; 2. Model architecture."
                                                            ),
                                                        },
                                                        "result_preview_meta": {"truncated": False},
                                                    },
                                                }
                                            ],
                                            "route_logs": {},
                                        },
                                    }
                                }
                            }
                        ]
                    }
                },
            },
        }
    )

    cumulative = task._cumulative_execution_evidence_summary(
        {
            "status": "completed",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "write_file",
                        "status": "success",
                        "action_call_id": "call-write",
                    }
                ],
                "route_logs": {},
            },
        }
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(
        cumulative,
        include_body_previews=True,
    )

    actions = verifier_summary["actions"]
    assert [action["id"] for action in actions] == ["browse", "write_file"]
    browse_preview = actions[0]["result_preview"]
    assert browse_preview["selected_url"] == "https://example.test/specific"
    assert "Specific official syllabus" in browse_preview["content"]
    assert any(
        ref["field"] == "selected_url" and ref["value"] == "https://example.test/specific"
        for ref in verifier_summary["source_refs"]
    )


def test_cumulative_evidence_ledger_keeps_current_action_result_when_old_items_overflow(tmp_path):
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    agent = _create_agent("agent-task-current-evidence-priority").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded report.",
        success_criteria=["Current source evidence remains verifier-visible."],
        execution="flat",
    )
    for index in range(150):
        task.iterations.append(
            {
                "iteration": index + 1,
                "execution_meta": {
                    "status": "completed",
                    "logs": {
                        "action_logs": [
                            {
                                "action_id": "browse",
                                "status": "success",
                                "action_call_id": f"act_call_old_{index}",
                                "model_digest": {
                                    "result_preview": {
                                        "selected_url": f"https://example.test/old/{index}",
                                        "content": f"Old bounded evidence {index}",
                                    },
                                    "result_preview_meta": {"truncated": False},
                                },
                            }
                        ],
                        "route_logs": {},
                    },
                },
            }
        )

    current_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "browse",
                    "status": "success",
                    "action_call_id": "act_call_current",
                    "model_digest": {
                        "result_preview": {
                            "selected_url": "https://example.test/current",
                            "content": "Current official syllabus evidence.",
                        },
                        "result_preview_meta": {"truncated": False},
                    },
                }
            ],
            "route_logs": {},
        },
    }

    ledger = task._cumulative_evidence_ledger(current_meta)
    current_id = "agent_task_action_result:browse:act_call_current"

    assert current_id in {item.get("id") for item in ledger.get("items", [])}
    guard = validate_evidence_use(
        [{"claim": "Current syllabus evidence.", "evidence_ids": [current_id], "support_type": "content"}],
        ledger,
    )
    assert guard["valid"] is True


def test_cumulative_evidence_ledger_excludes_stale_workspace_artifact_snapshots(tmp_path):
    agent = _create_agent("agent-task-current-artifact-version").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Repair and verify a mutable Workspace report.",
        success_criteria=["Only the current report version is verifier-visible."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {},
                "blocks": {
                    "evidence": {
                        "evidence_items": [
                            {
                                "id": "artifact-old",
                                "kind": "artifact_ref",
                                "status": "ok",
                                "body_state": "bounded",
                                "path": "final.md",
                                "role": "workspace_artifact",
                                "source": "agent_task.iteration.1.workspace_artifact",
                                "content_version_id": "cv_1",
                                "sha256": "old-sha",
                                "body": "Old report body.",
                            },
                            {
                                "id": "locator-old",
                                "kind": "workspace_artifact.acceptance_locator",
                                "status": "ok",
                                "body_state": "ref_only",
                                "path": "final.md",
                                "content_version_id": "cv_1",
                                "criterion_id": "artifact:heading:risks",
                            },
                            {
                                "id": "targeted-old",
                                "kind": "workspace_artifact.targeted_readback",
                                "status": "ok",
                                "body_state": "bounded",
                                "path": "final.md",
                                "content_version_id": "cv_1",
                                "body": "Old unsupported report sentence.",
                            },
                            {
                                "id": "targeted-old-unversioned",
                                "kind": "workspace_artifact.targeted_readback",
                                "status": "ok",
                                "body_state": "bounded",
                                "path": "final.md",
                                "body": "Legacy targeted readback without snapshot identity.",
                            },
                            {
                                "id": "independent-source",
                                "kind": "agent_task.action.result",
                                "status": "ok",
                                "body_state": "bounded",
                                "action_id": "market_snapshot",
                                "action_call_id": "call-source",
                                "body": "Independent market fact.",
                            },
                        ]
                    }
                },
            },
        }
    )
    current_meta = {
        "status": "completed",
        "logs": {},
        "blocks": {
            "evidence": {
                "evidence_items": [
                    {
                        "id": "artifact-current",
                        "kind": "artifact_ref",
                        "status": "ok",
                        "body_state": "bounded",
                        "path": "final.md",
                        "role": "workspace_artifact",
                        "source": "agent_task.iteration.2.grounding_workspace_patch",
                        "content_version_id": "cv_2",
                        "sha256": "current-sha",
                        "body": "Current corrected report body.",
                    }
                ]
            }
        },
    }

    ledger = task._cumulative_evidence_ledger(current_meta)
    items = {str(item.get("id")): item for item in ledger.get("items", [])}

    assert "artifact-current" in items
    assert items["artifact-current"]["content_version_id"] == "cv_2"
    assert "artifact-old" not in items
    assert "locator-old" not in items
    assert "targeted-old" not in items
    assert "targeted-old-unversioned" not in items
    assert "independent-source" in items


def test_workspace_artifact_readback_evidence_carries_content_identity():
    ref = {
        "path": "final.md",
        "source": "agent_task.iteration.2.grounding_workspace_patch",
        "locator_id": "loc_1",
        "content_version_id": "cv_2",
        "sha256": "current-sha",
        "bytes": 128,
        "preview": "Current corrected report body.",
    }

    readback = AgentTask._workspace_artifact_readback_evidence_item(ref)
    targeted = AgentTask._workspace_artifact_targeted_readback_evidence_item(
        readback,
        {
            "kind": "section_search",
            "path": "final.md",
            "status": "read",
            "query": "risk",
            "content": "Current risk section.",
        },
    )

    assert readback["locator_id"] == "loc_1"
    assert readback["content_version_id"] == "cv_2"
    assert readback["provenance"]["content_version_id"] == "cv_2"
    assert targeted["locator_id"] == "loc_1"
    assert targeted["content_version_id"] == "cv_2"
    assert targeted["provenance"]["content_version_id"] == "cv_2"


def test_verifier_ledger_preserves_pinned_finalizer_action_result_after_block_compaction():
    target_id = "agent_task_action_result:write_xlsx_file:act_call_target"
    evidence_items: list[dict[str, Any]] = []
    for index in range(109):
        evidence_items.append(
            {
                "id": f"workspace_artifact_readback:noise:{index}",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": f"noise-{index}.md",
                "body": f"Noise evidence {index}",
            }
        )
    evidence_items.append(
        {
            "id": target_id,
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "action_id": "write_xlsx_file",
            "action_call_id": "act_call_target",
            "path": "/tmp/run/artifacts/report.xlsx",
            "body": '{"filename":"report.xlsx","path":"/tmp/run/artifacts/report.xlsx","size":5990}',
        }
    )

    ledger = AgentTask._evidence_ledger_from_execution_meta(
        {
            "status": "completed",
            "logs": {},
            "blocks": {
                "evidence": {
                    "evidence_items": evidence_items,
                    "pinned_evidence_ids": [target_id],
                }
            },
        }
    )

    ids = [item.get("id") for item in ledger.get("items", [])]
    assert target_id in ids


@pytest.mark.asyncio
async def test_verifier_input_has_one_bounded_body_ledger_and_body_light_indexes(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("agent-task-bounded-verifier-input").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        task_id="bounded-verifier-input",
        goal="Verify a complex source-grounded report without duplicating evidence bodies.",
        success_criteria=["The report is supported by the required Action evidence."],
        execution="taskboard",
        options={
            "capability_evidence_requirements": [
                {
                    "capability_id": "deep_research",
                    "capability_kind": "action",
                    "kind": "action_succeeded",
                    "required": True,
                    "criterion_id": "criterion:research",
                }
            ]
        },
    )
    captured: dict[str, Any] = {}

    class FakeRequest:
        def input(self, value):
            captured["input"] = value
            return self

        def instruct(self, value):
            captured["instruct"] = value
            return self

        def output(self, value, *, format):
            captured["output"] = value
            captured["format"] = format
            return self

        async def async_get_data(self):
            return {
                "is_complete": False,
                "requires_block": False,
                "reason": "More evidence is required.",
                "failure_analysis": "The required Action evidence is not yet sufficient.",
                "acceptance_delta": ["Collect sufficient Action evidence."],
                "missing_criteria": ["The report is supported by the required Action evidence."],
                "replan_instruction": "Collect another bounded Action result.",
                "repair_constraints": [],
                "next_step_requirements": ["Return bounded Action evidence."],
                "final_result_required": True,
                "final_result": "",
                "material_claim_coverage_complete": True,
                "material_claim_checks": [],
            }

    async def noop_async(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(agent, "create_temp_request", lambda: FakeRequest())
    monkeypatch.setattr(
        cast(Any, task),
        "_apply_language_policy_to_request",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cast(Any, task),
        "_ensure_workspace_artifact_targeted_readback_evidence",
        noop_async,
    )
    monkeypatch.setattr(
        cast(Any, task),
        "_emit_process_progress_from_output",
        noop_async,
    )

    long_body = "bounded evidence body " * 240
    pinned_id = "agent_task_action_result:deep_research:call-pinned"
    evidence_items: list[dict[str, Any]] = [
        {
            "id": "workspace_targeted:report:risks",
            "kind": "workspace_artifact.targeted_readback",
            "status": "ok",
            "body_state": "bounded",
            "path": "reports/final.md",
            "body": f"Risk section. {long_body}",
        },
        {
            "id": "workspace_locator:report:risks",
            "kind": "workspace_artifact.acceptance_locator",
            "status": "ok",
            "body_state": "ref_only",
            "path": "reports/final.md",
            "criterion_id": "criterion:risks",
            "criterion": "Risk section",
            "locator": {"start_line": 40, "end_line": 55},
        },
        {
            "id": "source:failed",
            "kind": "agent_task.action.result",
            "status": "failed",
            "body_state": "ref_only",
            "action_id": "browse",
            "action_call_id": "call-failed",
            "source_url": "https://example.test/failed",
        },
        {
            "id": "source:empty",
            "kind": "agent_task.action.result",
            "status": "empty",
            "body_state": "ref_only",
            "action_id": "browse",
            "action_call_id": "call-empty",
            "source_url": "https://example.test/empty",
        },
        {
            "id": "source:ref-only",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "ref_only",
            "action_id": "browse",
            "action_call_id": "call-ref-only",
            "source_url": "https://example.test/ref-only",
        },
    ]
    evidence_items.extend(
        {
            "id": f"source:bounded:{index}",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "action_id": "browse",
            "action_call_id": f"call-bounded-{index}",
            "source_url": f"https://example.test/source/{index}",
            "aliases": [f"source-{index}", f"call-bounded-{index}"],
            "body": f"Source {index}. {long_body}",
        }
        for index in range(74)
    )
    evidence_items.append(
        {
            "id": pinned_id,
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "action_id": "deep_research",
            "action_call_id": "call-pinned",
            "path": "reports/research.json",
            "aliases": ["deep_research", "call-pinned"],
            "body": f"Pinned finalizer evidence. {long_body}",
        }
    )
    action_logs = [
        {
            "action_id": "browse",
            "status": "success",
            "action_call_id": f"call-log-{index}",
            "model_digest": {
                "result_preview": {
                    "selected_url": f"https://example.test/log/{index}",
                    "content": f"Action log {index}. {long_body}",
                },
                "result_preview_meta": {"truncated": False},
            },
        }
        for index in range(24)
    ]
    action_logs.append(
        {
            "action_id": "deep_research",
            "status": "success",
            "action_call_id": "call-pinned",
            "model_digest": {
                "result_preview": {"path": "reports/research.json"},
                "result_preview_meta": {"truncated": False},
            },
        }
    )
    execution_meta = {
        "status": "completed",
        "route": {"selected_route": "agent_task"},
        "diagnostics": [{"code": "diagnostic.example", "message": "bounded"}],
        "logs": {
            "action_logs": action_logs,
            "route_logs": {},
            "capability_evidence_requirements": [
                {
                    "capability_id": "deep_research",
                    "capability_kind": "action",
                    "kind": "action_succeeded",
                    "required": True,
                    "criterion_id": "criterion:research",
                }
            ],
        },
        "blocks": {
            "evidence": {
                "evidence_items": evidence_items,
                "pinned_evidence_ids": [pinned_id],
            }
        },
    }
    embedded_ledger = {
        "items": [{"id": "embedded", "body": long_body}],
        "acceptance_locator_view": {"items": [{"id": "embedded-locator"}]},
    }

    await task._request_verification(
        1,
        plan={"execution_shape": "actions", "step_instruction": "Collect evidence."},
        execution_result={
            "status": "completed",
            "evidence_ledger": embedded_ledger,
            "taskboard_evidence_view": embedded_ledger,
            "taskboard_scoped_evidence_view": embedded_ledger,
            "evidence_use": [
                {
                    "claim": "Copied from an earlier selection domain.",
                    "evidence_ids": ["ref_not_offered_in_terminal_ledger"],
                    "support_type": "content",
                }
            ],
            "nested": {"taskboard_evidence_view": embedded_ledger},
            "final_result": "reports/final.md",
        },
        execution_meta=execution_meta,
        context_pack=cast(Any, {}),
    )

    verifier_input = captured["input"]
    assert "current_evidence_ledger" not in verifier_input
    assert "acceptance_locator_view" not in verifier_input["evidence_ledger"]
    assert "evidence_summary" not in verifier_input["execution_meta"]
    assert not {
        "evidence_ledger",
        "taskboard_evidence_view",
        "taskboard_scoped_evidence_view",
        "evidence_use",
    }.intersection(verifier_input["execution_result"])
    assert "embedded-locator" not in json.dumps(
        verifier_input["execution_result"],
        ensure_ascii=False,
    )

    ledger = verifier_input["evidence_ledger"]
    assert ledger["omitted_count"] > 0
    assert all(
        "reference_id" in item
        and not {"id", "evidence_id", "cite_as", "aliases"}.intersection(item)
        for item in ledger["items"]
    )
    pinned_item = next(item for item in ledger["items"] if item.get("action_id") == "deep_research")
    assert pinned_item["reference_id"].startswith("ref_")
    non_ledger_input = dict(verifier_input)
    non_ledger_input.pop("evidence_ledger")
    assert '"reference_id"' not in json.dumps(
        non_ledger_input,
        ensure_ascii=False,
    )
    assert "carrier_context" not in verifier_input
    assert "terminal_carriers" not in verifier_input
    assert all(
        not {
            "reference_id",
            "id",
            "evidence_id",
            "cite_as",
            "criterion_id",
            "content_fingerprint",
            "source_evidence_ids",
        }.intersection(item)
        for item in verifier_input["acceptance_locator_view"]["items"]
    )
    assert all(
        set(item).intersection({"claim_key", "text", "delivery_kind", "path"})
        == set(item)
        and item["claim_key"].startswith("claim_")
        for item in verifier_input["material_claim_candidates"]
    )

    for summary_key in (
        "execution_evidence_summary",
        "cumulative_execution_evidence_summary",
    ):
        for action in verifier_input[summary_key]["actions"]:
            assert "result_preview" not in action
            assert "input_preview" not in action
    for artifact in verifier_input["trusted_workspace_artifacts"]:
        assert "evidence_id" not in artifact
        assert "content" not in artifact.get("readback", {})
        assert isinstance(artifact["readback"]["available"], bool)
    assert {requirement["capability_id"] for requirement in verifier_input["capability_evidence_requirements"]} == {
        "deep_research"
    }
    serialized_input_characters = len(json.dumps(verifier_input, ensure_ascii=False))
    assert serialized_input_characters <= 160_000
    assert task.diagnostics["verifier_prompt_projection"][-1] == {
        "serialized_input_characters": serialized_input_characters,
        "target_characters": 160_000,
        "over_target": False,
    }


def test_path_only_host_file_action_result_remains_evidence_ref():
    host_path = "/tmp/agently-host-artifacts/report.xlsx"
    ledger = AgentTask._evidence_ledger_from_execution_meta(
        {
            "status": "completed",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "write_xlsx_file",
                        "status": "success",
                        "action_call_id": "act_call_xlsx",
                        "data": {
                            "filename": "report.xlsx",
                            "path": host_path,
                            "size": 5990,
                        },
                    }
                ],
                "route_logs": {},
            },
        }
    )

    action_item = next(
        item
        for item in ledger["items"]
        if item.get("kind") == "agent_task.action.result" and item.get("action_id") == "write_xlsx_file"
    )
    assert action_item["id"] == "agent_task_action_result:write_xlsx_file:act_call_xlsx"
    assert action_item["path"] == host_path
    assert "report.xlsx" in str(action_item.get("body"))
    assert "5990" in str(action_item.get("body"))
    assert any(ref.get("value") == host_path for ref in ledger.get("source_refs", []))


@pytest.mark.asyncio
async def test_path_only_host_file_action_upgrades_only_after_workspace_readback(tmp_path):
    agent = _create_agent("agent-task-host-file-workspace-readback").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Return a generated file.",
        success_criteria=["The generated file is available as a trusted Workspace ref."],
        execution="taskboard",
    )
    await task.workspace.write_file("reports/final.txt", "generated file body")
    absolute_path = str(task.workspace.resolve_file_path("reports/final.txt"))
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_xlsx_file",
                    "status": "success",
                    "action_call_id": "act_call_workspace",
                    "data": {
                        "filename": "final.txt",
                        "path": absolute_path,
                        "size": 19,
                    },
                }
            ],
            "route_logs": {},
        },
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "status": "completed",
            "artifact_manifest": {"path": "reports/final.txt"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.host_file.workspace_readback",
    )

    assert task._workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == "reports/final.txt"
    assert delivered["workspace_artifact_delivery"]["status"] == "adopted_existing"


@pytest.mark.asyncio
async def test_path_only_host_file_action_outside_workspace_stays_external_diagnostic(tmp_path):
    agent = _create_agent("agent-task-host-file-outside-workspace").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Return a generated file.",
        success_criteria=["The generated file boundary is preserved."],
        execution="taskboard",
    )
    external_path = tmp_path / "host-artifacts" / "report.xlsx"
    external_path.parent.mkdir()
    external_path.write_bytes(b"external artifact")
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_xlsx_file",
                    "status": "success",
                    "action_call_id": "act_call_external",
                    "data": {
                        "filename": "report.xlsx",
                        "path": str(external_path),
                        "size": external_path.stat().st_size,
                    },
                }
            ],
            "route_logs": {},
        },
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "status": "completed",
            "artifact_manifest": {"path": "report.xlsx"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.host_file.external_pointer",
    )

    diagnostic_codes = {item.get("code") for item in delivered.get("diagnostics", []) if isinstance(item, Mapping)}
    assert delivered.get("file_refs") == []
    assert "agent_task.workspace_artifact.action_file_outside_workspace" in diagnostic_codes
    assert delivered["status"] == "blocked"


def test_cumulative_verifier_evidence_uses_raw_action_data_when_digest_missing(tmp_path):
    from agently.core.application import AgentTask

    agent = _create_agent("agent-task-raw-action-data-evidence").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded repository report.",
        success_criteria=["The report grounds claims in files that were read."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "read_repo_file",
                            "status": "success",
                            "action_call_id": "call-config",
                            "model_digest": {},
                            "raw": {
                                "kwargs": {"path": "configs/_base_/default.yaml", "max_chars": 8000},
                                "data": {
                                    "path": "configs/_base_/default.yaml",
                                    "content": "model:\n  rewrite_max_completion_tokens: 64000\n",
                                    "sha256": "abc123",
                                    "truncated": False,
                                },
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
        }
    )

    cumulative = task._cumulative_execution_evidence_summary(
        {"status": "completed", "logs": {"action_logs": [], "route_logs": {}}}
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(
        cumulative,
        include_body_previews=True,
    )

    action = verifier_summary["actions"][0]
    assert action["id"] == "read_repo_file"
    assert action["input_preview"]["path"] == "configs/_base_/default.yaml"
    assert action["result_preview"]["path"] == "configs/_base_/default.yaml"
    assert "rewrite_max_completion_tokens" in action["result_preview"]["content"]
    assert action["result_preview_meta"]["truncated"] is False
    assert any(
        ref["field"] == "path" and ref["value"] == "configs/_base_/default.yaml"
        for ref in verifier_summary["source_refs"]
    )
    assert any(
        ref["field"] == "path"
        and ref["value"] == "configs/_base_/default.yaml"
        and ref["content_state"] == "bounded_readback_available"
        for ref in verifier_summary["source_refs"]
    )


def test_source_refs_treat_excerpt_and_snippet_as_bounded_readback():
    action_refs = AgentTask._collect_source_refs_from_action_records(
        [
            {
                "id": "read_action_artifact",
                "status": "success",
                "action_call_id": "call-artifact",
                "result_preview": {
                    "key_files": [
                        {
                            "path": "docs/guide/configuration.md",
                            "excerpt": "Configuration excerpt read from the cloned repository artifact.",
                        },
                        {
                            "path": "docs/guide/dl-analogy.md",
                            "snippet": "Bounded snippet from the deep learning analogy guide.",
                        },
                    ]
                },
            }
        ]
    )
    action_ref_states = {ref["value"]: ref["content_state"] for ref in action_refs if ref.get("field") == "path"}
    assert action_ref_states["docs/guide/configuration.md"] == "bounded_readback_available"
    assert action_ref_states["docs/guide/dl-analogy.md"] == "bounded_readback_available"

    taskboard_refs = AgentTask._collect_taskboard_source_refs(
        {
            "cards": [
                {
                    "preview": {
                        "path": "docs/guide/skill-document.md",
                        "excerpt": "Bounded excerpt from the skill document guide.",
                    }
                }
            ]
        }
    )

    assert taskboard_refs[0]["path"] == "docs/guide/skill-document.md"
    assert taskboard_refs[0]["content_state"] == "bounded_readback_available"


def test_taskboard_final_source_refs_are_visible_to_verifier_summary(tmp_path):
    agent = _create_agent("agent-task-taskboard-final-source-refs").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded repository report.",
        success_criteria=["The report grounds claims in files that were read."],
        execution="taskboard",
    )
    evidence_view = {
        "source_refs": [
            {
                "path": "docs/guide/configuration.md",
                "content_state": "bounded_readback_available",
                "excerpt": "Configuration guide excerpt.",
            },
            {
                "path": "docs/guide/unread.md",
                "content_state": "ref_only",
            },
        ]
    }

    final_source_refs = task._taskboard_final_source_refs_from_evidence_view(evidence_view)
    summary = task._execution_log_summary(
        {
            "status": "completed",
            "logs": {
                "artifact_refs": [{"path": "final.md", "role": "workspace_artifact"}],
                "source_refs": final_source_refs,
            },
        }
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(
        summary,
        include_body_previews=True,
    )
    planner_anchors = AgentTask._planner_evidence_anchors_from_summary(summary)

    verifier_states = {ref["path"]: ref["content_state"] for ref in verifier_summary["source_refs"]}
    planner_states = {ref["value"]: ref["content_state"] for ref in planner_anchors["source_refs"]}
    assert verifier_states["docs/guide/configuration.md"] == "bounded_readback_available"
    assert verifier_states["docs/guide/unread.md"] == "ref_only"
    assert planner_states["docs/guide/configuration.md"] == "bounded_readback_available"
    assert planner_states["docs/guide/unread.md"] == "ref_only"


def test_flat_source_refs_distinguish_ref_only_repo_manifest_paths(tmp_path):
    agent = _create_agent("agent-task-ref-only-repo-manifest").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded repository report.",
        success_criteria=["The report grounds claims in files that were read."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "clone_repo",
                            "status": "success",
                            "action_call_id": "call-clone",
                            "model_digest": {
                                "result_preview": {
                                    "repo": "example/repo",
                                    "files": [
                                        {"path": "README.md"},
                                        {"path": "docs/guide.md"},
                                    ],
                                },
                                "result_preview_meta": {"truncated": False},
                            },
                        },
                        {
                            "action_id": "read_repo_file",
                            "status": "success",
                            "action_call_id": "call-read",
                            "model_digest": {
                                "result_preview": {
                                    "path": "README.md",
                                    "content": "# SkillOpt\n\nThe README content was actually read.",
                                    "sha256": "read-sha",
                                    "truncated": False,
                                },
                                "result_preview_meta": {"truncated": False},
                            },
                        },
                    ],
                    "route_logs": {},
                },
            },
        }
    )

    cumulative = task._cumulative_execution_evidence_summary(
        {"status": "completed", "logs": {"action_logs": [], "route_logs": {}}}
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(
        cumulative,
        include_body_previews=True,
    )
    planner_anchors = task._iteration_prompt_summaries()[0]["evidence_anchors"]

    ref_states = {
        (ref["action_call_id"], ref["value"]): ref["content_state"]
        for ref in verifier_summary["source_refs"]
        if ref["field"] == "path"
    }
    assert ref_states[("call-clone", "README.md")] == "ref_only"
    assert ref_states[("call-clone", "docs/guide.md")] == "ref_only"
    assert ref_states[("call-read", "README.md")] == "bounded_readback_available"
    assert any(
        ref["value"] == "README.md" and ref["content_state"] == "bounded_readback_available"
        for ref in planner_anchors["source_refs"]
    )
    assert any(
        ref["value"] == "docs/guide.md" and ref["content_state"] == "ref_only" for ref in planner_anchors["source_refs"]
    )


def test_planner_repair_context_keeps_previous_exact_evidence_anchors(tmp_path):
    agent = _create_agent("agent-task-planner-evidence-anchors").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Repair a source-grounded report without inventing source URLs.",
        success_criteria=["The report cites exact source URLs from action evidence."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "plan": {"step_instruction": "Search for source evidence.", "execution_shape": "actions"},
            "execution_meta": {
                "status": "failed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "web_search",
                            "status": "partial_success",
                            "action_call_id": "call-search",
                            "model_digest": {
                                "result_preview": [
                                    {
                                        "title": "NVDA: NVIDIA Corp - Stock Price, Quote and News - CNBC",
                                        "href": "https://www.cnbc.com/quotes/NVDA",
                                        "body": "Nvidia stock coverage and related news snippets.",
                                    },
                                    {
                                        "title": "NVDA Stock Quote Price and Forecast | CNN",
                                        "href": "https://www.cnn.com/markets/stocks/NVDA",
                                        "body": "NVIDIA BioNeMo and Halos announcement snippets.",
                                    },
                                ],
                                "result_preview_meta": {"truncated": False},
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
            "verification": {
                "is_complete": False,
                "reason": "Source URLs must be exact.",
                "missing_criteria": ["Generated report cited fabricated article URLs."],
                "replan_instruction": "Rewrite using exact URLs from evidence.",
            },
        }
    )
    task.iterations.append(
        {
            "iteration": 2,
            "plan": {"step_instruction": "Rewrite the report.", "execution_shape": "direct"},
            "execution_meta": {"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
            "verification": {
                "is_complete": False,
                "reason": "Latest rewrite still missed exact evidence refs.",
                "missing_criteria": ["Exact source URLs are still missing."],
                "replan_instruction": "Repair citations from available evidence.",
            },
        }
    )

    summaries = task._iteration_prompt_summaries()
    first_anchors = summaries[0]["evidence_anchors"]
    assert any(
        ref["field"] == "href" and ref["value"] == "https://www.cnbc.com/quotes/NVDA"
        for ref in first_anchors["source_refs"]
    )
    assert first_anchors["action_result_previews"][0]["result_preview"][1]["href"] == (
        "https://www.cnn.com/markets/stocks/NVDA"
    )

    repair_context = task._planner_repair_context(summaries)
    available = repair_context["available_evidence_anchors"]
    assert any(
        ref["field"] == "href" and ref["value"] == "https://www.cnbc.com/quotes/NVDA"
        for ref in available["source_refs"]
    )
    assert available["action_result_previews"][0]["result_preview"][0]["href"] == "https://www.cnbc.com/quotes/NVDA"


def test_planner_repair_context_carries_structured_material_claim_contract_without_prose_parsing():
    repair_contract = {
        "gate_kind": "factual_integrity",
        "issue_code": "unsupported_material_claim",
        "contract_subject": "carrier:cv_1",
        "requirements": [
            {
                "claim_key": "claim:1",
                "carrier_id": "cv_1",
                "artifact_quote": "Unsupported claim.",
                "state": "unsupported",
            }
        ],
    }
    context = AgentTask._planner_repair_context(
        [
            {
                "iteration": 1,
                "verification_ref": "verification:1",
                "verification": {
                    "is_complete": False,
                    "reason": "Free-form reason must not own the repair contract.",
                    "material_claim_repair_contract": repair_contract,
                },
            }
        ]
    )

    assert context["material_claim_repair_contract"] == repair_contract
    assert context["material_claim_repair_contract"]["requirements"][0]["claim_key"] == "claim:1"


@pytest.mark.asyncio
async def test_action_succeeded_evidence_satisfied_in_earlier_iteration(tmp_path):
    """action_succeeded evidence accumulates across iterations: the action
    succeeds in iteration 1, and a later iteration must not false-fail the
    requirement just because the action did not re-run."""
    agent = _capability_gate_agent("agent-task-action-succeeded-cumulative")

    async def step(iteration_index, plan, context_pack):
        if iteration_index == 1:
            # The required action succeeds here; iteration 2 omits it but the
            # capability_used requirement for "later-skill" is still unmet.
            logs = {
                "action_logs": {"build_action": {"name": "build_action", "status": "success"}},
                "route_logs": {},
            }
            route = "model_request"
        else:
            logs = {"action_logs": {}, "route_logs": {"plan": {"selected_skills": [{"skill_id": "later-skill"}]}}}
            route = "skills"
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": route},
                "logs": logs,
            },
        )

    task = agent.create_task(
        task_id="action-succeeded-cumulative",
        goal="Use an action and a skill across steps.",
        success_criteria=["The action ran and the skill was used."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        options={
            "capability_evidence_requirements": [
                {"capability_id": "build_action", "kind": "action_succeeded"},
                {"capability_id": "later-skill", "capability_kind": "skill", "kind": "capability_used"},
            ]
        },
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert len(meta["iterations"]) == 2
    # Iteration 2 must not report build_action as missing — it succeeded in iter 1.
    second = meta["iterations"][1]["verification"]
    assert "build_action" not in " ".join(second.get("missing_capability_evidence", []))


@pytest.mark.asyncio
async def test_unenforced_evidence_kind_is_recorded_not_silently_blocking(tmp_path):
    """A reserved/not-yet-wired evidence kind (e.g. artifact_readback) does not
    block acceptance but is surfaced as an unenforced requirement rather than
    silently dropped."""
    agent = _capability_gate_agent("agent-task-unenforced-kind")

    async def step(iteration_index, plan, context_pack):
        return (
            {"step_result": "done", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {"action_logs": {}, "route_logs": {}},
            },
        )

    task = agent.create_task(
        task_id="unenforced-kind",
        goal="Produce an artifact.",
        success_criteria=["An artifact exists."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"capability_evidence_requirements": [{"capability_id": "the-artifact", "kind": "artifact_readback"}]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step}

    result = await task.async_run()
    meta = await task.async_meta()

    # Unenforced kind must not block; it is recorded for visibility.
    assert result["status"] == "completed"
    verification = meta["iterations"][0]["verification"]
    assert verification.get("missing_capability_evidence", []) == []
    unenforced = verification.get("unenforced_evidence_requirements", [])
    assert any(item.get("capability_id") == "the-artifact" for item in unenforced)


def test_agent_task_module_does_not_import_orchestrator_internals():
    """BUG_FIX 4.1 layering: AgentTask consumes the inert options snapshot and
    must not import AgentOrchestrator / HybridRoutePlanner internals."""
    import ast
    import inspect

    from agently.core.application import AgentTask as AgentTaskClass

    source_file = inspect.getsourcefile(AgentTaskClass)
    assert source_file is not None
    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported)
    assert "AgentOrchestrator" not in joined
    assert "HybridRoutePlanner" not in joined


def test_example_design_system_fingerprint_smoke(tmp_path):
    """BUG_FIX 4.4: design-system fingerprint smoke fails the 2026-06-12 light
    bypass artifact and passes a skill-template-derived artifact. Fingerprints
    are read from the installed skill, not hand-written into core."""
    from examples.agent_task.agently_architecture_diagram_cocoon_skill_task import (
        _design_system_fingerprint_hits,
        _design_system_fingerprints,
    )

    skill_dir = tmp_path / "architecture-diagram"
    (skill_dir / "resources").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        'Use #020617 background, JetBrains Mono font, <pattern id="grid"> and stroke-dasharray.',
        encoding="utf-8",
    )
    (skill_dir / "resources" / "template.html").write_text("<svg></svg>", encoding="utf-8")

    fingerprints = _design_system_fingerprints(skill_dir)
    assert "#020617" in fingerprints
    assert "JetBrains Mono" in fingerprints

    light_bypass_artifact = (
        "<html><body style=\"background:#f5f5f5;font-family:'Segoe UI'\">" "<svg></svg></body></html>"
    )
    assert _design_system_fingerprint_hits(light_bypass_artifact, fingerprints) == []

    template_derived_artifact = (
        "<html><style>body{background:#020617;font-family:'JetBrains Mono'}</style>"
        '<svg><pattern id="grid"></pattern><rect stroke-dasharray="4,4"/></svg></html>'
    )
    hits = _design_system_fingerprint_hits(template_derived_artifact, fingerprints)
    assert "#020617" in hits
    assert "JetBrains Mono" in hits
    assert len(hits) >= (len(fingerprints) + 1) // 2
