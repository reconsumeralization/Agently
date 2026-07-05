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
import json
import time
import uuid
from typing import Any, Callable


class ConsoleExchangeProvider:
    """Connected-mode exchange provider for CLI products.

    ``publish_request`` renders the exchange as a terminal card;
    ``await_response`` reads one line from stdin (through ``input_func``) and
    maps it to a typed response payload. The hot-wait timeout is applied by
    the ExecutionExchangeManager, not here.
    """

    def __init__(
        self,
        *,
        input_func: Callable[[str], str] = input,
        print_func: Callable[[str], None] = print,
    ):
        self._input_func = input_func
        self._print_func = print_func
        self._pending: dict[str, dict[str, Any]] = {}

    def publish_request(self, execution_id: str, request: dict[str, Any], *, interrupt: dict[str, Any]):
        exchange_id = str(request.get("exchange_id") or f"console:{ uuid.uuid4().hex }")
        entry = {
            "execution_id": execution_id,
            "request": dict(request),
            "interrupt_payload": interrupt.get("payload"),
            "published_at": time.time(),
        }
        self._pending[exchange_id] = entry
        self._print_func(self._render_card(exchange_id, request, interrupt))
        return {
            "exchange_id": exchange_id,
            "request_ref": {"collection": "console_exchanges", "id": exchange_id},
            "provider_metadata": {"provider": "console"},
        }

    @staticmethod
    def _render_card(exchange_id: str, request: dict[str, Any], interrupt: dict[str, Any]) -> str:
        kind = str(request.get("exchange_kind") or "exchange")
        audit_metadata = request.get("audit_metadata")
        audit_metadata = audit_metadata if isinstance(audit_metadata, dict) else {}
        subject = str(audit_metadata.get("subject") or interrupt.get("exchange_kind") or kind)
        payload = interrupt.get("payload")
        try:
            payload_text = json.dumps(payload, ensure_ascii=False, default=str)[:500]
        except Exception:
            payload_text = str(payload)[:500]
        lines = [
            "",
            f"[Agently Exchange] { kind.upper() } required — { subject }",
            f"  exchange_id: { exchange_id }",
            f"  payload: { payload_text }",
        ]
        return "\n".join(lines)

    async def await_response(self, request: dict[str, Any]):
        kind = str(request.get("exchange_kind") or "exchange")
        if kind == "approval":
            prompt = "Approve? [y/N] "
        else:
            prompt = "Response: "
        try:
            answer = await asyncio.to_thread(self._input_func, prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        exchange_id = str(request.get("exchange_id") or "")
        self._pending.pop(exchange_id, None)
        text = str(answer).strip()
        if kind == "approval":
            approved = text.lower() in {"y", "yes"}
            return {
                "status": "approved" if approved else "denied",
                "approved": approved,
                "reason": "Approved from console." if approved else "Denied from console.",
            }
        return {"text": text}

    async def cancel_request(self, request: dict[str, Any], *, reason: str = ""):
        exchange_id = str(request.get("exchange_id") or "")
        if self._pending.pop(exchange_id, None) is not None:
            self._print_func(f"[Agently Exchange] { exchange_id } cancelled: { reason }")

    async def list_pending(self, scope: Any = None):
        return [dict(entry["request"]) for entry in self._pending.values()]
