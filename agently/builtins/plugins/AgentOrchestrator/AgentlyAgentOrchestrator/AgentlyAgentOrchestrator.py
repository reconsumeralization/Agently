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

from typing import TYPE_CHECKING, Any

from agently.types.options import ExecutionOptions
from agently.types.plugins import AgentOrchestrator

from .modules.execution import AgentExecution

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        AgentExecutionMode,
        RunContext,
    )
    from agently.utils import Settings


class AgentlyAgentOrchestrator(AgentOrchestrator):
    """Default Agent auto-orchestration plugin."""

    name = "AgentlyAgentOrchestrator"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    OPTIONS_SCHEMAS = {
        "execution": ExecutionOptions,
    }

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def __init__(self, *, plugin_manager: Any = None, settings: "Settings"):
        self.plugin_manager = plugin_manager
        self.settings = settings

    def create_execution(
        self,
        agent: "BaseAgent",
        *,
        mode: "AgentExecutionMode | str" = "one_turn",
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: "ExecutionOptions | dict[str, Any] | None" = None,
        parent_run_context: "RunContext | None" = None,
    ) -> AgentExecution:
        return AgentExecution(
            agent,
            mode=mode,
            lineage=lineage,
            limits=limits,
            options=options,
            parent_run_context=parent_run_context,
        )
