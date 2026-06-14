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
from typing import Any, Protocol, runtime_checkable

from agently.types.data import (
    BlockCompileRequest,
    EvidenceEnvelope,
    ExecutionBlockGraph,
    PlanBlock,
)

from .base import AgentlyPlugin


@runtime_checkable
class Blocks(AgentlyPlugin, Protocol):
    """Core-owned Blocks plugin contract.

    Implementations expose planner-facing PlanBlocks, lower bounded
    ExecutionPlan or TaskDAG segments to ExecutionBlockGraph, bind those graphs
    to TriggerFlow execution, and map runtime outputs to result/evidence views.
    They do not own AgentTaskLoop control flow, permission grants, TaskDAG
    validation, TriggerFlow dispatch, or terminal task acceptance.
    """

    def list_plan_block_summaries(self, context: Mapping[str, Any] | None = None) -> list[PlanBlock]: ...

    def compile(self, request: BlockCompileRequest | Mapping[str, Any]) -> ExecutionBlockGraph: ...

    def bind_runtime(self, graph: ExecutionBlockGraph, flow: Any = None) -> Any: ...

    def map_evidence(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> EvidenceEnvelope: ...

    def map_result(
        self,
        graph: ExecutionBlockGraph,
        runtime_output: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...
