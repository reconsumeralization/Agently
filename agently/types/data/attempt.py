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

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal


AttemptDecisionAction = Literal["retry", "raise", "yield_error", "stop"]


@dataclass
class AttemptState:
    """Provider-agnostic lifecycle state for one retryable attempt loop."""

    attempt_index: int = 1
    output_started: bool = False
    max_attempts: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttemptObservation:
    """A framework fact reported by a handler and interpreted by core."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttemptDecision:
    action: AttemptDecisionAction
    reason: str | None = None
    error: BaseException | None = None
    observations: list[AttemptObservation] = field(default_factory=list)

    @classmethod
    def retry(cls, *, reason: str | None = None) -> "AttemptDecision":
        return cls("retry", reason=reason)

    @classmethod
    def raise_error(cls, error: BaseException | None = None, *, reason: str | None = None) -> "AttemptDecision":
        return cls("raise", reason=reason, error=error)

    @classmethod
    def yield_error(cls, error: BaseException | None = None, *, reason: str | None = None) -> "AttemptDecision":
        return cls("yield_error", reason=reason, error=error)

    @classmethod
    def stop(cls, *, reason: str | None = None) -> "AttemptDecision":
        return cls("stop", reason=reason)


AttemptExecuteHandler = Callable[[AttemptState], AsyncGenerator[tuple[str, Any], None]]
AttemptErrorHandler = Callable[[BaseException, AttemptState], AttemptDecision | Awaitable[AttemptDecision]]
AttemptObservationHandler = Callable[[AttemptObservation, AttemptState], None | Awaitable[None]]
AttemptOutputStartedHandler = Callable[[tuple[str, Any], AttemptState], bool]


@dataclass(frozen=True)
class AttemptHandlers:
    execute: AttemptExecuteHandler
    handle_error: AttemptErrorHandler
    on_observation: AttemptObservationHandler | None = None
    is_output_started: AttemptOutputStartedHandler | None = None
