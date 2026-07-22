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

from agently.types.data import TaskWorkspaceTerminalStatus

from .TaskShared import *

# A bounded AgentTask step should hand inconclusive action evidence back to the
# task planner quickly; broader retry strategy belongs to the next task step.
_AGENT_TASK_DEFAULT_ACTION_LOOP_MAX_ROUNDS = 2


class AgentTaskRuntimeMixin(AgentTaskMixinBase):
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
                await self._ensure_final_reflection()
                await self._emit("result", self.result)
                await self._apply_terminal_task_workspace_retention(
                    status="completed" if self.status == "completed" else "failed"
                )
                self._release_terminal_action_artifact_scope()
                await self._close_streams()
                return self.result
            self.status = "running"
            execution = self._flow.create_execution(auto_close=False, record_store=False)
            terminal_retention_status: TaskWorkspaceTerminalStatus | None = None
            self._lifecycle_error = None
            try:
                await self._record_phase(
                    "configured",
                    diagnostics={
                        "goals": [self.goal],
                        "success_criteria": self.success_criteria,
                        "execution_strategy": self.execution_strategy,
                        "effective_execution_strategy": self.effective_execution_strategy,
                        "max_iterations": self.max_iterations,
                        "required_capabilities": self.options.get("capability_constraints", {}),
                    },
                )
                await execution.async_start({"task_id": self.id})
                # The lifecycle graph schedules later iterations through
                # chunk-owned signals. Seal and drain those accepted signals
                # before inspecting terminal state; otherwise async_start can
                # return after lifecycle.start while transition.decide is
                # still running in the queue.
                await execution.async_close(reason="agent_task.lifecycle_complete")
                if self._lifecycle_error is not None:
                    raise self._lifecycle_error
                if self.status == "running":
                    if self.max_iterations is not None:
                        self.status = "max_iterations"
                        self.diagnostics.setdefault("terminal_reason", "max_iterations")
                        reason = "Task exhausted max_iterations before verification completed."
                    else:
                        self.status = "blocked"
                        self.diagnostics.setdefault("terminal_reason", "no_terminal_result")
                        reason = "Task execution ended without a terminal result."
                    self.result = {
                        "status": self.status,
                        "accepted": False,
                        "artifact_status": "partial",
                        "task_id": self.id,
                        "execution_strategy": self.execution_strategy,
                        "effective_execution_strategy": self.effective_execution_strategy,
                        "reason": reason,
                        "final_response": self._agent_task_user_final_response(
                            accepted=False,
                            artifact_status="partial",
                            status=self.status,
                            reason=reason,
                        ),
                        "final_result": "",
                        "artifact_refs": [],
                        "missing_criteria": [],
                    }
                    await self._emit("agent_task.blocked", self.result)
                await self._ensure_final_reflection()
                await self._emit("result", self.result)
                terminal_retention_status = (
                    "completed" if self.status == "completed" else "failed"
                )
                return self.result
            except asyncio.CancelledError as error:
                self.status = "cancelled"
                self._error = error
                self.result = {
                    "status": "cancelled",
                    "accepted": False,
                    "artifact_status": "partial",
                    "task_id": self.id,
                    "execution_strategy": self.execution_strategy,
                    "effective_execution_strategy": self.effective_execution_strategy,
                    "reason": "AgentTask was cancelled by its host.",
                    "final_response": "Task was cancelled by its host.",
                    "final_result": "",
                    "artifact_refs": [],
                    "missing_criteria": [],
                }
                await self._emit(
                    "agent_task.cancelled",
                    {"status": "cancelled", "task_id": self.id, "message": "AgentTask was cancelled by its host."},
                )
                terminal_retention_status = "cancelled"
                raise
            except BaseException as error:
                self.status = "timed_out" if self._is_timeout_error(error) else "error"
                self._error = error
                message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                self.diagnostics.setdefault("errors", []).append(
                    {"type": error.__class__.__name__, "message": message, "status": self.status}
                )
                await self._emit("agent_task.error", self.diagnostics["errors"][-1])
                terminal_retention_status = "failed"
                raise
            finally:
                # Always close the auto_close=False execution so its runtime is
                # not leaked when the loop raises (e.g. a timed-out request).
                try:
                    await execution.async_close()
                except Exception:
                    pass
                if terminal_retention_status is not None:
                    await self._apply_terminal_task_workspace_retention(
                        status=terminal_retention_status
                    )
                    self._release_terminal_action_artifact_scope()
                self.completed_at = time.time()
                self._completed = True
                await self._close_streams()

    def _run(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.async_run())
        return self.async_run()

    def _release_terminal_action_artifact_scope(self) -> None:
        """Release an untransferred task scope without changing task outcome."""

        transferred_to = str(
            getattr(self, "_action_artifact_scope_transferred_to_execution_id", "") or ""
        ).strip()
        artifact_scope = {"kind": "agent_task", "id": self.id}
        if transferred_to:
            self.diagnostics["action_artifact_release"] = {
                "status": "transferred",
                "scope": artifact_scope,
                "owner": {"kind": "agent_execution", "id": transferred_to},
            }
            return
        action = getattr(self.agent, "action", None)
        release_scope = getattr(action, "_release_artifact_scope", None)
        if not callable(release_scope):
            return
        try:
            released = release_scope(artifact_scope)
            self.diagnostics["action_artifact_release"] = {
                "status": "released",
                "scope": artifact_scope,
                "released_count": released if isinstance(released, int) else 0,
            }
        except Exception as error:
            self.diagnostics["action_artifact_release"] = {
                "status": "failed",
                "scope": artifact_scope,
                "diagnostics": [
                    {
                        "code": "agent_task.action_artifact_release_failed",
                        "message": _compact_agent_task_error_message(
                            error,
                            fallback=error.__class__.__name__,
                        ),
                    }
                ],
            }

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
            "Task exceeded its wall-clock budget " f"({limit_name}={ timeout_value }){ stage_text } before completion."
        )
        self.result = {
            "status": "timed_out",
            "accepted": False,
            "artifact_status": "partial",
            "task_id": self.id,
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "reason": reason,
            "final_response": self._agent_task_user_final_response(
                accepted=False,
                artifact_status="partial",
                status="timed_out",
                reason=reason,
            ),
            "final_result": "",
            "artifact_refs": [],
            "missing_criteria": [],
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
        child_meta: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
        error_info = {
            "type": error.__class__.__name__,
            "message": message,
            "stage": "execute",
            "iteration": iteration_index,
        }
        self.diagnostics.setdefault("execution_errors", []).append(error_info)
        selected_route = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "unknown")
        failed_meta = self._failed_execution_meta_from_child(
            child_meta,
            execution_id=execution_id,
            selected_route=selected_route,
            error_info=error_info,
        )
        return (
            {
                "step_result": "",
                "evidence": [f"Execution failed: {error.__class__.__name__}: {message}"],
                "remaining_work": [f"Retry or replan after execution failure: {message}"],
                "error": error_info,
            },
            failed_meta,
        )

    def _failed_execution_meta_from_child(
        self,
        child_meta: Mapping[str, Any] | None,
        *,
        execution_id: str | None,
        selected_route: str,
        error_info: Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(child_meta, Mapping):
            failed_meta = DataFormatter.sanitize(dict(child_meta))
            if not isinstance(failed_meta, dict):
                failed_meta = {}
        else:
            failed_meta = {}
        failed_meta["execution_id"] = str(
            failed_meta.get("execution_id") or execution_id or f"{self.id}:failed-step"
        )
        failed_meta["status"] = "failed"
        route = failed_meta.get("route")
        if not isinstance(route, Mapping):
            route = {"selected_route": selected_route}
        route = dict(route)
        route.setdefault("selected_route", selected_route)
        route["status"] = "failed"
        failed_meta["route"] = DataFormatter.sanitize(route)
        logs = failed_meta.get("logs")
        if not isinstance(logs, Mapping):
            logs = {}
        logs = dict(logs)
        logs.setdefault("action_logs", {})
        logs.setdefault("route_logs", {})
        errors = logs.get("errors")
        if not isinstance(errors, list):
            errors = []
        errors.append(DataFormatter.sanitize(dict(error_info)))
        logs["errors"] = errors
        failed_meta["logs"] = DataFormatter.sanitize(logs)
        diagnostics = failed_meta.get("diagnostics")
        if not isinstance(diagnostics, Mapping):
            diagnostics = {}
        diagnostics = dict(diagnostics)
        diagnostics["execution_error"] = DataFormatter.sanitize(dict(error_info))
        failed_meta["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return failed_meta

    async def _read_child_execution_meta(self, execution: Any) -> dict[str, Any] | None:
        getter = getattr(execution, "async_get_meta", None)
        if not callable(getter):
            return None
        try:
            meta = await cast(Callable[[], Awaitable[Any]], getter)()
        except Exception:
            return None
        return cast(dict[str, Any], meta) if isinstance(meta, Mapping) else None

    async def _await_task_request(self, awaitable, *, stage: str):
        task = asyncio.ensure_future(awaitable)
        heartbeat_task = self._start_heartbeat(stage=stage)
        request_started_monotonic = time.monotonic()
        request_timeout = self._task_request_timeout()
        no_progress_timeout = self._task_no_progress_timeout()
        try:
            while True:
                now = time.monotonic()
                latest_progress = max(
                    request_started_monotonic,
                    self._last_stream_emit_monotonic,
                )
                deadline_candidates: list[tuple[float, str, float | None]] = []
                task_remaining = self._task_deadline_remaining()
                if task_remaining is not None:
                    deadline_candidates.append(
                        (task_remaining, "max_seconds", self._task_max_seconds())
                    )
                if request_timeout is not None:
                    deadline_candidates.append(
                        (
                            request_timeout - (now - request_started_monotonic),
                            "request_timeout_seconds",
                            request_timeout,
                        )
                    )
                if no_progress_timeout is not None:
                    deadline_candidates.append(
                        (
                            no_progress_timeout - (now - latest_progress),
                            "max_no_progress_seconds",
                            no_progress_timeout,
                        )
                    )
                if not deadline_candidates:
                    return await task
                expired = [item for item in deadline_candidates if item[0] <= 0]
                if expired:
                    _, limit_name, timeout = min(expired, key=lambda item: item[0])
                    break
                wait_seconds = min(item[0] for item in deadline_candidates)
                done, _ = await asyncio.wait({task}, timeout=wait_seconds)
                if task in done:
                    return await task

            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if limit_name == "max_no_progress_seconds":
                reason = (
                    f"AgentTask {stage} request made no progress before idle deadline: "
                    f"max_no_progress_seconds={timeout}."
                )
            elif limit_name == "max_seconds":
                reason = (
                    f"AgentTask {stage} request exceeded task max_seconds="
                    f"{timeout}."
                )
            else:
                reason = f"AgentTask {stage} request timed out after {timeout} seconds."
            raise _AgentTaskDeadlineExceeded(
                stage,
                reason=reason,
                limit_name=limit_name,
                timeout_seconds=timeout,
            )
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            raise
        finally:
            await self._stop_heartbeat(heartbeat_task)

    async def _await_stream_next(self, stream: Any, *, stage: str) -> Any:
        timeout_info = self._task_request_wait_timeout()
        limit_name = timeout_info[1] if timeout_info is not None else "max_seconds"
        timeout_seconds = timeout_info[0] if timeout_info is not None else None
        remaining = self._task_deadline_remaining()
        if remaining is not None and (timeout_seconds is None or remaining < timeout_seconds):
            timeout_seconds = remaining
            limit_name = "max_seconds"
        if timeout_seconds is None:
            heartbeat_task = self._start_heartbeat(stage=stage)
            try:
                return await stream.__anext__()
            finally:
                await self._stop_heartbeat(heartbeat_task)
        if timeout_seconds <= 0:
            raise _AgentTaskDeadlineExceeded(
                stage,
                limit_name=limit_name,
                timeout_seconds=timeout_seconds if limit_name != "max_seconds" else self._task_max_seconds(),
            )
        heartbeat_task = self._start_heartbeat(stage=stage)
        try:
            return await asyncio.wait_for(stream.__anext__(), timeout=timeout_seconds)
        except StopAsyncIteration:
            raise
        except (asyncio.TimeoutError, TimeoutError) as error:
            if limit_name == "max_seconds":
                reason = f"AgentTask {stage} stream exceeded task max_seconds before the next event."
                configured_timeout = self._task_max_seconds()
            elif limit_name == "max_no_progress_seconds":
                reason = f"AgentTask {stage} stream made no progress for {timeout_seconds} seconds."
                configured_timeout = timeout_seconds
            else:
                reason = f"AgentTask {stage} stream produced no event for {timeout_seconds} seconds."
                configured_timeout = timeout_seconds
            raise _AgentTaskDeadlineExceeded(
                stage,
                reason=reason,
                limit_name=limit_name,
                timeout_seconds=configured_timeout,
            ) from error
        finally:
            await self._stop_heartbeat(heartbeat_task)

    def _heartbeat_interval_seconds(self) -> float | None:
        agent_task_options = self.options.get("agent_task")
        configured = None
        if isinstance(agent_task_options, dict):
            configured = agent_task_options.get("heartbeat_interval_seconds")
        if configured is None:
            configured = self.options.get("heartbeat_interval_seconds", 10)
        interval = self._normalize_timeout(configured)
        if interval is None or interval <= 0:
            return None
        return interval

    def _start_heartbeat(
        self,
        *,
        stage: str,
        iteration: int | None = None,
    ) -> asyncio.Task[Any] | None:
        interval = self._heartbeat_interval_seconds()
        if interval is None:
            return None
        task = asyncio.create_task(
            self._heartbeat_loop(
                stage=stage,
                iteration=iteration,
                interval=interval,
            )
        )
        self._track_background_stream_task(task)
        return task

    async def _stop_heartbeat(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        self._background_stream_tasks.discard(task)

    async def _heartbeat_loop(
        self,
        *,
        stage: str,
        iteration: int | None,
        interval: float,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()
                quiet_for = now - self._last_stream_emit_monotonic
                if quiet_for < interval:
                    continue
                if now - self._last_heartbeat_emit_monotonic < interval:
                    continue
                self._last_heartbeat_emit_monotonic = now
                await self._emit(
                    "agent_task.heartbeat",
                    {
                        "task_id": self.id,
                        "stage": stage,
                        "iteration": iteration,
                        "status": self.status,
                        "quiet_for_seconds": round(quiet_for, 3),
                    },
                    meta={
                        "task_id": self.id,
                        "status": self.status,
                        "stage": stage,
                        "iteration": iteration,
                        "stream_kind": "heartbeat",
                    },
                )
        except asyncio.CancelledError:
            raise

    def _task_request_timeout(self) -> float | None:
        # Explicit per plan/verify request timeout. max_seconds is enforced
        # separately as the task wall-clock budget, while max_no_progress_seconds
        # is reported as an idle guard rather than as a request timeout.
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict):
            configured = agent_task_options.get("request_timeout_seconds")
            if configured is not None:
                return self._normalize_timeout(configured)
        configured = self.options.get("request_timeout_seconds")
        if configured is not None:
            return self._normalize_timeout(configured)
        return None

    def _task_no_progress_timeout(self) -> float | None:
        return self._normalize_timeout(self.limits.get("max_no_progress_seconds"))

    def _task_request_wait_timeout(self) -> tuple[float, str] | None:
        candidates: list[tuple[float, str]] = []
        request_timeout = self._task_request_timeout()
        if request_timeout is not None:
            candidates.append((request_timeout, "request_timeout_seconds"))
        no_progress_timeout = self._task_no_progress_timeout()
        if no_progress_timeout is not None:
            candidates.append((no_progress_timeout, "max_no_progress_seconds"))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])

    def _child_execution_limits(self) -> dict[str, Any]:
        return dict(self.limits)

    def _task_action_loop_max_rounds(self) -> int | None:
        configured: Any = _AGENT_TASK_DEFAULT_ACTION_LOOP_MAX_ROUNDS
        if "action_loop_max_rounds" in self.options:
            configured = self.options.get("action_loop_max_rounds")
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict) and "action_loop_max_rounds" in agent_task_options:
            configured = agent_task_options.get("action_loop_max_rounds")
        if configured is None:
            return None
        if isinstance(configured, bool):
            return _AGENT_TASK_DEFAULT_ACTION_LOOP_MAX_ROUNDS
        if isinstance(configured, int) and configured >= 0:
            return configured
        return _AGENT_TASK_DEFAULT_ACTION_LOOP_MAX_ROUNDS

    def _explicit_task_action_loop_max_rounds(self) -> tuple[bool, int | None]:
        configured: Any = None
        supplied = False
        if "action_loop_max_rounds" in self.options:
            supplied = True
            configured = self.options.get("action_loop_max_rounds")
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict) and "action_loop_max_rounds" in agent_task_options:
            supplied = True
            configured = agent_task_options.get("action_loop_max_rounds")
        if not supplied or configured is None:
            return supplied, None
        if isinstance(configured, bool) or not isinstance(configured, int) or configured < 0:
            return True, _AGENT_TASK_DEFAULT_ACTION_LOOP_MAX_ROUNDS
        return True, configured

    def _apply_child_execution_action_loop_guard(
        self,
        execution: Any,
        *,
        max_rounds: int | None = None,
    ) -> Any:
        if max_rounds is None:
            max_rounds = self._task_action_loop_max_rounds()
        if max_rounds is None:
            return execution
        request = getattr(execution, "request", None)
        settings = getattr(request, "settings", None)
        set_setting = getattr(settings, "set", None)
        if callable(set_setting):
            set_setting("action.loop.max_rounds", max_rounds)
            set_setting("tool.loop.max_rounds", max_rounds)
        return execution

    def _disable_child_execution_action_loop(self, execution: Any) -> Any:
        request = getattr(execution, "request", None)
        settings = getattr(request, "settings", None)
        set_setting = getattr(settings, "set", None)
        if callable(set_setting):
            set_setting("action.loop.enabled", False)
            set_setting("tool.loop.enabled", False)
        return execution

    def _apply_taskboard_action_loop_round_dispatch_policy(self, execution: Any) -> Any:
        """Preserve value dependencies whenever a TaskBoard child enters ActionLoop."""

        request = getattr(execution, "request", None)
        settings = getattr(request, "settings", None)
        set_setting = getattr(settings, "set", None)
        if callable(set_setting):
            # TaskBoard owns card-level orchestration but cannot prove that calls
            # using different Action IDs are value-independent. Keep same-Action
            # fan-out parallel while requiring a fresh planning round before a
            # different Action capability is dispatched.
            set_setting("action.loop.round_dispatch_policy", "single_action_id_cohort")
            set_setting("tool.loop.round_dispatch_policy", "single_action_id_cohort")
            supplied, max_rounds = self._explicit_task_action_loop_max_rounds()
            # TaskBoard cards own an adaptive work unit. The generic child-loop
            # convenience guard must not cut a search -> selected read ->
            # synthesis value dependency. Task and no-progress deadlines remain
            # the safety owner unless the caller explicitly supplies a bound.
            set_setting("action.loop.max_rounds", max_rounds if supplied else None)
            set_setting("tool.loop.max_rounds", max_rounds if supplied else None)
        return execution

    def _apply_taskboard_card_action_loop_policy(self, execution: Any, card: Any) -> Any:
        """Keep exact batches bounded while allowing value-dependent Action backedges."""

        self._apply_taskboard_action_loop_round_dispatch_policy(execution)
        required_action_ids = self._taskboard_card_required_action_ids(card)
        if len(required_action_ids) <= 1:
            return self._apply_child_execution_action_loop_guard(execution, max_rounds=1)
        supplied, max_rounds = self._explicit_task_action_loop_max_rounds()
        request = getattr(execution, "request", None)
        settings = getattr(request, "settings", None)
        set_setting = getattr(settings, "set", None)
        if not callable(set_setting):
            return execution
        if not supplied:
            # A dependency-capable card may need search -> selected read ->
            # validation. Task/no-progress deadlines remain the safety owner;
            # the generic two-round convenience default must not cut the data
            # dependency simply because several calls share one card.
            set_setting("action.loop.max_rounds", None)
            set_setting("tool.loop.max_rounds", None)
            return execution
        set_setting("action.loop.max_rounds", max_rounds)
        set_setting("tool.loop.max_rounds", max_rounds)
        return execution

    def _child_execution_options(self) -> dict[str, Any]:
        options = dict(self.options)
        options.pop("request_timeout_seconds", None)
        # Every nested model consumer performs its own intent-bound TaskContext
        # read. Preserve the AgentTask's declared disclosure budget and
        # required-overflow policy across that execution boundary.
        options["context_budget"] = dict(self.context_budget)
        agent_task_options = options.get("agent_task")
        if isinstance(agent_task_options, dict):
            filtered_agent_task_options = dict(agent_task_options)
            filtered_agent_task_options.pop("request_timeout_seconds", None)
            options["agent_task"] = filtered_agent_task_options
        return options

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
        heartbeat_task = self._start_heartbeat(stage=stage)
        if remaining is None:
            try:
                return await awaitable
            finally:
                await self._stop_heartbeat(heartbeat_task)
        try:
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
        finally:
            await self._stop_heartbeat(heartbeat_task)

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


__all__ = ["AgentTaskRuntimeMixin"]
