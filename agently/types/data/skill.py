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

from typing import Any, Literal
from typing_extensions import TypedDict

from .action import ActionResult
from .execution_environment import ExecutionEnvironmentRequirement


SkillMode = Literal["model_decision", "required"]
SkillScope = Literal["request", "session", "agent", "execution"]
SkillStageKind = Literal["model", "action", "branch", "for_each", "wait", "validate", "emit"]
SkillExecutionStatus = Literal[
    "created",
    "planned",
    "running",
    "success",
    "no_match",
    "approval_required",
    "blocked",
    "error",
]


class SkillCard(TypedDict, total=False):
    skill_id: str
    version: str
    display_name: str
    purpose: str
    activation_hints: dict[str, Any]
    task_fit_examples: list[str]
    input_expectations: str
    output_expectations: str
    available_action_summary: list[str]
    required_permissions: dict[str, Any]
    risk_profile: str
    composition_hints: list[str]
    content_refs: list[str]


class SkillStage(TypedDict, total=False):
    stage_id: str
    id: str
    kind: SkillStageKind | str
    prompt: str
    action: str
    input: dict[str, Any]
    validation: dict[str, Any]
    emits: list[dict[str, Any]]
    meta: dict[str, Any]


class SkillContract(TypedDict, total=False):
    skill_id: str
    version: str
    source: dict[str, Any]
    trust_level: str
    card: SkillCard
    declared_permissions: dict[str, Any]
    dependencies: list[str]
    assets: dict[str, Any]
    declarative_stages: list[SkillStage]
    action_requirements: list[str]
    execution_environment_requirements: list[ExecutionEnvironmentRequirement]
    validation_rules: list[dict[str, Any]]
    completion_rules: dict[str, Any]
    extension_slots: dict[str, Any]
    metadata: dict[str, Any]


class SkillPlanSelection(TypedDict, total=False):
    skill_id: str
    version: str
    display_name: str
    scope: SkillScope
    reason: str
    selected_by: str
    required: bool
    card: SkillCard
    stages: list[SkillStage]


class SkillPlanRejection(TypedDict, total=False):
    skill_id: str
    reason_code: str
    reason: str


class SkillExecutionPlan(TypedDict, total=False):
    plan_id: str
    mode: SkillMode
    status: str
    task_summary: str
    selected_skills: list[SkillPlanSelection]
    rejected_skills: list[SkillPlanRejection]
    composed_stage_graph: list[dict[str, Any]]
    prompt_bindings: list[dict[str, Any]]
    action_bindings: list[dict[str, Any]]
    resource_bindings: list[dict[str, Any]]
    execution_environment_requirements: list[ExecutionEnvironmentRequirement]
    approval_requests: list[dict[str, Any]]
    state_keys: list[str]
    expected_result_shape: dict[str, Any]
    stream_policy: dict[str, Any]
    fallback_policy: dict[str, Any]
    cleanup_policy: dict[str, Any]
    diagnostics: list[dict[str, Any]]


class SkillExecutionDict(TypedDict, total=False):
    execution_id: str
    plan_id: str
    status: SkillExecutionStatus
    output: Any
    result: Any
    plan: SkillExecutionPlan
    runtime_stream: list[dict[str, Any]]
    skill_logs: list[dict[str, Any]]
    action_logs: list[ActionResult]
    approval_records: list[dict[str, Any]]
    intervention_records: list[dict[str, Any]]
    close_snapshot: dict[str, Any]
