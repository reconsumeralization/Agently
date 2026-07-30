from __future__ import annotations

import asyncio
import contextvars
import time
from collections import deque
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from agently_stage import Stage, StageClosedError


T = TypeVar("T")


@dataclass(frozen=True)
class ManagedTaskOutcome:
    """Agently-owned projection of one locally managed task completion."""

    task: asyncio.Task[Any]
    origin: str
    cancelled: bool
    error: BaseException | None


def create_deadline(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    return time.monotonic() + max(0.0, timeout)


def remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


class StageManagedTaskScope:
    """Private Agently adapter over Stage's caller-loop task mechanism."""

    def __init__(
        self,
        *,
        on_done: Callable[[], object] | None = None,
    ) -> None:
        self._retained_tasks: set[asyncio.Task[Any]] = set()
        self._retained_outcomes: deque[ManagedTaskOutcome] = deque()
        self._on_done = on_done
        self._stage = Stage(on_adopted_done=self._handle_task_done)
        self._closed = False
        self._close_completed = False

    def _handle_task_done(self, task: asyncio.Task[Any], origin: str) -> None:
        retained = task in self._retained_tasks
        cancelled = task.cancelled()
        error = None if cancelled else task.exception()
        if not retained and self._on_done is None:
            return
        if retained:
            self._retained_tasks.discard(task)
            if not cancelled and error is not None:
                self._retained_outcomes.append(
                    ManagedTaskOutcome(
                        task=task,
                        origin=origin,
                        cancelled=False,
                        error=error,
                    )
                )
        if self._on_done is not None:
            self._on_done()

    def spawn(
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        origin: str,
        retain_outcome: bool = False,
    ) -> asyncio.Task[T]:
        if self._closed:
            raise StageClosedError(
                "Cannot submit work to a closed Agently managed-task scope"
            )
        loop = asyncio.get_running_loop()
        task = contextvars.copy_context().run(loop.create_task, coroutine)
        try:
            return self.adopt(task, origin=origin, retain_outcome=retain_outcome)
        except BaseException:
            task.cancel()
            raise

    def adopt(
        self,
        task: asyncio.Task[T],
        *,
        origin: str,
        retain_outcome: bool = False,
    ) -> asyncio.Task[T]:
        if self._closed:
            raise StageClosedError(
                "Cannot adopt work into a closed Agently managed-task scope"
            )
        registered_origin = self._stage.origin_for_adopted(task)
        if registered_origin is not None:
            if registered_origin != origin:
                raise RuntimeError(
                    f"An Agently managed task cannot have two origins: {registered_origin!r} and {origin!r}"
                )
            if retain_outcome:
                self._retained_tasks.add(task)
            return task
        if retain_outcome:
            self._retained_tasks.add(task)
        try:
            self._stage.adopt(task, origin=origin)
        except BaseException:
            self._retained_tasks.discard(task)
            raise
        return task

    def suppress_retained_outcome(self, tasks: Iterable[asyncio.Task[Any]]) -> None:
        for task in tasks:
            self._retained_tasks.discard(task)

    def take_retained_outcomes(self) -> tuple[ManagedTaskOutcome, ...]:
        outcomes = tuple(self._retained_outcomes)
        self._retained_outcomes.clear()
        return outcomes

    @property
    def pending_count(self) -> int:
        return self._stage.adopted_count

    @property
    def pending_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return self._stage.adopted_tasks

    @property
    def pending_origins(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                origin
                for task in self._stage.adopted_tasks
                if (origin := self._stage.origin_for_adopted(task)) is not None
            )
        )

    def origin_for(self, task: asyncio.Task[Any]) -> str | None:
        return self._stage.origin_for_adopted(task)

    async def wait_settled(self, timeout: float | None = None) -> None:
        await self._stage.async_wait_settled(timeout=timeout)

    async def close(
        self,
        *,
        timeout: float | None = None,
        cancel: bool = False,
    ) -> None:
        if self._close_completed:
            return
        self._closed = True
        if cancel:
            await self._stage.async_cancel_and_wait_settled(timeout=timeout)
        await self._stage.async_close(timeout=timeout)
        self._close_completed = True
