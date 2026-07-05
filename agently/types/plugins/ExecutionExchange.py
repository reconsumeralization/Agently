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

from typing import Any, Awaitable, Protocol, runtime_checkable

from agently.types.data import ExecutionExchangeProviderResult, ExecutionExchangeRequest


@runtime_checkable
class ExecutionExchangeProvider(Protocol):
    """Transport/execution seam for execution exchanges.

    ``publish_request`` is the only required member and keeps the round-001
    contract: it is called after the interrupt is persisted and before it is
    exposed, and may merge ``exchange_id``/``request_ref``/metadata back into
    the ExternalWait envelope.

    Providers may additionally implement optional capability methods that the
    ExecutionExchangeManager feature-detects with ``getattr``:

    - ``await_response(request) -> Any | None``: connected-mode hot wait; block
      until a response payload is available or return ``None`` on timeout /
      abandon. Never called by TriggerFlow core — only by execution-handle
      owners through the manager.
    - ``cancel_request(request, *, reason) -> None``: notified when a pending
      exchange is cancelled or expired so the channel can clean up.
    - ``list_pending(scope) -> list[ExecutionExchangeRequest]``: enumerate
      durable pending requests for host recovery after restart.
    """

    def publish_request(
        self,
        execution_id: str,
        request: ExecutionExchangeRequest,
        *,
        interrupt: dict[str, Any],
    ) -> ExecutionExchangeProviderResult | dict[str, Any] | None | Awaitable[ExecutionExchangeProviderResult | dict[str, Any] | None]: ...
