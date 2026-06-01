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

from typing import Any, Literal, TypeAlias
from typing_extensions import TypedDict

from .workspace import WorkspaceRecordRef


AgentExecutionMode: TypeAlias = Literal["one_turn", "task_step"]
AgentExecutionStatus: TypeAlias = Literal["created", "running", "success", "blocked", "error", "cancelled"] | str


class AgentExecutionLineage(TypedDict, total=False):
    task_id: str | None
    iteration_id: str | None
    step_id: str | None
    parent_execution_id: str | None
    scope: dict[str, Any]


class AgentExecutionLimits(TypedDict, total=False):
    allow_create_task: bool
    max_model_requests: int | None
    max_nested_agent_steps: int | None
    max_seconds: float | None
    max_no_progress_seconds: float | None


class AgentExecutionOutputPolicy(TypedDict, total=False):
    delta_emit_interval: float | None
    delta_max_chars: int | None
    delta_max_items: int | None
    flush_on_done: bool


class AgentExecutionWorkspaceRefs(TypedDict, total=False):
    observations: list[str]
    artifacts: list[str]
    decisions: list[str]
    checkpoints: list[str]
    verification_evidence: list[str]


class AgentExecutionDiagnostics(TypedDict, total=False):
    budget: dict[str, Any]
    limit_events: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    stalls: list[dict[str, Any]]
    timeouts: list[dict[str, Any]]
    stages: dict[str, Any]
    last_progress: dict[str, Any]


class AgentExecutionRouteInfo(TypedDict, total=False):
    selected_route: str
    selected_by: str | None
    options: dict[str, Any]
    reusable: bool


class AgentExecutionActionLog(TypedDict, total=False):
    action_call_id: str | None
    action_id: str
    status: str
    success: bool | None
    source: str
    route: str
    data: dict[str, Any]
    model_digest: dict[str, Any]
    artifact_refs: list[dict[str, Any]]


class AgentExecutionMeta(TypedDict, total=False):
    execution_id: str
    execution_mode: AgentExecutionMode
    status: AgentExecutionStatus
    lineage: AgentExecutionLineage
    limits: AgentExecutionLimits
    output_policy: AgentExecutionOutputPolicy
    route_plan: dict[str, Any]
    route: AgentExecutionRouteInfo
    close_snapshot: dict[str, Any]
    logs: dict[str, Any]
    diagnostics: AgentExecutionDiagnostics
    workspace_refs: AgentExecutionWorkspaceRefs


class AgentExecutionStreamMeta(TypedDict, total=False):
    execution_id: str
    execution_mode: AgentExecutionMode
    lineage: AgentExecutionLineage


class AgentExecutionWorkspaceRecord(TypedDict, total=False):
    record: WorkspaceRecordRef
    checkpoint: WorkspaceRecordRef | None
    workspace_refs: AgentExecutionWorkspaceRefs
