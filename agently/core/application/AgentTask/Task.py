# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Generator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, cast, Literal, TYPE_CHECKING

from agently.core.orchestration import (
    TaskBoard,
    TriggerFlow,
    build_task_board_evidence_view,
    coerce_task_board_planning_result,
    resolve_task_board_planning_policy,
    task_board_planning_output_schema,
)
from agently.core.orchestration.TaskBoard.TaskBoardValidation import task_board_card_required
from agently.types.data import AgentExecutionStreamData, ReplanSignal, TaskBoardCardResult
from agently.utils import DataFormatter, FunctionShifter

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import WorkspaceContextPackage, WorkspaceRecordRef


AgentTaskStatus = Literal[
    "created",
    "running",
    "completed",
    "blocked",
    "max_iterations",
    "timed_out",
    "capability_unavailable",
    "error",
]
AgentTaskExecutionStrategy = Literal["auto", "flat", "taskboard"]

_AGENT_TASK_EXECUTION_STRATEGY_ALIASES = {
    "": "auto",
    "default": "auto",
    "automatic": "auto",
    "linear": "flat",
    "react": "flat",
    "flat_react": "flat",
    "task_board": "taskboard",
    "board": "taskboard",
    "taskboard_evidenceview": "taskboard",
}

_STEP_EXECUTION_SHAPES = {
    "direct",
    "actions",
    "skills",
    "dynamic_task",
    "execution_dag",
}

_DAG_STEP_EXECUTION_SHAPES = {"dynamic_task", "execution_dag"}

# Upper bound on the in-memory stream replay buffer for late subscribers.
_STREAM_REPLAY_LIMIT = 5000
_VERIFIER_PROMPT_VALUE_CHARS = 12000
_VERIFIER_PROMPT_ITEM_CHARS = 2400


class _AgentTaskDeadlineExceeded(TimeoutError):
    def __init__(
        self,
        stage: str,
        *,
        reason: str | None = None,
        limit_name: str = "max_seconds",
        timeout_seconds: float | None = None,
    ):
        super().__init__(reason or f"AgentTask exceeded {limit_name} while running stage '{stage}'.")
        self.stage = stage
        self.reason = reason
        self.limit_name = limit_name
        self.timeout_seconds = timeout_seconds


