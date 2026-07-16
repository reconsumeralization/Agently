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
        return event not in {"error", "status"}

    @staticmethod
    def _error_reason(error: BaseException) -> str:
        """Return a bounded, serializable explanation without a traceback."""

        reason = str(error).strip()
        # OpenAICompatible may include a full request body after this label in
        # synthesized transport errors. The status stream needs the provider
        # explanation, not potentially sensitive prompt content.
        if "\nRequest Data:" in reason:
            reason = reason.split("\nRequest Data:", 1)[0].rstrip()
        if not reason:
            reason = error.__class__.__name__
        return reason[:4096]

    def _failure_status(
        self,
        error: BaseException,
        *,
        retry: bool,
        next_attempt_index: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "failed",
            "attempt_index": self.state.attempt_index,
            "retry": retry,
            "reason": self._error_reason(error),
            "error_type": error.__class__.__name__,
        }
        if next_attempt_index is not None:
            payload["next_attempt_index"] = next_attempt_index
        to_diagnostic = getattr(error, "to_diagnostic", None)
        if callable(to_diagnostic):
            try:
                error_diagnostic = to_diagnostic()
            except Exception:
                error_diagnostic = None
            if isinstance(error_diagnostic, dict):
                payload["error_diagnostic"] = dict(error_diagnostic)
        return payload

    def _cancelled_status(self, error: BaseException) -> dict[str, Any]:
        return {
            "status": "cancelled",
            "attempt_index": self.state.attempt_index,
            "retry": False,
            "reason": self._error_reason(error),
            "error_type": error.__class__.__name__,
        }

    def _completed_status(self) -> dict[str, Any]:
        return {
            "status": "completed",
            "attempt_index": self.state.attempt_index,
            "retry": False,
        }

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
                        error = payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
                        status = self._failure_status(error, retry=False)
                        await self._observe(AttemptObservation("status", dict(status)))
                        yield "status", status
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
                        return
                    is_output_started = self.handlers.is_output_started or self._default_output_started
                    if not self.state.output_started and is_output_started(item, self.state):
                        self.state.output_started = True
                        await self._observe(AttemptObservation("output_started", {"attempt_index": self.state.attempt_index}))
                    yield item
                completed = self._completed_status()
                await self._observe(AttemptObservation("status", dict(completed)))
                yield "status", completed
                return
            except BaseException as error:
                if isinstance(error, (GeneratorExit, SystemExit)):
                    # GeneratorExit is raised when a downstream consumer closes the stream
                    # early. It is not a request outcome and must not become a failed status.
                    raise
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
                    # This is best effort: an uncatchable process termination cannot emit a
                    # stream marker. Do not route cancellation through provider error policy.
                    yield "status", self._cancelled_status(error)
                    raise
                decision = await self._maybe_await(self.handlers.handle_error(error, self.state))
                if not isinstance(decision, AttemptDecision):
                    raise TypeError("Attempt error handler must return AttemptDecision.") from error
                await self._observe_all(decision.observations)

                if decision.action == "retry":
                    retry_allowed = True
                    if self.state.output_started and not decision.allow_after_output_started:
                        retry_allowed = False
                    if self.state.max_attempts is not None and self.state.attempt_index >= self.state.max_attempts:
                        retry_allowed = False
                    next_attempt_index = self.state.attempt_index + 1 if retry_allowed else None
                    status = self._failure_status(
                        error,
                        retry=retry_allowed,
                        next_attempt_index=next_attempt_index,
                    )
                    await self._observe(AttemptObservation("status", dict(status)))
                    yield "status", status
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
                    status = self._failure_status(yielded_error, retry=False)
                    await self._observe(AttemptObservation("status", dict(status)))
                    yield "status", status
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
                    status = self._failure_status(error, retry=False)
                    await self._observe(AttemptObservation("status", dict(status)))
                    yield "status", status
                    return
                status = self._failure_status(decision.error or error, retry=False)
                await self._observe(AttemptObservation("status", dict(status)))
                yield "status", status
                raise decision.error or error
