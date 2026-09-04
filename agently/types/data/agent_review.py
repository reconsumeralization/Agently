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

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias
from typing_extensions import NotRequired, TypedDict


class AgentReviewResult(TypedDict):
    """Normalized observation produced by one AgentExecution review handler."""

    review_id: str
    index: int
    required: bool
    source: Literal["handler", "model"]
    handler: str | None
    passed: bool
    score: NotRequired[float | None]
    summary: NotRequired[str]
    issues: NotRequired[list[str]]
    suggestions: NotRequired[list[str]]


@dataclass(frozen=True, slots=True)
class AgentReviewContext:
    """Read-only execution context exposed to review and verification handlers."""

    execution: object
    prompt: Mapping[str, object]
    goals: tuple[str, ...]
    success_criteria: tuple[str, ...]
    artifact_refs: tuple[Mapping[str, object], ...]
    task_workspace: object
    required: bool
    index: int


AgentReviewHandlerResult: TypeAlias = bool | Mapping[str, object]
AgentReviewHandler: TypeAlias = Callable[
    [object, AgentReviewContext],
    AgentReviewHandlerResult | Awaitable[AgentReviewHandlerResult],
]


__all__ = [
    "AgentReviewContext",
    "AgentReviewHandler",
    "AgentReviewHandlerResult",
    "AgentReviewResult",
]
