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

from typing import Any, Awaitable, Callable, Literal

from typing_extensions import TypedDict


PolicyApprovalSource = Literal[
    "action",
    "execution_resource",
    "skills_capability",
    "task_dag",
    "agent_task",
    "triggerflow",
]
PolicyApprovalStatus = Literal["approved", "denied", "pending"]


class PolicyApprovalRequest(TypedDict, total=False):
    request_id: str
    source: PolicyApprovalSource | str
    capability: str
    subject: str
    risk: str
    payload: dict[str, Any]
    policy: dict[str, Any]
    lineage: dict[str, Any]
    execution_id: str
    meta: dict[str, Any]


class PolicyApprovalDecision(TypedDict, total=False):
    status: PolicyApprovalStatus
    approved: bool
    reason: str
    policy_override: dict[str, Any]
    wait_strategy: str
    handler: str
    meta: dict[str, Any]


PolicyApprovalHandler = Callable[
    [PolicyApprovalRequest],
    PolicyApprovalDecision | bool | Awaitable[PolicyApprovalDecision | bool],
]
