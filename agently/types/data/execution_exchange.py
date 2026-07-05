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

from typing import Any, Awaitable, Callable, Literal, TypeAlias

from typing_extensions import TypedDict

ExecutionExchangeKind: TypeAlias = Literal[
    "approval",
    "decision",
    "control",
    "clarification",
    "supplement",
    "ack",
]
ExecutionExchangeStatus: TypeAlias = Literal[
    "pending",
    "responded",
    "cancelled",
    "expired",
]
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
    exchange_kind: ExecutionExchangeKind | str | None
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


class ExecutionExchangeRouting(TypedDict, total=False):
    provider_id: str | None
    channel_id: str | None
    wait_mode: ExecutionExchangeWaitMode
    hot_wait_timeout: float | None
    cold_persistence_policy: Literal["persist", "cancel", "fail_closed"] | str
    handler: str
    meta: dict[str, Any]


class ExecutionExchangeResponse(TypedDict, total=False):
    exchange_id: str | None
    interrupt_id: str
    payload: Any
    actor_id: str | None
    resume_request_id: str | None
    responded_at: float
    meta: dict[str, Any]


class ExecutionExchangeView(TypedDict, total=False):
    exchange_id: str | None
    interrupt_id: str
    execution_id: str
    kind: ExecutionExchangeKind | str | None
    status: ExecutionExchangeStatus
    subject: str
    source: str
    payload: Any
    request: ExecutionExchangeRequest
    response: Any
    actor_id: str | None
    created_at: float | None
    resolved_at: float | None


ExchangeRoutingHandler = Callable[
    [ExecutionExchangeRequest],
    "ExecutionExchangeRouting | None | Awaitable[ExecutionExchangeRouting | None]",
]
