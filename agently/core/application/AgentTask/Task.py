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
import html
import json
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
from agently.types.data import AgentExecutionStreamData, ReplanSignal, TaskBoardCardResult, TaskBoardRevision
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import DataFormatter, FunctionShifter
from agently.utils.LanguagePolicy import (
    apply_language_policy_to_prompt,
    language_policy_from_prompt_snapshot,
    resolve_language_policy,
)

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


def _is_retry_status_marker_source(path: str, value: Any) -> bool:
    return (
        (path == "$status" or path.endswith(".$status"))
        and isinstance(value, Mapping)
        and value.get("status") == "failed"
        and value.get("retry") is True
    )


def _format_retry_marker(value: Any) -> str:
    reason = value.get("reason") if isinstance(value, Mapping) else None
    text = str(reason).strip() if reason is not None else ""
    if not text:
        text = "Retrying model request."
    return f"<$retry>{html.escape(text, quote=False)}</$retry>"

_STEP_EXECUTION_SHAPES = {
    "direct",
    "actions",
    "skills",
    "dynamic_task",
    "execution_dag",
}

_DAG_STEP_EXECUTION_SHAPES = {"dynamic_task", "execution_dag"}
_TASKBOARD_CONTROL_CARD_SHAPES = {
    "control",
    "model_control",
    "synthesis",
    "synthesize",
    "finalize",
    "final",
    "verification",
    "verify",
}
_TASKBOARD_READBACK_CARD_SHAPES = {
    "readback",
    "artifact_readback",
    "cold_readback",
    "evidence_readback",
}
_TASKBOARD_READBACK_PREVIEW_CHARS = 4000
_WORKSPACE_ARTIFACT_PREVIEW_BYTES = 4000

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
        execution_result = await self._deliver_workspace_artifact(
            execution_result,
            plan=plan,
            execution_meta=execution_meta,
            source=f"agent_task.iteration.{iteration_index}.workspace_artifact",
            context_pack=context_pack,
            iteration_index=iteration_index,
            allow_stream_draft=not execution_failed,
        )
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
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
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
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
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
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
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
        lifecycle_flow = TriggerFlow(name=f"agent-task-taskboard-lifecycle-{ self.id }")
        tick_requested_event = f"agent_task.taskboard.lifecycle.tick.requested.{ self.id }"
        finalize_requested_event = f"agent_task.taskboard.lifecycle.finalize.requested.{ self.id }"
        revision_state_key = "taskboard_revision_json"

        def _board_from_revision(revision: TaskBoardRevision | Mapping[str, Any]) -> TaskBoard:
            return TaskBoard(
                revision,
                handler=lambda context: self._run_taskboard_card(context, context_pack),
                planning_policy=planning_result.planning_policy,
            )

        def _pack_revision_state(revision: TaskBoardRevision | Mapping[str, Any]) -> str:
            effective_revision = TaskBoardRevision.from_value(revision)
            return json.dumps(effective_revision.to_dict(), ensure_ascii=False, sort_keys=True)

        def _unpack_revision_state(data: TriggerFlowRuntimeData[Any, Any, Any]) -> TaskBoardRevision:
            raw_revision = data.get_state(revision_state_key, None, inherit=False)
            if isinstance(raw_revision, str) and raw_revision.strip():
                return TaskBoardRevision.from_value(json.loads(raw_revision))
            if isinstance(raw_revision, Mapping):
                return TaskBoardRevision.from_value(raw_revision)
            return TaskBoardRevision.from_value(board.revision)

        async def start_lifecycle(data: TriggerFlowRuntimeData[Any, Any, Any]):
            max_ticks = self._taskboard_max_ticks()
            topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick_requested_event": tick_requested_event,
                "finalize_requested_event": finalize_requested_event,
                "max_ticks": max_ticks,
                "tick_fanout": "taskboard_runtime_signal_net",
            }
            await data.async_set_state(revision_state_key, _pack_revision_state(board.revision), emit=False)
            await data.async_set_state("tick_index", 1, emit=False)
            await data.async_set_state("max_ticks", max_ticks, emit=False)
            await data.async_set_state("runtime_topology", topology, emit=False)
            await self._record_phase(
                "taskboard_lifecycle_started",
                iteration=iteration_index,
                diagnostics={
                    "revision_id": board.revision.revision_id,
                    "runtime_topology": topology,
                },
            )
            await self._emit(
                "agent_task.taskboard.lifecycle.started",
                {
                    "revision_id": board.revision.revision_id,
                    "runtime_topology": topology,
                },
            )
            await data.async_emit_nowait(tick_requested_event, {"tick_index": 1})
            return {"runtime_topology": topology}

        async def run_lifecycle_tick(data: TriggerFlowRuntimeData[Any, Any, Any]):
            if data.get_state("terminal_result", None, inherit=False) is not None:
                return data.get_state("terminal_result", inherit=False)
            if data.get_state("final_result", None, inherit=False) is not None:
                return data.get_state("final_result", inherit=False)
            payload = data.input if isinstance(data.input, Mapping) else {}
            try:
                tick_index = int(payload.get("tick_index") or data.get_state("tick_index", 1, inherit=False) or 1)
            except (TypeError, ValueError):
                tick_index = 1
            max_ticks = data.get_state("max_ticks", self._taskboard_max_ticks(), inherit=False)
            try:
                max_ticks_int = int(max_ticks)
            except (TypeError, ValueError):
                max_ticks_int = self._taskboard_max_ticks()
            if self._task_deadline_exceeded():
                result = await self._terminate_timed_out(tick_index, stage="taskboard_tick")
                await data.async_set_state("terminal_result", result, emit=False)
                return result

            revision = _unpack_revision_state(data)
            current_board = _board_from_revision(revision)
            schedule = current_board.schedule()
            tick_concurrency = self._taskboard_concurrency()
            await self._emit(
                f"agent_task.taskboard.tick.{tick_index}.scheduled",
                {
                    "schedule": schedule.to_dict(),
                    "evidence_view": build_task_board_evidence_view(current_board.revision).to_dict(),
                    "concurrency": tick_concurrency,
                },
            )
            if not schedule.runnable_card_ids:
                await data.async_set_state(revision_state_key, _pack_revision_state(current_board.revision), emit=False)
                await data.async_set_state("terminal_reason", "no_runnable_cards", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
                return {"terminal": False, "status": "ready_to_finalize"}

            try:
                tick_result = await self._await_task_deadline(
                    current_board.async_run_tick(timeout=None, concurrency=tick_concurrency),
                    stage="taskboard_tick",
                )
            except _AgentTaskDeadlineExceeded as error:
                result = await self._terminate_timed_out(
                    tick_index,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result
            except TimeoutError:
                result = await self._terminate_timed_out(
                    tick_index,
                    stage="taskboard_tick",
                    reason="TaskBoard tick timed out.",
                    limit_name="taskboard_tick_timeout_seconds",
                    timeout_seconds=self._taskboard_tick_timeout(),
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result

            await data.async_set_state(revision_state_key, _pack_revision_state(tick_result.revision), emit=False)
            await data.async_set_state("tick_index", tick_index + 1, emit=False)
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
                    "runtime_topology": {
                        "driver": "triggerflow_taskboard_lifecycle",
                        "tick": DataFormatter.sanitize(tick_result.triggerflow_snapshot.get("runtime_topology", {})),
                    },
                },
            )
            if self._taskboard_revision_completed(tick_result.revision):
                await data.async_set_state("terminal_reason", "board_completed", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
            elif tick_index >= max_ticks_int:
                await data.async_set_state("terminal_reason", "max_ticks_reached", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
            else:
                await data.async_emit_nowait(tick_requested_event, {"tick_index": tick_index + 1})
            return tick_result.revision.to_dict()

        async def finalize_lifecycle(data: TriggerFlowRuntimeData[Any, Any, Any]):
            if data.get_state("terminal_result", None, inherit=False) is not None:
                return data.get_state("terminal_result", inherit=False)
            if data.get_state("final_result", None, inherit=False) is not None:
                return data.get_state("final_result", inherit=False)
            revision = _unpack_revision_state(data)
            try:
                result = await self._finalize_taskboard(revision)
            except _AgentTaskDeadlineExceeded as error:
                result = await self._terminate_timed_out(
                    self.max_iterations,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result
            await data.async_set_state("final_result", result, emit=False)
            return result

        lifecycle_flow.to(start_lifecycle, name="task_board.lifecycle.start")
        lifecycle_flow.when(tick_requested_event).to(run_lifecycle_tick, name="task_board.lifecycle.tick")
        lifecycle_flow.when(finalize_requested_event).to(finalize_lifecycle, name="task_board.lifecycle.finalize")

        execution = lifecycle_flow.create_execution(auto_close=False, concurrency=1)
        await execution.async_start(board.revision.to_dict())
        snapshot = await execution.async_close()
        result = snapshot.get("terminal_result") or snapshot.get("final_result")
        if isinstance(result, Mapping):
            return dict(result)
        raw_revision = snapshot.get(revision_state_key)
        if isinstance(raw_revision, str) and raw_revision.strip():
            revision = TaskBoardRevision.from_value(json.loads(raw_revision))
        else:
            revision = TaskBoardRevision.from_value(board.revision)
        return await self._finalize_taskboard(revision)

    async def _request_taskboard_plan(self, context_pack: "WorkspaceContextPackage"):
        policy = resolve_task_board_planning_policy(
            self._taskboard_effort(),
            metadata={"execution_strategy": self.execution_strategy, "task_id": self.id},
        )
        language_policy = self._language_policy()
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "context_pack": DataFormatter.sanitize(context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "planning_policy": policy.to_prompt_payload(),
                "planner_capabilities": self._planner_capabilities(),
                "language_policy": language_policy,
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
            "style checks, and non-critical cross-checks as optional or degradable through failure_policy. "
            "Use allowed_execution_shape='control' for synthesis, verification, finalization, or board-continuation "
            "decision cards that should be handled by one structured model request. Use allowed_execution_shape='readback' "
            "for cards whose only job is bounded cold artifact readback. Use an action-capable shape such as 'actions' "
            "or 'auto' for cards that need external tools, Workspace operations, side effects, or mixed action/readback work. "
            "After evidence fan-in, do not create a serial chain of control-only cards for synthesis, finalization, "
            "review, and next-step decision when one control card can return the deliverable, sufficient/gaps, "
            "next_board_action, diagnostics, and optional patch_proposal. Multiple dependent control cards are only "
            "justified for distinct user-visible artifacts, different upstream evidence sets, or materially separate "
            "decisions that cannot be verified in one request."
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
        if self._taskboard_card_uses_readback(context.card):
            return await self._run_taskboard_readback_card(context, context_pack)
        if self._taskboard_card_uses_control_request(context.card):
            return await self._run_taskboard_control_card(context, context_pack)
        return await self._run_taskboard_agent_card(context, context_pack)

    def _bind_action_workspace(self, execution: Any) -> None:
        request = getattr(execution, "request", None)
        set_settings = getattr(request, "set_settings", None)
        if callable(set_settings):
            set_settings("action.workspace", self.workspace)

    @staticmethod
    def _workspace_artifact_manifest_path(manifest: Mapping[str, Any] | None) -> str:
        if isinstance(manifest, Mapping):
            for key in ("path", "output_path", "file_path"):
                value = str(manifest.get(key) or "").strip()
                if value:
                    return value
            deliverables = manifest.get("deliverables")
            if isinstance(deliverables, Sequence) and not isinstance(deliverables, str | bytes | bytearray):
                for item in deliverables:
                    if isinstance(item, Mapping):
                        value = str(item.get("path") or item.get("output_path") or "").strip()
                        if value:
                            return value
        return "final.md"

    @classmethod
    def _workspace_artifact_manifest_content(cls, manifest: Mapping[str, Any] | None) -> str:
        if not isinstance(manifest, Mapping):
            return ""
        for key in ("content", "markdown", "body", "text"):
            value = manifest.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        sections = manifest.get("sections")
        if not isinstance(sections, Sequence) or isinstance(sections, str | bytes | bytearray):
            return ""
        chunks: list[str] = []
        for section in sections:
            if isinstance(section, str):
                text = section.strip()
                if text:
                    chunks.append(text)
                continue
            if not isinstance(section, Mapping):
                continue
            title = str(section.get("title") or section.get("name") or "").strip()
            body = ""
            for key in ("content", "markdown", "body", "text"):
                value = section.get(key)
                if isinstance(value, str) and value.strip():
                    body = value.strip()
                    break
            if not body:
                continue
            if title and not body.lstrip().startswith("#"):
                chunks.append(f"## {title}\n\n{body}")
            else:
                chunks.append(body)
        return "\n\n".join(chunks).strip()

    @staticmethod
    def _workspace_artifact_untrusted_refs(result: Mapping[str, Any], manifest: Mapping[str, Any] | None) -> list[Any]:
        refs: list[Any] = []
        raw_refs = result.get("file_refs")
        if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str | bytes | bytearray):
            refs.extend(raw_refs)
        if isinstance(manifest, Mapping):
            manifest_refs = manifest.get("file_refs")
            if isinstance(manifest_refs, Sequence) and not isinstance(manifest_refs, str | bytes | bytearray):
                refs.extend(manifest_refs)
        return refs

    @staticmethod
    def _append_workspace_artifact_meta(execution_meta: Mapping[str, Any] | None, refs: list[dict[str, Any]]) -> None:
        if not refs or not isinstance(execution_meta, dict):
            return
        logs = execution_meta.setdefault("logs", {})
        if not isinstance(logs, dict):
            logs = {}
            execution_meta["logs"] = logs
        artifact_refs = logs.setdefault("artifact_refs", [])
        if not isinstance(artifact_refs, list):
            artifact_refs = []
            logs["artifact_refs"] = artifact_refs
        artifact_refs.extend(DataFormatter.sanitize(refs))
        workspace_refs = execution_meta.setdefault("workspace_refs", {})
        if not isinstance(workspace_refs, dict):
            workspace_refs = {}
            execution_meta["workspace_refs"] = workspace_refs
        workspace_refs.setdefault("agent_task_artifacts", []).extend(DataFormatter.sanitize(refs))
        logs["workspace_refs"] = workspace_refs

    async def _deliver_workspace_artifact(
        self,
        execution_result: Any,
        *,
        plan: Mapping[str, Any] | None = None,
        execution_meta: Mapping[str, Any] | None = None,
        source: str = "agent_task.workspace_artifact",
        context_pack: "WorkspaceContextPackage | None" = None,
        iteration_index: int | None = None,
        card_context: Any | None = None,
        allow_stream_draft: bool = True,
    ) -> Any:
        if not isinstance(execution_result, Mapping):
            return execution_result
        result = dict(execution_result)
        manifest = result.get("artifact_manifest")
        manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
        diagnostics: list[Any] = []
        raw_diagnostics = result.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(raw_diagnostics)
        elif raw_diagnostics:
            diagnostics.append(raw_diagnostics)

        untrusted_refs = self._workspace_artifact_untrusted_refs(result, manifest_dict)
        if untrusted_refs:
            diagnostics.append(
                {
                    "code": "agent_task.workspace_artifact.untrusted_model_file_refs",
                    "message": "Model-declared file_refs are diagnostics only; trusted file refs require Workspace write/readback.",
                    "file_refs": DataFormatter.sanitize(untrusted_refs),
                }
            )
        result["file_refs"] = []
        if manifest_dict:
            manifest_dict.pop("file_refs", None)
            result["artifact_manifest"] = DataFormatter.sanitize(manifest_dict)

        deliverable_mode = str((plan or {}).get("deliverable_mode") or "").strip()
        content, content_key = self._select_workspace_artifact_content(
            result,
            manifest_dict,
            deliverable_mode=deliverable_mode,
        )
        path = self._workspace_artifact_manifest_path(manifest_dict)
        if (
            not content
            and allow_stream_draft
            and deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}
        ):
            stream_delivery = await self._stream_workspace_artifact_draft(
                path=path,
                plan=plan,
                execution_result=result,
                execution_meta=execution_meta,
                source=source,
                context_pack=context_pack,
                iteration_index=iteration_index,
                card_context=card_context,
            )
            if stream_delivery is not None:
                trusted_refs = stream_delivery["file_refs"]
                manifest_dict.update(
                    {
                        "path": trusted_refs[0]["path"],
                        "bytes": trusted_refs[0]["bytes"],
                        "sha256": trusted_refs[0]["sha256"],
                        "file_refs": trusted_refs,
                        "source": source,
                    }
                )
                result["file_refs"] = trusted_refs
                result["artifact_manifest"] = DataFormatter.sanitize(manifest_dict)
                result["workspace_artifact_delivery"] = DataFormatter.sanitize(stream_delivery)
                diagnostics.append(
                    {
                        "code": "agent_task.workspace_artifact.stream_drafted",
                        "message": "Workspace artifact body was generated through a dedicated text stream and written by AgentTask.",
                        "path": trusted_refs[0]["path"],
                        "source": source,
                    }
                )
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
                self._append_workspace_artifact_meta(execution_meta, trusted_refs)
                self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                    DataFormatter.sanitize(stream_delivery)
                )
                return DataFormatter.sanitize(result)
        if not content:
            if diagnostics:
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return DataFormatter.sanitize(result)

        delivery_record: dict[str, Any] = {
            "source": source,
            "path": path,
            "status": "started",
            "mode": deliverable_mode or "artifact_markdown",
            "content_key": content_key,
        }
        preserved = await self._preserve_existing_workspace_artifact_if_preferable(
            path=path,
            new_content=content,
            source=source,
            content_key=content_key,
        )
        if preserved is not None:
            ref = preserved["file_ref"]
            delivery_record.update(
                {
                    "status": "preserved_existing",
                    "reason": "existing_workspace_artifact_is_substantially_larger",
                    "existing_bytes": preserved["existing_bytes"],
                    "new_bytes": preserved["new_bytes"],
                    "file_refs": [DataFormatter.sanitize(ref)],
                }
            )
            diagnostics.append(
                {
                    "code": "agent_task.workspace_artifact.preserved_existing",
                    "message": (
                        "Existing Workspace artifact was preserved because the proposed replacement was "
                        "substantially smaller. Return a full replacement body to overwrite it."
                    ),
                    "path": path,
                    "source": source,
                    "content_key": content_key,
                    "existing_bytes": preserved["existing_bytes"],
                    "new_bytes": preserved["new_bytes"],
                }
            )
            trusted_refs = [DataFormatter.sanitize(ref)]
            result["file_refs"] = trusted_refs
            manifest_dict.update(
                {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "file_refs": trusted_refs,
                    "source": source,
                }
            )
            result["artifact_manifest"] = DataFormatter.sanitize(manifest_dict)
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            self._append_workspace_artifact_meta(execution_meta, trusted_refs)
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
            return DataFormatter.sanitize(result)
        try:
            write_result = await self.workspace.write_file(path, content, append=False)
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {"type": error.__class__.__name__, "message": str(error)},
                }
            )
            diagnostics.append(
                {
                    "code": "agent_task.workspace_artifact.write_failed",
                    "message": str(error) or error.__class__.__name__,
                    "path": path,
                    "source": source,
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            return DataFormatter.sanitize(result)

        ref = {
            "path": str(read_result.get("path") or write_result.get("path") or path),
            "bytes": int(read_result.get("bytes") or write_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or write_result.get("sha256") or ""),
            "media_type": read_result.get("media_type") or write_result.get("media_type"),
            "content_kind": str(read_result.get("content_kind") or write_result.get("content_kind") or "text"),
            "role": "workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "truncated": bool(read_result.get("truncated")),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "handler_id": read_result.get("handler_id"),
        }
        delivery_record.update(
            {
                "status": "delivered",
                "write": DataFormatter.sanitize(write_result),
                "readback": {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "truncated": ref["truncated"],
                    "read_bytes": ref["read_bytes"],
                    "handler_id": ref["handler_id"],
                },
                "file_refs": [DataFormatter.sanitize(ref)],
            }
        )
        trusted_refs = [DataFormatter.sanitize(ref)]
        result["file_refs"] = trusted_refs
        manifest_dict.update(
            {
                "path": ref["path"],
                "bytes": ref["bytes"],
                "sha256": ref["sha256"],
                "file_refs": trusted_refs,
                "source": source,
            }
        )
        result["artifact_manifest"] = DataFormatter.sanitize(manifest_dict)
        result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
        if diagnostics:
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        self._append_workspace_artifact_meta(execution_meta, trusted_refs)
        self.diagnostics.setdefault("workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
        return DataFormatter.sanitize(result)

    async def _preserve_existing_workspace_artifact_if_preferable(
        self,
        *,
        path: str,
        new_content: str,
        source: str,
        content_key: str,
    ) -> dict[str, Any] | None:
        new_bytes = len(new_content.encode("utf-8"))
        if new_bytes <= 0:
            return None
        try:
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except FileNotFoundError:
            return None
        except Exception:
            return None
        existing_bytes = int(read_result.get("bytes") or 0)
        if existing_bytes <= 0:
            return None
        if existing_bytes < max(new_bytes * 2, new_bytes + 1024):
            return None
        ref = {
            "path": str(read_result.get("path") or path),
            "bytes": existing_bytes,
            "sha256": str(read_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": str(read_result.get("content_kind") or "text"),
            "role": "workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "truncated": bool(read_result.get("truncated")),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "handler_id": read_result.get("handler_id"),
        }
        return {
            "file_ref": DataFormatter.sanitize(ref),
            "existing_bytes": existing_bytes,
            "new_bytes": new_bytes,
            "content_key": content_key,
        }

    @classmethod
    def _select_workspace_artifact_content(
        cls,
        result: Mapping[str, Any],
        manifest_dict: Mapping[str, Any],
        *,
        deliverable_mode: str,
    ) -> tuple[str, str]:
        manifest_content = cls._workspace_artifact_manifest_content(manifest_dict)
        candidates: list[tuple[str, str]] = []
        if manifest_content.strip():
            candidates.append(("artifact_manifest", manifest_content.strip()))
        for key in ("artifact_markdown", "artifact_html", "candidate_final_result", "final_result", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append((key, value.strip()))
        if not candidates:
            return "", ""
        if deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}:
            # The Workspace file is the trusted deliverable. If the model emits a
            # short display placeholder in artifact_markdown and the full body in
            # answer/candidate_final_result, write the complete body instead.
            key, content = max(candidates, key=lambda item: len(item[1]))
            return content, key
        for preferred_key in ("artifact_manifest", "artifact_markdown", "artifact_html", "candidate_final_result", "final_result", "answer"):
            for key, content in candidates:
                if key == preferred_key:
                    return content, key
        return candidates[0][1], candidates[0][0]

    async def _stream_workspace_artifact_draft(
        self,
        *,
        path: str,
        plan: Mapping[str, Any] | None,
        execution_result: Mapping[str, Any],
        execution_meta: Mapping[str, Any] | None,
        source: str,
        context_pack: "WorkspaceContextPackage | None" = None,
        iteration_index: int | None = None,
        card_context: Any | None = None,
    ) -> dict[str, Any] | None:
        draft_execution = self.agent.create_execution(
            lineage={
                "task_id": self.id,
                "iteration_id": f"iter-{iteration_index}" if iteration_index is not None else None,
                "step_id": "workspace_artifact_draft",
                "scope": {"strategy_phase": "agent_task_workspace_artifact_draft"},
            },
            limits=self.limits,
            options=self.options,
        )
        draft_execution.route_policy(
            {
                "allowed_routes": ["model_request"],
                "on_violation": "block",
                "owner": "AgentTaskLoop",
                "step_execution_shape": "workspace_artifact_draft",
            }
        )
        language_policy = self._language_policy()
        draft_execution.language(language_policy.get("language", "auto"))
        draft_execution.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "execution_strategy": self.execution_strategy,
                "artifact_path": path,
                "plan": DataFormatter.sanitize(plan or {}),
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_meta_summary": self._execution_log_summary(execution_meta or {}),
                "context_pack": DataFormatter.sanitize(context_pack or {}),
                "card": DataFormatter.sanitize(getattr(card_context, "card", None).to_dict())
                if card_context is not None and hasattr(getattr(card_context, "card", None), "to_dict")
                else {},
                "dependency_results": DataFormatter.sanitize(
                    {
                        key: value.to_dict() if hasattr(value, "to_dict") else value
                        for key, value in dict(getattr(card_context, "dependency_results", {}) or {}).items()
                    }
                )
                if card_context is not None
                else {},
                "language_policy": language_policy,
            }
        )
        draft_execution.instruct(
            (
                "Write only the final Markdown artifact body for the AgentTask. "
                "Do not output JSON, YAML, XML, code fences, file_refs, or a wrapper object. "
                "Use only the provided task context, execution result, dependency results, and evidence summaries. "
                "If the source evidence is incomplete, write a clear source-boundary section instead of fabricating facts. "
                "The framework will stream your Markdown into the Workspace artifact path and read it back."
            )
        )

        delivery_record: dict[str, Any] = {
            "source": source,
            "path": path,
            "status": "started",
            "mode": "streamed_workspace_artifact",
            "draft_execution_id": str(getattr(draft_execution, "id", "") or ""),
        }
        wrote_any = False
        bytes_written = 0
        try:
            async for delta in draft_execution.get_async_generator(type="delta"):
                chunk = str(delta or "")
                if not chunk:
                    continue
                await self.workspace.write_file(path, chunk, append=wrote_any)
                wrote_any = True
                bytes_written += len(chunk.encode("utf-8"))
                if iteration_index is not None:
                    await self._emit(
                        f"agent_task.iteration.{iteration_index}.workspace_artifact_draft.delta",
                        {"path": path, "bytes_written": bytes_written},
                        event_type="delta",
                        delta=chunk,
                        is_complete=False,
                        meta={
                            "task_id": self.id,
                            "iteration": iteration_index,
                            "stage": "workspace_artifact_draft",
                            "stream_kind": "workspace_artifact_draft",
                            "path": path,
                        },
                    )
            draft_meta = await draft_execution.async_get_meta()
            delivery_record["draft_meta"] = {
                "execution_id": draft_meta.get("execution_id"),
                "status": draft_meta.get("status"),
                "route": DataFormatter.sanitize(draft_meta.get("route")),
            }
        except Exception as error:
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {"type": error.__class__.__name__, "message": str(error)},
                    "bytes_written": bytes_written,
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
            return None
        if not wrote_any:
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {
                        "type": "EmptyWorkspaceArtifactDraft",
                        "message": "Workspace artifact draft stream produced no content.",
                    },
                    "bytes_written": bytes_written,
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
            return None

        try:
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {"type": error.__class__.__name__, "message": str(error)},
                    "bytes_written": bytes_written,
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
            return None

        ref = {
            "path": str(read_result.get("path") or path),
            "bytes": int(read_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": str(read_result.get("content_kind") or "text"),
            "role": "workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "truncated": bool(read_result.get("truncated")),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "handler_id": read_result.get("handler_id"),
        }
        delivery_record.update(
            {
                "status": "delivered",
                "bytes_written": bytes_written,
                "readback": {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "truncated": ref["truncated"],
                    "read_bytes": ref["read_bytes"],
                    "handler_id": ref["handler_id"],
                },
                "file_refs": [DataFormatter.sanitize(ref)],
            }
        )
        return DataFormatter.sanitize(delivery_record)

    async def _run_taskboard_agent_card(self, context: Any, context_pack: "WorkspaceContextPackage") -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        readback_records = self._taskboard_action_artifact_recall_records(evidence_view)
        max_attempts = self._taskboard_card_max_attempts()
        previous_errors: list[dict[str, Any]] = []
        language_policy = self._language_policy()
        for attempt_index in range(1, max_attempts + 1):
            execution = self.agent.create_execution(
                lineage={
                    "task_id": self.id,
                    "iteration_id": f"taskboard:{context.card.id}:attempt:{attempt_index}",
                    "step_id": "taskboard_card",
                    "scope": {
                        "strategy_phase": "taskboard_card_execution",
                        "card_id": context.card.id,
                        "attempt_index": attempt_index,
                    },
                },
                limits=self.limits,
                options=self.options,
            )
            self._bind_action_workspace(execution)
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
                    "previous_attempt_errors": previous_errors,
                    "attempt": {
                        "attempt_index": attempt_index,
                        "max_attempts": max_attempts,
                    },
                    "context_pack": DataFormatter.sanitize(context_pack),
                    "execution_prompt": self._execution_prompt_context(),
                    "language_policy": language_policy,
                }
            )
            execution.language(language_policy.get("language", "auto"))
            execution.instruct(
                "Execute exactly one TaskBoard card as a bounded AgentExecution step. "
                "Use TaskBoard evidence view as the hot summary; request full content only through available "
                "Workspace or Action refs when needed. If previous_attempt_errors is non-empty, avoid repeating "
                "the same failing source or method when a bounded fallback can satisfy the card. If available_readback "
                "lists Action artifact refs and a bounded preview is insufficient, call read_action_artifact with "
                "the artifact_id and action_call_id before blocking on missing evidence. Return card-local evidence "
                "and remaining work. If the card's original method fails but equivalent evidence or a bounded fallback "
                "is available, return status completed with diagnostics that explain the degraded source boundary. "
                "Only return failed or blocked when the card cannot produce the required outcome or the missing "
                "evidence is truly critical. If this card produces the user-facing deliverable, use candidate_final_result, "
                "final_result, or artifact_markdown only when the complete body is short enough for the bounded output. "
                "For long reports, exam papers, or multi-section deliverables, prefer artifact_manifest with "
                "sections/content so the JSON stays a control plane instead of a giant body field. "
                "AgentTask will write/read back Workspace files and produce trusted file_refs; do not invent file_refs "
                "for deliverables. Review or "
                "verification cards must not put review notes in those deliverable fields unless they include the "
                "full corrected deliverable body. Do not claim the whole task is complete; TaskBoard and AgentTask "
                "own lifecycle completion."
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
                        "Short markdown deliverable body when this card creates a bounded markdown artifact",
                        False,
                    ),
                    "artifact_manifest": (
                        dict,
                        "Workspace artifact manifest for sectioned or file-backed deliverables",
                        False,
                    ),
                    "file_refs": (
                        [dict],
                        "Existing evidence refs only; deliverable refs become trusted only after AgentTask Workspace write/readback",
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
                {
                    "execution_id": execution.id,
                    "card_id": context.card.id,
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                },
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
                execution_id = str(getattr(execution, "id", "") or "") or None
                retry_diagnostic = self._taskboard_card_retry_diagnostic(
                    card_id=context.card.id,
                    error=error,
                    execution_id=execution_id,
                    attempt_index=attempt_index,
                    max_attempts=max_attempts,
                )
                previous_errors.append(retry_diagnostic)
                if attempt_index < max_attempts and self._taskboard_card_error_retryable(error):
                    self.diagnostics.setdefault("taskboard_card_retries", []).append(retry_diagnostic)
                    await self._emit(
                        f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.execution.retry",
                        retry_diagnostic,
                    )
                    continue
                return self._failed_taskboard_card_result(
                    card_id=context.card.id,
                    error=error,
                    execution_id=execution_id,
                )
            card_output = await self._deliver_workspace_artifact(
                card_output,
                plan={
                    "deliverable_mode": "workspace_artifact"
                    if isinstance(card_output, Mapping)
                    and (
                        card_output.get("artifact_manifest")
                        or card_output.get("artifact_markdown")
                        or card_output.get("candidate_final_result")
                        or card_output.get("final_result")
                    )
                    else ""
                },
                execution_meta=cast(dict[str, Any], execution_meta),
                source=f"agent_task.taskboard.card.{context.card.id}.workspace_artifact",
                context_pack=context_pack,
                card_context=context,
            )
            summary = self._execution_log_summary(cast(dict[str, Any], execution_meta))
            card_status = self._taskboard_card_status(card_output, execution_meta)
            diagnostics = []
            if isinstance(card_output, Mapping):
                raw_diagnostics = card_output.get("diagnostics")
                if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                    diagnostics.extend(dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics)
            output_file_refs: list[Any] = []
            if isinstance(card_output, Mapping):
                raw_file_refs = card_output.get("file_refs")
                if isinstance(raw_file_refs, Sequence) and not isinstance(raw_file_refs, str | bytes | bytearray):
                    output_file_refs.extend(DataFormatter.sanitize(item) for item in raw_file_refs)
                artifact_manifest = card_output.get("artifact_manifest")
                if isinstance(artifact_manifest, Mapping):
                    manifest_refs = artifact_manifest.get("file_refs")
                    if isinstance(manifest_refs, Sequence) and not isinstance(manifest_refs, str | bytes | bytearray):
                        output_file_refs.extend(DataFormatter.sanitize(item) for item in manifest_refs)
            diagnostics.append(
                {
                    "execution_id": execution_meta.get("execution_id"),
                    "route": DataFormatter.sanitize(execution_meta.get("route", {})),
                    "evidence_summary": DataFormatter.sanitize(summary),
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                    "previous_attempt_errors": previous_errors,
                }
            )
            return TaskBoardCardResult(
                card_id=context.card.id,
                status=card_status,
                preview=DataFormatter.sanitize(card_output),
                artifact_refs=tuple(
                    [
                        *(summary.get("artifact_refs", []) if isinstance(summary.get("artifact_refs"), list) else []),
                        *output_file_refs,
                    ]
                ),
                diagnostics=tuple(diagnostics),
                metadata={
                    "execution_id": execution_meta.get("execution_id"),
                    "execution_strategy": self.execution_strategy,
                    "attempt_index": attempt_index,
                    "max_attempts": max_attempts,
                },
            )
        return self._failed_taskboard_card_result(
            card_id=context.card.id,
            error=RuntimeError("TaskBoard card execution exhausted retry attempts."),
            execution_id=None,
        )

    async def _run_taskboard_control_card(self, context: Any, context_pack: "WorkspaceContextPackage") -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        language_policy = self._language_policy()
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
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
                "planning_policy": context.planning_policy.to_prompt_payload() if context.planning_policy is not None else {},
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Execute one TaskBoard control card with a single structured model request. "
            "This card is for synthesis, verification, finalization, or deciding the next board action; "
            "do not plan or call tools from this request. Use TaskBoardEvidenceView as the hot evidence summary "
            "and preserve cold refs as pointers. If bounded previews are insufficient, set next_board_action to "
            "'readback' or 'repair' and explain the exact missing refs or gaps instead of inventing facts. "
            "When the card can produce the user-facing deliverable, use artifact_markdown, candidate_final_result, "
            "or final_result only when the complete body is short enough for the bounded output. For sectioned or "
            "long artifacts, prefer artifact_manifest with sections/content so JSON remains the control plane; "
            "AgentTask will write/read back Workspace files and produce "
            "trusted file_refs. Do not invent file_refs for deliverables. If the task is source-grounded, include "
            "the concrete source URLs, file paths, or evidence refs used by the deliverable in the deliverable body; "
            "do not mention a source title without its verifier-visible URL/path when such a ref exists. "
            "Also return whether the card is sufficient "
            "and what continuation, if any, the board should consider."
        )
        request.output(
            {
                "status": (str, "completed, blocked, failed, or skipped for this card", False),
                "answer": (str, "Card-local synthesis or decision summary", True),
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
                    "Short markdown deliverable body when this card creates a bounded markdown artifact",
                    False,
                ),
                "artifact_manifest": (
                    dict,
                    "Artifact manifest proposal for sectioned or file-backed deliverables",
                    False,
                ),
                "file_refs": (
                    [dict],
                    "Existing evidence refs only; model-declared deliverable refs are untrusted until framework write/readback",
                    False,
                ),
                "sufficient": (bool, "True when this card has enough evidence to satisfy its objective", False),
                "next_board_action": (
                    str,
                    "finalize, continue, readback, repair, patch, block, or stop",
                    False,
                ),
                "gaps": ([str], "Evidence or quality gaps that remain after this control request", False),
                "evidence": ([str], "Evidence used by this control card", False),
                "remaining_work": ([str], "Remaining work for this card, empty when complete", False),
                "diagnostics": ([dict], "Optional control-card diagnostics", False),
                "patch_proposal": (dict, "Optional TaskBoardPatch proposal when next_board_action is patch", False),
            },
            format="json",
        )
        await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.control.started",
            {"card_id": context.card.id},
        )
        result_handle = request.get_result()
        try:
            card_output = await self._await_taskboard_card_execution(
                self._consume_taskboard_control_request(context.card.id, result_handle),
                card_id=context.card.id,
                stage="control",
            )
        except Exception as error:
            return self._failed_taskboard_card_result(
                card_id=context.card.id,
                error=error,
                execution_id=None,
            )
        card_output = await self._deliver_workspace_artifact(
            card_output,
            plan={
                "deliverable_mode": "workspace_artifact"
                if isinstance(card_output, Mapping)
                and (
                    card_output.get("artifact_manifest")
                    or card_output.get("artifact_markdown")
                    or card_output.get("candidate_final_result")
                    or card_output.get("final_result")
                )
                else ""
            },
            source=f"agent_task.taskboard.card.{context.card.id}.workspace_artifact",
            context_pack=context_pack,
            card_context=context,
        )
        diagnostics = []
        if isinstance(card_output, Mapping):
            raw_diagnostics = card_output.get("diagnostics")
            if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                diagnostics.extend(dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics)
        diagnostics.append(
            {
                "execution_kind": "taskboard_control_request",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "next_board_action": card_output.get("next_board_action") if isinstance(card_output, Mapping) else None,
                "sufficient": card_output.get("sufficient") if isinstance(card_output, Mapping) else None,
            }
        )
        card_status = self._taskboard_control_card_status(card_output)
        output_file_refs: list[Any] = []
        if isinstance(card_output, Mapping):
            raw_file_refs = card_output.get("file_refs")
            if isinstance(raw_file_refs, Sequence) and not isinstance(raw_file_refs, str | bytes | bytearray):
                output_file_refs.extend(DataFormatter.sanitize(item) for item in raw_file_refs)
        return TaskBoardCardResult(
            card_id=context.card.id,
            status=card_status,
            preview=DataFormatter.sanitize(card_output),
            artifact_refs=tuple(output_file_refs),
            diagnostics=tuple(diagnostics),
            patch_proposal=dict(card_output["patch_proposal"])
            if isinstance(card_output, Mapping) and isinstance(card_output.get("patch_proposal"), Mapping)
            else None,
            metadata={
                "execution_kind": "taskboard_control_request",
                "execution_strategy": self.execution_strategy,
                "next_board_action": card_output.get("next_board_action") if isinstance(card_output, Mapping) else None,
            },
        )

    async def _run_taskboard_readback_card(
        self,
        context: Any,
        context_pack: "WorkspaceContextPackage",
    ) -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        refs = self._taskboard_readback_artifact_refs(evidence_view)
        await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.started",
            {
                "card_id": context.card.id,
                "ref_count": len(refs),
            },
        )
        if not refs:
            payload = {
                "status": "blocked",
                "answer": "No Action artifact refs are available for this readback card.",
                "readbacks": [],
                "evidence": [],
                "remaining_work": ["Upstream cards must produce Action artifact refs before readback can run."],
                "diagnostics": [
                    {
                        "code": "taskboard.readback.no_refs",
                        "card_id": context.card.id,
                        "evidence_scope": evidence_card_ids or "all",
                    }
                ],
            }
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.completed",
                {"card_id": context.card.id, "status": "blocked", "success_count": 0, "ref_count": 0},
            )
            return TaskBoardCardResult(
                card_id=context.card.id,
                status="blocked",
                preview=DataFormatter.sanitize(payload),
                diagnostics=tuple(payload["diagnostics"]),
                metadata={
                    "execution_kind": "taskboard_artifact_readback",
                    "execution_strategy": self.execution_strategy,
                    "ref_count": 0,
                    "success_count": 0,
                },
            )

        action = getattr(self.agent, "action", None)
        reader = getattr(action, "async_read_action_artifact", None)
        if not callable(reader):
            payload = {
                "status": "failed",
                "answer": "Action artifact readback is unavailable on the bound Agent.",
                "readbacks": [],
                "evidence": [],
                "remaining_work": ["Provide an Agent Action runtime with async_read_action_artifact(...)."],
                "diagnostics": [
                    {
                        "code": "taskboard.readback.reader_unavailable",
                        "card_id": context.card.id,
                        "ref_count": len(refs),
                    }
                ],
            }
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.completed",
                {"card_id": context.card.id, "status": "failed", "success_count": 0, "ref_count": len(refs)},
            )
            return TaskBoardCardResult(
                card_id=context.card.id,
                status="failed",
                preview=DataFormatter.sanitize(payload),
                artifact_refs=tuple(refs),
                diagnostics=tuple(payload["diagnostics"]),
                metadata={
                    "execution_kind": "taskboard_artifact_readback",
                    "execution_strategy": self.execution_strategy,
                    "ref_count": len(refs),
                    "success_count": 0,
                },
            )

        readbacks: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        for ref in refs:
            artifact_id = str(ref.get("artifact_id") or "")
            action_call_id = str(ref.get("action_call_id") or "")
            try:
                raw_readback = await self._await_taskboard_card_execution(
                    cast(Awaitable[Any], reader(artifact_id, action_call_id or None)),
                    card_id=context.card.id,
                    stage="readback",
                )
            except Exception as error:
                raw_readback = {
                    "ok": False,
                    "status": "error",
                    "artifact_id": artifact_id,
                    "action_call_id": action_call_id,
                    "error": f"{ error.__class__.__name__}: { error }",
                }
            compact = self._compact_taskboard_action_artifact_readback(raw_readback, ref)
            readbacks.append(compact)
            if not compact.get("ok"):
                diagnostics.append(
                    {
                        "code": "taskboard.readback.ref_failed",
                        "artifact_id": artifact_id,
                        "action_call_id": action_call_id,
                        "status": compact.get("status"),
                        "error": compact.get("error"),
                    }
                )

        success_count = sum(1 for item in readbacks if item.get("ok"))
        failed_count = len(readbacks) - success_count
        status = "completed" if success_count > 0 else "failed"
        payload = {
            "status": status,
            "answer": f"Read { success_count } of { len(refs) } Action artifact refs with bounded previews.",
            "readbacks": readbacks,
            "evidence": [
                f"artifact:{ item.get('artifact_id') } status={ item.get('status') }"
                for item in readbacks
                if item.get("artifact_id")
            ],
            "remaining_work": [] if failed_count == 0 else [f"{ failed_count } artifact refs could not be read."],
            "diagnostics": diagnostics,
        }
        await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.completed",
            {
                "card_id": context.card.id,
                "status": status,
                "success_count": success_count,
                "failed_count": failed_count,
                "ref_count": len(refs),
            },
        )
        diagnostics.append(
            {
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
            }
        )
        return TaskBoardCardResult(
            card_id=context.card.id,
            status=status,
            preview=DataFormatter.sanitize(payload),
            artifact_refs=tuple(refs),
            diagnostics=tuple(diagnostics),
            metadata={
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
            },
        )

    async def _consume_taskboard_control_request(self, card_id: str, result_handle: Any) -> Any:
        async for item in result_handle.get_async_generator(type="instant"):
            await self._emit_taskboard_control_stream_item(card_id, item)
        return await result_handle.async_get_data(raise_ensure_failure=False)

    async def _emit_taskboard_control_stream_item(
        self,
        card_id: str,
        item: Any,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        return await self._emit(
            f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.control.{raw_path}",
            getattr(item, "value", None),
            event_type="delta",
            delta=getattr(item, "delta", None),
            is_complete=bool(getattr(item, "is_complete", False)),
            meta={
                "task_id": self.id,
                "status": self.status,
                "stage": "taskboard_card_control",
                "card_id": card_id,
                "stream_kind": "taskboard_control_request",
                "control_path": raw_path,
            },
        )

    async def _bridge_taskboard_card_execution_stream(self, card_id: str, execution: Any) -> None:
        try:
            async for item in execution.get_async_generator(type="instant"):
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
        language_policy = self._language_policy()
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
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
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Synthesize the final result for this TaskBoard task from completed card evidence. "
            "Verify every success criterion. Use the hot evidence view for summaries and preserve cold refs "
            "as evidence pointers; do not invent unsupported facts. When candidate_final_result contains a "
            "complete answer/report/artifact body that satisfies the criteria, preserve it as final_result "
            "instead of rewriting it into a shorter summary. For source-grounded tasks, the final_result must include "
            "the concrete source URLs, file paths, or evidence refs that support the deliverable; source titles or "
            "general source names without verifier-visible URL/path refs are not enough when refs are available. "
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

    def _taskboard_card_max_attempts(self) -> int:
        value = self._taskboard_option("taskboard_card_max_attempts")
        try:
            attempts = int(value) if value is not None else 2
        except (TypeError, ValueError):
            attempts = 2
        return min(max(1, attempts), 5)

    def _taskboard_card_error_retryable(self, error: Exception) -> bool:
        if self._is_timeout_error(error):
            return True
        text = f"{ error.__class__.__name__}: { str(error) }".lower()
        retry_markers = (
            "429",
            "chunked",
            "connect",
            "connection",
            "eof",
            "parse_failed",
            "rate limit",
            "request failed",
            "request_failed",
            "temporarily",
            "timeout",
            "tls",
        )
        return any(marker in text for marker in retry_markers)

    def _taskboard_card_retry_diagnostic(
        self,
        *,
        card_id: str,
        error: Exception,
        execution_id: str | None,
        attempt_index: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        message = str(error) or error.__class__.__name__
        return {
            "type": error.__class__.__name__,
            "code": "taskboard.card.timeout" if self._is_timeout_error(error) else "taskboard.card.execution_error",
            "message": message[:1000],
            "card_id": card_id,
            "execution_id": execution_id,
            "execution_strategy": self.execution_strategy,
            "stage": "taskboard_card",
            "attempt_index": attempt_index,
            "max_attempts": max_attempts,
            "retry_scheduled": attempt_index < max_attempts,
            "timeout_seconds": self._taskboard_card_timeout() if self._is_timeout_error(error) else None,
            "status": "retrying" if attempt_index < max_attempts else "failed",
        }

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
    def _taskboard_control_card_status(card_output: Any) -> str:
        if isinstance(card_output, Mapping):
            status = str(card_output.get("status") or "completed").strip().lower()
            if status in {"completed", "blocked", "failed", "skipped"}:
                return status
            next_action = str(card_output.get("next_board_action") or "").strip().lower()
            if next_action in {"readback", "needs_readback", "repair", "patch", "continue", "block"}:
                return "blocked"
            remaining = card_output.get("remaining_work")
            gaps = card_output.get("gaps")
            if AgentTask._has_remaining_work(remaining) or AgentTask._has_remaining_work(gaps):
                return "blocked"
        return "completed"

    @staticmethod
    def _taskboard_card_execution_shape(card: Any) -> str:
        return str(getattr(card, "allowed_execution_shape", "") or "auto").strip().lower().replace("-", "_")

    @classmethod
    def _taskboard_card_uses_control_request(cls, card: Any) -> bool:
        return cls._taskboard_card_execution_shape(card) in _TASKBOARD_CONTROL_CARD_SHAPES

    @classmethod
    def _taskboard_card_uses_readback(cls, card: Any) -> bool:
        return cls._taskboard_card_execution_shape(card) in _TASKBOARD_READBACK_CARD_SHAPES

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
    def _taskboard_readback_artifact_refs(evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        records = AgentTask._taskboard_action_artifact_recall_records(evidence_view)
        if not records:
            return []
        refs = records[0].get("artifact_refs")
        if not isinstance(refs, list):
            return []
        return [dict(ref) for ref in refs if isinstance(ref, Mapping)]

    @classmethod
    def _compact_taskboard_action_artifact_readback(
        cls,
        readback: Any,
        ref: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(readback, Mapping):
            readback = {
                "ok": False,
                "status": "invalid_result",
                "error": f"Action artifact reader returned { type(readback).__name__ }.",
            }
        artifact_id = str(readback.get("artifact_id") or ref.get("artifact_id") or "")
        action_call_id = str(readback.get("action_call_id") or ref.get("action_call_id") or "")
        value = readback.get("value", readback.get("data", readback.get("result")))
        original_chars = cls._serialized_prompt_chars(value)
        preview = cls._compact_verifier_prompt_value(value, max_chars=_TASKBOARD_READBACK_PREVIEW_CHARS)
        preview_chars = cls._serialized_prompt_chars(preview)
        compact: dict[str, Any] = {
            "ok": bool(readback.get("ok")),
            "status": str(readback.get("status") or ""),
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": str(readback.get("artifact_type") or ref.get("artifact_type") or ""),
            "label": str(readback.get("label") or ref.get("label") or ""),
            "media_type": str(readback.get("media_type") or ref.get("media_type") or ""),
            "ref": DataFormatter.sanitize(dict(ref)),
            "value_preview": preview,
            "value_preview_meta": {
                "truncated": preview_chars < original_chars,
                "original_chars": original_chars,
                "preview_chars": preview_chars,
                "limit_chars": _TASKBOARD_READBACK_PREVIEW_CHARS,
            },
        }
        error = readback.get("error")
        if error:
            compact["error"] = cls._truncate_prompt_text(error, 1200)
        meta = readback.get("meta")
        if isinstance(meta, Mapping):
            compact["meta"] = cls._compact_verifier_prompt_value(meta, max_chars=1200)
        return compact

    @staticmethod
    def _serialized_prompt_chars(value: Any) -> int:
        try:
            return len(json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, default=str))
        except Exception:
            return len(str(value or ""))

    @staticmethod
    def _taskboard_available_readback(evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        records = AgentTask._taskboard_action_artifact_recall_records(evidence_view)
        refs = records[0]["artifact_refs"] if records else []
        return {
            "schema_version": "agent_task_taskboard_readback/v1",
            "taskboard_readback_shape": {
                "available": bool(refs),
                "allowed_execution_shape": "readback",
                "artifact_refs": DataFormatter.sanitize(refs),
            },
            "action_artifact_readback": {
                "available": bool(refs),
                "action_id": "read_action_artifact",
                "artifact_refs": DataFormatter.sanitize(refs),
            },
            "policy": (
                "Use a TaskBoard readback card only when bounded previews are insufficient and the only "
                "remaining work is scoped cold artifact readback. Mixed tool/readback work may still use "
                "the ActionRuntime read_action_artifact action."
            ),
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

    def _language_policy(self) -> dict[str, Any]:
        raw_policy = self._agent_task_option("language_policy", None)
        if raw_policy is None:
            raw_policy = self._agent_task_option("language", None)
        if raw_policy is None:
            raw_policy = language_policy_from_prompt_snapshot(self.options.get("execution_prompt_snapshot"))
        if raw_policy is None:
            getter = getattr(getattr(self.agent, "settings", None), "get", None)
            if callable(getter):
                raw_policy = getter("agent.language_policy", None)
        progress_language = (
            self._agent_task_option("progress_language", None)
            or self._agent_task_option("stream_progress_language", None)
        )
        if isinstance(raw_policy, Mapping):
            return dict(resolve_language_policy(base=raw_policy, progress_language=progress_language))
        return dict(resolve_language_policy(raw_policy or "auto", progress_language=progress_language))

    def _apply_language_policy_to_request(self, request: Any, policy: Mapping[str, Any] | None = None) -> None:
        apply_language_policy_to_prompt(getattr(request, "prompt", request), policy or self._language_policy())

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
            if "side_effect_level" in item:
                entry["side_effect_level"] = str(item.get("side_effect_level") or "")
            if "replay_safe" in item:
                entry["replay_safe"] = self._normalize_bool(item.get("replay_safe"), default=False)
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

    def _untried_read_action_continuation(
        self,
        execution_evidence_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        read_action_ids = {
            str(item.get("id") or "").strip()
            for item in self._planner_capabilities()
            if isinstance(item, Mapping)
            and str(item.get("kind") or "").strip() == "action"
            and str(item.get("id") or "").strip()
            and (
                str(item.get("side_effect_level") or "").strip().lower() == "read"
                or bool(item.get("replay_safe")) is True
            )
        }
        if not read_action_ids:
            return {}
        used_action_ids = set(self._normalize_string_list(execution_evidence_summary.get("action_ids")))
        used_action_ids.update(self._satisfied_capabilities)
        untried_action_ids = sorted(read_action_ids - used_action_ids)
        if not untried_action_ids:
            return {}
        blocked_actions = self._normalize_string_list(execution_evidence_summary.get("blocked_actions"))
        approval_required_actions = self._normalize_string_list(
            execution_evidence_summary.get("approval_required_actions")
        )
        if blocked_actions or approval_required_actions:
            return {}
        failed_actions = self._normalize_string_list(execution_evidence_summary.get("failed_actions"))
        unsafe_failed_actions = [action_id for action_id in failed_actions if action_id not in read_action_ids]
        if unsafe_failed_actions:
            return {}
        return {
            "reason": "read_action_continuation_available",
            "untried_action_ids": untried_action_ids,
            "failed_read_action_ids": sorted(action_id for action_id in failed_actions if action_id in read_action_ids),
        }

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
        language_policy = self._language_policy()
        self._apply_language_policy_to_request(request, language_policy)
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
                "language_policy": language_policy,
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
            "When repair_context is present, use it as verification feedback: understand why prior work was incomplete, "
            "compare the acceptance delta, and then choose the next bounded step. The verifier does not choose tools, "
            "routes, execution shapes, or exact methods; the planner owns the next action while respecting grounded "
            "acceptance facts and deterministic guards. "
            "For web discovery tasks, if the task context already names an official domain, homepage, or URL and "
            "search results are empty, unstable, or inconclusive, plan a Browse step for that known entry point and "
            "follow same-site navigation links before concluding that the required source is unavailable. Search "
            "result snippets are discovery hints, not source evidence. Before using a search result snippet or a broad "
            "announcement page as the source boundary, plan Browse/readback for the candidate page and relevant same-site "
            "index/list/download/navigation pages so a more specific official source can be discovered. "
            f"Set execution_shape to {allowed_shapes}. "
            "Use a DAG-shaped execution only when execution_policy allows it or a concrete DynamicTask candidate is available."
            + strategy_note
            + capability_note
            + " Optionally set step_scope.allowed_capability_ids to limit this bounded step to specific capability "
            "ids when it is only meant to gather evidence; leave it empty when the step may use any available capability."
            " For long deliverables, choose deliverable_mode='workspace_artifact' or "
            "'sectioned_workspace_artifact' and instruct the execution step to return a sectioned artifact_manifest; "
            "use artifact_markdown only for bounded short deliverables. AgentTask will write/read back Workspace "
            "files; the model must not self-declare trusted file_refs for a deliverable."
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
                "deliverable_mode": (
                    str,
                    "inline_final, workspace_artifact, or sectioned_workspace_artifact for expected deliverables",
                    False,
                ),
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
        self._bind_action_workspace(execution)
        step_execution = self._configure_step_execution(execution, plan)
        language_policy = self._language_policy()
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
                "language_policy": language_policy,
            }
        )
        execution.language(language_policy.get("language", "auto"))
        execution.instruct(
            (
                "Execute exactly one bounded step for the AgentTask. "
                f"Use the selected execution shape: {step_execution.get('effective_shape', 'direct')}. "
                f"The AgentTask execution_strategy is {self.execution_strategy}. "
                "Respect the caller-provided execution_prompt context and output contract when present. "
                "Return concrete evidence for the verifier. If this step produces the requested final answer, report, "
                "file body, or artifact body, put the complete candidate deliverable in candidate_final_result instead "
                "of burying the only copy inside evidence when it fits the bounded output. If the plan deliverable_mode "
                "is workspace_artifact or sectioned_workspace_artifact, prefer an artifact_manifest with sections/content "
                "for long or multi-section deliverables; use artifact_markdown only for bounded short bodies. AgentTask "
                "will write/read back Workspace files and produce trusted file_refs. "
                "Do not invent file_refs for deliverables. "
                "For web-source steps, treat Search results as discovery hints only. Browse official pages and follow "
                "same-site index/list/download/navigation links before relying on a broad announcement page as the "
                "source boundary. "
                "Do not claim final completion unless evidence supports it."
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
                "artifact_markdown": (
                    str,
                    "Short markdown deliverable body when this step creates one and it fits bounded output",
                    False,
                ),
                "artifact_manifest": (
                    dict,
                    "Workspace artifact manifest for file-backed or sectioned deliverables",
                    False,
                ),
                "file_refs": (
                    [dict],
                    "Existing evidence refs only; deliverable refs become trusted only after AgentTask Workspace write/readback",
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
            async for item in execution.get_async_generator(type="instant"):
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
                        "failure_analysis": verification.get("failure_analysis", ""),
                        "acceptance_delta": verification.get("acceptance_delta", []),
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
        acceptance_delta = normalize_list(verification.get("acceptance_delta"))
        repair_constraints = normalize_list(verification.get("repair_constraints"))
        next_step_requirements = normalize_list(verification.get("next_step_requirements"))
        replan_instruction = str(verification.get("replan_instruction") or "").strip()
        failure_analysis = str(verification.get("failure_analysis") or "").strip()
        if not any([missing_criteria, acceptance_delta, repair_constraints, next_step_requirements, replan_instruction, failure_analysis]):
            return {}
        return {
            "source_iteration": latest.get("iteration"),
            "verification_ref": latest.get("verification_ref"),
            "reason": str(verification.get("reason") or ""),
            "failure_analysis": failure_analysis,
            "acceptance_delta": acceptance_delta,
            "missing_criteria": missing_criteria,
            "advisory_repair_constraints": repair_constraints,
            "advisory_next_step_requirements": next_step_requirements,
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
        cumulative_evidence_summary = self._compact_verifier_evidence_summary(
            self._cumulative_execution_evidence_summary(execution_meta)
        )
        candidate_final_result = self._candidate_final_result_from_execution_result(execution_result)
        request = self.agent.create_temp_request()
        language_policy = self._language_policy()
        self._apply_language_policy_to_request(request, language_policy)
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
                "cumulative_execution_evidence_summary": cumulative_evidence_summary,
                "capability_evidence_requirements": self._capability_evidence_requirements(),
                "context_pack": self._compact_context_pack_for_verifier(context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "previous_iterations": self._iteration_prompt_summaries(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Verify the task against every success criterion. "
            "Also consider caller-provided execution_prompt constraints when they are present. "
            "Treat numeric criteria such as 'at least N' as exact counting rules and fail verification when the "
            "evidence does not meet the count. "
            "Require source/evidence references when the criteria ask for evidence. "
            "Use both execution_evidence_summary and cumulative_execution_evidence_summary; the final verification "
            "must account for evidence gathered in earlier iterations, not only the current write/finalize step. "
            "For source-grounded tasks, compare the candidate's factual claims, named sections, coverage mappings, "
            "quoted source titles, URLs, and artifact statements against verifier-visible evidence and bounded Action "
            "result previews. A citation, source URL, or file ref alone does not ground a mismatched claim; the claim "
            "must be supported by the referenced evidence content. When multiple same-site official sources are "
            "available, prefer the most specific source that directly matches the task over broader announcement or "
            "summary pages. Reject candidates that ignore a more specific verifier-visible source and ground the "
            "deliverable only in a weaker source. Reject candidates that introduce unsupported source facts, syllabus "
            "headings, repository details, dates, numbers, or report conclusions. "
            "If bounded previews are enough to contradict the candidate, set is_complete=false. If the previews are "
            "too truncated to verify a material claim, set is_complete=false and ask for scoped evidence readback. "
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
            "use it as final_result. When the plan or success criteria require a Workspace artifact, accept only "
            "trusted Workspace write/readback refs from execution evidence; model-declared file_refs are diagnostics. "
            "If evidence is incomplete, set is_complete=false and explain failure_analysis and acceptance_delta: "
            "why the task is not accepted, which acceptance facts are missing or weak, and what evidence boundary "
            "blocked verification. The verifier does not choose tools, routes, execution shapes, or exact methods. "
            "repair_constraints and next_step_requirements are advisory compatibility fields only; keep them factual "
            "and do not turn them into a narrow tool script. Also include a short human-readable replan_instruction. "
            "Set requires_block=true only when the task cannot continue."
        )
        request.output(
            {
                "is_complete": (bool, "True only when all success criteria are satisfied", True),
                "requires_block": (bool, "True only when the task cannot continue", True),
                "reason": (str, "Concise verification reason", True),
                "failure_analysis": (
                    str,
                    "Why the current result is incomplete or unacceptable; empty when complete",
                    False,
                ),
                "acceptance_delta": (
                    [str],
                    "Specific acceptance facts or evidence gaps that remain unsatisfied",
                    False,
                ),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when none"),
                "replan_instruction": (str, "Instruction for the next planning round when incomplete"),
                "repair_constraints": (
                    [str],
                    "Advisory factual constraints; not a tool, route, or execution-shape plan",
                ),
                "next_step_requirements": (
                    [str],
                    "Advisory observable requirements for future evidence; not a hard method script",
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
                "failure_analysis": str(verification),
                "acceptance_delta": self.success_criteria,
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

    def _cumulative_execution_evidence_summary(self, current_execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        for iteration in self.iterations:
            if not isinstance(iteration, Mapping):
                continue
            previous_meta = iteration.get("execution_meta")
            if isinstance(previous_meta, Mapping):
                summaries.append(self._execution_log_summary(dict(previous_meta)))
        summaries.append(self._execution_log_summary(dict(current_execution_meta)))

        combined: dict[str, Any] = {
            "model_response_count": 0,
            "action_log_count": 0,
            "action_ids": [],
            "action_statuses": {},
            "actions": [],
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "required_actions": [],
            "missing_required_actions": [],
            "selected_skill_ids": [],
            "required_skills": [],
            "missing_required_skills": [],
            "capabilities_used": [],
            "capability_evidence": {
                "actions": {"succeeded": [], "failed": []},
                "skills": {"selected": []},
                "artifacts": {"readback": []},
                "validations": {"passed": [], "failed": []},
            },
            "artifact_refs": [],
            "workspace_refs": {},
            "errors": [],
            "replan_signals": [],
            "status": str(current_execution_meta.get("status") or ""),
        }

        for summary in summaries:
            combined["model_response_count"] += int(summary.get("model_response_count") or 0)
            actions = summary.get("actions")
            if isinstance(actions, list):
                combined["actions"].extend(actions)
            artifact_refs = summary.get("artifact_refs")
            if isinstance(artifact_refs, list):
                combined["artifact_refs"].extend(artifact_refs)
            errors = summary.get("errors")
            if isinstance(errors, list):
                combined["errors"].extend(errors)
            replan_signals = summary.get("replan_signals")
            if isinstance(replan_signals, list):
                combined["replan_signals"].extend(replan_signals)
            workspace_refs = summary.get("workspace_refs")
            if isinstance(workspace_refs, Mapping):
                self._merge_workspace_ref_summary(combined["workspace_refs"], workspace_refs)
            for key in (
                "action_ids",
                "failed_actions",
                "blocked_actions",
                "approval_required_actions",
                "required_actions",
                "missing_required_actions",
                "selected_skill_ids",
                "required_skills",
                "missing_required_skills",
                "capabilities_used",
            ):
                combined[key] = self._merge_string_lists(combined.get(key), summary.get(key))
            action_statuses = summary.get("action_statuses")
            if isinstance(action_statuses, Mapping):
                combined["action_statuses"].update(DataFormatter.sanitize(action_statuses))
            capability_evidence = summary.get("capability_evidence")
            if isinstance(capability_evidence, Mapping):
                self._merge_capability_evidence_summary(combined["capability_evidence"], capability_evidence)

        combined["actions"] = self._dedupe_action_records(combined["actions"])
        combined["action_log_count"] = len(combined["actions"])
        combined["artifact_refs"] = self._dedupe_ref_records(combined["artifact_refs"])
        combined["errors"] = self._dedupe_jsonable_records(combined["errors"])
        combined["replan_signals"] = self._dedupe_jsonable_records(combined["replan_signals"])
        return DataFormatter.sanitize(combined)

    @staticmethod
    def _merge_workspace_ref_summary(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            key_text = str(key)
            if isinstance(value, list):
                bucket = target.setdefault(key_text, [])
                if isinstance(bucket, list):
                    for item in value:
                        if item not in bucket:
                            bucket.append(item)
                continue
            if key_text not in target:
                target[key_text] = value

    @classmethod
    def _merge_capability_evidence_summary(cls, target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for kind, value in source.items():
            if not isinstance(value, Mapping):
                continue
            bucket = target.setdefault(str(kind), {})
            if not isinstance(bucket, dict):
                continue
            for field, items in value.items():
                bucket[str(field)] = cls._merge_string_lists(bucket.get(str(field)), items)

    @classmethod
    def _compact_context_pack_for_verifier(cls, context_pack: "WorkspaceContextPackage") -> dict[str, Any]:
        compact = cls._compact_verifier_prompt_value(context_pack, max_chars=_VERIFIER_PROMPT_VALUE_CHARS)
        return compact if isinstance(compact, dict) else {}

    @classmethod
    def _compact_verifier_evidence_summary(cls, summary: Mapping[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, value in summary.items():
            if key == "actions" and isinstance(value, list):
                compact[key] = [cls._compact_action_record_for_verifier(ref) for ref in value[:16]]
                if len(value) > 16:
                    compact[key].append({"omitted": len(value) - 16, "reason": "prompt_budget"})
                continue
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
    def _compact_action_record_for_verifier(cls, record: Any) -> Any:
        if not isinstance(record, Mapping):
            return cls._compact_verifier_prompt_value(record, max_chars=900)
        keep_keys = (
            "id",
            "name",
            "status",
            "action_type",
            "kind",
            "action_call_id",
            "result_preview_meta",
            "result_preview_sha256",
        )
        compact: dict[str, Any] = {key: record.get(key) for key in keep_keys if key in record}
        if "result_preview" in record:
            compact["result_preview"] = cls._compact_action_preview_value(record.get("result_preview"), max_chars=5200)
        if isinstance(record.get("artifact_refs"), list):
            refs = record.get("artifact_refs") or []
            compact["artifact_refs"] = [cls._compact_artifact_ref_for_verifier(ref) for ref in refs[:4]]
            if len(refs) > 4:
                compact["artifact_refs"].append({"omitted": len(refs) - 4, "reason": "prompt_budget"})
        if record.get("file_refs"):
            compact["file_refs"] = cls._compact_verifier_prompt_value(record.get("file_refs"), max_chars=1000)
        return compact

    @classmethod
    def _compact_action_preview_value(
        cls,
        value: Any,
        *,
        max_chars: int,
        depth: int = 0,
    ) -> Any:
        value = DataFormatter.sanitize(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls._truncate_prompt_text_middle(value, max_chars)
        if depth >= 4:
            return cls._truncate_prompt_text_middle(value, max_chars)
        if isinstance(value, list):
            limit = 12
            return [
                cls._compact_action_preview_value(item, max_chars=max(360, max_chars // 2), depth=depth + 1)
                for item in value[:limit]
            ] + ([{"omitted": len(value) - limit, "reason": "prompt_budget"}] if len(value) > limit else [])
        if isinstance(value, dict):
            compacted: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 36:
                    compacted["omitted"] = {"count": len(value) - 36, "reason": "prompt_budget"}
                    break
                key_text = str(key)
                item_chars = max_chars
                if key_text in {"content", "raw", "text", "output", "result", "data", "body", "preview"}:
                    item_chars = max(1600, max_chars)
                compacted[key_text] = cls._compact_action_preview_value(
                    item,
                    max_chars=item_chars,
                    depth=depth + 1,
                )
            return compacted
        return cls._truncate_prompt_text_middle(value, max_chars)

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

    @staticmethod
    def _truncate_prompt_text_middle(value: Any, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        marker = "\n[truncated middle for verifier prompt]\n"
        available = max(0, max_chars - len(marker))
        head = max(1, available // 2)
        tail = max(1, available - head)
        return text[:head].rstrip() + marker + text[-tail:].lstrip()

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
            "failure_analysis": str(verification.get("failure_analysis") or verification.get("reason") or ""),
            "acceptance_delta": self._normalize_string_list(verification.get("acceptance_delta")),
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
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Execution step status is {execution_status}{detail}."],
            )
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
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [str(blocking_signal.get("reason") or "Execution emitted a blocked ReplanSignal.")],
            )
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
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Unresolved execution risk actions: {', '.join(risky_actions)}"],
            )
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
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Missing required capability evidence: {', '.join(missing_required)}"],
            )
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
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Missing required capability evidence: {', '.join(missing_capability_evidence)}"],
            )
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
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    ["Final result is missing."],
                )
        continuation = self._untried_read_action_continuation(execution_evidence_summary)
        if normalized["requires_block"] and continuation:
            normalized["requires_block"] = False
            guard_reasons = [reason for reason in guard_reasons if reason != "requires_block_true"]
            guard_reasons.append("untried_read_action_available")
            normalized["continuation_opportunities"] = continuation
            untried = ", ".join(continuation.get("untried_action_ids") or [])
            message = (
                "Verifier requested blocking, but read-only evidence capabilities remain untried"
                + (f": {untried}." if untried else ".")
            )
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [message],
            )
            normalized["missing_criteria"] = self._merge_string_lists(
                normalized.get("missing_criteria"),
                [message],
            )
            if not normalized["replan_instruction"]:
                normalized["replan_instruction"] = "Plan another bounded evidence-gathering step before blocking."
            self.diagnostics.setdefault("verification_continuations", []).append(
                {
                    "task_id": self.id,
                    "reason": continuation.get("reason"),
                    "untried_action_ids": continuation.get("untried_action_ids", []),
                    "failed_read_action_ids": continuation.get("failed_read_action_ids", []),
                }
            )
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
        normalized["acceptance_delta"] = self._merge_string_lists(
            normalized.get("acceptance_delta"),
            normalized.get("missing_criteria"),
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

    async def get_async_generator(
        self,
        type: Literal["delta", "instant", "streaming_parse", "all"] | str | None = "delta",
        content: Any = None,
        **__,
    ) -> AsyncGenerator[Any, None]:
        if content is not None and type is None:
            type = content
        if self._completed:
            for item in self._stream_items:
                projected = self._project_stream_item(item, type)
                if projected is not None:
                    yield projected
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
                projected = self._project_stream_item(item, type)
                if projected is not None:
                    yield projected
            await start_task
        finally:
            if queue in self._stream_queues:
                self._stream_queues.remove(queue)

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return FunctionShifter.syncify_async_generator(self.get_async_generator(*args, **kwargs))

    @staticmethod
    def _project_stream_item(item: Any, type: Any) -> Any:
        if type == "all":
            return ("agent_task", item)
        if type == "delta":
            path = str(getattr(item, "path", "") or "")
            value = getattr(item, "value", None)
            if _is_retry_status_marker_source(path, value):
                return _format_retry_marker(value)
            if getattr(item, "event_type", None) != "delta":
                return None
            delta = getattr(item, "delta", None)
            if delta is None:
                return None
            return str(delta)
        return item

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
        language = self._language_policy().get("progress_language")
        if language in (None, "", "auto"):
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
        action_records = AgentTask._collect_execution_action_records(execution_meta)
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

    @classmethod
    def _collect_execution_action_records(
        cls,
        execution_meta: Mapping[str, Any],
        *,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        logs = execution_meta.get("logs", {})
        records = cls._collect_action_records(logs if isinstance(logs, dict) else {})
        if depth >= 3:
            return cls._dedupe_action_records(records)

        blocks = execution_meta.get("blocks")
        if not isinstance(blocks, Mapping):
            return cls._dedupe_action_records(records)
        evidence = blocks.get("evidence")
        if not isinstance(evidence, Mapping):
            return cls._dedupe_action_records(records)
        for key in ("execution_block_results", "plan_block_results"):
            block_results = evidence.get(key)
            if not isinstance(block_results, Sequence) or isinstance(block_results, (str, bytes, bytearray)):
                continue
            for block_result in block_results:
                if not isinstance(block_result, Mapping):
                    continue
                output = block_result.get("output")
                if not isinstance(output, Mapping):
                    continue
                nested_meta = output.get("execution_meta")
                if isinstance(nested_meta, Mapping):
                    records.extend(cls._collect_execution_action_records(nested_meta, depth=depth + 1))
                nested_result = output.get("execution_result")
                if isinstance(nested_result, Mapping):
                    nested_result_meta = nested_result.get("execution_meta")
                    if isinstance(nested_result_meta, Mapping):
                        records.extend(cls._collect_execution_action_records(nested_result_meta, depth=depth + 1))
        return cls._dedupe_action_records(records)

    @staticmethod
    def _dedupe_action_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for record in records:
            action_id = str(record.get("id") or record.get("name") or "")
            call_id = str(record.get("action_call_id") or "")
            preview_sha = str(record.get("result_preview_sha256") or "")
            preview = str(record.get("result_preview") or "")
            if len(preview) > 120:
                preview = preview[:120]
            key = (action_id, call_id, preview_sha, preview)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(dict(record))
        return deduped

    @staticmethod
    def _dedupe_ref_records(records: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for record in records:
            if isinstance(record, Mapping):
                key = "|".join(
                    str(record.get(field) or "")
                    for field in ("artifact_id", "action_call_id", "path", "sha256", "source_url")
                )
                if not key.strip("|"):
                    key = json.dumps(DataFormatter.sanitize(record), ensure_ascii=False, sort_keys=True)
            else:
                key = str(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    @staticmethod
    def _dedupe_jsonable_records(records: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for record in records:
            try:
                key = json.dumps(DataFormatter.sanitize(record), ensure_ascii=False, sort_keys=True)
            except Exception:
                key = str(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    @classmethod
    def _collect_action_records(cls, logs: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def add_entries(entries: Any) -> None:
            if isinstance(entries, dict):
                for action_id, record in entries.items():
                    if isinstance(record, dict):
                        records.append(cls._compact_action_record(action_id, record))
                    else:
                        records.append({"id": str(action_id), "name": str(action_id), "status": str(record or "")})
            elif isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict):
                        action_id = item.get("action_id") or item.get("id") or item.get("name") or ""
                        records.append(cls._compact_action_record(action_id, item))

        add_entries(logs.get("action_logs", {}))
        route_logs = logs.get("route_logs", {})
        if isinstance(route_logs, dict):
            add_entries(route_logs.get("action_logs", {}))
            route_output = route_logs.get("output", {})
            if isinstance(route_output, dict):
                add_entries(route_output.get("history", []))
        return records

    @classmethod
    def _compact_action_record(cls, action_id: Any, record: dict[str, Any]) -> dict[str, Any]:
        normalized_id = str(action_id or record.get("action_id") or record.get("id") or record.get("name") or "")
        status = str(record.get("status") or "").strip()
        if not status:
            if record.get("error"):
                status = "failed"
            elif "result" in record or "artifact" in record:
                status = "success"
        compact: dict[str, Any] = {
            "id": normalized_id,
            "name": str(record.get("name") or normalized_id),
            "status": status,
            "action_type": str(record.get("action_type") or record.get("type") or ""),
            "kind": str(record.get("kind") or ""),
        }
        action_call_id = str(record.get("action_call_id") or record.get("call_id") or "").strip()
        if action_call_id:
            compact["action_call_id"] = action_call_id

        model_digest = record.get("model_digest")
        if not isinstance(model_digest, Mapping):
            raw = record.get("raw")
            if isinstance(raw, Mapping) and isinstance(raw.get("model_digest"), Mapping):
                model_digest = raw.get("model_digest")
        digest = model_digest if isinstance(model_digest, Mapping) else record

        result_preview = digest.get("result_preview") if isinstance(digest, Mapping) else None
        if result_preview is None and isinstance(record.get("result_preview"), (Mapping, Sequence, str)):
            result_preview = record.get("result_preview")
        if result_preview is not None:
            compact["result_preview"] = cls._compact_action_preview_value(result_preview, max_chars=5200)
        result_preview_meta = digest.get("result_preview_meta") if isinstance(digest, Mapping) else None
        if result_preview_meta is None:
            result_preview_meta = record.get("result_preview_meta")
        if result_preview_meta is not None:
            compact["result_preview_meta"] = cls._compact_verifier_prompt_value(result_preview_meta, max_chars=500)

        for key in ("artifact_refs", "file_refs"):
            value = digest.get(key) if isinstance(digest, Mapping) else None
            if value is None:
                value = record.get(key)
            if key == "artifact_refs" and isinstance(value, list):
                compact[key] = [cls._compact_artifact_ref_for_verifier(ref) for ref in value[:8]]
                if len(value) > 8:
                    compact[key].append({"omitted": len(value) - 8, "reason": "prompt_budget"})
            elif key == "file_refs" and value:
                compact[key] = cls._compact_verifier_prompt_value(value, max_chars=1200)

        preview_meta = compact.get("result_preview_meta")
        if isinstance(preview_meta, Mapping):
            sha = preview_meta.get("sha256") or preview_meta.get("result_sha256")
            if sha:
                compact["result_preview_sha256"] = str(sha)
        return compact

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
