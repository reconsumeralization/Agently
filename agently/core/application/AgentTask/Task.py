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
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast, Literal, TYPE_CHECKING

from agently.core.orchestration import TriggerFlow
from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter, FunctionShifter

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import WorkspaceContextPack, WorkspaceRecordRef


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


class AgentTask:
    """Retained owner for one Agent-managed business task lifecycle."""

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        goal: str,
        success_criteria: list[str],
        workspace: str | os.PathLike[str] | None = None,
        max_iterations: int = 3,
        verify: Literal["before_done"] = "before_done",
        recall_profile: str = "software_dev",
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
        self.max_iterations = max(1, int(max_iterations))
        self.verify = verify
        self.recall_profile = recall_profile
        self.context_budget = dict(context_budget or {"chars": 6000})
        self.limits = dict(limits or {"max_model_requests": 3})
        self.options = dict(options or {})
        agent_with_workspace = cast(Any, agent)
        if workspace is not None:
            agent_with_workspace.use_workspace(workspace)
        if getattr(agent, "workspace", None) is None:
            raise RuntimeError("AgentTask requires a Workspace. Pass workspace=... or call agent.use_workspace(...).")
        self.workspace = agent_with_workspace.workspace
        self.status: AgentTaskStatus = "created"
        self.result: Any = None
        self.diagnostics: dict[str, Any] = {}
        self.iterations: list[dict[str, Any]] = []
        self.workspace_refs: dict[str, list[str]] = {
            "observations": [],
            "decisions": [],
            "verification": [],
            "checkpoints": [],
        }
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self._completed = False
        self._error: BaseException | None = None
        self._start_lock = asyncio.Lock()
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
            for iteration_index in range(1, self.max_iterations + 1):
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
            self.status = "running"
            execution = self._flow.create_execution(auto_close=False)
            try:
                await execution.async_start({"task_id": self.id})
                await execution.async_close()
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
                self.status = "error"
                self._error = error
                self.diagnostics.setdefault("errors", []).append(
                    {"type": error.__class__.__name__, "message": str(error)}
                )
                await self._emit("agent_task.error", self.diagnostics["errors"][-1])
                raise
            finally:
                self.completed_at = time.time()
                self._completed = True
                await self._close_streams()

    def _run(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.async_run())
        return self.async_run()

    async def _run_iteration(self, iteration_index: int) -> dict[str, Any]:
        await self._emit_progress(
            iteration_index,
            "context",
            f"Iteration {iteration_index}: building a Workspace context pack for the task goal.",
        )
        await self._emit(f"agent_task.iteration.{iteration_index}.started", {"iteration": iteration_index})
        context_pack = await self._build_context()
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
        plan = await self._request_plan(iteration_index, context_pack)
        await self._emit(f"agent_task.iteration.{iteration_index}.plan", plan)
        await self._emit_snapshot(
            iteration_index,
            "plan",
            {
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
        execution_result, execution_meta = await self._execute_step(iteration_index, plan, context_pack)
        await self._emit_snapshot(
            iteration_index,
            "execution",
            {
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_id": execution_meta.get("execution_id"),
                "route": execution_meta.get("route"),
                "logs": self._execution_log_summary(execution_meta),
            },
            message=f"Iteration {iteration_index}: bounded step finished; execution evidence was captured.",
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

        await self._emit_progress(
            iteration_index,
            "verify",
            f"Iteration {iteration_index}: verifying the evidence against every success criterion.",
        )
        verification = await self._request_verification(
            iteration_index,
            plan=plan,
            execution_result=execution_result,
            execution_meta=execution_meta,
            context_pack=context_pack,
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
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        if iteration_index >= self.max_iterations:
            self.status = "max_iterations"
            self.result = {
                "status": "max_iterations",
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "reason": verification.get("reason") or "Task did not pass verification before max_iterations.",
                "iterations": iteration_index,
                "verification": verification,
            }
            await self._emit_progress(
                iteration_index,
                "max_iterations",
                f"Iteration {iteration_index}: max_iterations reached before verification passed.",
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
            },
        )
        return {"terminal": False, "status": "continue"}

    async def _build_context(self) -> "WorkspaceContextPack":
        try:
            return await self.workspace.build_context(
                goal=self.goal,
                scope={"task_id": self.id},
                budget=self.context_budget,
                profile=self.recall_profile,
            )
        except Exception as error:
            fallback = await self.workspace.build_context(
                goal="",
                scope={"task_id": self.id},
                budget=self.context_budget,
                profile=self.recall_profile,
            )
            diagnostics = fallback.setdefault("diagnostics", {})
            diagnostics["fallback_reason"] = {
                "type": error.__class__.__name__,
                "message": str(error),
                "stage": "workspace.build_context",
            }
            self.diagnostics.setdefault("recall_fallbacks", []).append(diagnostics["fallback_reason"])
            return fallback

    async def _request_plan(self, iteration_index: int, context_pack: "WorkspaceContextPack") -> dict[str, Any]:
        request = self.agent.create_temp_request()
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "previous_iterations": self.iterations,
                "context_pack": DataFormatter.sanitize(context_pack),
            }
        )
        request.instruct(
            "Plan the next bounded AgentExecution step for this AgentTask. "
            "Use prior verification evidence when present. Do not finalize unless all success criteria can be verified."
        )
        request.output(
            {
                "step_instruction": (str, "Instruction for one bounded AgentExecution step", True),
                "expected_evidence": (str, "Evidence this step should produce", True),
                "rationale": (str, "Why this is the next step", True),
            },
            format="json",
        )
        plan = await self._await_task_request(request.async_get_data(), stage="plan")
        return dict(plan) if isinstance(plan, dict) else {"step_instruction": str(plan), "expected_evidence": "", "rationale": ""}

    async def _execute_step(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPack",
    ) -> tuple[Any, dict[str, Any]]:
        self.agent.request.prompt.set(
            "input",
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "plan": plan,
                "context_pack": DataFormatter.sanitize(context_pack),
            },
        )
        self.agent.request.prompt.set(
            "instruct",
            (
                "Execute exactly one bounded step for the AgentTask. "
                "Return concrete evidence for the verifier. Do not claim final completion unless evidence supports it."
            ),
        )
        self.agent.request.prompt.set(
            "output",
            {
                "step_result": (str, "Concrete result of this bounded step", True),
                "evidence": ([str], "Evidence produced by the step", True),
                "remaining_work": ([str], "Known remaining work, empty when none", True),
            },
        )
        self.agent.request.prompt.set("output_format", "json")
        execution = self.agent.create_execution(
            mode="task_step",
            lineage={
                "task_id": self.id,
                "iteration_id": f"iter-{iteration_index}",
                "step_id": "execute",
            },
            limits=self.limits,
            options=self.options,
        )
        await self._emit(f"agent_task.iteration.{iteration_index}.execution.started", {"execution_id": execution.id})
        result = await execution.async_get_data()
        meta = await execution.async_get_meta()
        await self._emit(f"agent_task.iteration.{iteration_index}.execution.completed", meta)
        return result, meta

    async def _request_verification(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        execution_result: Any,
        execution_meta: dict[str, Any],
        context_pack: "WorkspaceContextPack",
    ) -> dict[str, Any]:
        request = self.agent.create_temp_request()
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "plan": plan,
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_meta": DataFormatter.sanitize(execution_meta),
                "context_pack": DataFormatter.sanitize(context_pack),
                "previous_iterations": self.iterations,
            }
        )
        request.instruct(
            "Verify the task against every success criterion. "
            "Treat numeric criteria such as 'at least N' as exact counting rules and fail verification when the "
            "evidence does not meet the count. "
            "Require source/evidence references when the criteria ask for evidence. "
            "If execution metadata, action records, diagnostics, command output, or verifier-visible evidence shows "
            "a failed required action or failed validation command, do not mark complete. "
            "If a criterion requires a script, command, test, or external validation to pass, require explicit "
            "successful evidence for that validation before completion. "
            "When marking complete, put every required final deliverable in final_result; if final_result would omit "
            "a required deliverable, keep is_complete=false and ask for a replan. "
            "If evidence is incomplete, set is_complete=false and give a concrete replan_instruction. "
            "Set requires_block=true only when the task cannot continue."
        )
        request.output(
            {
                "is_complete": (bool, "True only when all success criteria are satisfied", True),
                "requires_block": (bool, "True only when the task cannot continue", True),
                "reason": (str, "Concise verification reason", True),
                "missing_criteria": ([str], "Unmet or weak criteria", True),
                "replan_instruction": (str, "Instruction for the next planning round when incomplete", True),
                "final_result": (str, "Final business result when complete", True),
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
                "final_result": "",
            }
        return verification

    async def _await_task_request(self, awaitable, *, stage: str):
        timeout = self._task_request_timeout()
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError as error:
            raise TimeoutError(f"AgentTask {stage} request timed out after {timeout} seconds.") from error

    def _task_request_timeout(self) -> float | None:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict):
            configured = agent_task_options.get("request_timeout_seconds")
            if configured is not None:
                return self._normalize_timeout(configured)
        configured = self.options.get("request_timeout_seconds")
        if configured is not None:
            return self._normalize_timeout(configured)
        for key in ("max_no_progress_seconds", "max_seconds"):
            configured = self.limits.get(key)
            if configured is not None:
                return self._normalize_timeout(configured)
        return None

    @staticmethod
    def _normalize_timeout(value: Any) -> float | None:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return None
        if timeout < 0:
            return None
        return timeout

    async def _record_decision(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "WorkspaceContextPack",
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
        checkpoint_ref = await self.workspace.checkpoint(
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
        await self.workspace.link(record_ref, decision_ref, relation="implements_decision")
        self._append_workspace_ref("observations", record_ref)
        self._append_workspace_ref("checkpoints", checkpoint_ref)
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
        await self.workspace.link(record_ref, observation_ref, relation="verifies_observation")
        self._append_workspace_ref("verification", record_ref)
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
            "max_iterations": self.max_iterations,
            "iterations": DataFormatter.sanitize(self.iterations),
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

    def _get_generator(self, *args: Any, **kwargs: Any):
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
                snapshot=DataFormatter.sanitize(snapshot),
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
                    "snapshot": snapshot,
                }
            )
            request.instruct(
                "Summarize AgentTask progress for a human operator using only the provided snapshot and task metadata. "
                "Do not add new facts, do not infer hidden results, and keep the message concise."
            )
            request.output(
                {
                    "message": (str, "One concise natural-language progress update.", True),
                },
                format="json",
            )
            raw = await asyncio.wait_for(request.async_get_data(), timeout=self._progress_timeout_seconds())
            message = ""
            if isinstance(raw, dict):
                message = str(raw.get("message") or "")
            else:
                message = str(raw or "")
            if not message.strip():
                return None
            return await self._emit(
                f"agent_task.iteration.{iteration}.progress.{stage}",
                {
                    "message": message.strip(),
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
                    "progress_source": "model",
                    "progress_model_key": model_key,
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

    @staticmethod
    def _execution_log_summary(execution_meta: dict[str, Any]) -> dict[str, Any]:
        logs = execution_meta.get("logs", {})
        if not isinstance(logs, dict):
            return {}
        action_logs = logs.get("action_logs", {})
        action_ids: list[str] = []
        action_statuses: dict[str, str] = {}
        if isinstance(action_logs, dict):
            for action_id, record in action_logs.items():
                normalized_id = str(action_id)
                action_ids.append(normalized_id)
                if isinstance(record, dict):
                    action_statuses[normalized_id] = str(record.get("status") or "")
        elif isinstance(action_logs, list):
            for item in action_logs:
                if isinstance(item, dict):
                    action_id = str(item.get("action_id") or item.get("name") or "")
                    if action_id:
                        action_ids.append(action_id)
                        action_statuses[action_id] = str(item.get("status") or "")
        return {
            "model_response_count": len(logs.get("model_responses", [])) if isinstance(logs.get("model_responses", []), list) else 0,
            "action_log_count": len(action_ids),
            "action_ids": action_ids,
            "action_statuses": action_statuses,
        }

    async def _emit(
        self,
        path: str,
        value: Any,
        *,
        event_type: Literal["delta", "done"] = "done",
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            is_complete=True,
            event_type=event_type,
            source="agent_task",
            task_id=self.id,
            meta=meta or {"task_id": self.id, "status": self.status},
        )
        self._stream_items.append(item)
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
            "max_iterations": self.max_iterations,
            "verify": self.verify,
        }
