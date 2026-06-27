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

from .TaskShared import *


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
                        "effective_execution_strategy": self.effective_execution_strategy,
                        "max_iterations": self.max_iterations,
                        "required_capabilities": self.options.get("capability_constraints", {}),
                    },
                )
                await execution.async_start({"task_id": self.id})
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
                        "reason": reason,
                        "iterations": len(self.iterations),
                    }
                    await self._emit("agent_task.blocked", self.result)
                await self._ensure_final_reflection()
                await self._emit("result", self.result)
                return self.result
            except BaseException as error:
                self.status = "timed_out" if self._is_timeout_error(error) else "error"
                self._error = error
                message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                self.diagnostics.setdefault("errors", []).append(
                    {"type": error.__class__.__name__, "message": message, "status": self.status}
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
            "Task exceeded its wall-clock budget " f"({limit_name}={ timeout_value }){ stage_text } before completion."
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
        message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
        error_info = {
            "type": error.__class__.__name__,
            "message": message,
            "stage": "execute",
            "iteration": iteration_index,
        }
        self.diagnostics.setdefault("execution_errors", []).append(error_info)
        selected_route = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "unknown")
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

    async def _await_task_request(self, awaitable, *, stage: str):
        timeout = self._task_request_timeout()
        task = asyncio.ensure_future(awaitable)
        heartbeat_task = self._start_heartbeat(stage=stage)
        try:
            if timeout is None:
                return await task
            return await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as error:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            reason = f"AgentTask {stage} request timed out after {timeout} seconds."
            raise _AgentTaskDeadlineExceeded(
                stage,
                reason=reason,
                limit_name="request_timeout_seconds",
                timeout_seconds=timeout,
            ) from error
        finally:
            await self._stop_heartbeat(heartbeat_task)

    async def _await_stream_next(self, stream: Any, *, stage: str) -> Any:
        timeout = self._task_request_timeout()
        limit_name = "request_timeout_seconds"
        timeout_seconds = timeout
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
                timeout_seconds=timeout if limit_name == "request_timeout_seconds" else self._task_max_seconds(),
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
            else:
                reason = f"AgentTask {stage} stream produced no event for {timeout_seconds} seconds."
                configured_timeout = timeout
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
                quiet_for = time.monotonic() - self._last_stream_emit_monotonic
                if quiet_for < interval:
                    continue
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

    def _child_execution_limits(self) -> dict[str, Any]:
        return dict(self.limits)

    def _child_execution_options(self) -> dict[str, Any]:
        options = dict(self.options)
        options.pop("request_timeout_seconds", None)
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
