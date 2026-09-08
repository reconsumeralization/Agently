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
import time
from typing import Any, Literal, TYPE_CHECKING

from agently.core.application.AgentExecution import RuntimeStageStallError

if TYPE_CHECKING:
    from .execution import AgentExecution


def execution_wall_clock_limits(owner: "AgentExecution") -> tuple[float | None, float | None]:
    """Task facade wall clocks are lifetime bounds; its request caps remain per step."""
    task_limits = owner.task_strategy_options().get("limits") if owner.is_task_strategy() else None
    task_limits = task_limits if isinstance(task_limits, dict) else {}
    result: list[float | None] = []
    for key in ("max_seconds", "max_no_progress_seconds"):
        candidates = [float(value) for value in (owner.limits.get(key), task_limits.get(key))
                      if not isinstance(value, bool) and isinstance(value, (int, float))]
        result.append(min(candidates) if candidates else None)
    return result[0], result[1]


async def await_route_with_limits(
    owner: "AgentExecution", run_coro: Any, *, enforce_execution_deadline: bool = False,
):
    max_seconds, max_no_progress_seconds = execution_wall_clock_limits(owner)
    if max_seconds is None and max_no_progress_seconds is None:
        return await run_coro

    task_strategy_owns_wall_clock = False
    is_task_strategy = getattr(owner, "is_task_strategy", None)
    if callable(is_task_strategy):
        try:
            task_strategy_owns_wall_clock = bool(is_task_strategy())
        except Exception:
            task_strategy_owns_wall_clock = False
    hard_deadline = (
        owner.execution_context.started_at + float(max_seconds)
        if max_seconds is not None and (enforce_execution_deadline or owner.revision > 0 or not task_strategy_owns_wall_clock)
        else None
    )
    idle_limit = (
        float(max_no_progress_seconds)
        if max_no_progress_seconds is not None
        and (enforce_execution_deadline or owner.revision > 0 or not task_strategy_owns_wall_clock)
        else None
    )
    if hard_deadline is not None and time.monotonic() >= hard_deadline:
        if asyncio.iscoroutine(run_coro):
            run_coro.close()
        raise build_execution_stall_error(
            owner,
            status="timed_out",
            message=f"AgentExecution hard deadline exceeded: max_seconds={max_seconds}.",
            elapsed_seconds=time.monotonic() - owner.execution_context.started_at,
            idle_seconds=time.monotonic() - owner.execution_context.last_progress_at,
            timeout_seconds=float(max_seconds) if max_seconds is not None else None,
        )
    task = asyncio.create_task(run_coro)
    try:
        while True:
            now = time.monotonic()
            next_timeouts: list[float] = []
            if hard_deadline is not None:
                next_timeouts.append(max(0.0, hard_deadline - now))
            if idle_limit is not None:
                idle_deadline = owner.execution_context.last_progress_at + idle_limit
                next_timeouts.append(max(0.0, idle_deadline - now))
            if not next_timeouts:
                return await task

            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=min(next_timeouts))
            except asyncio.TimeoutError as error:
                if task.done():
                    return await task
                now = time.monotonic()
                if hard_deadline is not None and now >= hard_deadline:
                    await cancel_limited_task(task)
                    raise build_execution_stall_error(
                        owner,
                        status="timed_out",
                        message=(
                            "AgentExecution hard deadline exceeded: "
                            f"max_seconds={ max_seconds }."
                        ),
                        elapsed_seconds=now - owner.execution_context.started_at,
                        idle_seconds=now - owner.execution_context.last_progress_at,
                        timeout_seconds=float(max_seconds) if max_seconds is not None else None,
                    ) from error
                if idle_limit is not None:
                    idle_seconds = now - owner.execution_context.last_progress_at
                    if idle_seconds >= idle_limit:
                        await cancel_limited_task(task)
                        raise build_execution_stall_error(
                            owner,
                            status="stalled",
                            message=(
                                "AgentExecution made no progress before idle deadline: "
                                f"max_no_progress_seconds={ max_no_progress_seconds }."
                            ),
                            elapsed_seconds=now - owner.execution_context.started_at,
                            idle_seconds=idle_seconds,
                            timeout_seconds=idle_limit,
                        ) from error
    except BaseException:
        await cancel_limited_task(task)
        raise


async def cancel_limited_task(task: "asyncio.Task[Any]"):
    if task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def build_execution_stall_error(
    owner: "AgentExecution",
    *,
    status: Literal["stalled", "timed_out"],
    message: str,
    elapsed_seconds: float | None,
    idle_seconds: float | None,
    timeout_seconds: float | None,
) -> RuntimeStageStallError:
    last_event = owner.execution_context.last_progress_event or {}
    return RuntimeStageStallError(
        message,
        stage=str(last_event.get("stage") or "agent_execution"),
        status=status,
        elapsed_seconds=elapsed_seconds,
        idle_seconds=idle_seconds,
        timeout_seconds=timeout_seconds,
        last_progress_event=(
            str(last_event.get("event_type"))
            if last_event.get("event_type") is not None
            else None
        ),
    )
