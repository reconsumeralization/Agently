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

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import BlockCompileRequest, EvidenceEnvelope, ExecutionBlockGraph, PlanBlock
from agently.types.plugins import Blocks as BlocksPlugin

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.core.orchestration.TriggerFlow import TriggerFlow
    from agently.utils import Settings


class Blocks:
    """Thin core entrypoint over the active Blocks plugin."""

    def __init__(self, plugin_manager: "PluginManager", settings: "Settings"):
        self.plugin_manager = plugin_manager
        self.settings = settings

    def _plugin(self) -> BlocksPlugin:
        plugin_name = str(self.settings.get("plugins.Blocks.activate", "AgentlyBlocks"))
        plugin_class = cast(type[BlocksPlugin], self.plugin_manager.get_plugin("Blocks", plugin_name))
        return plugin_class()

    def list_plan_block_summaries(self, context: Mapping[str, Any] | None = None) -> list[PlanBlock]:
        return self._plugin().list_plan_block_summaries(context)

    def compile(self, request: BlockCompileRequest | Mapping[str, Any]) -> ExecutionBlockGraph:
        return self._plugin().compile(request)

    def bind_runtime(self, graph: ExecutionBlockGraph, flow: "TriggerFlow | None" = None) -> Any:
        return self._plugin().bind_runtime(graph, flow)

    def map_evidence(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> EvidenceEnvelope:
        return self._plugin().map_evidence(graph, runtime_output)

    def map_result(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return self._plugin().map_result(graph, runtime_output)
