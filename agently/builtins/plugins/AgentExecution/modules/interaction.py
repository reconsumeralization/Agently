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

import inspect
import uuid
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import (
    AgentInteractionHandler,
    ExecutionExchangeProviderResult,
    ExecutionExchangeRequest,
)

if TYPE_CHECKING:
    from .execution import AgentExecution


class _AgentInteractionProvider:
    """Execution-local adapter from one Agent handler to ExecutionExchange."""

    def __init__(self, handler: AgentInteractionHandler) -> None:
        self._handler = handler
        self._pending: dict[str, tuple[str, dict[str, Any]]] = {}

    def publish_request(
        self,
        execution_id: str,
        request: ExecutionExchangeRequest,
        *,
        interrupt: dict[str, Any],
    ) -> ExecutionExchangeProviderResult:
        exchange_id = str(
            request.get("exchange_id")
            or f"agent-interaction:{uuid.uuid4().hex}"
        )
        self._pending[exchange_id] = (str(execution_id), dict(interrupt))
        return {
            "exchange_id": exchange_id,
            "provider_metadata": {"provider": "agent_interaction"},
        }

    async def await_response(self, request: ExecutionExchangeRequest) -> object | None:
        exchange_id = str(request.get("exchange_id") or "")
        entry = self._pending.pop(exchange_id, None)
        if entry is None:
            return None
        execution_id, interrupt = entry
        interrupt["external_wait_request"] = dict(request)

        from agently.base import execution_exchange

        view = execution_exchange.project_exchange(execution_id, interrupt)
        response = self._handler(view)
        if inspect.isawaitable(response):
            response = await response
        return cast(object | None, response)

    async def cancel_request(
        self,
        request: ExecutionExchangeRequest,
        *,
        reason: str = "",
    ) -> None:
        del reason
        exchange_id = str(request.get("exchange_id") or "")
        self._pending.pop(exchange_id, None)


def declare_interaction(
    execution: "AgentExecution",
    handler: AgentInteractionHandler,
) -> "AgentExecution":
    target = execution._reconfiguration_target()
    if not callable(handler):
        raise TypeError("Agent interaction handler must be callable.")
    provider = _AgentInteractionProvider(handler)
    target._interaction_handler = handler
    target._interaction_provider = provider
    target.execution_context.execution_exchange_provider = provider
    # A request-local handler is a connected mechanism. This setting belongs to
    # the isolated ModelRequest, so it never changes the Agent/global posture.
    target.request.settings.set("interaction.mode", "hot")
    return target


__all__ = ["declare_interaction"]
