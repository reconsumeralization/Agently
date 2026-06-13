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


async def await_route_with_limits(owner: "AgentExecution", run_coro: Any):
    max_seconds = owner.limits.get("max_seconds")
    max_no_progress_seconds = owner.limits.get("max_no_progress_seconds")
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
        if max_seconds is not None and not task_strategy_owns_wall_clock
        else None
    )
    idle_limit = float(max_no_progress_seconds) if max_no_progress_seconds is not None else None
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
        if not task.done():
            task.cancel()
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
