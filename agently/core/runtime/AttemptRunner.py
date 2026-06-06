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
import inspect
from typing import Any, AsyncGenerator

from agently.types.data import AttemptDecision, AttemptHandlers, AttemptObservation, AttemptState


def core_attempt_runner_entrypoint(func: Any) -> Any:
    """Mark a request_model method whose implementation delegates to AttemptRunner."""

    setattr(func, "_agently_core_attempt_runner_entrypoint", True)
    return func


def is_core_attempt_runner_entrypoint(method: Any) -> bool:
    func = getattr(method, "__func__", method)
    return bool(getattr(func, "_agently_core_attempt_runner_entrypoint", False))


class AttemptRunner:
    """Provider-agnostic async attempt lifecycle runner.

    The runner owns retry/output-started lifecycle mechanics. Domain handlers
    own concrete behavior and must report facts through decisions or
    observations rather than emitting framework runtime events directly.
    """

    def __init__(self, handlers: AttemptHandlers, *, state: AttemptState | None = None):
        self.handlers = handlers
        self.state = state or AttemptState()

    @staticmethod
    def _default_output_started(item: tuple[str, Any], _state: AttemptState) -> bool:
        event, _payload = item
        return event != "error"

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _observe(self, observation: AttemptObservation) -> None:
        if self.handlers.on_observation is None:
            return
        await self._maybe_await(self.handlers.on_observation(observation, self.state))

    async def _observe_all(self, observations: list[AttemptObservation]) -> None:
        for observation in observations:
            await self._observe(observation)

    async def run_stream(self) -> AsyncGenerator[tuple[str, Any], None]:
        while True:
            try:
                async for item in self.handlers.execute(self.state):
                    event, payload = item
                    if event == "error":
                        await self._observe(
                            AttemptObservation(
                                "error_yielded",
                                {
                                    "attempt_index": self.state.attempt_index,
                                    "error": payload,
                                },
                            )
                        )
                        yield item
                        continue
                    is_output_started = self.handlers.is_output_started or self._default_output_started
                    if not self.state.output_started and is_output_started(item, self.state):
                        self.state.output_started = True
                        await self._observe(AttemptObservation("output_started", {"attempt_index": self.state.attempt_index}))
                    yield item
                return
            except BaseException as error:
                if isinstance(error, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    # Control-flow signals, not classifiable attempt errors. GeneratorExit is
                    # raised when a downstream consumer closes the stream early (e.g. a response
                    # adapter that breaks after the terminal event); routing it through error
                    # classification would emit a spurious, empty-message requester error.
                    raise
                decision = await self._maybe_await(self.handlers.handle_error(error, self.state))
                if not isinstance(decision, AttemptDecision):
                    raise TypeError("Attempt error handler must return AttemptDecision.") from error
                await self._observe_all(decision.observations)

                if decision.action == "retry":
                    if self.state.output_started and not decision.allow_after_output_started:
                        await self._observe(
                            AttemptObservation(
                                "retry_blocked",
                                {
                                    "attempt_index": self.state.attempt_index,
                                    "reason": decision.reason,
                                    "output_started": True,
                                },
                            )
                        )
                        raise error
                    if self.state.max_attempts is not None and self.state.attempt_index >= self.state.max_attempts:
                        raise error
                    self.state.attempt_index += 1
                    self.state.output_started = False
                    await self._observe(
                        AttemptObservation(
                            "retry",
                            {
                                "attempt_index": self.state.attempt_index,
                                "reason": decision.reason,
                            },
                        )
                    )
                    continue
                if decision.action == "yield_error":
                    yielded_error = decision.error or error
                    await self._observe(
                        AttemptObservation(
                            "error_yielded",
                            {
                                "attempt_index": self.state.attempt_index,
                                "error": yielded_error,
                            },
                        )
                    )
                    yield "error", yielded_error
                    return
                if decision.action == "stop":
                    return
                raise decision.error or error
