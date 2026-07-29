from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from agently_stage import LocalTaskOutcome, LocalTaskScope


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
        self._scope = LocalTaskScope(on_done=self._handle_stage_outcome)

    def _handle_stage_outcome(self, outcome: LocalTaskOutcome) -> None:
        retained = outcome.task in self._retained_tasks
        if not retained and self._on_done is None:
            return
        if retained:
            self._retained_tasks.discard(outcome.task)
            if not outcome.cancelled and outcome.error is not None:
                self._retained_outcomes.append(
                    ManagedTaskOutcome(
                        task=outcome.task,
                        origin=outcome.origin,
                        cancelled=False,
                        error=outcome.error,
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
        task = self._scope.spawn(coroutine, origin=origin)
        if retain_outcome:
            self._retained_tasks.add(task)
        return task

    def adopt(
        self,
        task: asyncio.Task[T],
        *,
        origin: str,
        retain_outcome: bool = False,
    ) -> asyncio.Task[T]:
        if retain_outcome:
            self._retained_tasks.add(task)
        try:
            return self._scope.adopt(task, origin=origin)
        except BaseException:
            self._retained_tasks.discard(task)
            raise

    def suppress_retained_outcome(self, tasks: Iterable[asyncio.Task[Any]]) -> None:
        for task in tasks:
            self._retained_tasks.discard(task)

    def take_retained_outcomes(self) -> tuple[ManagedTaskOutcome, ...]:
        outcomes = tuple(self._retained_outcomes)
        self._retained_outcomes.clear()
        return outcomes

    @property
    def pending_count(self) -> int:
        return self._scope.pending_count

    @property
    def pending_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return self._scope.pending_tasks

    @property
    def pending_origins(self) -> tuple[str, ...]:
        return self._scope.pending_origins

    def origin_for(self, task: asyncio.Task[Any]) -> str | None:
        return self._scope.origin_for(task)

    async def wait_settled(self, timeout: float | None = None) -> None:
        await self._scope.wait_settled(timeout=timeout)

    async def close(
        self,
        *,
        timeout: float | None = None,
        cancel: bool = False,
    ) -> None:
        await self._scope.close(timeout=timeout, cancel=cancel)
