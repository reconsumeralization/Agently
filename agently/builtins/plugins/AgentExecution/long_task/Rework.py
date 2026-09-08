"""Producer-owned selective re-entry from retained work, never terminal resume."""
from __future__ import annotations

import sys
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agently.core.orchestration.TaskBoard import (
    TaskBoardValidator,
    build_task_board_evidence_view,
)
from agently.types.data import TaskBoardRevision
from agently.utils import DataFormatter

from ..modules.model_stage import run_model_stage
from ..modules.revisions import content_digest

if TYPE_CHECKING:
    from ..modules.execution import AgentExecution


def retain_task_production(owner: AgentExecution) -> None:
    task = owner.task_record
    frames = list(task._lifecycle_frames.values())
    frame = max(frames, key=lambda item: int(item.get("iteration") or 0), default={})
    terminal_board = task._terminal_taskboard_state or {}
    board = terminal_board.get("revision") or frame.get("taskboard_revision")
    if board is None and task._resumed_taskboard_state is not None:
        board = task._resumed_taskboard_state.get("revision")
    content = {
        "strategy": task.effective_execution_strategy,
        "board": deepcopy(board) if isinstance(board, Mapping) else None,
        "tick_index": int(terminal_board.get("tick_index") or max(
            (int(item.get("taskboard_tick_index") or 0) for item in frames), default=0)),
        "iteration": int(task._lifecycle_state.iteration),
        "iterations": DataFormatter.sanitize(task.iterations),
    }
    owner._producer_state = {"kind": "long_task", "content": content, "digest": content_digest(content)}


async def _verify_reused_files(task: Any, results: list[Mapping[str, Any]]) -> None:
    for result in results:
        for ref in [*result.get("file_refs", []), *result.get("artifact_refs", [])]:
            if not isinstance(ref, Mapping):
                raise ValueError("Retained work contains an invalid resource reference.")
            path, digest = ref.get("path"), ref.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                raise RuntimeError("Rework reuse requires rebound content-addressed file evidence.")
            read = await task.task_workspace.read_file(path, max_bytes=sys.maxsize)
            if read.sha256 != digest:
                raise ValueError(f"Retained work resource changed: {path}.")


