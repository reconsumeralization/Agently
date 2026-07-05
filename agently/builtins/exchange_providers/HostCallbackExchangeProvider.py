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

import asyncio
import inspect
import time
import uuid
from typing import Any, Callable


class HostCallbackExchangeProvider:
    """Connected-mode exchange provider for embedded online hosts.

    ``publish_request`` notifies the host through ``on_publish`` (for example
    pushing an SSE ``input_required`` card); ``await_response`` blocks until
    the host resolves the exchange through :meth:`resolve` / :meth:`deny`
    (typically from an HTTP approve/deny endpoint on the same event loop).
    The hot-wait timeout is applied by the ExecutionExchangeManager.
    """

    def __init__(
        self,
        *,
        on_publish: Callable[[dict[str, Any]], Any] | None = None,
        provider_name: str = "host-callback",
    ):
        self._on_publish = on_publish
        self._provider_name = provider_name
        self._entries: dict[str, dict[str, Any]] = {}

    def publish_request(self, execution_id: str, request: dict[str, Any], *, interrupt: dict[str, Any]):
        exchange_id = str(request.get("exchange_id") or f"{ self._provider_name }:{ uuid.uuid4().hex }")
        stored_request = dict(request)
        stored_request["exchange_id"] = exchange_id
        entry = {
            "exchange_id": exchange_id,
            "execution_id": execution_id,
            "request": stored_request,
            "interrupt_payload": interrupt.get("payload"),
            "event": asyncio.Event(),
            "response": None,
            "published_at": time.time(),
        }
        self._entries[exchange_id] = entry
        if self._on_publish is not None:
            notification = {
                "exchange_id": exchange_id,
                "execution_id": execution_id,
                "interrupt_id": str(request.get("interrupt_id") or ""),
                "exchange_kind": request.get("exchange_kind"),
                "payload": interrupt.get("payload"),
                "request": dict(request),
            }
            result = self._on_publish(notification)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return {
            "exchange_id": exchange_id,
            "request_ref": {"collection": "host_callback_exchanges", "id": exchange_id},
            "provider_metadata": {"provider": self._provider_name},
        }

    async def await_response(self, request: dict[str, Any]):
        exchange_id = str(request.get("exchange_id") or "")
        entry = self._entries.get(exchange_id)
        if entry is None:
            return None
        try:
            await entry["event"].wait()
        except asyncio.CancelledError:
            raise
        self._entries.pop(exchange_id, None)
        return entry.get("response")

    def resolve(self, exchange_id: str, response: Any) -> bool:
        """Deliver the host decision; returns False when the id is unknown."""
        entry = self._entries.get(str(exchange_id))
        if entry is None:
            return False
        entry["response"] = response
        entry["event"].set()
        return True

    def deny(self, exchange_id: str, *, reason: str = "Denied by host.") -> bool:
        return self.resolve(
            exchange_id,
            {"status": "denied", "approved": False, "reason": reason},
        )

    def approve(self, exchange_id: str, *, reason: str = "Approved by host.", **extra: Any) -> bool:
        return self.resolve(
            exchange_id,
            {"status": "approved", "approved": True, "reason": reason, **extra},
        )

    async def cancel_request(self, request: dict[str, Any], *, reason: str = ""):
        exchange_id = str(request.get("exchange_id") or "")
        entry = self._entries.pop(exchange_id, None)
        if entry is not None:
            entry["response"] = None
            entry["event"].set()

    async def list_pending(self, scope: Any = None):
        return [dict(entry["request"]) for entry in self._entries.values() if not entry["event"].is_set()]

    def pending_views(self) -> list[dict[str, Any]]:
        return [
            {
                "exchange_id": entry["exchange_id"],
                "execution_id": entry["execution_id"],
                "exchange_kind": entry["request"].get("exchange_kind"),
                "payload": entry["interrupt_payload"],
                "published_at": entry["published_at"],
            }
            for entry in self._entries.values()
            if not entry["event"].is_set()
        ]
