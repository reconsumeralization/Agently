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


AgentExecutionStatus: TypeAlias = Literal["created", "running", "success", "blocked", "error", "cancelled"] | str


class AgentExecutionLineage(TypedDict):
    task_id: str | None
    iteration_id: str | None
    step_id: str | None
    parent_execution_id: str | None
    scope: dict[str, Any]


class AgentExecutionLimits(TypedDict):
    allow_create_task: bool
    max_model_requests: int | None
    max_nested_agent_steps: int | None
    max_seconds: float | None
    max_no_progress_seconds: float | None


class AgentExecutionWorkspaceRefs(TypedDict):
    observations: list[str]
    artifacts: list[str]
    decisions: list[str]
    checkpoints: list[str]
    verification_evidence: list[str]


class AgentExecutionDiagnostics(TypedDict):
    budget: dict[str, Any]
    limit_events: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    stalls: list[dict[str, Any]]
    timeouts: list[dict[str, Any]]
    stages: dict[str, Any]
    last_progress: dict[str, Any]
    required_capabilities: list[dict[str, Any]]


class AgentExecutionRouteInfo(TypedDict):
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


class AgentExecutionMeta(TypedDict):
    execution_id: str
    status: AgentExecutionStatus
    strategy: str | None
    goals: list[str]
    success_criteria: list[str]
    generated_success_criteria: list[str]
    task_refs: dict[str, Any]
    iterations: list[dict[str, Any]]
    lineage: AgentExecutionLineage
    limits: AgentExecutionLimits
    options: dict[str, Any]
    effective_options: dict[str, Any]
    consumed_options: dict[str, Any]
    route_plan: dict[str, Any]
    route: AgentExecutionRouteInfo
    close_snapshot: dict[str, Any]
    logs: dict[str, Any]
    diagnostics: AgentExecutionDiagnostics
    workspace_refs: AgentExecutionWorkspaceRefs


class PlannerSkillCandidate(TypedDict, total=False):
    """One installed-skill candidate, sanitized for planner consumption.

    Carries only inert data (no plugin objects): the skill id, the mode it is
    bound under, and an optional decision-card description. Produced by the
    orchestrator route from its route-planner output and passed into AgentTask
    options; AgentTask reads it but never reaches back into the plugin.
    """

    id: str
    mode: Literal["model_decision", "required"]
    description: str


class PlannerCapabilitySummary(TypedDict, total=False):
    """Sanitized planner-facing capability snapshot injected into AgentTask options.

    A typed, inert snapshot (skill ids + descriptions, no plugin objects),
    computed once at task construction from the top-level routing execution.
    AgentTask consumes only this snapshot; it must not import AgentOrchestrator
    or HybridRoutePlanner internals or hold an execution-draft reference.
    """

    skills: list[PlannerSkillCandidate]


class AgentExecutionStreamMeta(TypedDict, total=False):
    execution_id: str
    lineage: AgentExecutionLineage


class AgentExecutionWorkspaceRecord(TypedDict):
    record: WorkspaceRecordRef
    checkpoint: WorkspaceRecordRef | None
    workspace_refs: AgentExecutionWorkspaceRefs