async def prepare_task_rework(owner: AgentExecution) -> None:
    task = owner.task_record
    retained = owner._producer_state
    if retained.get("digest") != content_digest(retained.get("content")):
        raise ValueError("Retained task work identity changed before rework.")
    content = retained["content"]
    board: TaskBoardRevision | None = None
    if content["strategy"] == "taskboard":
        board = TaskBoardRevision.from_value(content["board"])
        offered = [card.id for card in board.graph.cards]
        work = [{"id": card.id, "objective": card.objective, "depends_on": list(card.depends_on),
                 "required_outputs": list(card.required_outputs), "status": card.status}
                for card in board.graph.cards]
        evidence = build_task_board_evidence_view(board).to_dict()
    else:
        iterations = content["iterations"]
        offered = [f"work_{index}" for index in range(len(iterations))]
        summaries = task._iteration_prompt_summaries()
        by_iteration = {item.get("iteration"): item for item in summaries}
        work = [{"id": key, "iteration": record.get("iteration"),
                 "summary": by_iteration.get(record.get("iteration"), {"coverage": "not_in_recent_window",
                     "observation_ref": record.get("observation_ref")})}
                for key, record in zip(offered, iterations)]
        evidence = {"source": "retained_iteration_summaries", "coverage": "bounded"}
    candidate_key = "candidate"
    while candidate_key in offered:
        candidate_key = "_" + candidate_key
    offered.append(candidate_key)
    work.append({"id": candidate_key, "kind": "candidate_delivery",
                 "objective": "Revise final delivery while preserving completed work."})
    prior_result = task.result if isinstance(task.result, Mapping) else {}
    previous_candidate = prior_result.get("final_result") or prior_result.get("final_response")
    decision = await run_model_stage(
        owner, producer="long_task", stage="rework_scope",
        stage_input={"work": work, "evidence": evidence, "previous_candidate": previous_candidate,
                     "feedback": owner._rework_feedback},
        stage_info={"offered_work_ids": offered, "dependency_policy": "invalidate selected work and all dependants"},
        stage_instructions=[
            "Select the existing work that must be revised to address the feedback under the original task contract.",
            "Return at least one exact offered work id. Preserve reusable evidence and unaffected completed work.",
            "Choose candidate_delivery when only final presentation needs repair; preserve completed work and do not repeat successful external effects.",
            "Bounded previews are orientation only, not complete source evidence. Subsequent work uses retained evidence and scoped readback.",
        ],
        output={"invalidated_work_ids": [(str, "Exact offered work id requiring new production; at least one.")]},
    )
    value = decision.value
    selected = value.get("invalidated_work_ids") if isinstance(value, Mapping) else None
    if (not isinstance(selected, list) or not selected or any(not isinstance(item, str) for item in selected)
        or not set(selected) <= set(offered)):
        raise ValueError("Task rework must select non-empty known work identities.")
    affected = set(selected) - {candidate_key}
    if board is not None:
        while True:
            expanded = affected | {card.id for card in board.graph.cards if set(card.depends_on) & affected}
            if expanded == affected:
                break
            affected = expanded
        await _verify_reused_files(task, [result.to_dict() for key, result in board.card_results.items()
                                         if key not in affected and result.status == "completed"])
        patch = {"base_revision": board.revision_id, "source": "execution_rework",
                 "operations": [{"op": "record_card_result", "result": {"card_id": key, "status": "pending"}}
                                for key in sorted(affected)] + [{"op": "set_board_status", "status": "pending"}]}
        revised = TaskBoardValidator().apply_patch(board, patch)
        task._resumed_taskboard_state = {"revision": revised.to_dict(), "stage": "execution_rework",
                                        "tick_index": content["tick_index"]}
    elif affected:
        first = min(offered.index(key) for key in affected)
        affected = set(offered[first:-1])
        # Flat is a serial work chain. Keep the original evidence/records cold,
        # and expose only the still-valid prefix as active iteration results.
        task.iterations = deepcopy(task.iterations[:first])
    if candidate_key in selected:
        affected.add(candidate_key)
    task._resumed_from_iteration = content["iteration"]
    task._resumed_prior_result = None
    task._latest_taskboard_acceptance_index = {}
    task._terminal_taskboard_state = None
    task._terminal_inline_values = {}
    task._terminal_deliverable_refs = []
    task._terminal_retained_refs = []
    task._completed = False
    task._error = None
    task.status = "created"
    task.result = None
    task.completed_at = None
    task._stream_items = []
    task._stream_queues = []
    owner._terminal_task_handoff_refs = []
    owner._review_contract["rework_feedback"] = owner._rework_feedback
    task.task_context.put(
        role="instruction",
        content={"revision_feedback": owner._rework_feedback, "invalidated_work_ids": sorted(affected),
                 "contract": "Produce revised work for the feedback. Invalidated outputs are historical candidates, not accepted results. "
                             "Reuse unchanged source evidence after content identity checks. Do not replay prior external effects without authorization."},
        entry_id=f"execution_rework:{owner.id}:{owner.revision}", required=True,
        source_ref=f"execution_rework:{owner.id}:{owner.revision}",
    )
    owner.diagnostics["rework"] = {"source_revision": owner.revision - 1, "revision": owner.revision,
                                    "selected_work_ids": selected, "invalidated_work_ids": sorted(affected),
                                    "decision_request_id": decision.request_id}


def export_task_state(owner: AgentExecution) -> dict[str, Any] | None:
    task = owner.task_record
    if task is None:
        return None
    if not task._completed or any(not item.done() for item in task._background_stream_tasks):
        raise RuntimeError("Execution save requires all producer-owned child work to settle.")
    context = task._context_resume_state()
    if any(item.get("kind") == "unsupported" for item in context["sources"]):
        raise RuntimeError("Task snapshot contains a ContextSource without a durable rebinding contract.")
    content = owner._producer_state.get("content", {})
    return {
        "task_id": task.id, "status": task.status, "manifest": task._resume_manifest(), "context_state": context,
        "iteration": content.get("iteration", 0),
        "taskboard_state": ({"revision": content["board"], "tick_index": content["tick_index"]}
                            if content.get("board") is not None else None),
        "iterations": deepcopy(task.iterations), "iterations_summary": task._resumed_iteration_summaries,
        "reflection_summaries": deepcopy(task.reflections),
        "task_reference_catalog": task._task_reference_catalog.snapshot(),
        "terminal_convergence": task._terminal_convergence_state.snapshot(),
        "lifecycle_state": task._lifecycle_state.to_dict(),
        "lifecycle_frames": deepcopy(task._lifecycle_frames),
        **{key: sorted(getattr(task, "_" + key)) for key in (
            "satisfied_required_actions", "satisfied_required_skills", "satisfied_capabilities",
            "satisfied_succeeded_actions", "failed_execution_shapes")},
    }


def restore_task_state(owner: AgentExecution, state: dict[str, Any] | None) -> None:
    if state is None:
        return
    from . import AgentTask
    task = AgentTask._from_resume_state(owner.agent, state["task_id"], state,
                                       task_workspace=owner.task_workspace, record_store=owner.record_store)
    task.iterations = deepcopy(state["iterations"])
    task._lifecycle_frames = deepcopy(state["lifecycle_frames"])
    task._completed = True
    task.result = deepcopy(owner.result)
    task.status = state["status"]
    owner.task_record = task