class AgentTask:
    """Retained owner for one Agent-managed business task lifecycle."""

    @staticmethod
    def normalize_execution_strategy(value: Any = "auto") -> AgentTaskExecutionStrategy:
        text = str(value if value is not None else "auto").strip().lower().replace("-", "_")
        normalized = _AGENT_TASK_EXECUTION_STRATEGY_ALIASES.get(text, text)
        if normalized not in {"auto", "flat", "taskboard"}:
            raise ValueError("AgentTask execution must be one of: 'auto', 'flat', or 'taskboard'.")
        return cast(AgentTaskExecutionStrategy, normalized)

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        goal: str,
        success_criteria: list[str],
        execution: AgentTaskExecutionStrategy | str | None = "auto",
        workspace: str | os.PathLike[str] | None = None,
        max_iterations: int = 3,
        verify: Literal["before_done"] = "before_done",
        context_profile: str = "auto",
        context_budget: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        task_id: str | None = None,
    ):
        if not str(goal or "").strip():
            raise ValueError("agent.create_task(...) requires a non-empty goal.")
        if not success_criteria:
            raise ValueError("agent.create_task(...) requires at least one success criterion.")
        self.agent = agent
        self.id = task_id or f"agent_task_{uuid.uuid4().hex}"
        self.goal = str(goal)
        self.success_criteria = [str(item) for item in success_criteria if str(item).strip()]
        self.execution_strategy = self.normalize_execution_strategy(execution)
        self.max_iterations = max(1, int(max_iterations))
        self.verify = verify
        self.context_profile = context_profile
        self.context_budget = dict(context_budget or {"chars": 6000})
        self.limits = dict(limits or {"max_model_requests": 3})
        self.options = dict(options or {})
        agent_with_workspace = cast(Any, agent)
        if workspace is not None:
            agent_with_workspace.use_workspace(workspace)
        if getattr(agent, "workspace", None) is None:
            raise RuntimeError(
                "AgentTask requires a Workspace binding. Standard Agents include a lazy Workspace; "
                "pass workspace=... or call agent.use_workspace(...) only when you need an explicit "
                "root, mode, or provider."
            )
        bound_workspace = agent_with_workspace.workspace
        # Bind the task file root as a lineage child of the Agent scope so the
        # task subtree (and any nested executions) lives under the Agent node and
        # can be pruned as one contained subtree (spec section 8.2).
        with_scope_node = getattr(bound_workspace, "with_scope_node", None)
        if callable(with_scope_node):
            self.workspace: Any = with_scope_node(
                "tasks",
                self.id,
                scope={"task_id": self.id},
                search_scope={"task_id": self.id},
            )
        else:
            self.workspace = bound_workspace
        self.status: AgentTaskStatus = "created"
        self.result: Any = None
        self.diagnostics: dict[str, Any] = {}
        self.iterations: list[dict[str, Any]] = []
        self.workspace_refs: dict[str, list[str]] = {
            "observations": [],
            "decisions": [],
            "verification": [],
            "checkpoints": [],
            "evidence_links": [],
        }
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self._completed = False
        self._error: BaseException | None = None
        self._start_lock = asyncio.Lock()
        # Required capabilities are satisfied cumulatively across iterations: a
        # capability used by any bounded step counts as satisfied for the task,
        # so a model_request step and a skills step in different iterations can
        # together satisfy required actions and required skills.
        self._satisfied_required_actions: set[str] = set()
        self._satisfied_required_skills: set[str] = set()
        # Capability ids used in any bounded step, accumulated across iterations so
        # a capability-evidence requirement can be satisfied cumulatively.
        self._satisfied_capabilities: set[str] = set()
        # Action ids that succeeded in any bounded step, accumulated the same way
        # so an action_succeeded evidence requirement is not lost on a later step.
        self._satisfied_succeeded_actions: set[str] = set()
        # Execution shapes that failed earlier in this task. In auto mode the
        # planner should adapt instead of repeatedly selecting the same failing
        # route shape.
        self._failed_execution_shapes: set[str] = set()
        # Durable resume state (populated by AgentTask.async_resume before run).
        self._resumed_from_iteration: int = 0
        self._resumed_iteration_summaries: list[dict[str, Any]] = []
        self._resumed_prior_result: Any = None
        self._stream_items: list[AgentExecutionStreamData] = []
        self._stream_queues: list[asyncio.Queue[Any]] = []
        self._background_stream_tasks: set[asyncio.Task[Any]] = set()
        self._flow = self._build_flow()

        self.run: Any = self._run
        self.meta: Any = self._meta
        self.get_meta: Any = self.meta
        self.stream: Any = self.get_async_generator
        self.get_generator: Any = self._get_generator

    def _build_flow(self):
        flow = TriggerFlow(name="agent-task-loop")

        async def loop(data):
            await data.async_set_state("task_id", self.id, emit=False)
            await self._emit("agent_task.started", self._task_summary())
            start_iteration = self._resumed_from_iteration + 1
            if start_iteration > 1:
                await self._emit(
                    "agent_task.resumed",
                    {"task_id": self.id, "resumed_from_iteration": self._resumed_from_iteration},
                )
            if self.execution_strategy == "taskboard":
                result = await self._run_taskboard()
                await data.async_set_state("agent_task.latest_iteration", result, emit=False)
                await data.async_set_state("agent_task.result", self.result, emit=False)
                await data.async_set_state("agent_task.status", self.status, emit=False)
                return
            for iteration_index in range(start_iteration, self.max_iterations + 1):
                result = await self._run_iteration(iteration_index)
                await data.async_set_state("agent_task.latest_iteration", result, emit=False)
                if result["terminal"]:
                    break
            await data.async_set_state("agent_task.result", self.result, emit=False)
            await data.async_set_state("agent_task.status", self.status, emit=False)

        flow.to(loop, name="agent_task_loop")
        return flow

    async def async_run(self) -> Any:
        async with self._start_lock:
            if self._completed:
                if self._error is not None:
                    raise self._error
                return self.result
            self.started_at = time.time()
            if self._resumed_prior_result is not None:
                # The resumed snapshot was already terminal; expose its result
                # without re-running any iteration.
                self.result = self._resumed_prior_result
                self.status = cast(Any, self._resumed_prior_result.get("status", "completed"))
                self._completed = True
                self.completed_at = time.time()
                await self._emit("agent_task.resumed", {"task_id": self.id, "terminal": True})
                await self._emit("result", self.result)
                await self._close_streams()
                return self.result
            self.status = "running"
            execution = self._flow.create_execution(auto_close=False)
            try:
                await self._record_phase(
                    "configured",
                    diagnostics={
                        "goals": [self.goal],
                        "success_criteria": self.success_criteria,
                        "execution_strategy": self.execution_strategy,
                        "max_iterations": self.max_iterations,
                        "required_capabilities": self.options.get("capability_constraints", {}),
                    },
                )
                await execution.async_start({"task_id": self.id})
                if self.status == "running":
                    self.status = "max_iterations"
                    self.diagnostics.setdefault("terminal_reason", "max_iterations")
                    self.result = {
                        "status": self.status,
                        "accepted": False,
                        "artifact_status": "partial",
                        "reason": "Task exhausted max_iterations before verification completed.",
                        "iterations": len(self.iterations),
                    }
                    await self._emit("agent_task.blocked", self.result)
                await self._emit("result", self.result)
                return self.result
            except BaseException as error:
                self.status = "timed_out" if self._is_timeout_error(error) else "error"
                self._error = error
                self.diagnostics.setdefault("errors", []).append(
                    {"type": error.__class__.__name__, "message": str(error), "status": self.status}
                )
                await self._emit("agent_task.error", self.diagnostics["errors"][-1])
                raise
            finally:
                # Always close the auto_close=False execution so its runtime is
                # not leaked when the loop raises (e.g. a timed-out request).
                try:
                    await execution.async_close()
                except Exception:
                    pass
                self.completed_at = time.time()
                self._completed = True
                await self._close_streams()

    def _run(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.async_run())
        return self.async_run()

    @staticmethod
    def _is_timeout_error(error: BaseException) -> bool:
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return True
        return getattr(error, "status", None) == "timed_out"

    async def _terminate_timed_out(
        self,
        iteration_index: int,
        *,
        stage: str | None = None,
        reason: str | None = None,
        limit_name: str = "max_seconds",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.status = "timed_out"
        stage_text = f" during the { stage } stage" if stage else ""
        timeout_value = timeout_seconds if timeout_seconds is not None else self._task_max_seconds()
        reason = reason or (
            "Task exceeded its wall-clock budget "
            f"({limit_name}={ timeout_value }){ stage_text } before completion."
        )
        self.result = {
            "status": "timed_out",
            "accepted": False,
            "artifact_status": "partial",
            "task_id": self.id,
            "reason": reason,
            "iterations": len(self.iterations),
        }
        self.diagnostics.setdefault("terminal_reason", "timed_out")
        await self._emit_progress(iteration_index, "timed_out", f"Iteration {iteration_index}: { reason }")
        await self._record_phase(
            "terminal",
            iteration=iteration_index,
            diagnostics={
                "status": self.status,
                "accepted": False,
                "artifact_status": "partial",
                "stage": stage,
                "limit_name": limit_name,
                "timeout_seconds": timeout_value,
                "reason": reason,
            },
        )
        await self._emit("agent_task.blocked", self.result)
        return {"terminal": True, "status": self.status}

    def _failed_execution_result(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        error: Exception,
        execution_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        message = str(error) or error.__class__.__name__
        error_info = {
            "type": error.__class__.__name__,
            "message": message,
            "stage": "execute",
            "iteration": iteration_index,
        }
        self.diagnostics.setdefault("execution_errors", []).append(error_info)
        selected_route = str(
            plan.get("effective_execution_shape")
            or plan.get("execution_shape")
            or "unknown"
        )
        return (
            {
                "step_result": "",
                "evidence": [f"Execution failed: {error.__class__.__name__}: {message}"],
                "remaining_work": [f"Retry or replan after execution failure: {message}"],
                "error": error_info,
            },
            {
                "execution_id": execution_id or f"{self.id}:iter-{iteration_index}:failed-step",
                "status": "failed",
                "route": {
                    "selected_route": selected_route,
                    "status": "failed",
                },
                "logs": {
                    "action_logs": {},
                    "route_logs": {},
                    "errors": [error_info],
                },
                "diagnostics": {
                    "execution_error": error_info,
                },
            },
        )

    async def _run_iteration(self, iteration_index: int) -> dict[str, Any]:
        try:
            if self._task_deadline_exceeded():
                return await self._terminate_timed_out(iteration_index, stage="plan")
            await self._emit_progress(
                iteration_index,
                "context",
                f"Iteration {iteration_index}: building a Workspace context pack for the task goal.",
            )
            await self._emit(f"agent_task.iteration.{iteration_index}.started", {"iteration": iteration_index})
            context_pack = await self._await_task_deadline(
                self._build_context(),
                stage="context",
            )
            await self._emit(f"agent_task.iteration.{iteration_index}.context", context_pack)
            await self._emit_snapshot(
                iteration_index,
                "context",
                {
                    "context_item_count": len(context_pack.get("items", [])),
                    "diagnostics": context_pack.get("diagnostics", {}),
                },
                message=(
                    f"Iteration {iteration_index}: context pack ready with "
                    f"{len(context_pack.get('items', []))} item(s)."
                ),
            )

            await self._emit_progress(
                iteration_index,
                "plan",
                f"Iteration {iteration_index}: asking the model to plan one bounded execution step.",
            )
            plan = self._normalize_step_plan(
                await self._await_task_deadline(
                    self._request_plan(iteration_index, context_pack),
                    stage="plan",
                )
            )
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
        await self._record_phase(
            "planned",
            iteration=iteration_index,
            diagnostics={
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get("effective_execution_shape", plan.get("execution_shape", "direct")),
                "step_instruction": plan.get("step_instruction", ""),
                "expected_evidence": plan.get("expected_evidence", ""),
                "rationale": plan.get("rationale", ""),
            },
        )
        await self._emit(f"agent_task.iteration.{iteration_index}.plan", plan)
        await self._emit_snapshot(
            iteration_index,
            "plan",
            {
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get("effective_execution_shape", plan.get("execution_shape", "direct")),
                "step_instruction": plan.get("step_instruction", ""),
                "expected_evidence": plan.get("expected_evidence", ""),
                "rationale": plan.get("rationale", ""),
            },
            message=f"Iteration {iteration_index}: plan ready; next bounded step is selected.",
        )
        decision_ref = await self._record_decision(iteration_index, plan, context_pack)
        await self._emit(f"agent_task.iteration.{iteration_index}.decision", {"record": decision_ref})

        await self._emit_progress(
            iteration_index,
            "execute",
            f"Iteration {iteration_index}: executing the bounded step and collecting evidence.",
        )
        await self._record_phase(
            "executing",
            iteration=iteration_index,
            diagnostics={
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get("effective_execution_shape", plan.get("execution_shape", "direct")),
                "step_instruction": plan.get("step_instruction", ""),
            },
        )
        try:
            execution_result, execution_meta = await self._await_task_deadline(
                self._execute_step(iteration_index, plan, context_pack),
                stage="execute",
            )
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
        execution_failed = str(execution_meta.get("status") or "").strip().lower() in {
            "failed",
            "error",
            "timed_out",
            "blocked",
        }
        if execution_failed:
            self._record_failed_execution_shape(plan, execution_meta)
        await self._emit_snapshot(
            iteration_index,
            "execution",
            {
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_id": execution_meta.get("execution_id"),
                "route": execution_meta.get("route"),
                "logs": self._execution_log_summary(execution_meta),
            },
            message=(
                f"Iteration {iteration_index}: bounded step failed; failure evidence was captured."
                if execution_failed
                else f"Iteration {iteration_index}: bounded step finished; execution evidence was captured."
            ),
        )
        observation_ref, checkpoint_ref = await self._record_observation(
            iteration_index,
            plan=plan,
            decision_ref=decision_ref,
            execution_result=execution_result,
            execution_meta=execution_meta,
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.observation",
            {"record": observation_ref, "checkpoint": checkpoint_ref},
        )
        await self._record_phase(
            "evidence_recorded",
            iteration=iteration_index,
            diagnostics={
                "observation_ref": observation_ref,
                "checkpoint_ref": checkpoint_ref,
                "execution_id": execution_meta.get("execution_id"),
                "route": execution_meta.get("route"),
            },
        )

        await self._emit_progress(
            iteration_index,
            "verify",
            f"Iteration {iteration_index}: verifying the evidence against every success criterion.",
        )
        try:
            verification = await self._await_task_deadline(
                self._request_verification(
                    iteration_index,
                    plan=plan,
                    execution_result=execution_result,
                    execution_meta=execution_meta,
                    context_pack=context_pack,
                ),
                stage="verify",
            )
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
        await self._record_phase(
            "verified",
            iteration=iteration_index,
            diagnostics={
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
                "missing_criteria": verification.get("missing_criteria", []),
                "final_result_present": bool(str(verification.get("final_result") or "").strip()),
            },
        )
        await self._record_phase(
            "guarded",
            iteration=iteration_index,
            diagnostics={
                "guard_reasons": verification.get("guard_reasons", []),
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
            },
        )
        verification_ref = await self._record_verification(iteration_index, verification, observation_ref)
        await self._emit_snapshot(
            iteration_index,
            "verification",
            {
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
                "reason": verification.get("reason"),
                "missing_criteria": verification.get("missing_criteria", []),
                "replan_instruction": verification.get("replan_instruction", ""),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
            },
            message=(
                f"Iteration {iteration_index}: verification "
                f"{'passed' if verification.get('is_complete') else 'requires another step'}."
            ),
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.verification",
            {"verification": verification, "record": verification_ref},
        )

        iteration_record = {
            "iteration": iteration_index,
            "plan": plan,
            "decision_ref": decision_ref,
            "execution_meta": execution_meta,
            "observation_ref": observation_ref,
            "verification": verification,
            "verification_ref": verification_ref,
            "context_item_count": len(context_pack.get("items", [])),
        }
        self.iterations.append(DataFormatter.sanitize(iteration_record))
        # The cumulative satisfied-capability sets are updated inside
        # _normalize_verification; persist a resumable snapshot for this
        # iteration so a crashed task can continue from the next iteration.
        await self._write_resume_snapshot(iteration_index, verification)

        if bool(verification.get("is_complete")):
            self.status = "completed"
            self.result = {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "task_id": self.id,
                "final_result": verification.get("final_result") or execution_result,
                "iterations": iteration_index,
                "verification": verification,
            }
            await self._emit_progress(
                iteration_index,
                "completed",
                f"Iteration {iteration_index}: all success criteria are satisfied; the task is complete.",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": True, "artifact_status": "accepted"},
            )
            await self._emit("agent_task.completed", self.result)
            return {"terminal": True, "status": self.status}

        if verification.get("requires_block"):
            self.status = "blocked"
            self.result = {
                "status": "blocked",
                "accepted": False,
                "artifact_status": "blocked",
                "task_id": self.id,
                "reason": verification.get("reason") or "Verifier blocked the task.",
                "iterations": iteration_index,
                "verification": verification,
            }
            await self._emit_progress(
                iteration_index,
                "blocked",
                f"Iteration {iteration_index}: verifier blocked the task because it cannot continue safely.",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": False, "artifact_status": "blocked"},
            )
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        if iteration_index >= self.max_iterations:
            missing_capabilities = self._normalize_string_list(
                verification.get("missing_required_capabilities")
            )
            if missing_capabilities:
                self.status = "capability_unavailable"
                reason = (
                    "Task could not satisfy required capabilities before max_iterations: "
                    f"{', '.join(missing_capabilities)}."
                )
            else:
                self.status = "max_iterations"
                reason = verification.get("reason") or "Task did not pass verification before max_iterations."
            self.result = {
                "status": self.status,
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "reason": reason,
                "iterations": iteration_index,
                "verification": verification,
            }
            if missing_capabilities:
                self.result["missing_required_capabilities"] = missing_capabilities
            await self._emit_progress(
                iteration_index,
                self.status,
                f"Iteration {iteration_index}: { reason }",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": False, "artifact_status": "partial"},
            )
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        await self._emit_progress(
            iteration_index,
            "replan",
            f"Iteration {iteration_index}: verifier found gaps; the next iteration will replan.",
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.replan",
            {
                "reason": verification.get("reason"),
                "replan_instruction": verification.get("replan_instruction"),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
                "replan_signals": verification.get("replan_signals", []),
            },
        )
        await self._record_phase(
            "replanned",
            iteration=iteration_index,
            diagnostics={
                "reason": verification.get("reason"),
                "replan_instruction": verification.get("replan_instruction"),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
                "replan_signals": verification.get("replan_signals", []),
            },
        )
        return {"terminal": False, "status": "continue"}

    async def _run_taskboard(self) -> dict[str, Any]:
        iteration_index = 1
        try:
            if self._task_deadline_exceeded():
                return await self._terminate_timed_out(iteration_index)
            await self._emit_progress(
                iteration_index,
                "context",
                "TaskBoard: building a Workspace context pack for board planning.",
            )
            context_pack = await self._await_task_deadline(
                self._build_context(),
                stage="context",
            )
            await self._emit("agent_task.taskboard.context", context_pack)

            await self._emit_progress(
                iteration_index,
                "taskboard_plan",
                "TaskBoard: asking the model to plan the initial board.",
            )
            planning_result = await self._await_task_deadline(
                self._request_taskboard_plan(context_pack),
                stage="taskboard_plan",
            )
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )

        board = TaskBoard(
            planning_result.revision,
            handler=lambda context: self._run_taskboard_card(context, context_pack),
            planning_policy=planning_result.planning_policy,
        )
        await self._record_phase(
            "taskboard_planned",
            iteration=iteration_index,
            diagnostics={
                "board_id": board.revision.board_id,
                "revision_id": board.revision.revision_id,
                "card_count": len(board.revision.graph.cards),
                "execution_strategy": self.execution_strategy,
            },
        )
        await self._emit(
            "agent_task.taskboard.plan",
            {
                "revision": board.revision.to_dict(),
                "planning_policy": planning_result.planning_policy.to_prompt_payload(),
            },
        )

        for tick_index in range(1, self._taskboard_max_ticks() + 1):
            if self._task_deadline_exceeded():
                return await self._terminate_timed_out(tick_index, stage="taskboard_tick")
            schedule = board.schedule()
            tick_concurrency = self._taskboard_concurrency()
            await self._emit(
                f"agent_task.taskboard.tick.{tick_index}.scheduled",
                {
                    "schedule": schedule.to_dict(),
                    "evidence_view": build_task_board_evidence_view(board.revision).to_dict(),
                    "concurrency": tick_concurrency,
                },
            )
            if not schedule.runnable_card_ids:
                break
            tick_timeout = self._taskboard_tick_timeout()
            try:
                tick_result = await self._await_task_deadline(
                    board.async_run_tick(timeout=tick_timeout, concurrency=tick_concurrency),
                    stage="taskboard_tick",
                )
            except _AgentTaskDeadlineExceeded as error:
                return await self._terminate_timed_out(
                    tick_index,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
            except TimeoutError:
                return await self._terminate_timed_out(
                    tick_index,
                    stage="taskboard_tick",
                    reason=f"TaskBoard tick timed out after {tick_timeout} seconds.",
                    limit_name="taskboard_tick_timeout_seconds",
                    timeout_seconds=tick_timeout,
                )
            board.revision = tick_result.revision
            await self._emit(
                f"agent_task.taskboard.tick.{tick_index}.completed",
                {
                    "revision": tick_result.revision.to_dict(),
                    "schedule": tick_result.schedule.to_dict(),
                    "card_results": {key: value.to_dict() for key, value in tick_result.card_results.items()},
                    "evidence_view": build_task_board_evidence_view(tick_result.revision).to_dict(),
                    "runtime_topology": DataFormatter.sanitize(tick_result.triggerflow_snapshot.get("runtime_topology", {})),
                },
            )
            await self._record_phase(
                "taskboard_tick",
                iteration=tick_index,
                diagnostics={
                    "revision_id": tick_result.revision.revision_id,
                    "runnable_card_ids": list(tick_result.schedule.runnable_card_ids),
                    "completed_card_ids": list(tick_result.schedule.completed_card_ids),
                    "concurrency": tick_concurrency,
                },
            )
            if self._taskboard_revision_completed(tick_result.revision):
                break

        try:
            return await self._finalize_taskboard(board.revision)
        except _AgentTaskDeadlineExceeded as error:
            return await self._terminate_timed_out(
                self.max_iterations,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )

    async def _request_taskboard_plan(self, context_pack: "WorkspaceContextPackage"):
        policy = resolve_task_board_planning_policy(
            self._taskboard_effort(),
            metadata={"execution_strategy": self.execution_strategy, "task_id": self.id},
        )
        request = self.agent.create_temp_request()
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "planning_policy": policy.to_prompt_payload(),
                "planner_capabilities": self._planner_capabilities(),
            }
        )
        request.instruct(
            "Plan a TaskBoard for this submitted task. "
            "TaskBoard is already selected by the caller; do not decide whether to use TaskBoard. "
            "Use the planning_policy as vocabulary guidance for orchestration complexity, evidence depth, "
            "reflection density, and repair tendency. Do not create hard budgets, fixed card counts, "
            "or action allowlists from the effort profile. "
            "Plan card objectives and done_when conditions around user-visible outcomes, not around one "
            "specific provider, endpoint, file format, or auxiliary guidance source unless the user explicitly "
            "requires that exact source or artifact. Mark replaceable evidence attempts, optional guidance, "
            "style checks, and non-critical cross-checks as optional or degradable through failure_policy."
        )
        request.output(task_board_planning_output_schema(), format="json")
        raw_plan = await self._await_task_request(request.async_get_data(), stage="taskboard_plan")
        if not isinstance(raw_plan, Mapping):
            raise TypeError("TaskBoard planning request must return a mapping.")
        return coerce_task_board_planning_result(
            raw_plan,
            board_id=self.id,
            graph_id=f"{self.id}.taskboard",
            effort=self._taskboard_effort(),
            planning_policy=policy,
            metadata={"execution_strategy": self.execution_strategy},
        )

    async def _run_taskboard_card(self, context: Any, context_pack: "WorkspaceContextPackage") -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        readback_records = self._taskboard_action_artifact_recall_records(evidence_view)
        execution = self.agent.create_execution(
            lineage={
                "task_id": self.id,
                "iteration_id": f"taskboard:{context.card.id}",
                "step_id": "taskboard_card",
                "scope": {"strategy_phase": "taskboard_card_execution", "card_id": context.card.id},
            },
            limits=self.limits,
            options=self.options,
        )
        if readback_records:
            set_recall_records = getattr(execution.execution_context, "set_action_artifact_recall_records", None)
            if callable(set_recall_records):
                set_recall_records(readback_records, source="AgentTaskTaskBoard.evidence_view")
        execution.route_policy({
            "allowed_routes": ["model_request"],
            "on_violation": "block",
            "owner": "AgentTaskTaskBoard",
            "step_execution_shape": "taskboard_card",
        })
        execution.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "card": context.card.to_dict(),
                "dependency_results": {
                    key: value.to_dict() for key, value in dict(context.dependency_results).items()
                },
                "taskboard_evidence_view": evidence_view,
                "available_readback": self._taskboard_available_readback(evidence_view),
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": self._execution_prompt_context(),
            }
        )
        execution.instruct(
            "Execute exactly one TaskBoard card as a bounded AgentExecution step. "
            "Use TaskBoard evidence view as the hot summary; request full content only through available "
            "Workspace or Action refs when needed. If available_readback lists Action artifact refs and a "
            "bounded preview is insufficient, call read_action_artifact with the artifact_id and action_call_id "
            "before blocking on missing evidence. Return card-local evidence and remaining work. "
            "If the card's original method fails but equivalent evidence or a bounded fallback is available, "
            "return status completed with diagnostics that explain the degraded source boundary. Only return "
            "failed or blocked when the card cannot produce the required outcome or the missing evidence is "
            "truly critical. If this card produces the user-facing deliverable, put the complete deliverable "
            "body in artifact_markdown, candidate_final_result, or final_result. Review or verification cards "
            "must not put review notes in those deliverable fields unless they include the full corrected "
            "deliverable body. Do not claim the whole task is complete; TaskBoard and AgentTask own lifecycle completion."
        )
        execution.output(
            {
                "status": (str, "completed, blocked, or failed for this card", False),
                "answer": (str, "Card-local result or artifact summary", True),
                "candidate_final_result": (
                    str,
                    "Complete user-facing deliverable body when this card directly produces one",
                    False,
                ),
                "final_result": (
                    str,
                    "Complete final deliverable body when this card directly produces the final answer",
                    False,
                ),
                "artifact_markdown": (
                    str,
                    "Complete markdown deliverable body when this card creates a markdown artifact",
                    False,
                ),
                "evidence": ([str], "Evidence produced or used by this card", False),
                "remaining_work": ([str], "Remaining work for this card, empty when complete", False),
                "diagnostics": ([dict], "Optional card diagnostics", False),
            },
            format="json",
        )
        await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.execution.started",
            {"execution_id": execution.id, "card_id": context.card.id},
        )
        stream_task = asyncio.create_task(self._bridge_taskboard_card_execution_stream(context.card.id, execution))
        try:
            card_output = await self._await_taskboard_card_execution(
                execution.async_get_data(),
                card_id=context.card.id,
                stage="data",
            )
            execution_meta = await self._await_taskboard_card_execution(
                execution.async_get_meta(),
                card_id=context.card.id,
                stage="meta",
            )
            await stream_task
        except Exception as error:
            if not stream_task.done():
                stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stream_task
            return self._failed_taskboard_card_result(
                card_id=context.card.id,
                error=error,
                execution_id=str(getattr(execution, "id", "") or "") or None,
            )
        summary = self._execution_log_summary(cast(dict[str, Any], execution_meta))
        card_status = self._taskboard_card_status(card_output, execution_meta)
        diagnostics = []
        if isinstance(card_output, Mapping):
            raw_diagnostics = card_output.get("diagnostics")
            if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                diagnostics.extend(dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics)
        diagnostics.append(
            {
                "execution_id": execution_meta.get("execution_id"),
                "route": DataFormatter.sanitize(execution_meta.get("route", {})),
                "evidence_summary": DataFormatter.sanitize(summary),
            }
        )
        return TaskBoardCardResult(
            card_id=context.card.id,
            status=card_status,
            preview=DataFormatter.sanitize(card_output),
            artifact_refs=tuple(summary.get("artifact_refs", []) if isinstance(summary.get("artifact_refs"), list) else []),
            diagnostics=tuple(diagnostics),
            metadata={
                "execution_id": execution_meta.get("execution_id"),
                "execution_strategy": self.execution_strategy,
            },
        )

    async def _bridge_taskboard_card_execution_stream(self, card_id: str, execution: Any) -> None:
        try:
            async for item in execution.get_async_generator():
                await self._emit_taskboard_card_execution_stream_item(card_id, execution, item)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.diagnostics.setdefault("stream_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": str(error),
                    "card_id": card_id,
                    "stage": "taskboard_card",
                    "child_execution_id": str(getattr(execution, "id", "") or ""),
                }
            )

    async def _emit_taskboard_card_execution_stream_item(
        self,
        card_id: str,
        execution: Any,
        item: Any,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        event_type: Literal["delta", "done"] = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        item_meta = getattr(item, "meta", None)
        meta: dict[str, Any] = {
            "task_id": self.id,
            "status": self.status,
            "stage": "taskboard_card",
            "card_id": card_id,
            "stream_kind": "child_execution",
            "child_execution_id": str(getattr(execution, "id", "") or ""),
            "child_path": raw_path,
            "child_source": str(getattr(item, "source", "") or ""),
            "child_route": str(getattr(item, "route", "") or ""),
        }
        if isinstance(item_meta, Mapping):
            meta["child_meta"] = DataFormatter.sanitize(dict(item_meta))
        return await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.execution.{raw_path}",
            getattr(item, "value", None),
            event_type=event_type,
            delta=getattr(item, "delta", None),
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            meta=meta,
        )

    async def _await_taskboard_card_execution(
        self,
        awaitable: Awaitable[Any],
        *,
        card_id: str,
        stage: str,
    ) -> Any:
        timeout = self._taskboard_card_timeout()
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError as error:
            raise TimeoutError(
                f"TaskBoard card '{card_id}' {stage} request timed out after {timeout} seconds."
            ) from error

    def _failed_taskboard_card_result(
        self,
        *,
        card_id: str,
        error: Exception,
        execution_id: str | None = None,
    ) -> TaskBoardCardResult:
        message = str(error) or error.__class__.__name__
        is_timeout = self._is_timeout_error(error)
        if is_timeout and message == error.__class__.__name__:
            message = (
                f"TaskBoard card '{card_id}' execution timed out after "
                f"{self._task_request_timeout()} seconds."
            )
        diagnostic = {
            "type": error.__class__.__name__,
            "code": "taskboard.card.timeout" if is_timeout else "taskboard.card.execution_error",
            "message": message,
            "card_id": card_id,
            "execution_id": execution_id,
            "execution_strategy": self.execution_strategy,
            "stage": "taskboard_card",
            "timeout_seconds": self._taskboard_card_timeout() if is_timeout else None,
            "status": "failed",
        }
        self.diagnostics.setdefault("taskboard_card_errors", []).append(diagnostic)
        return TaskBoardCardResult(
            card_id=card_id,
            status="failed",
            preview=f"TaskBoard card execution failed: { error.__class__.__name__}: { message }",
            diagnostics=(diagnostic,),
            metadata={
                "execution_id": execution_id,
                "execution_strategy": self.execution_strategy,
                "status": "failed",
            },
        )

    async def _finalize_taskboard(self, revision: Any) -> dict[str, Any]:
        schedule = TaskBoard(revision, handler=lambda _context: None).schedule()
        result_status = self._taskboard_terminal_status(revision, schedule)
        evidence_view = build_task_board_evidence_view(revision).to_dict()
        candidate_final_result = self._taskboard_candidate_final_result(revision)
        can_attempt_degraded_final = self._taskboard_can_attempt_degraded_final(revision, schedule)
        if result_status != "completed" and not can_attempt_degraded_final:
            self.status = "blocked" if result_status == "blocked" else "error"
            self.result = {
                "status": self.status,
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "execution_strategy": self.execution_strategy,
                "reason": "TaskBoard did not reach a completed board state.",
                "taskboard": {
                    "revision": revision.to_dict(),
                    "schedule": schedule.to_dict(),
                    "evidence_view": evidence_view,
                },
            }
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        final = await self._request_taskboard_final(
            revision,
            evidence_view,
            candidate_final_result=candidate_final_result,
            board_status=result_status,
            schedule=schedule,
            allow_degraded_final=result_status != "completed",
        )
        final = self._normalize_taskboard_final_result(final, candidate_final_result)
        accepted = self._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        self.status = "completed" if accepted else "blocked"
        self.result = {
            "status": self.status,
            "accepted": accepted,
            "artifact_status": "accepted" if accepted else "partial",
            "task_id": self.id,
            "execution_strategy": self.execution_strategy,
            "final_result": final.get("final_result", ""),
            "reason": final.get("reason", ""),
            "missing_criteria": final.get("missing_criteria", []),
            "taskboard": {
                "revision": revision.to_dict(),
                "schedule": schedule.to_dict(),
                "evidence_view": evidence_view,
                "terminal_status": result_status,
                "degraded_finalization_attempted": result_status != "completed",
            },
        }
        await self._record_phase(
            "terminal",
            diagnostics={
                "status": self.status,
                "accepted": accepted,
                "execution_strategy": self.execution_strategy,
                "taskboard_revision_id": revision.revision_id,
            },
        )
        await self._emit("agent_task.completed" if accepted else "agent_task.blocked", self.result)
        return {"terminal": True, "status": self.status}

    async def _request_taskboard_final(
        self,
        revision: Any,
        evidence_view: Mapping[str, Any],
        *,
        candidate_final_result: str = "",
        board_status: str = "completed",
        schedule: Any = None,
        allow_degraded_final: bool = False,
    ) -> dict[str, Any]:
        request = self.agent.create_temp_request()
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "board_status": board_status,
                "allow_degraded_final": allow_degraded_final,
                "schedule": DataFormatter.sanitize(schedule.to_dict() if schedule is not None else {}),
                "taskboard_evidence_view": DataFormatter.sanitize(evidence_view),
                "revision": DataFormatter.sanitize(revision.to_dict()),
                "candidate_final_result": self._compact_verifier_prompt_value(candidate_final_result),
                "execution_prompt": self._execution_prompt_context(),
            }
        )
        request.instruct(
            "Synthesize the final result for this TaskBoard task from completed card evidence. "
            "Verify every success criterion. Use the hot evidence view for summaries and preserve cold refs "
            "as evidence pointers; do not invent unsupported facts. When candidate_final_result contains a "
            "complete answer/report/artifact body that satisfies the criteria, preserve it as final_result "
            "instead of rewriting it into a shorter summary. "
            "If allow_degraded_final is true, the board has stopped with failed, blocked, skipped, or pending "
            "cards. You may still accept only when the completed/degraded evidence is enough to satisfy the "
            "user goal and success criteria with explicit missing-source or degraded-source boundaries in "
            "the final_result. If critical evidence is missing, set accepted=false and explain the missing criteria."
        )
        request.output(
            {
                "accepted": (bool, "True only when all success criteria are satisfied", True),
                "reason": (str, "Concise final verification reason", True),
                "final_result": (str, "Final business result when accepted", True),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when accepted", False),
            },
            format="json",
        )
        result = await self._await_task_request(request.async_get_data(), stage="taskboard_finalize")
        if isinstance(result, Mapping):
            return dict(result)
        return {"accepted": False, "reason": str(result), "final_result": "", "missing_criteria": self.success_criteria}

    def _taskboard_candidate_final_result(self, revision: Any) -> str:
        graph = getattr(revision, "graph", None)
        cards = list(getattr(graph, "cards", []) or [])
        card_results = getattr(revision, "card_results", {}) or {}
        depended_on: set[str] = set()
        for card in cards:
            depended_on.update(str(card_id) for card_id in getattr(card, "depends_on", ()) or ())
        leaf_ids = {str(getattr(card, "id", "")) for card in cards if str(getattr(card, "id", "")) not in depended_on}
        structured_candidates: list[str] = []
        leaf_fallback_candidates: list[str] = []
        fallback_candidates: list[str] = []
        for card_id, result in card_results.items():
            if str(getattr(result, "status", "")) != "completed":
                continue
            preview = getattr(result, "preview", None)
            structured_candidate = self._candidate_final_result_from_execution_result(
                preview,
                include_answer=False,
            )
            if structured_candidate:
                structured_candidates.append(structured_candidate)
                continue
            fallback_candidate = self._candidate_final_result_from_execution_result(
                preview,
                include_answer=True,
            )
            if not fallback_candidate:
                continue
            fallback_candidates.append(fallback_candidate)
            if not leaf_ids or str(card_id) in leaf_ids:
                leaf_fallback_candidates.append(fallback_candidate)
        if structured_candidates:
            return max(structured_candidates, key=len, default="")
        if leaf_fallback_candidates:
            return max(leaf_fallback_candidates, key=len, default="")
        return max(fallback_candidates, key=len, default="")

    @classmethod
    def _normalize_taskboard_final_result(cls, final: dict[str, Any], candidate_final_result: str) -> dict[str, Any]:
        candidate = candidate_final_result.strip()
        if not candidate:
            return final
        accepted = cls._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        if not accepted:
            return final
        final_result = str(final.get("final_result") or "").strip()
        if not final_result or cls._looks_like_candidate_prefix(final_result, candidate):
            normalized = dict(final)
            normalized["final_result"] = candidate
            return normalized
        return final

    @staticmethod
    def _looks_like_candidate_prefix(value: str, candidate: str) -> bool:
        value = value.strip()
        candidate = candidate.strip()
        if not value or len(value) >= len(candidate):
            return False
        if candidate.startswith(value):
            return True
        compact_value = " ".join(value.split())
        compact_candidate = " ".join(candidate.split())
        return len(compact_value) < len(compact_candidate) and compact_candidate.startswith(compact_value)

    def _taskboard_effort(self) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, Mapping):
            effort = agent_task_options.get("effort")
            if effort is not None:
                return effort
        return "medium"

    def _taskboard_tick_timeout(self) -> float | None:
        return self._taskboard_option_timeout("taskboard_tick_timeout_seconds")

    def _taskboard_card_timeout(self) -> float | None:
        timeout = self._taskboard_option_timeout("taskboard_card_timeout_seconds")
        if timeout is not None:
            return timeout
        return self._task_request_timeout()

    def _taskboard_max_ticks(self) -> int:
        value = self._taskboard_option("taskboard_max_ticks")
        try:
            ticks = int(value) if value is not None else self.max_iterations
        except (TypeError, ValueError):
            ticks = self.max_iterations
        return max(1, ticks)

    def _taskboard_concurrency(self) -> int | None:
        value = self._taskboard_option("taskboard_concurrency")
        if value is None:
            return None
        try:
            concurrency = int(value)
        except (TypeError, ValueError):
            return None
        return concurrency if concurrency > 0 else None

    def _taskboard_option_timeout(self, key: str) -> float | None:
        value = self._taskboard_option(key)
        if value is None:
            return None
        return self._normalize_timeout(value)

    def _taskboard_option(self, key: str) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, Mapping) and key in agent_task_options:
            return agent_task_options.get(key)
        return self.options.get(key)

    @staticmethod
    def _taskboard_revision_completed(revision: Any) -> bool:
        cards = list(revision.graph.cards)
        if not cards:
            return False
        for card in cards:
            result = revision.card_results.get(card.id)
            status = str(result.status if result is not None else card.status).strip().lower()
            if task_board_card_required(card):
                if status != "completed":
                    return False
            elif status not in {"completed", "failed", "blocked", "skipped"}:
                return False
        return True

    @staticmethod
    def _taskboard_can_attempt_degraded_final(revision: Any, schedule: Any) -> bool:
        if getattr(schedule, "runnable_card_ids", ()):
            return False
        return any(str(result.status).strip().lower() == "completed" for result in revision.card_results.values())

    @staticmethod
    def _taskboard_terminal_status(revision: Any, schedule: Any) -> str:
        card_by_id = revision.graph.card_by_id()
        required_statuses = {
            card_id: str(result.status)
            for card_id, result in revision.card_results.items()
            if task_board_card_required(card_by_id[card_id])
        }
        if "failed" in required_statuses.values():
            return "failed"
        if "blocked" in required_statuses.values() or schedule.blocked_card_ids:
            return "blocked"
        if AgentTask._taskboard_revision_completed(revision):
            return "completed"
        return "running"

    @staticmethod
    def _taskboard_card_status(card_output: Any, execution_meta: Mapping[str, Any]) -> str:
        execution_status = str(execution_meta.get("status") or "").strip().lower()
        if execution_status in {"failed", "error", "timed_out", "blocked"}:
            return "failed"
        if isinstance(card_output, Mapping):
            status = str(card_output.get("status") or "completed").strip().lower()
            if status in {"completed", "blocked", "failed", "skipped"}:
                return status
            remaining = card_output.get("remaining_work")
            if isinstance(remaining, Sequence) and not isinstance(remaining, str | bytes | bytearray) and remaining:
                return "blocked"
        return "completed"

    @staticmethod
    def _taskboard_action_artifact_recall_records(evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_refs = evidence_view.get("artifact_refs")
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes | bytearray):
            return []
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_refs:
            if not isinstance(item, Mapping):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            if not artifact_id:
                continue
            action_call_id = str(item.get("action_call_id") or "").strip()
            key = (artifact_id, action_call_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "artifact_id": artifact_id,
                    "action_call_id": action_call_id,
                    "artifact_type": str(item.get("artifact_type") or ""),
                    "role": str(item.get("role") or ""),
                    "label": str(item.get("label") or ""),
                    "media_type": str(item.get("media_type") or ""),
                    "bytes": item.get("bytes", item.get("size")),
                    "sha256": item.get("sha256"),
                    "truncated": bool(item.get("truncated")),
                    "full_value_available": bool(item.get("full_value_available", item.get("available", False))),
                }
            )
        if not refs:
            return []
        return [
            {
                "action_id": "taskboard_upstream_evidence",
                "status": "success",
                "artifact_refs": refs,
            }
        ]

    @staticmethod
    def _taskboard_available_readback(evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        records = AgentTask._taskboard_action_artifact_recall_records(evidence_view)
        refs = records[0]["artifact_refs"] if records else []
        return {
            "schema_version": "agent_task_taskboard_readback/v1",
            "action_artifact_readback": {
                "available": bool(refs),
                "action_id": "read_action_artifact",
                "artifact_refs": DataFormatter.sanitize(refs),
            },
            "policy": "Use readback only when bounded previews are insufficient for the current card objective.",
        }

    async def _build_context(self) -> "WorkspaceContextPackage":
        try:
            return await self.workspace.build_context(
                goal=self.goal,
                scope={"task_id": self.id},
                budget=self.context_budget,
                profile=self.context_profile,
            )
        except Exception as error:
            fallback_reason: dict[str, Any] = {
                "type": error.__class__.__name__,
                "message": str(error),
                "stage": "workspace.build_context",
            }
            self.diagnostics.setdefault("recall_fallbacks", []).append(fallback_reason)
            try:
                fallback = await self.workspace.build_context(
                    goal="",
                    scope={"task_id": self.id},
                    budget=self.context_budget,
                    profile=self.context_profile,
                )
            except Exception as fallback_error:
                # A failing recall backend must not break the task loop. Return an
                # empty context pack so planning continues with no recalled context.
                fallback_reason["fallback_error"] = {
                    "type": fallback_error.__class__.__name__,
                    "message": str(fallback_error),
                }
                return cast(
                    "WorkspaceContextPackage",
                    {
                        "goal": self.goal,
                        "profile": self.context_profile,
                        "items": [],
                        "omitted": [],
                        "diagnostics": {"fallback_reason": fallback_reason},
                    },
                )
            diagnostics = fallback.setdefault("diagnostics", {})
            diagnostics["fallback_reason"] = fallback_reason
            return fallback

    def _step_execution_policy(self) -> dict[str, Any]:
        agent_task_options = self.options.get("agent_task")
        effort = agent_task_options.get("effort") if isinstance(agent_task_options, dict) else None
        effort = effort if isinstance(effort, dict) else {}
        execution_policy = effort.get("execution")
        policy = dict(execution_policy) if isinstance(execution_policy, dict) else {}
        raw_step_plan = str(
            policy.get("step_plan")
            or policy.get("step_execution")
            or policy.get("execution_shape")
            or "direct"
        ).strip().lower()
        if raw_step_plan in {"dynamic_task", "task_dag", "execution_dag"}:
            raw_step_plan = "dag"
        if raw_step_plan not in {"direct", "auto", "dag"}:
            raw_step_plan = "direct"
        if self.execution_strategy == "flat":
            raw_step_plan = "direct"
        policy["step_plan"] = raw_step_plan
        policy["execution_strategy"] = self.execution_strategy
        if "max_tasks" not in policy and "max_plan_items" in policy:
            policy["max_tasks"] = policy.get("max_plan_items")
        policy.setdefault("allow_dag_steps", raw_step_plan in {"auto", "dag"})
        if self.execution_strategy == "flat":
            policy["allow_dag_steps"] = False
        failed_dag_shapes = sorted(self._failed_execution_shapes.intersection(_DAG_STEP_EXECUTION_SHAPES))
        if raw_step_plan == "auto" and failed_dag_shapes:
            policy["allow_dag_steps"] = False
            policy["suppressed_execution_shapes"] = failed_dag_shapes
        return policy

    def _execution_prompt_context(self) -> dict[str, Any]:
        raw = self.options.get("execution_prompt_snapshot")
        if not isinstance(raw, Mapping):
            return {}
        return cast(dict[str, Any], DataFormatter.sanitize(dict(raw)))

    def _normalize_step_plan(self, plan: Any) -> dict[str, Any]:
        normalized: dict[str, Any]
        if isinstance(plan, dict):
            normalized = plan
        else:
            normalized = {"step_instruction": str(plan), "expected_evidence": "", "rationale": ""}
        raw_shape = (
            normalized.get("execution_shape")
            or normalized.get("step_kind")
            or normalized.get("route")
            or normalized.get("route_hint")
            or "direct"
        )
        shape = self._normalize_step_execution_shape(raw_shape)
        normalized["execution_shape"] = shape
        normalized.setdefault("effective_execution_shape", shape)
        normalized.setdefault("step_instruction", "")
        normalized.setdefault("expected_evidence", "")
        normalized.setdefault("rationale", "")
        # Structured step scope (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # scope comes from explicit capability lists, never from parsing the
        # natural-language step_instruction. `allowed_action_ids` is retained as an
        # internal alias for the action-id enforcement seam.
        raw_scope = normalized.get("step_scope")
        if not isinstance(raw_scope, dict):
            raw_scope = {}
        allowed_capability_ids = self._normalize_string_list(
            raw_scope.get("allowed_capability_ids") or normalized.get("allowed_action_ids")
        )
        normalized["step_scope"] = {"allowed_capability_ids": allowed_capability_ids}
        normalized["allowed_action_ids"] = allowed_capability_ids
        return normalized

    @staticmethod
    def _normalize_step_execution_shape(value: Any) -> str:
        text = str(value or "direct").strip().lower().replace("-", "_")
        aliases = {
            "model": "direct",
            "model_request": "direct",
            "direct_request": "direct",
            "flat": "direct",
            "flat_react": "direct",
            "action": "actions",
            "tool": "actions",
            "tools": "actions",
            "skill": "skills",
            "dag": "dynamic_task",
            "task_dag": "dynamic_task",
            "dynamic_task_dag": "dynamic_task",
            "agent_execution_dag": "execution_dag",
        }
        normalized = aliases.get(text, text)
        return normalized if normalized in _STEP_EXECUTION_SHAPES else "direct"

    def _step_dynamic_task_candidate(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        raw_candidate: Any = None
        for key in ("dynamic_task", "task_dag", "execution_dag"):
            item = plan.get(key)
            if isinstance(item, Mapping):
                raw_candidate = item
                break
        if raw_candidate is None and isinstance(plan.get("dynamic_task_plan"), Mapping):
            raw_candidate = {"plan": plan["dynamic_task_plan"]}
        if raw_candidate is None:
            return None
        candidate = dict(raw_candidate)
        if not candidate.get("mode"):
            candidate["mode"] = "submitted" if candidate.get("plan") is not None else "auto"
        return candidate

    def _configure_step_execution(self, execution: Any, plan: dict[str, Any]) -> dict[str, Any]:
        policy = self._step_execution_policy()
        requested_shape = str(plan.get("execution_shape") or "direct")
        effective_shape = requested_shape
        candidate_added = False
        dag_allowed = False
        warning: str | None = None

        if requested_shape in _DAG_STEP_EXECUTION_SHAPES:
            dag_suppressed = (
                policy.get("step_plan") == "auto"
                and bool(self._failed_execution_shapes.intersection(_DAG_STEP_EXECUTION_SHAPES))
            )
            dag_policy_allows = (
                policy.get("step_plan") != "direct"
                and bool(policy.get("allow_dag_steps") or policy.get("step_plan") in {"auto", "dag"})
            )
            candidate = self._step_dynamic_task_candidate(plan)
            has_dynamic_candidates = bool(
                getattr(self.agent, "_dynamic_task_candidates", []) or getattr(execution, "local_dynamic_task_candidates", [])
            )
            dag_allowed = bool(
                not dag_suppressed
                and dag_policy_allows
                and (
                    candidate
                    or has_dynamic_candidates
                )
            )
            if dag_suppressed:
                effective_shape = "direct"
                warning = "dag_shape_failed_previously"
            elif not dag_policy_allows:
                effective_shape = "direct"
                warning = "dag_shape_not_enabled"
            elif candidate is not None:
                add_candidate = getattr(execution, "_add_dynamic_task_candidate", None)
                if callable(add_candidate):
                    add_candidate(candidate)
                    candidate_added = True
            elif not has_dynamic_candidates and dag_allowed:
                auto_candidate: dict[str, Any] = {"mode": "auto"}
                for key in ("max_tasks", "timeout", "max_retries", "output_schema", "ensure_keys", "output_format"):
                    if policy.get(key) is not None:
                        auto_candidate[key] = policy.get(key)
                add_candidate = getattr(execution, "_add_dynamic_task_candidate", None)
                if callable(add_candidate):
                    add_candidate(auto_candidate)
                    candidate_added = True
            else:
                effective_shape = "direct"
                warning = "dag_shape_not_enabled"

        plan["effective_execution_shape"] = effective_shape
        # Structured step scope (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # when the plan names an explicit capability allowlist, narrow this step's
        # action candidates to it via the execution-local action-id seam. Scope
        # comes from the structured step_scope field, never from parsing the
        # step_instruction prose. The hard guarantee remains the verifier evidence
        # gate; this only prevents an evidence-gathering step from silently
        # completing the whole task with unrelated capabilities.
        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, dict):
            step_scope = {}
        allowed_capability_ids = self._normalize_string_list(step_scope.get("allowed_capability_ids"))
        if allowed_capability_ids and effective_shape in {"direct", "actions"}:
            local_action_ids = getattr(execution, "local_action_ids", None)
            if isinstance(local_action_ids, list):
                for capability_id in allowed_capability_ids:
                    if capability_id not in local_action_ids:
                        local_action_ids.append(capability_id)
            sync_action_scope = getattr(execution, "_sync_action_scope", None)
            if callable(sync_action_scope):
                sync_action_scope(source="AgentTaskLoop.step_scope")
        action_scope_source = "step_scope" if allowed_capability_ids else ""
        if effective_shape == "actions" and not allowed_capability_ids:
            action_capability_ids = [
                str(item.get("id") or "").strip()
                for item in self._planner_capabilities()
                if isinstance(item, Mapping)
                and str(item.get("kind") or "").strip() == "action"
                and str(item.get("id") or "").strip()
            ]
            if action_capability_ids:
                use_actions = getattr(execution, "use_actions", None)
                if callable(use_actions):
                    use_actions(action_capability_ids)
                    action_scope_source = "planner_capabilities"
        step_execution = {
            "requested_shape": requested_shape,
            "effective_shape": effective_shape,
            "dag_allowed": dag_allowed,
            "dynamic_task_candidate_added": candidate_added,
            "step_scope": DataFormatter.sanitize(step_scope),
            "action_scope_source": action_scope_source,
            "policy": DataFormatter.sanitize(policy),
        }
        route_policy = self._route_policy_for_step_execution(effective_shape)
        if route_policy:
            apply_route_policy = getattr(execution, "route_policy", None)
            if callable(apply_route_policy):
                apply_route_policy(route_policy)
            step_execution["route_policy"] = DataFormatter.sanitize(route_policy)
        if warning is not None:
            step_execution["warning"] = warning
            execution_warnings = plan.get("execution_warnings")
            if not isinstance(execution_warnings, list):
                execution_warnings = []
            execution_warnings.append(warning)
            plan["execution_warnings"] = execution_warnings
            self.diagnostics.setdefault("step_execution_warnings", []).append(
                {"iteration_shape": requested_shape, "warning": warning}
            )
        plan["step_execution"] = step_execution
        record_option = getattr(execution, "record_consumed_option", None)
        if callable(record_option):
            record_option("agent_task.step.execution_shape", effective_shape, owner="AgentTaskLoop")
            if route_policy:
                record_option("agent_task.step.route_policy", route_policy, owner="AgentTaskLoop")
            if policy.get("step_plan") != "direct":
                record_option("effort.execution.step_plan", policy.get("step_plan"), owner="AgentTaskLoop")
        return step_execution

    @staticmethod
    def _route_policy_for_step_execution(effective_shape: str) -> dict[str, Any]:
        route_by_shape = {
            "direct": "model_request",
            "actions": "model_request",
            "skills": "skills",
            "dynamic_task": "dynamic_task",
            "execution_dag": "dynamic_task",
        }
        route = route_by_shape.get(str(effective_shape or "").strip())
        if route is None:
            return {}
        return {
            "allowed_routes": [route],
            # A bounded step that cannot honor its selected shape must not silently
            # run model_request: block so the loop sees the mismatch and replans.
            "on_violation": "block",
            "owner": "AgentTaskLoop",
            "step_execution_shape": effective_shape,
        }

    def _planner_capabilities(self) -> list[dict[str, Any]]:
        """Planner-facing capability candidate snapshot (inert data only).

        Read from the typed snapshot the orchestrator route injected into options
        at task construction (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC).
        Covers actions, skills, skill packs, and dynamic-task candidates as one
        capability list. AgentTask consumes only this snapshot; it does not reach
        back into the routing plugin.
        """
        raw = self.options.get("planner_capabilities")
        if not isinstance(raw, list):
            return []
        capabilities: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            capability_id = str(item.get("id") or item.get("capability_id") or "").strip()
            if not capability_id:
                continue
            entry: dict[str, Any] = {
                "id": capability_id,
                "kind": str(item.get("kind") or "action"),
                "route": str(item.get("route") or "model_request"),
                "guidance_access": str(item.get("guidance_access") or "none"),
                "description": str(item.get("description") or ""),
            }
            if item.get("mode"):
                entry["mode"] = str(item.get("mode"))
            capabilities.append(entry)
        return capabilities

    def _capability_evidence_requirements(self) -> list[dict[str, Any]]:
        """Structured, authored completion-evidence requirements (inert data).

        The load-bearing gate's trigger
        (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC): which capabilities
        must appear in execution evidence for the task to be acceptable. Authored
        as a structured option, independent of capability mode; never inferred
        from free-text criteria. Accepts either a list of capability-id strings
        (treated as `capability_used`) or a list of EvidenceRequirement dicts. The
        legacy `skill_evidence_requirements` option is read as a fallback alias.
        """
        raw = self.options.get("capability_evidence_requirements")
        if raw is None:
            raw = self.options.get("skill_evidence_requirements")
        if isinstance(raw, dict):
            raw = raw.get("capabilities") or raw.get("skills")
        if not isinstance(raw, (list, tuple)):
            return []
        requirements: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                capability_id = item.strip()
                if capability_id:
                    requirements.append(
                        {"capability_id": capability_id, "kind": "capability_used", "required": True, "source": "criterion"}
                    )
                continue
            if not isinstance(item, dict):
                continue
            capability_id = str(item.get("capability_id") or item.get("id") or "").strip()
            if not capability_id:
                continue
            requirement: dict[str, Any] = {
                "capability_id": capability_id,
                "kind": str(item.get("kind") or "capability_used"),
                "required": bool(item.get("required", True)),
                "source": str(item.get("source") or "criterion"),
            }
            if item.get("capability_kind"):
                requirement["capability_kind"] = str(item.get("capability_kind"))
            if item.get("criterion_id"):
                requirement["criterion_id"] = str(item.get("criterion_id"))
            requirements.append(requirement)
        return requirements

    def _evaluate_capability_evidence(self) -> tuple[list[str], list[dict[str, Any]]]:
        """Deterministically check structured evidence requirements.

        Returns (missing_capability_ids, unenforced_requirements). Checks run
        against capability evidence accumulated across iterations
        (`_satisfied_capabilities` for `capability_used`,
        `_satisfied_succeeded_actions` for `action_succeeded`). Only the wired
        combinations are enforced; anything without a structural producer
        (the reserved evidence kinds, and `capability_used` for a
        `dynamic_task` capability whose usage is not recorded in evidence) is
        returned as an unenforced diagnostic rather than silently passing or
        false-failing.
        """
        requirements = self._capability_evidence_requirements()
        if not requirements:
            return [], []

        missing: list[str] = []
        unenforced: list[dict[str, Any]] = []
        for requirement in requirements:
            if not requirement.get("required", True):
                continue
            capability_id = str(requirement.get("capability_id") or "").strip()
            if not capability_id:
                continue
            kind = str(requirement.get("kind") or "capability_used")
            capability_kind = str(requirement.get("capability_kind") or "")
            if kind == "capability_used" and capability_kind != "dynamic_task":
                if capability_id not in self._satisfied_capabilities:
                    missing.append(capability_id)
            elif kind == "action_succeeded":
                if capability_id not in self._satisfied_succeeded_actions:
                    missing.append(capability_id)
            else:
                unenforced.append(
                    {"task_id": self.id, "capability_id": capability_id, "kind": kind, "capability_kind": capability_kind}
                )
        # De-duplicate while preserving order.
        deduped: list[str] = []
        for capability_id in missing:
            if capability_id not in deduped:
                deduped.append(capability_id)
        return deduped, unenforced

    async def _request_plan(self, iteration_index: int, context_pack: "WorkspaceContextPackage") -> dict[str, Any]:
        request = self.agent.create_temp_request()
        planner_capabilities = self._planner_capabilities()
        execution_prompt = self._execution_prompt_context()
        previous_iterations = self._iteration_prompt_summaries()
        repair_context = self._planner_repair_context(previous_iterations)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "previous_iterations": previous_iterations,
                "repair_context": repair_context,
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": execution_prompt,
                "execution_policy": self._step_execution_policy(),
                "execution_strategy": self.execution_strategy,
                "planner_capabilities": planner_capabilities,
            }
        )
        # Explanatory note only (not a guarantee): the hard guarantee is the
        # verifier evidence gate, not this prompt text. It tells the planner which
        # capabilities exist and that some kinds (e.g. a Skill's guidance) load
        # only on their own route.
        capability_note = (
            " Available capabilities are listed in planner_capabilities, each with a kind "
            "(action/skill/skill_pack/dynamic_task), the execution_shape route that exposes it, and "
            "guidance_access. A capability whose guidance_access is route_context (such as a Skill's "
            "guidance) only reaches the model on its own route, so choose that execution_shape when such "
            "a capability is the intended way to satisfy a criterion."
            if planner_capabilities
            else ""
        )
        allowed_shapes = (
            "direct or actions"
            if self.execution_strategy == "flat"
            else "direct, actions, skills, dynamic_task, or execution_dag"
        )
        strategy_note = (
            " The selected execution_strategy is flat: keep the task in a linear AgentTask loop and do not plan DAG or TaskBoard steps."
            if self.execution_strategy == "flat"
            else ""
        )
        request.instruct(
            "Plan the next bounded AgentExecution step for this AgentTask. "
            "Treat execution_prompt as caller-provided task context, including any input, instructions, and output contract. "
            "Use prior verification evidence when present. Do not finalize unless all success criteria can be verified. "
            "When repair_context is present, treat repair_constraints and next_step_requirements as hard planning inputs "
            "for the next bounded step. The verifier does not choose tools or routes; the planner must choose a valid "
            "bounded step that directly addresses those constraints without repeating an unrelated previous step. "
            f"Set execution_shape to {allowed_shapes}. "
            "Use a DAG-shaped execution only when execution_policy allows it or a concrete DynamicTask candidate is available."
            + strategy_note
            + capability_note
            + " Optionally set step_scope.allowed_capability_ids to limit this bounded step to specific capability "
            "ids when it is only meant to gather evidence; leave it empty when the step may use any available capability."
        )
        request.output(
            {
                "execution_shape": (
                    str,
                    "Execution shape for this bounded step: direct, actions, skills, dynamic_task, or execution_dag",
                    False,
                ),
                "step_instruction": (str, "Instruction for one bounded AgentExecution step", True),
                "expected_evidence": (str, "Evidence this step should produce", True),
                "rationale": (str, "Why this is the next step", True),
                "step_scope": (
                    dict,
                    "Optional structured scope: {allowed_capability_ids: [...]}; empty means no restriction",
                    False,
                ),
                "dynamic_task": (
                    dict,
                    "Optional bounded DynamicTask candidate when execution_shape is dynamic_task or execution_dag",
                    False,
                ),
            },
            format="json",
        )
        plan = await self._await_task_request(request.async_get_data(), stage="plan")
        return self._normalize_step_plan(plan)

    async def _execute_step(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> tuple[Any, dict[str, Any]]:
        override = self._step_stage_override("_execute_step")
        if override is not None:
            result = override(iteration_index, plan, context_pack)
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result
            return cast(tuple[Any, dict[str, Any]], result)

        plan = self._normalize_step_plan(plan)
        execution_plan = self._build_blocks_execution_plan(iteration_index, plan, context_pack)
        blocks_entrypoint = self._resolve_blocks()
        execution_graph = blocks_entrypoint.compile(
            {
                "execution_id": f"{self.id}:iter-{iteration_index}",
                "task_frame_id": execution_plan.task_frame_id,
                "plan_id": execution_plan.plan_id,
                "plan_blocks": [block.to_dict() for block in execution_plan.plan_blocks],
                "edges": [edge.to_dict() for edge in execution_plan.edges],
                "capability_resolution": self._blocks_capability_resolution(plan).to_dict(),
                "evidence_requirements": [dict(item) for item in execution_plan.evidence_requirements],
                "result_contracts": [dict(item) for item in execution_plan.result_contracts],
                "runtime_policy": {"checkpoint_policy": dict(execution_plan.checkpoint_policy)},
                "budget": dict(plan.get("budget", {})) if isinstance(plan.get("budget"), Mapping) else {},
            }
        )
        flow = blocks_entrypoint.bind_runtime(execution_graph)

        async def run_agent_step(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            execution_result, execution_meta = await self._run_bounded_agent_execution_step(
                iteration_index,
                plan,
                context_pack,
            )
            return {
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_meta": DataFormatter.sanitize(execution_meta),
            }

        blocks_execution = flow.create_execution(
            auto_close=False,
            workspace=False,
            runtime_resources={"blocks.handlers": {"agent_task_bounded_step": run_agent_step}},
        )
        try:
            await blocks_execution.async_start(
                {
                    "task_id": self.id,
                    "iteration": iteration_index,
                    "plan": DataFormatter.sanitize(plan),
                    "context_pack": DataFormatter.sanitize(context_pack),
                }
            )
            snapshot = await blocks_execution.async_close()
        except Exception as error:
            result, failed_meta = self._failed_execution_result(
                iteration_index,
                plan=plan,
                error=error,
                execution_id=f"{self.id}:iter-{iteration_index}:blocks-step",
            )
            failed_meta["blocks"] = {
                "execution_plan": execution_plan.to_dict(),
                "execution_block_graph": execution_graph.to_dict(),
                "diagnostics": [
                    {
                        "type": error.__class__.__name__,
                        "message": str(error),
                        "stage": "blocks_execution",
                    }
                ],
            }
            await self._emit(
                f"agent_task.iteration.{iteration_index}.execution.failed",
                {"execution_meta": failed_meta},
            )
            await self._record_phase(
                "execution_failed",
                iteration=iteration_index,
                diagnostics={
                    "execution_id": failed_meta.get("execution_id"),
                    "route": failed_meta.get("route"),
                    "error": failed_meta.get("diagnostics", {}).get("execution_error"),
                    "blocks": DataFormatter.sanitize(failed_meta.get("blocks")),
                },
            )
            return result, failed_meta

        evidence = blocks_entrypoint.map_evidence(execution_graph, snapshot)
        block_result = dict(blocks_entrypoint.map_result(execution_graph, snapshot))
        agent_step_output = self._extract_agent_step_block_output(snapshot)
        execution_result = agent_step_output.get("execution_result")
        raw_meta = agent_step_output.get("execution_meta")
        execution_meta = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
        if not execution_meta:
            execution_meta = {
                "execution_id": f"{self.id}:iter-{iteration_index}:missing-agent-step-meta",
                "status": "failed",
                "route": {"selected_route": "agent_step", "status": "failed"},
                "logs": {
                    "action_logs": {},
                    "route_logs": {},
                    "errors": [{"message": "agent_step block returned no execution_meta"}],
                },
            }
        self._attach_blocks_evidence(
            execution_meta,
            execution_plan=execution_plan,
            execution_graph=execution_graph,
            evidence=evidence,
            block_result=block_result,
            snapshot=snapshot,
        )
        self._reconcile_effective_shape(plan, execution_meta)
        status = str(execution_meta.get("status") or "").strip().lower()
        if status not in {"failed", "error", "timed_out", "blocked"}:
            await self._emit(f"agent_task.iteration.{iteration_index}.execution.completed", execution_meta)
        return execution_result, cast(dict[str, Any], execution_meta)

    async def _run_bounded_agent_execution_step(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> tuple[Any, dict[str, Any]]:
        plan = self._normalize_step_plan(plan)
        execution = self.agent.create_execution(
            lineage={
                "task_id": self.id,
                "iteration_id": f"iter-{iteration_index}",
                "step_id": "execute",
                "scope": {"strategy_phase": "agent_task_execution_step"},
            },
            limits=self.limits,
            options=self.options,
        )
        step_execution = self._configure_step_execution(execution, plan)
        execution.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "plan": DataFormatter.sanitize(plan),
                "step_execution": step_execution,
                "execution_strategy": self.execution_strategy,
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": self._execution_prompt_context(),
            }
        )
        execution.instruct(
            (
                "Execute exactly one bounded step for the AgentTask. "
                f"Use the selected execution shape: {step_execution.get('effective_shape', 'direct')}. "
                f"The AgentTask execution_strategy is {self.execution_strategy}. "
                "Respect the caller-provided execution_prompt context and output contract when present. "
                "Return concrete evidence for the verifier. If this step produces the requested final answer, report, "
                "file body, or artifact body, put the complete candidate deliverable in candidate_final_result instead "
                "of burying the only copy inside evidence. Do not claim final completion unless evidence supports it."
            )
        )
        execution.output(
            {
                "step_result": (str, "Concrete result of this bounded step", True),
                "candidate_final_result": (
                    str,
                    "Complete answer/report/artifact body produced by this step when it may satisfy the final task",
                    False,
                ),
                "evidence": ([str], "Evidence produced by the step", True),
                "remaining_work": ([str], "Known remaining work, empty when none"),
            },
            format="json",
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.execution.started",
            {"execution_id": execution.id, "step_execution": step_execution},
        )
        stream_task = asyncio.create_task(self._bridge_step_execution_stream(iteration_index, execution))
        try:
            result = await execution.async_get_data()
            meta = await execution.async_get_meta()
            await stream_task
        except Exception as error:
            if not stream_task.done():
                stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stream_task
            result, failed_meta = self._failed_execution_result(
                iteration_index,
                plan=plan,
                error=error,
                execution_id=str(getattr(execution, "id", "") or "") or None,
            )
            await self._emit(
                f"agent_task.iteration.{iteration_index}.execution.failed",
                {"execution_meta": failed_meta},
            )
            await self._record_phase(
                "execution_failed",
                iteration=iteration_index,
                diagnostics={
                    "execution_id": failed_meta.get("execution_id"),
                    "route": failed_meta.get("route"),
                    "error": failed_meta.get("diagnostics", {}).get("execution_error"),
                },
            )
            return result, failed_meta
        return result, cast(dict[str, Any], meta)

    async def _bridge_step_execution_stream(self, iteration_index: int, execution: Any) -> None:
        try:
            async for item in execution.get_async_generator():
                await self._emit_step_execution_stream_item(iteration_index, execution, item)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.diagnostics.setdefault("stream_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": str(error),
                    "iteration": iteration_index,
                    "stage": "execution",
                    "child_execution_id": str(getattr(execution, "id", "") or ""),
                }
            )

    async def _emit_step_execution_stream_item(
        self,
        iteration_index: int,
        execution: Any,
        item: Any,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        event_type: Literal["delta", "done"] = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        item_meta = getattr(item, "meta", None)
        meta: dict[str, Any] = {
            "task_id": self.id,
            "status": self.status,
            "iteration": iteration_index,
            "stage": "execution",
            "stream_kind": "child_execution",
            "child_execution_id": str(getattr(execution, "id", "") or ""),
            "child_path": raw_path,
            "child_source": str(getattr(item, "source", "") or ""),
            "child_route": str(getattr(item, "route", "") or ""),
        }
        if isinstance(item_meta, Mapping):
            meta["child_meta"] = DataFormatter.sanitize(dict(item_meta))
        return await self._emit(
            f"agent_task.iteration.{iteration_index}.execution.{raw_path}",
            getattr(item, "value", None),
            event_type=event_type,
            delta=getattr(item, "delta", None),
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            meta=meta,
        )

    @staticmethod
    def _stream_path_token(value: Any) -> str:
        token = str(value or "").strip().replace("/", ".")
        return token or "item"

    def _step_stage_override(self, stage_name: str):
        overrides = getattr(self, "_agent_task_step_overrides", None)
        if not isinstance(overrides, dict):
            return None
        handler = overrides.get(stage_name)
        return handler if callable(handler) else None

    def _build_blocks_execution_plan(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ):
        from agently.types.data import ExecutionPlan, PlanBlockInstance

        effective_shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "direct")
        execution_policy = self._step_execution_policy()
        plan_block_kind = "flow_segment" if effective_shape in _DAG_STEP_EXECUTION_SHAPES else "agent_step"
        plan_block_id = plan_block_kind
        plan_block_label = "dag-segment" if plan_block_kind == "flow_segment" else "agent-step"
        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, dict):
            step_scope = {}
        budget = plan.get("budget")
        return ExecutionPlan(
            plan_id=f"{self.id}:iter-{iteration_index}:execution-plan",
            task_frame_id=f"{self.id}:iter-{iteration_index}:task-frame",
            plan_blocks=(
                PlanBlockInstance(
                    id=f"iter-{iteration_index}:{plan_block_label}",
                    plan_block_id=plan_block_id,
                    kind=plan_block_kind,
                    intent=str(plan.get("step_instruction") or ""),
                    bound_inputs={
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "goal": self.goal,
                        "success_criteria": self.success_criteria,
                        "preferred_execution_shape": effective_shape,
                        "step_plan": execution_policy.get("step_plan", "direct"),
                        "plan": DataFormatter.sanitize(plan),
                        "execution_prompt": self._execution_prompt_context(),
                        "context_summary": {
                            "item_count": len(context_pack.get("items", [])),
                            "profile": context_pack.get("profile"),
                        },
                    },
                    output_contract={
                        "execution_result": "bounded AgentExecution step result",
                        "execution_meta": "bounded AgentExecution route metadata and evidence",
                    },
                    evidence_contract={
                        "expected_evidence": str(plan.get("expected_evidence") or ""),
                        "effective_execution_shape": effective_shape,
                        "step_plan": execution_policy.get("step_plan", "direct"),
                    },
                    runtime_preferences={"handler": "agent_task_bounded_step"},
                    budget=dict(budget) if isinstance(budget, Mapping) else {},
                ),
            ),
            semantic_outputs={"step": f"iter-{iteration_index}:{plan_block_label}"},
            evidence_requirements=tuple(
                {"capability_id": capability_id, "source": "step_scope"}
                for capability_id in self._normalize_string_list(step_scope.get("allowed_capability_ids"))
            ),
            result_contracts=(
                {
                    "name": "agent_task_step",
                    "requires": ["execution_result", "execution_meta"],
                },
            ),
            checkpoint_policy={"scope": "agent_task_iteration", "iteration": iteration_index},
        )

    @staticmethod
    def _extract_agent_step_block_output(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        blocks_state = snapshot.get("blocks", {})
        if not isinstance(blocks_state, Mapping):
            return {}
        results = blocks_state.get("execution_block_results", ())
        if not isinstance(results, (list, tuple)):
            return {}
        for item in results:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("kind") or "") in {"agent_step", "flow_segment"}:
                output = item.get("output")
                return dict(output) if isinstance(output, Mapping) else {}
        return {}

    def _blocks_capability_resolution(self, plan: dict[str, Any]):
        from agently.types.data import CapabilityResolution

        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, dict):
            step_scope = {}
        scoped_ids = self._normalize_string_list(step_scope.get("allowed_capability_ids"))
        return CapabilityResolution(
            allowed_capabilities=tuple(scoped_ids),
            scoped_action_candidates=tuple(
                {"action_id": capability_id, "capability_id": capability_id, "source": "AgentTaskLoop.step_scope"}
                for capability_id in scoped_ids
            ),
            diagnostics=(
                {
                    "source": "AgentTaskLoop",
                    "step_execution_shape": str(
                        plan.get("effective_execution_shape") or plan.get("execution_shape") or "direct"
                    ),
                    "grants_capability": False,
                },
            ),
        )

    @staticmethod
    def _attach_blocks_evidence(
        execution_meta: dict[str, Any],
        *,
        execution_plan: Any,
        execution_graph: Any,
        evidence: Any,
        block_result: Mapping[str, Any],
        snapshot: Mapping[str, Any],
    ) -> None:
        blocks_state = snapshot.get("blocks", {}) if isinstance(snapshot, Mapping) else {}
        execution_meta["blocks"] = {
            "execution_plan": execution_plan.to_dict(),
            "execution_block_graph": execution_graph.to_dict(),
            "evidence": evidence.to_dict(),
            "result": dict(block_result),
            "snapshot": DataFormatter.sanitize({"blocks": blocks_state}),
        }

    @staticmethod
    def _resolve_blocks():
        from agently.base import blocks

        return blocks

    def _record_failed_execution_shape(self, plan: dict[str, Any], execution_meta: dict[str, Any]) -> None:
        route = execution_meta.get("route", {})
        route_name = ""
        if isinstance(route, dict):
            route_name = str(route.get("selected_route") or "")
        shape = self._shape_for_route(route_name) if route_name else ""
        if not shape:
            shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "")
        shape = self._normalize_step_execution_shape(shape)
        if shape:
            self._failed_execution_shapes.add(shape)

    def _iteration_prompt_summaries(self) -> list[dict[str, Any]]:
        """Bounded, low-noise iteration history for plan/verify prompts.

        Full iteration records (including execution_meta) stay in self.iterations
        and the Workspace; prompts receive only step intent, the verification
        outcome, and Workspace refs so context does not grow unboundedly with the
        full execution metadata of every prior step.
        """
        limit = self._iterations_prompt_limit()
        records = self.iterations[-limit:] if isinstance(limit, int) and limit > 0 else self.iterations
        summaries: list[dict[str, Any]] = []
        for record in records:
            plan = record.get("plan")
            if not isinstance(plan, dict):
                plan = {}
            verification = record.get("verification")
            if not isinstance(verification, dict):
                verification = {}
            summaries.append(
                {
                    "iteration": record.get("iteration"),
                    "step_instruction": plan.get("step_instruction", ""),
                    "effective_execution_shape": plan.get(
                        "effective_execution_shape", plan.get("execution_shape", "")
                    ),
                    "verification": {
                        "is_complete": verification.get("is_complete"),
                        "reason": verification.get("reason", ""),
                        "missing_criteria": verification.get("missing_criteria", []),
                        "replan_instruction": verification.get("replan_instruction", ""),
                        "repair_constraints": verification.get("repair_constraints", []),
                        "next_step_requirements": verification.get("next_step_requirements", []),
                    },
                    "observation_ref": record.get("observation_ref"),
                    "verification_ref": record.get("verification_ref"),
                }
            )
        # Prior-run summaries (from a resumed snapshot) come first so the model
        # sees the full task history; the recent-window limit applies to the
        # combined sequence.
        combined = [*self._resumed_iteration_summaries, *summaries]
        if isinstance(limit, int) and limit > 0 and len(combined) > limit:
            combined = combined[-limit:]
        return combined

    @staticmethod
    def _planner_repair_context(previous_iterations: list[dict[str, Any]]) -> dict[str, Any]:
        if not previous_iterations:
            return {}
        latest = previous_iterations[-1]
        if not isinstance(latest, Mapping):
            return {}
        verification = latest.get("verification")
        if not isinstance(verification, Mapping):
            return {}
        if verification.get("is_complete") is True:
            return {}

        def normalize_list(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, tuple):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        missing_criteria = normalize_list(verification.get("missing_criteria"))
        repair_constraints = normalize_list(verification.get("repair_constraints"))
        next_step_requirements = normalize_list(verification.get("next_step_requirements"))
        replan_instruction = str(verification.get("replan_instruction") or "").strip()
        if not any([missing_criteria, repair_constraints, next_step_requirements, replan_instruction]):
            return {}
        return {
            "source_iteration": latest.get("iteration"),
            "verification_ref": latest.get("verification_ref"),
            "reason": str(verification.get("reason") or ""),
            "missing_criteria": missing_criteria,
            "repair_constraints": repair_constraints,
            "next_step_requirements": next_step_requirements,
            "replan_instruction": replan_instruction,
        }

    def _iterations_prompt_limit(self) -> int | None:
        configured = self._agent_task_option("iterations_prompt_limit", None)
        if configured is None:
            return None
        try:
            limit = int(configured)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    @staticmethod
    def _shape_for_route(route: str) -> str:
        return {
            "model_request": "direct",
            "skills": "skills",
            "dynamic_task": "dynamic_task",
            "agent_task": "dynamic_task",
        }.get(str(route or "").strip(), "direct")

    def _reconcile_effective_shape(self, plan: dict[str, Any], execution_meta: dict[str, Any]) -> None:
        """Write the route actually taken back into the plan's effective shape.

        Keeps effective_execution_shape consistent with the executed route so
        diagnostics never report a shape the run did not actually take.
        """
        route_info = execution_meta.get("route")
        if not isinstance(route_info, dict):
            route_info = {}
        actual_route = str(route_info.get("selected_route") or "")
        if not actual_route:
            return
        requested_shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "direct")
        # "actions" and "direct" both run the model_request route; treat them as
        # consistent so a normal actions step is not flagged as a mismatch.
        consistent = {"direct", "actions"} if actual_route == "model_request" else {self._shape_for_route(actual_route)}
        if requested_shape in consistent:
            return
        plan["effective_execution_shape"] = self._shape_for_route(actual_route)
        plan["route_shape_reconciled"] = {
            "requested_shape": requested_shape,
            "actual_route": actual_route,
            "effective_shape": plan["effective_execution_shape"],
        }
        self.diagnostics.setdefault("route_shape_reconciliations", []).append(plan["route_shape_reconciled"])

    async def _request_verification(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        execution_result: Any,
        execution_meta: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> dict[str, Any]:
        evidence_summary = self._compact_verifier_evidence_summary(
            self._execution_log_summary(execution_meta)
        )
        candidate_final_result = self._candidate_final_result_from_execution_result(execution_result)
        request = self.agent.create_temp_request()
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "plan": self._compact_verifier_prompt_value(plan, max_chars=_VERIFIER_PROMPT_ITEM_CHARS),
                "candidate_final_result": self._compact_verifier_prompt_value(
                    candidate_final_result,
                    max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
                ),
                "execution_result": self._compact_verifier_prompt_value(
                    execution_result,
                    max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
                ),
                "execution_meta": self._verification_execution_meta_summary(execution_meta, evidence_summary),
                "execution_evidence_summary": evidence_summary,
                "capability_evidence_requirements": self._capability_evidence_requirements(),
                "context_pack": self._compact_context_pack_for_verifier(context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "previous_iterations": self._iteration_prompt_summaries(),
            }
        )
        request.instruct(
            "Verify the task against every success criterion. "
            "Also consider caller-provided execution_prompt constraints when they are present. "
            "Treat numeric criteria such as 'at least N' as exact counting rules and fail verification when the "
            "evidence does not meet the count. "
            "Require source/evidence references when the criteria ask for evidence. "
            "If execution metadata, action records, diagnostics, command output, or verifier-visible evidence shows "
            "a failed required action or failed validation command, do not mark complete. "
            "If a criterion requires a script, command, test, or external validation to pass, require explicit "
            "successful evidence for that validation before completion. "
            "Decide final_result_required from the goal and success criteria: set it true when the task demands a "
            "concrete final deliverable (answer, file, report, artifact, or similar) and false when the work is "
            "purely an action or side effect with no expected returned deliverable. "
            "When marking complete, put every required final deliverable in final_result; if final_result_required is "
            "true and final_result would omit a required deliverable, keep is_complete=false and ask for a replan. "
            "When candidate_final_result contains a complete answer/report/artifact body that satisfies the criteria, "
            "use it as final_result; the caller or host may persist final_result to the requested file path after "
            "verification. Do not require a Workspace file artifact unless the success criteria explicitly require a "
            "Workspace write/readback action. "
            "If evidence is incomplete, set is_complete=false and give concrete repair_constraints and "
            "next_step_requirements for the planner. These fields describe what must be fixed or produced next; "
            "do not use them to choose tools, routes, or execution shapes unless a success criterion explicitly "
            "requires a specific capability. Also include a short human-readable replan_instruction. "
            "Set requires_block=true only when the task cannot continue."
        )
        request.output(
            {
                "is_complete": (bool, "True only when all success criteria are satisfied", True),
                "requires_block": (bool, "True only when the task cannot continue", True),
                "reason": (str, "Concise verification reason", True),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when none"),
                "replan_instruction": (str, "Instruction for the next planning round when incomplete"),
                "repair_constraints": (
                    [str],
                    "Verifier-owned constraints the next planner step must satisfy; not a tool or route plan",
                ),
                "next_step_requirements": (
                    [str],
                    "Observable requirements for the next bounded step output or evidence",
                ),
                "final_result_required": (bool, "True when the goal expects a concrete returned final deliverable"),
                "final_result": (str, "Final business result when complete"),
            },
            format="json",
        )
        verification = await self._await_task_request(request.async_get_data(), stage="verify")
        if not isinstance(verification, dict):
            return {
                "is_complete": False,
                "requires_block": False,
                "reason": str(verification),
                "missing_criteria": self.success_criteria,
                "replan_instruction": "Run another bounded step with stronger evidence.",
                "repair_constraints": self.success_criteria,
                "next_step_requirements": ["Run another bounded step with stronger evidence."],
                "final_result_required": False,
                "final_result": "",
            }
        return self._normalize_verification(
            verification,
            execution_evidence_summary=self._execution_log_summary(execution_meta),
            candidate_final_result=candidate_final_result,
        )

    @classmethod
    def _candidate_final_result_from_execution_result(
        cls,
        execution_result: Any,
        *,
        include_answer: bool = True,
    ) -> str:
        if isinstance(execution_result, Mapping):
            keys: tuple[str, ...] = (
                "candidate_final_result",
                "final_result",
                "artifact_markdown",
                "artifact_html",
            )
            if include_answer:
                keys = keys + ("answer", "result")
            for key in keys:
                value = execution_result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if not include_answer:
                return ""
            step_result = execution_result.get("step_result")
            if isinstance(step_result, str) and len(step_result.strip()) > 200:
                return step_result.strip()
            remaining_work = execution_result.get("remaining_work")
            evidence = execution_result.get("evidence")
            if not cls._has_remaining_work(remaining_work) and isinstance(evidence, Sequence) and not isinstance(
                evidence, str | bytes | bytearray
            ):
                text_items = [item.strip() for item in evidence if isinstance(item, str) and item.strip()]
                if text_items:
                    longest = max(text_items, key=len)
                    if len(longest) > max(800, len(str(step_result or "")) * 3):
                        return longest
        if isinstance(execution_result, str) and execution_result.strip():
            return execution_result.strip()
        return ""

    @staticmethod
    def _has_remaining_work(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return any(bool(str(item).strip()) for item in value)
        return bool(value)

    @classmethod
    def _verification_execution_meta_summary(
        cls,
        execution_meta: Mapping[str, Any],
        evidence_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        route = execution_meta.get("route")
        diagnostics = execution_meta.get("diagnostics")
        return {
            "status": str(execution_meta.get("status") or ""),
            "route": cls._compact_verifier_prompt_value(route, max_chars=1200),
            "diagnostics": cls._compact_verifier_prompt_value(diagnostics, max_chars=1200),
            "evidence_summary": dict(evidence_summary),
        }

    @classmethod
    def _compact_context_pack_for_verifier(cls, context_pack: "WorkspaceContextPackage") -> dict[str, Any]:
        compact = cls._compact_verifier_prompt_value(context_pack, max_chars=_VERIFIER_PROMPT_VALUE_CHARS)
        return compact if isinstance(compact, dict) else {}

    @classmethod
    def _compact_verifier_evidence_summary(cls, summary: Mapping[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, value in summary.items():
            if key == "artifact_refs" and isinstance(value, list):
                compact[key] = [cls._compact_artifact_ref_for_verifier(ref) for ref in value[:24]]
                if len(value) > 24:
                    compact[key].append({"omitted": len(value) - 24, "reason": "prompt_budget"})
                continue
            if key == "workspace_refs" and isinstance(value, Mapping):
                compact[key] = cls._compact_verifier_prompt_value(value, max_chars=2400)
                continue
            compact[key] = cls._compact_verifier_prompt_value(value, max_chars=_VERIFIER_PROMPT_ITEM_CHARS)
        return compact

    @classmethod
    def _compact_artifact_ref_for_verifier(cls, ref: Any) -> Any:
        if not isinstance(ref, Mapping):
            return cls._compact_verifier_prompt_value(ref, max_chars=600)
        keep_keys = (
            "artifact_id",
            "action_call_id",
            "role",
            "label",
            "media_type",
            "size",
            "bytes",
            "sha256",
            "truncated",
            "available",
            "full_value_available",
            "path",
        )
        compact = {key: ref.get(key) for key in keep_keys if key in ref}
        if "preview" in ref:
            compact["preview"] = cls._compact_verifier_prompt_value(ref.get("preview"), max_chars=600)
        return compact

    @classmethod
    def _compact_verifier_prompt_value(
        cls,
        value: Any,
        *,
        max_chars: int = _VERIFIER_PROMPT_ITEM_CHARS,
        depth: int = 0,
    ) -> Any:
        value = DataFormatter.sanitize(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, bytes):
            text = value[:max_chars].decode("utf-8", "replace")
            return {"bytes": len(value), "preview": cls._truncate_prompt_text(text, max_chars)}
        if isinstance(value, str):
            return cls._truncate_prompt_text(value, max_chars)
        if depth >= 5:
            return cls._truncate_prompt_text(value, max_chars)
        if isinstance(value, list):
            limit = 24
            items = [
                cls._compact_verifier_prompt_value(
                    item,
                    max_chars=max(240, max_chars // 2),
                    depth=depth + 1,
                )
                for item in value[:limit]
            ]
            if len(value) > limit:
                items.append({"omitted": len(value) - limit, "reason": "prompt_budget"})
            return items
        if isinstance(value, dict):
            limit = 48
            compacted: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= limit:
                    compacted["omitted"] = {"count": len(value) - limit, "reason": "prompt_budget"}
                    break
                key_text = str(key)
                item_chars = max_chars
                if key_text in {"content", "raw", "text", "output", "result", "data", "body", "preview"}:
                    item_chars = max(240, max_chars // 3)
                compacted[key_text] = cls._compact_verifier_prompt_value(
                    item,
                    max_chars=item_chars,
                    depth=depth + 1,
                )
            return compacted
        return cls._truncate_prompt_text(value, max_chars)

    @staticmethod
    def _truncate_prompt_text(value: Any, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 32)].rstrip() + "\n[truncated for verifier prompt]"

    def _normalize_verification(
        self,
        verification: dict[str, Any],
        *,
        execution_evidence_summary: dict[str, Any],
        candidate_final_result: str = "",
    ) -> dict[str, Any]:
        normalized: dict[str, Any] = {
            "is_complete": self._normalize_bool(verification.get("is_complete"), default=False),
            "requires_block": self._normalize_bool(verification.get("requires_block"), default=False),
            "reason": str(verification.get("reason") or ""),
            "missing_criteria": self._normalize_string_list(verification.get("missing_criteria")),
            "replan_instruction": str(verification.get("replan_instruction") or ""),
            "repair_constraints": self._normalize_string_list(verification.get("repair_constraints")),
            "next_step_requirements": self._normalize_string_list(verification.get("next_step_requirements")),
            "final_result": str(verification.get("final_result") or ""),
        }
        guard_reasons: list[str] = []
        if normalized["requires_block"]:
            normalized["is_complete"] = False
            guard_reasons.append("requires_block_true")
        if normalized["missing_criteria"]:
            normalized["is_complete"] = False
            guard_reasons.append("missing_criteria_present")
        execution_status = str(execution_evidence_summary.get("status") or "").strip().lower()
        if execution_status in {"failed", "error", "timed_out", "blocked"}:
            normalized["is_complete"] = False
            guard_reasons.append("execution_status_failed")
            execution_errors = execution_evidence_summary.get("errors", [])
            error_message = ""
            if isinstance(execution_errors, list) and execution_errors:
                first_error = execution_errors[0]
                if isinstance(first_error, dict):
                    error_message = str(first_error.get("message") or first_error.get("type") or "")
                else:
                    error_message = str(first_error)
            detail = f": {error_message}" if error_message else ""
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Execution step status is {execution_status}{detail}.",
            ]
        replan_signals = [
            dict(signal)
            for signal in execution_evidence_summary.get("replan_signals", [])
            if isinstance(signal, dict)
        ]
        if replan_signals:
            normalized["replan_signals"] = replan_signals
        blocking_signal = next(
            (signal for signal in replan_signals if str(signal.get("status") or "") == "blocked"),
            None,
        )
        if blocking_signal is not None:
            normalized["is_complete"] = False
            normalized["requires_block"] = True
            guard_reasons.append("structured_replan_signal_blocked")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                str(blocking_signal.get("reason") or "Execution emitted a blocked ReplanSignal."),
            ]
        actionable_signals = [
            signal
            for signal in replan_signals
            if str(signal.get("status") or "") in {"repair", "replan_segment", "replan_goal", "clarify"}
        ]
        if actionable_signals:
            normalized["is_complete"] = False
            guard_reasons.append("structured_replan_signal")
            if not normalized["replan_instruction"]:
                reasons = [
                    str(signal.get("reason") or signal.get("status") or "")
                    for signal in actionable_signals
                    if str(signal.get("reason") or signal.get("status") or "").strip()
                ]
                normalized["replan_instruction"] = (
                    "Handle structured ReplanSignal before accepting completion"
                    + (f": {'; '.join(reasons)}." if reasons else ".")
                )
        risky_actions = [
            *self._normalize_string_list(execution_evidence_summary.get("failed_actions")),
            *self._normalize_string_list(execution_evidence_summary.get("blocked_actions")),
            *self._normalize_string_list(execution_evidence_summary.get("approval_required_actions")),
        ]
        if risky_actions:
            normalized["is_complete"] = False
            guard_reasons.append("execution_risk_actions_present")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Unresolved execution risk actions: {', '.join(risky_actions)}",
            ]
        # Accumulate satisfied required capabilities across iterations, then guard
        # on what is still missing for the whole task rather than per-step.
        self._satisfied_required_actions.update(
            self._normalize_string_list(execution_evidence_summary.get("action_ids"))
        )
        self._satisfied_required_skills.update(
            self._normalize_string_list(execution_evidence_summary.get("selected_skill_ids"))
        )
        self._satisfied_capabilities.update(
            self._normalize_string_list(execution_evidence_summary.get("capabilities_used"))
        )
        capability_evidence = execution_evidence_summary.get("capability_evidence")
        if isinstance(capability_evidence, dict) and isinstance(capability_evidence.get("actions"), dict):
            self._satisfied_succeeded_actions.update(
                self._normalize_string_list(capability_evidence["actions"].get("succeeded"))
            )
        required_actions = self._normalize_string_list(execution_evidence_summary.get("required_actions"))
        required_skills = self._normalize_string_list(execution_evidence_summary.get("required_skills"))
        missing_required = [
            *[action_id for action_id in required_actions if action_id not in self._satisfied_required_actions],
            *[skill_id for skill_id in required_skills if skill_id not in self._satisfied_required_skills],
        ]
        if missing_required:
            normalized["is_complete"] = False
            guard_reasons.append("required_capability_evidence_missing")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Missing required capability evidence: {', '.join(missing_required)}",
            ]
        # Load-bearing structured capability-evidence gate
        # (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC). A deterministic
        # correspondence check between authored, structured requirements (which
        # capabilities must appear in execution evidence) and the accumulated
        # capability/action evidence. It is independent of capability mode: a
        # model_decision skill (or any capability) the goal depends on can be
        # required as evidence here WITHOUT forcing routing. No prose or model
        # reading participates in the pass/fail decision. Only the kinds with a
        # deterministic check are enforced; reserved kinds are recorded as
        # unenforced diagnostics for the advisory model verifier.
        missing_capability_evidence, unenforced_requirements = self._evaluate_capability_evidence()
        if missing_capability_evidence:
            normalized["is_complete"] = False
            guard_reasons.append("capability_evidence_missing")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Missing required capability evidence: {', '.join(missing_capability_evidence)}",
            ]
            missing_required = [*missing_required, *missing_capability_evidence]
        if unenforced_requirements:
            self.diagnostics.setdefault("unenforced_evidence_requirements", []).extend(unenforced_requirements)
        normalized["missing_required_capabilities"] = missing_required
        normalized["missing_capability_evidence"] = missing_capability_evidence
        # Surface unenforced requirements so a reserved or not-yet-wired evidence
        # kind is visible rather than a silent no-op (it does not block).
        normalized["unenforced_evidence_requirements"] = unenforced_requirements
        final_result_required = self._normalize_bool(
            verification.get("final_result_required"), default=False
        )
        normalized["final_result_required"] = final_result_required
        if normalized["is_complete"] and final_result_required and not normalized["final_result"].strip():
            if candidate_final_result.strip():
                normalized["final_result"] = candidate_final_result.strip()
            else:
                normalized["is_complete"] = False
                guard_reasons.append("final_result_missing")
                normalized["missing_criteria"] = [
                    *normalized["missing_criteria"],
                    "Final result is missing.",
                ]
        if guard_reasons:
            normalized["guard_reasons"] = guard_reasons
            if not normalized["replan_instruction"]:
                normalized["replan_instruction"] = "Run another bounded step and produce explicit evidence for the guarded criteria."
            self.diagnostics.setdefault("verification_guards", []).append(
                {
                    "task_id": self.id,
                    "guard_reasons": guard_reasons,
                    "missing_criteria": normalized["missing_criteria"],
                }
            )
        normalized["repair_constraints"] = self._merge_string_lists(
            normalized.get("repair_constraints"),
            normalized.get("missing_criteria"),
        )
        normalized["next_step_requirements"] = self._merge_string_lists(
            normalized.get("next_step_requirements"),
            [normalized.get("replan_instruction")] if normalized.get("replan_instruction") else [],
        )
        for key, value in verification.items():
            normalized.setdefault(key, DataFormatter.sanitize(value))
        return normalized

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, tuple):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @classmethod
    def _merge_string_lists(cls, *values: Any) -> list[str]:
        merged: list[str] = []
        for value in values:
            for item in cls._normalize_string_list(value):
                if item not in merged:
                    merged.append(item)
        return merged

    async def _await_task_request(self, awaitable, *, stage: str):
        timeout = self._task_request_timeout()
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError as error:
            reason = f"AgentTask {stage} request timed out after {timeout} seconds."
            raise _AgentTaskDeadlineExceeded(
                stage,
                reason=reason,
                limit_name="request_timeout_seconds",
                timeout_seconds=timeout,
            ) from error

    def _task_request_timeout(self) -> float | None:
        # Per plan/verify request timeout. max_seconds is the task wall-clock
        # budget (enforced separately in the loop) and must not be reused as a
        # per-request timeout; the no-progress idle limit is the closest fallback.
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict):
            configured = agent_task_options.get("request_timeout_seconds")
            if configured is not None:
                return self._normalize_timeout(configured)
        configured = self.options.get("request_timeout_seconds")
        if configured is not None:
            return self._normalize_timeout(configured)
        configured = self.limits.get("max_no_progress_seconds")
        if configured is not None:
            return self._normalize_timeout(configured)
        return None

    def _task_max_seconds(self) -> float | None:
        return self._normalize_timeout(self.limits.get("max_seconds"))

    def _task_deadline_exceeded(self) -> bool:
        remaining = self._task_deadline_remaining()
        return remaining is not None and remaining <= 0

    def _task_deadline_remaining(self) -> float | None:
        max_seconds = self._task_max_seconds()
        if max_seconds is None or self.started_at is None:
            return None
        return max_seconds - (time.time() - self.started_at)

    async def _await_task_deadline(self, awaitable: Awaitable[Any], *, stage: str) -> Any:
        remaining = self._task_deadline_remaining()
        if remaining is None:
            return await awaitable
        if remaining <= 0:
            self._close_unawaited(awaitable)
            raise _AgentTaskDeadlineExceeded(
                stage,
                limit_name="max_seconds",
                timeout_seconds=self._task_max_seconds(),
            )
        task = asyncio.ensure_future(awaitable)
        done, pending = await asyncio.wait({task}, timeout=remaining)
        if pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            raise _AgentTaskDeadlineExceeded(
                stage,
                limit_name="max_seconds",
                timeout_seconds=self._task_max_seconds(),
            )
        return await next(iter(done))

    @staticmethod
    def _close_unawaited(awaitable: Awaitable[Any]) -> None:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
            return
        cancel = getattr(awaitable, "cancel", None)
        if callable(cancel):
            cancel()

    @staticmethod
    def _normalize_timeout(value: Any) -> float | None:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return None
        if timeout < 0:
            return None
        return timeout

    # ── Durable resume ──

    @staticmethod
    def _resume_run_id(task_id: str) -> str:
        # Namespaced so resume snapshots never mix with the task's per-step
        # observation checkpoints under the bare task_id.
        return f"{ task_id }::resume"

    def _resume_manifest(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "execution_strategy": self.execution_strategy,
            "max_iterations": self.max_iterations,
            "verify": self.verify,
            "context_profile": self.context_profile,
            "context_budget": DataFormatter.sanitize(self.context_budget),
            "limits": DataFormatter.sanitize(self.limits),
            "options": DataFormatter.sanitize(self.options),
        }

    async def _write_resume_snapshot(self, iteration_index: int, verification: dict[str, Any]) -> None:
        """Persist a resumable snapshot keyed by task_id after an iteration.

        Stores the task manifest, the last completed iteration, the bounded
        iteration summaries, the cumulative satisfied-capability sets, and the
        last verification outcome so a crashed task can continue (or report its
        terminal result) from a fresh process.
        """
        try:
            await self.workspace.put_snapshot(
                self._resume_run_id(self.id),
                DataFormatter.sanitize(
                    {
                        "resume_version": 1,
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "manifest": self._resume_manifest(),
                        "iterations_summary": self._iteration_prompt_summaries(),
                        "satisfied_required_actions": sorted(self._satisfied_required_actions),
                        "satisfied_required_skills": sorted(self._satisfied_required_skills),
                        "satisfied_capabilities": sorted(self._satisfied_capabilities),
                        "satisfied_succeeded_actions": sorted(self._satisfied_succeeded_actions),
                        "failed_execution_shapes": sorted(self._failed_execution_shapes),
                        "last_verification": {
                            "is_complete": bool(verification.get("is_complete")),
                            "requires_block": bool(verification.get("requires_block")),
                            "reason": str(verification.get("reason") or ""),
                            "final_result": str(verification.get("final_result") or ""),
                        },
                    }
                ),
                step_id=f"iteration-{ iteration_index }",
            )
        except Exception as error:
            # Snapshot persistence must never break the task loop.
            self.diagnostics.setdefault("resume_snapshot_errors", []).append(
                {"type": error.__class__.__name__, "message": str(error)}
            )

    @classmethod
    async def async_resume(
        cls,
        agent: "BaseAgent",
        task_id: str,
        *,
        workspace: str | os.PathLike[str] | None = None,
    ) -> "AgentTask":
        """Rebuild an AgentTask from its latest durable snapshot.

        The returned task continues from the iteration after the last completed
        one (or, when the last snapshot was already terminal, exposes that
        terminal result without re-running). Completed iterations are not
        re-executed, so their side effects are not repeated; an iteration that
        was in flight at crash time is re-planned.
        """
        agent_any = cast(Any, agent)
        if workspace is not None:
            agent_any.use_workspace(workspace)
        bound_workspace = getattr(agent, "workspace", None)
        if bound_workspace is None:
            raise RuntimeError(
                "AgentTask.async_resume requires a Workspace binding. Pass workspace=... "
                "or call agent.use_workspace(...) before resuming."
            )
        state = await bound_workspace.get_snapshot(cls._resume_run_id(str(task_id)))
        manifest = state.get("manifest") if isinstance(state, dict) else None
        if not isinstance(manifest, dict) or not manifest.get("goal"):
            raise ValueError(
                f"No resumable AgentTask snapshot was found for task_id '{ task_id }'."
            )
        task = cls(
            agent,
            goal=str(manifest.get("goal") or ""),
            success_criteria=list(manifest.get("success_criteria") or []),
            execution=cast(Any, manifest.get("execution_strategy", "auto")),
            workspace=workspace,
            max_iterations=int(manifest.get("max_iterations") or 3),
            verify=cast(Any, manifest.get("verify", "before_done")),
            context_profile=str(manifest.get("context_profile", "auto")),
            context_budget=cast(Any, manifest.get("context_budget")),
            limits=cast(Any, manifest.get("limits")),
            options=cast(Any, manifest.get("options")),
            task_id=str(task_id),
        )
        task._resumed_from_iteration = int(state.get("iteration") or 0)
        summaries = state.get("iterations_summary")
        task._resumed_iteration_summaries = list(summaries) if isinstance(summaries, list) else []
        task._satisfied_required_actions = set(
            cls._normalize_string_list(state.get("satisfied_required_actions"))
        )
        task._satisfied_required_skills = set(
            cls._normalize_string_list(state.get("satisfied_required_skills"))
        )
        task._satisfied_capabilities = set(
            cls._normalize_string_list(state.get("satisfied_capabilities"))
        )
        task._satisfied_succeeded_actions = set(
            cls._normalize_string_list(state.get("satisfied_succeeded_actions"))
        )
        task._failed_execution_shapes = set(
            cls._normalize_string_list(state.get("failed_execution_shapes"))
        )
        last_verification = state.get("last_verification")
        if isinstance(last_verification, dict):
            task._resumed_prior_result = cls._terminal_result_from_resume(
                task_id=str(task_id),
                resumed_from_iteration=task._resumed_from_iteration,
                last_verification=last_verification,
            )
        return task

    def resume(self, *args: Any, **kwargs: Any):
        raise TypeError("Use the async classmethod AgentTask.async_resume(agent, task_id, ...).")

    @staticmethod
    def _terminal_result_from_resume(
        *,
        task_id: str,
        resumed_from_iteration: int,
        last_verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        if bool(last_verification.get("is_complete")):
            return {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "task_id": task_id,
                "final_result": last_verification.get("final_result") or "",
                "iterations": resumed_from_iteration,
                "resumed": True,
            }
        if bool(last_verification.get("requires_block")):
            return {
                "status": "blocked",
                "accepted": False,
                "artifact_status": "blocked",
                "task_id": task_id,
                "reason": last_verification.get("reason") or "Verifier blocked the task.",
                "iterations": resumed_from_iteration,
                "resumed": True,
            }
        return None

    async def _record_decision(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPackage",
    ) -> "WorkspaceRecordRef":
        record_ref = await self.workspace.ingest(
            content={
                "iteration": iteration_index,
                "plan": DataFormatter.sanitize(plan),
                "context_pack_diagnostics": DataFormatter.sanitize(context_pack.get("diagnostics", {})),
                "context_item_count": len(context_pack.get("items", [])),
            },
            collection="decisions",
            kind="agent_task_decision",
            summary=f"{self.id} iteration {iteration_index} planning decision",
            scope={"task_id": self.id, "iteration": iteration_index},
            source={"type": "agent_task", "phase": "plan"},
            meta={"task_id": self.id, "iteration": iteration_index},
        )
        self._append_workspace_ref("decisions", record_ref)
        return record_ref

    async def _record_observation(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        decision_ref: "WorkspaceRecordRef",
        execution_result: Any,
        execution_meta: dict[str, Any],
    ) -> tuple["WorkspaceRecordRef", "WorkspaceRecordRef | None"]:
        record_ref = await self.workspace.ingest(
            content={
                "iteration": iteration_index,
                "plan": DataFormatter.sanitize(plan),
                "decision_ref": decision_ref,
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_meta": DataFormatter.sanitize(execution_meta),
            },
            collection="observations",
            kind="agent_task_observation",
            summary=f"{self.id} iteration {iteration_index} execution observation",
            scope={"task_id": self.id, "iteration": iteration_index},
            source={"type": "agent_task", "phase": "execute", "execution_id": execution_meta.get("execution_id")},
            meta={"task_id": self.id, "iteration": iteration_index},
        )
        checkpoint_ref = await self.workspace.put_checkpoint(
            self.id,
            {
                "task_id": self.id,
                "iteration": iteration_index,
                "status": self.status,
                "decision_ref": decision_ref,
                "observation_ref": record_ref,
            },
            step_id=f"iteration-{iteration_index}",
        )
        decision_link = await self.workspace.link_evidence(
            record_ref,
            decision_ref,
            relation="implements_decision",
            execution_id=str(execution_meta.get("execution_id") or "") or None,
            checkpoint_id=checkpoint_ref.get("id"),
            meta={"owner": "AgentTask", "task_id": self.id, "iteration": iteration_index},
        )
        checkpoint_link = await self.workspace.link_evidence(
            record_ref,
            checkpoint_ref,
            relation="checkpointed_by",
            execution_id=str(execution_meta.get("execution_id") or "") or None,
            checkpoint_id=checkpoint_ref.get("id"),
            meta={"owner": "AgentTask", "task_id": self.id, "iteration": iteration_index},
        )
        self._append_workspace_ref("observations", record_ref)
        self._append_workspace_ref("checkpoints", checkpoint_ref)
        self._append_workspace_ref("evidence_links", decision_link)
        self._append_workspace_ref("evidence_links", checkpoint_link)
        await self._emit(
            "agent_task.checkpoint",
            {"iteration": iteration_index, "checkpoint": checkpoint_ref},
        )
        return record_ref, checkpoint_ref

    async def _record_verification(
        self,
        iteration_index: int,
        verification: dict[str, Any],
        observation_ref: "WorkspaceRecordRef",
    ) -> "WorkspaceRecordRef":
        record_ref = await self.workspace.ingest(
            content={
                "iteration": iteration_index,
                "verification": DataFormatter.sanitize(verification),
                "observation_ref": observation_ref,
            },
            collection="verification",
            kind="agent_task_verification",
            summary=f"{self.id} iteration {iteration_index} verification",
            scope={"task_id": self.id, "iteration": iteration_index},
            source={"type": "agent_task", "phase": "verify"},
            meta={"task_id": self.id, "iteration": iteration_index},
        )
        evidence_link = await self.workspace.link_evidence(
            record_ref,
            observation_ref,
            relation="verifies_observation",
            meta={"owner": "AgentTask", "task_id": self.id, "iteration": iteration_index},
        )
        self._append_workspace_ref("verification", record_ref)
        self._append_workspace_ref("evidence_links", evidence_link)
        return record_ref

    def _append_workspace_ref(self, collection: str, ref: dict[str, Any] | None):
        if not ref:
            return
        bucket = self.workspace_refs.setdefault(collection, [])
        ref_id = str(ref.get("id") or "")
        if ref_id and ref_id not in bucket:
            bucket.append(ref_id)

    async def async_meta(self) -> dict[str, Any]:
        if not self._completed:
            await self.async_run()
        return {
            "task_id": self.id,
            "status": self.status,
            "goal": self.goal,
            "success_criteria": DataFormatter.sanitize(self.success_criteria),
            "execution_strategy": self.execution_strategy,
            "max_iterations": self.max_iterations,
            "iterations": DataFormatter.sanitize(self.iterations),
            "resumed_from_iteration": self._resumed_from_iteration,
            "resumed_iteration_summaries": DataFormatter.sanitize(self._resumed_iteration_summaries),
            "result": DataFormatter.sanitize(self.result),
            "diagnostics": DataFormatter.sanitize(self.diagnostics),
            "workspace_refs": DataFormatter.sanitize(self.workspace_refs),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def _meta(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.async_meta())
        return self.async_meta()

    async def get_async_generator(self, *_, **__) -> AsyncGenerator[AgentExecutionStreamData, None]:
        if self._completed:
            for item in self._stream_items:
                yield item
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in self._stream_items:
            await queue.put(item)
        self._stream_queues.append(queue)
        start_task = asyncio.create_task(self.async_run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
            await start_task
        finally:
            if queue in self._stream_queues:
                self._stream_queues.remove(queue)

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[AgentExecutionStreamData, None, None]:
        return FunctionShifter.syncify_async_generator(self.get_async_generator(*args, **kwargs))

    async def _emit_progress(
        self,
        iteration: int | None,
        stage: str,
        message: str,
    ) -> AgentExecutionStreamData | None:
        if not self._stream_progress_enabled():
            return None
        if self._progress_model_key() is not None:
            return None
        emit_coro = self._emit(
            f"agent_task.progress.{stage}" if iteration is None else f"agent_task.iteration.{iteration}.progress.{stage}",
            {
                "message": message,
                "iteration": iteration,
                "stage": stage,
                "status": self.status,
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "progress",
                "progress_source": "template",
            },
        )
        if self._stream_progress_background_enabled():
            task = asyncio.create_task(emit_coro)
            self._track_background_stream_task(task)
            return None
        return await emit_coro

    async def _emit_snapshot(
        self,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
        *,
        message: str,
    ) -> AgentExecutionStreamData | None:
        if not self._stream_snapshots_enabled():
            return None
        item = await self._emit(
            f"agent_task.iteration.{iteration}.snapshot.{stage}",
            {
                "message": message,
                "iteration": iteration,
                "stage": stage,
                "snapshot": snapshot,
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "snapshot",
            },
        )
        self._schedule_model_progress_from_snapshot(
            iteration=iteration,
            stage=stage,
            snapshot=snapshot,
        )
        return item

    def _agent_task_option(self, key: str, default: Any = None) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict) and key in agent_task_options:
            return agent_task_options.get(key)
        return self.options.get(key, default)

    def _stream_progress_enabled(self) -> bool:
        return self._normalize_bool(self._agent_task_option("stream_progress", False), default=False)

    def _stream_progress_background_enabled(self) -> bool:
        return self._normalize_bool(
            self._agent_task_option("stream_progress_background", True),
            default=True,
        )

    def _stream_snapshots_enabled(self) -> bool:
        return self._normalize_bool(self._agent_task_option("stream_snapshots", True), default=True)

    def _progress_model_key(self) -> str | None:
        model_key = (
            self._agent_task_option("progress_model_key", None)
            or self._agent_task_option("stream_progress_model_key", None)
        )
        if model_key is None:
            return None
        normalized = str(model_key).strip()
        return normalized or None

    def _progress_timeout_seconds(self) -> float:
        timeout = self._agent_task_option("progress_timeout_seconds", 20)
        normalized = self._normalize_timeout(timeout)
        return 20.0 if normalized is None else normalized

    def _schedule_model_progress_from_snapshot(
        self,
        *,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
    ) -> None:
        if not self._stream_progress_enabled():
            return
        model_key = self._progress_model_key()
        if model_key is None:
            return
        task = asyncio.create_task(
            self._emit_model_progress_from_snapshot(
                iteration=iteration,
                stage=stage,
                snapshot=self._operator_safe_progress_snapshot(stage, DataFormatter.sanitize(snapshot)),
                model_key=model_key,
            )
        )
        self._track_background_stream_task(task)

    async def _emit_model_progress_from_snapshot(
        self,
        *,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
        model_key: str,
    ) -> AgentExecutionStreamData | None:
        try:
            request = self.agent.create_temp_request(model_key=model_key)
            progress_language = self._progress_language()
            request.set_settings("runtime.side_channel", True)
            request.set_settings("model_request.side_channel", True)
            request.input(
                {
                    "task_id": self.id,
                    "goal": self.goal,
                    "success_criteria": self.success_criteria,
                    "iteration": iteration,
                    "stage": stage,
                    "status": self.status,
                    "progress_language": progress_language,
                    "snapshot": snapshot,
                }
            )
            request.instruct(
                "Summarize AgentTask progress for a human operator using only the provided snapshot and task metadata. "
                "Do not add new facts, do not infer hidden results, and keep the message concise. "
                f"Write the message in this language: { progress_language }."
            )
            request.output(
                {
                    "message": (str, "One concise natural-language progress update.", True),
                },
                format="json",
            )
            result = request.get_result()
            streamed_message = ""
            final_stream_value = ""
            async for item in result.get_async_generator(type="instant"):
                raw_path = str(getattr(item, "path", "") or getattr(item, "wildcard_path", "") or "")
                if raw_path != "message" and not raw_path.endswith(".message"):
                    continue
                delta = getattr(item, "delta", None)
                value = getattr(item, "value", None)
                event_type = getattr(item, "event_type", None)
                if isinstance(delta, str) and delta:
                    streamed_message += delta
                    await self._emit_progress_delta(
                        iteration=iteration,
                        stage=stage,
                        delta=delta,
                        message_so_far=streamed_message,
                        model_key=model_key,
                        language=progress_language,
                    )
                elif event_type == "delta" and isinstance(value, str) and value:
                    suffix = value[len(streamed_message):] if value.startswith(streamed_message) else value
                    if suffix:
                        streamed_message += suffix
                        await self._emit_progress_delta(
                            iteration=iteration,
                            stage=stage,
                            delta=suffix,
                            message_so_far=streamed_message,
                            model_key=model_key,
                            language=progress_language,
                        )
                elif bool(getattr(item, "is_complete", False)) and isinstance(value, str):
                    final_stream_value = value
            raw = await asyncio.wait_for(result.async_get_data(), timeout=self._progress_timeout_seconds())
            message = ""
            if isinstance(raw, dict):
                message = str(raw.get("message") or "")
            else:
                message = str(raw or "")
            if not message.strip() and final_stream_value.strip():
                message = final_stream_value
            if not message.strip() and streamed_message.strip():
                message = streamed_message
            if not message.strip():
                return None
            return await self._emit(
                f"agent_task.iteration.{iteration}.progress.{stage}",
                {
                    "message": message.strip(),
                    "iteration": iteration,
                    "stage": stage,
                    "status": self.status,
                    "language": progress_language,
                },
                meta={
                    "task_id": self.id,
                    "status": self.status,
                    "iteration": iteration,
                    "stage": stage,
                    "stream_kind": "progress",
                    "progress_source": "model",
                    "progress_model_key": model_key,
                    "progress_language": progress_language,
                },
            )
        except Exception as error:
            self.diagnostics.setdefault("progress_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": str(error),
                    "iteration": iteration,
                    "stage": stage,
                    "model_key": model_key,
                }
            )
            return None

    async def _emit_progress_delta(
        self,
        *,
        iteration: int,
        stage: str,
        delta: str,
        message_so_far: str,
        model_key: str,
        language: str,
    ) -> AgentExecutionStreamData:
        return await self._emit(
            f"agent_task.iteration.{iteration}.progress.{stage}.message",
            {
                "message": message_so_far,
                "iteration": iteration,
                "stage": stage,
                "status": self.status,
                "language": language,
            },
            event_type="delta",
            delta=delta,
            is_complete=False,
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "progress_delta",
                "progress_source": "model",
                "progress_model_key": model_key,
                "progress_language": language,
            },
        )

    def _progress_language(self) -> str:
        language = (
            self._agent_task_option("progress_language", None)
            or self._agent_task_option("stream_progress_language", None)
        )
        if language is None:
            getter = getattr(getattr(self.agent, "settings", None), "get", None)
            if callable(getter):
                language = getter("agent_task.progress.language", "auto")
        normalized = str(language or "auto").strip()
        return normalized or "auto"

    @staticmethod
    def _normalize_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on", "enabled"}:
                return True
            if lowered in {"0", "false", "no", "off", "disabled"}:
                return False
        return bool(value)

    def _track_background_stream_task(self, task: asyncio.Task[Any]) -> None:
        self._background_stream_tasks.add(task)

        def discard(done_task: asyncio.Task[Any]) -> None:
            self._background_stream_tasks.discard(done_task)
            try:
                error = done_task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self.diagnostics.setdefault("stream_errors", []).append(
                    {"type": error.__class__.__name__, "message": str(error)}
                )

        task.add_done_callback(discard)

    async def _cancel_background_stream_tasks(self) -> None:
        if not self._background_stream_tasks:
            return
        tasks = list(self._background_stream_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    def _operator_safe_progress_snapshot(cls, stage: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], cls._strip_developer_diagnostics({"stage": stage, **snapshot}))

    # Keys whose subtrees carry developer-facing diagnostics (errors, recall
    # fallbacks, backend traces) and must never reach an operator-facing progress
    # summary. Stripping is structural by key — no backend-specific error-string
    # matching — so it stays correct for any Workspace backend or recall source.
    _DEVELOPER_DIAGNOSTIC_KEYS = frozenset({
        "diagnostics",
        "context_pack_diagnostics",
        "fallback_reason",
        "errors",
        "progress_errors",
        "stream_errors",
    })

    @classmethod
    def _strip_developer_diagnostics(cls, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key)
                if normalized_key in cls._DEVELOPER_DIAGNOSTIC_KEYS or "diagnostic" in normalized_key:
                    cleaned[normalized_key] = {"omitted": "developer_diagnostics"}
                    continue
                cleaned[normalized_key] = cls._strip_developer_diagnostics(item)
            return cleaned
        if isinstance(value, list):
            return [cls._strip_developer_diagnostics(item) for item in value]
        return value

    @staticmethod
    def _execution_log_summary(execution_meta: dict[str, Any]) -> dict[str, Any]:
        logs = execution_meta.get("logs", {})
        if not isinstance(logs, dict):
            logs = {}
        action_records = AgentTask._collect_action_records(logs)
        action_ids = [record["id"] for record in action_records if record.get("id")]
        action_statuses = {
            record["id"]: record.get("status", "")
            for record in action_records
            if record.get("id")
        }
        # Risk status is judged by the final status per action id (last record
        # wins), so an action that failed and then succeeded within the same step
        # is treated as recovered and does not block verification.
        failed_actions = AgentTask._action_ids_by_final_status(action_statuses, {"failed", "failure", "error"})
        blocked_actions = AgentTask._action_ids_by_final_status(action_statuses, {"blocked"})
        approval_required_actions = AgentTask._action_ids_by_final_status(action_statuses, {"approval_required"})
        required_actions, required_skills = AgentTask._required_capability_constraints(execution_meta)
        missing_required_actions = [action_id for action_id in required_actions if action_id not in action_ids]
        selected_skill_ids = AgentTask._selected_skill_ids(logs)
        missing_required_skills = [
            skill_id
            for skill_id in required_skills
            if skill_id not in selected_skill_ids
        ]
        succeeded_actions = AgentTask._action_ids_by_final_status(
            action_statuses, {"success", "succeeded", "partial_success"}
        )
        route = execution_meta.get("route", {})
        artifact_refs = logs.get("artifact_refs", [])
        workspace_refs = execution_meta.get("workspace_refs") or logs.get("workspace_refs", {})
        raw_errors = logs.get("errors", [])
        execution_errors: list[Any]
        if isinstance(raw_errors, list):
            execution_errors = raw_errors
        elif raw_errors:
            execution_errors = [raw_errors]
        else:
            execution_errors = []
        diagnostics = execution_meta.get("diagnostics", {})
        if isinstance(diagnostics, dict) and diagnostics.get("execution_error"):
            execution_errors.append(diagnostics["execution_error"])
        replan_signals = AgentTask._collect_replan_signals(execution_meta)
        # Unified capability-evidence view (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # one capability id space across kinds plus per-kind evidence buckets. A
        # capability is "used" when it ran (action) or was selected (skill). The
        # artifacts/validations buckets are reserved: no structural producer feeds
        # them yet, so the verifier guard does not enforce those evidence kinds.
        capabilities_used: list[str] = []
        for capability_id in [*action_ids, *selected_skill_ids]:
            if capability_id and capability_id not in capabilities_used:
                capabilities_used.append(capability_id)
        return {
            "model_response_count": len(logs.get("model_responses", [])) if isinstance(logs.get("model_responses", []), list) else 0,
            "action_log_count": len(action_ids),
            "action_ids": action_ids,
            "action_statuses": action_statuses,
            "actions": action_records,
            "failed_actions": failed_actions,
            "blocked_actions": blocked_actions,
            "approval_required_actions": approval_required_actions,
            "required_actions": required_actions,
            "missing_required_actions": missing_required_actions,
            "selected_skill_ids": selected_skill_ids,
            "required_skills": required_skills,
            "missing_required_skills": missing_required_skills,
            "capabilities_used": capabilities_used,
            "capability_evidence": {
                "actions": {"succeeded": succeeded_actions, "failed": failed_actions},
                "skills": {"selected": selected_skill_ids},
                "artifacts": {"readback": []},
                "validations": {"passed": [], "failed": []},
            },
            "artifact_refs": DataFormatter.sanitize(artifact_refs),
            "workspace_refs": DataFormatter.sanitize(workspace_refs),
            "route": DataFormatter.sanitize(route),
            "status": str(execution_meta.get("status") or ""),
            "errors": DataFormatter.sanitize(execution_errors),
            "replan_signals": DataFormatter.sanitize(replan_signals),
        }

    @staticmethod
    def _collect_replan_signals(execution_meta: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_values: list[Any] = []
        direct_signal = execution_meta.get("replan_signal")
        if direct_signal is not None:
            raw_values.append(direct_signal)
        direct_signals = execution_meta.get("replan_signals")
        if isinstance(direct_signals, (list, tuple)):
            raw_values.extend(direct_signals)
        blocks = execution_meta.get("blocks")
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                diagnostics = evidence.get("diagnostics")
                if isinstance(diagnostics, (list, tuple)):
                    raw_values.extend(
                        item
                        for item in diagnostics
                        if isinstance(item, Mapping) and item.get("kind") == "replan_signal"
                    )
            snapshot = blocks.get("snapshot")
            snapshot_blocks = snapshot.get("blocks") if isinstance(snapshot, Mapping) else None
            if isinstance(snapshot_blocks, Mapping):
                replan_signals = snapshot_blocks.get("replan_signals")
                if isinstance(replan_signals, (list, tuple)):
                    raw_values.extend(replan_signals)

        signals: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for value in raw_values:
            if not isinstance(value, Mapping):
                continue
            candidate = dict(value)
            if candidate.get("kind") == "replan_signal":
                candidate.pop("kind", None)
            try:
                normalized = ReplanSignal.from_value(candidate).to_dict()
            except Exception as error:
                normalized = {
                    "status": "blocked",
                    "reason": f"Invalid ReplanSignal payload: { error }",
                    "diagnostics": [{"type": error.__class__.__name__, "message": str(error)}],
                }
            key = (str(normalized.get("status") or ""), str(normalized.get("reason") or ""))
            if key not in seen:
                seen.add(key)
                signals.append(normalized)
        return signals

    @staticmethod
    def _required_capability_constraints(execution_meta: dict[str, Any]) -> tuple[list[str], list[str]]:
        constraints = execution_meta.get("effective_options", {})
        if not isinstance(constraints, dict):
            constraints = execution_meta.get("options", {})
        if not isinstance(constraints, dict):
            return [], []
        capability_constraints = constraints.get("capability_constraints", {})
        if not isinstance(capability_constraints, dict):
            return [], []
        actions = capability_constraints.get("actions", {})
        skills = capability_constraints.get("skills", {})
        required_actions = actions.get("required", []) if isinstance(actions, dict) else []
        required_skills = skills.get("required", []) if isinstance(skills, dict) else []
        return (
            AgentTask._normalize_string_list(required_actions),
            AgentTask._normalize_string_list(required_skills),
        )

    @staticmethod
    def _selected_skill_ids(logs: dict[str, Any]) -> list[str]:
        route_logs = logs.get("route_logs", {})
        if not isinstance(route_logs, dict):
            return []
        plan = route_logs.get("plan", {})
        if not isinstance(plan, dict):
            return []
        selected = plan.get("selected_skills", [])
        if not isinstance(selected, list):
            return []
        skill_ids: list[str] = []
        for item in selected:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or item.get("id") or item.get("name") or "").strip()
            if skill_id and skill_id not in skill_ids:
                skill_ids.append(skill_id)
        return skill_ids

    @staticmethod
    def _collect_action_records(logs: dict[str, Any]) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []

        def add_entries(entries: Any) -> None:
            if isinstance(entries, dict):
                for action_id, record in entries.items():
                    if isinstance(record, dict):
                        records.append(AgentTask._compact_action_record(action_id, record))
                    else:
                        records.append({"id": str(action_id), "name": str(action_id), "status": str(record or "")})
            elif isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict):
                        action_id = item.get("action_id") or item.get("id") or item.get("name") or ""
                        records.append(AgentTask._compact_action_record(action_id, item))

        add_entries(logs.get("action_logs", {}))
        route_logs = logs.get("route_logs", {})
        if isinstance(route_logs, dict):
            add_entries(route_logs.get("action_logs", {}))
            route_output = route_logs.get("output", {})
            if isinstance(route_output, dict):
                add_entries(route_output.get("history", []))
        return records

    @staticmethod
    def _compact_action_record(action_id: Any, record: dict[str, Any]) -> dict[str, str]:
        normalized_id = str(action_id or record.get("action_id") or record.get("id") or record.get("name") or "")
        status = str(record.get("status") or "").strip()
        if not status:
            if record.get("error"):
                status = "failed"
            elif "result" in record or "artifact" in record:
                status = "success"
        return {
            "id": normalized_id,
            "name": str(record.get("name") or normalized_id),
            "status": status,
            "action_type": str(record.get("action_type") or record.get("type") or ""),
            "kind": str(record.get("kind") or ""),
        }

    @staticmethod
    def _action_ids_by_status(records: list[dict[str, str]], statuses: set[str]) -> list[str]:
        result: list[str] = []
        for record in records:
            status = record.get("status", "").strip().lower()
            if status in statuses and record.get("id"):
                result.append(record["id"])
        return result

    @staticmethod
    def _action_ids_by_final_status(action_statuses: dict[str, str], statuses: set[str]) -> list[str]:
        return [
            action_id
            for action_id, status in action_statuses.items()
            if action_id and str(status).strip().lower() in statuses
        ]

    async def _record_phase(
        self,
        phase: str,
        *,
        iteration: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        record = {
            "phase": phase,
            "iteration": iteration,
            "status": self.status,
            "diagnostics": DataFormatter.sanitize(diagnostics or {}),
        }
        self.diagnostics.setdefault("phases", []).append(record)
        return await self._emit(
            f"agent_task.phase.{phase}",
            record,
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "phase": phase,
                "stream_kind": "phase",
            },
        )

    async def _emit(
        self,
        path: str,
        value: Any,
        *,
        event_type: Literal["delta", "done"] = "done",
        delta: str | None = None,
        is_complete: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        completed = event_type == "done"
        if is_complete is not None:
            completed = is_complete
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            delta=delta,
            is_complete=completed,
            event_type=event_type,
            source="agent_task",
            task_id=self.id,
            meta=meta or {"task_id": self.id, "status": self.status},
        )
        self._stream_items.append(item)
        # Bound the replay buffer so a very long task does not grow it without
        # limit. Late subscribers replay at most the most recent window.
        if len(self._stream_items) > _STREAM_REPLAY_LIMIT:
            del self._stream_items[: len(self._stream_items) - _STREAM_REPLAY_LIMIT]
        for queue in list(self._stream_queues):
            await queue.put(item)
        return item

    async def _close_streams(self):
        await self._cancel_background_stream_tasks()
        for queue in list(self._stream_queues):
            await queue.put(None)

    def _task_summary(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "execution_strategy": self.execution_strategy,
            "max_iterations": self.max_iterations,
            "verify": self.verify,
        }
