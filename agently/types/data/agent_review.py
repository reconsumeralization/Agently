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
from typing import Literal, TYPE_CHECKING, TypeAlias
from typing_extensions import TypedDict

from .agent_artifact import AgentArtifactResult

if TYPE_CHECKING:
    from agently.core.TaskWorkspace import TaskWorkspace
    from agently.types.plugins import AgentExecution


AgentReviewQuality: TypeAlias = Literal["strong", "adequate", "weak", "not_assessable"]
AgentReviewFailureAction: TypeAlias = Literal["warn", "block"]


class AgentReviewIssue(TypedDict):
    criterion: str
    finding: str
    evidence: str
    suggestions: list[str]


class AgentReviewCheck(TypedDict):
    rule_key: str
    status: Literal["satisfied", "violated", "not_assessable"]
    evidence: str


class AgentReviewResult(TypedDict):
    """Normalized observation produced by one AgentExecution review handler."""

    review_id: str
    index: int
    on_fail: AgentReviewFailureAction
    source: Literal["handler", "model", "host"]
    handler: str | None
    passed: bool
    quality_level: AgentReviewQuality | None
    summary: str
    checks: list[AgentReviewCheck]
    issues: list[AgentReviewIssue]
    overall_suggestions: list[str]


@dataclass(frozen=True, slots=True)
class AgentReviewContext:
    """Execution context for a replacement reviewer, including trusted artifacts."""

    execution: "AgentExecution"
    prompt: Mapping[str, object]
    goals: tuple[str, ...]
    success_criteria: tuple[str, ...]
    artifact_refs: tuple[AgentArtifactResult, ...]
    task_workspace: "TaskWorkspace"
    on_fail: AgentReviewFailureAction
    index: int
    rules: tuple[str, ...] = ()


AgentReviewHandlerResult: TypeAlias = bool | Mapping[str, object]
AgentReviewHandler: TypeAlias = Callable[
    [object, AgentReviewContext],
    AgentReviewHandlerResult | Awaitable[AgentReviewHandlerResult],
]


__all__ = [
    "AgentReviewContext",
    "AgentReviewCheck",
    "AgentReviewIssue",
    "AgentReviewQuality",
    "AgentReviewFailureAction",
    "AgentReviewHandler",
    "AgentReviewHandlerResult",
    "AgentReviewResult",
]
