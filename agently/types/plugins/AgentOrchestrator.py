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

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .base import AgentlyPlugin

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        AgentExecutionMode,
        AgentExecutionOutputPolicy,
        RunContext,
    )
    from agently.types.plugins.AgentExecution import AgentExecution


@runtime_checkable
class AgentOrchestrator(AgentlyPlugin, Protocol):
    """Creates response-style executions for Agent auto-orchestration."""

    def create_execution(
        self,
        agent: "BaseAgent",
        *,
        mode: "AgentExecutionMode | str" = "one_turn",
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        output_policy: "AgentExecutionOutputPolicy | dict[str, Any] | None" = None,
        parent_run_context: "RunContext | None" = None,
    ) -> "AgentExecution": ...
