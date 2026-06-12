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

from typing import Any, Literal, TypeAlias

from typing_extensions import TypedDict

ExecutionExchangeWaitMode: TypeAlias = Literal[
    "connected",
    "disconnected",
    "connected_then_disconnected",
]
ExecutionExchangeDispatchState: TypeAlias = Literal[
    "planned",
    "persisted",
    "exposed",
    "exposure_failed",
    "accepted",
    "dispatched",
    "completed",
    "dispatch_failed",
    "cancelled",
]


class ExecutionExchangeRequest(TypedDict, total=False):
    request_id: str
    interrupt_id: str
    exchange_kind: str | None
    exchange_id: str | None
    callback_idempotency_key: str | None
    actor_id: str | None
    channel_id: str | None
    provider_id: str | None
    wait_mode: ExecutionExchangeWaitMode
    hot_wait_timeout: float | None
    cold_persistence_policy: Literal["persist", "cancel", "fail_closed"] | str
    request_payload_schema: dict[str, Any] | None
    response_payload_schema: dict[str, Any] | None
    dispatch_state: ExecutionExchangeDispatchState
    audit_metadata: dict[str, Any]
    provider_metadata: dict[str, Any]


class ExecutionExchangeProviderResult(TypedDict, total=False):
    exchange_id: str | None
    request_ref: Any
    audit_metadata: dict[str, Any]
    provider_metadata: dict[str, Any]
