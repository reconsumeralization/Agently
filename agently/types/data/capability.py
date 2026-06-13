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

CapabilitySideEffectOwner = Literal["action", "skill", "provider", "workflow"]

CapabilityRiskLevel = Literal[
    "read_only",
    "filesystem_write",
    "local_exec",
    "network",
    "external_side_effect",
]


class CapabilitySideEffectDescriptor(TypedDict, total=False):
    """Single shared side-effect declaration contract.

    Action declarations, SkillsExecutor inference, and outbound policy input must
    converge on this one descriptor instead of maintaining parallel declaration
    shapes for filesystem, network, credential, local-exec, durable-data, and
    external side-effect claims (spec sections 8.4 / 11.5).

    The descriptor is the producer/consumer seam:

    - Action declarations report file/network/credential/local-exec/external
      side-effect needs through it.
    - SkillsExecutor inference produces it.
    - PolicyApproval / outbound policy consume it.
    - Workspace links decision and outcome evidence back to it.

    A declared descriptor never grants permission; it only declares intent that
    policy then allows or denies.
    """

    capability_id: str
    owner: CapabilitySideEffectOwner
    filesystem: dict[str, Any]
    network: dict[str, Any]
    credentials: list[str]
    local_execution: dict[str, Any]
    external_side_effects: list[str]
    durable_data_access: dict[str, Any]
    risk_level: CapabilityRiskLevel
    evidence_requirements: list[str]
    approval_required: bool
    diagnostics: dict[str, Any]
