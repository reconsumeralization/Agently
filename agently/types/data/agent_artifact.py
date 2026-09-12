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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TYPE_CHECKING, TypeAlias
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from agently.core.TaskWorkspace import TaskWorkspace
    from agently.types.plugins import AgentExecution


class AgentArtifactResult(TypedDict):
    """Guaranteed fields of one materialized and readback-verified artifact."""

    path: str
    sha256: str
    role: Literal["artifact"]
    complete_readback_verified: Literal[True]


@dataclass(frozen=True, slots=True)
class AgentArtifactContext:
    """Read-only context exposed to an AgentExecution artifact renderer."""

    execution: "AgentExecution"
    path: str
    index: int
    task_workspace: "TaskWorkspace"


AgentArtifactHandlerResult: TypeAlias = str | bytes
AgentArtifactHandler: TypeAlias = Callable[
    [object, AgentArtifactContext],
    AgentArtifactHandlerResult | Awaitable[AgentArtifactHandlerResult],
]


__all__ = [
    "AgentArtifactContext",
    "AgentArtifactHandler",
    "AgentArtifactHandlerResult",
    "AgentArtifactResult",
]
