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


SkillMode = Literal["model_decision", "required"]
SkillExecutionStatus = Literal["created", "running", "success", "no_match", "blocked", "error"]


class SkillCard(TypedDict, total=False):
    skill_id: str
    name: str
    display_name: str
    description: str
    purpose: str
    activation_hints: dict[str, Any]
    content_refs: list[str]


class SkillDecisionCard(TypedDict, total=False):
    skill_id: str
    name: str
    description: str
    keywords: list[str]
    guidance_excerpt: str
    resource_summary: list[dict[str, Any]]
    checksum: str


class SkillContract(TypedDict, total=False):
    skill_id: str
    version: str
    source: dict[str, Any]
    trust_level: str
    card: SkillCard
    guidance: dict[str, Any]
    assets: dict[str, Any]
    install_metadata: dict[str, Any]
    decision_card: SkillDecisionCard
    resource_index: dict[str, Any]
    checksums: dict[str, Any]
    diagnostics: list[dict[str, Any]]
    metadata: dict[str, Any]


class SkillsPackRecord(TypedDict, total=False):
    skills_pack_id: str
    name: str
    source: str
    source_type: str
    installed_skills: list[str]
    failed_skills: list[dict[str, Any]]
    status: str


class SkillPlanSelection(TypedDict, total=False):
    skill_id: str
    skills_pack_id: str
    skills_pack_name: str
    version: str
    display_name: str
    reason: str
    selected_by: str
    required: bool
    card: SkillCard
    decision_card: SkillDecisionCard
    guidance: dict[str, Any]
    resource_index: dict[str, Any]


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
    selected_skills_packs: list[SkillsPackRecord]
    rejected_skills: list[SkillPlanRejection]
    rejected_skills_packs: list[dict[str, Any]]
    decision_cards: list[SkillDecisionCard]
    prompt_bindings: list[dict[str, Any]]
    resource_bindings: list[dict[str, Any]]
    expected_result_shape: dict[str, Any]
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
    action_logs: list[dict[str, Any]]
    intervention_records: list[dict[str, Any]]
    close_snapshot: dict[str, Any]
