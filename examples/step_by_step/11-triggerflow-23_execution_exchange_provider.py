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

"""ExecutionExchange provider smoke for TriggerFlow approval waits.

This is a low-level infrastructure example. It does not use a model: the goal is
to show how a host-owned provider receives a typed exchange request, how a host
can render a normalized pending view, and how the same TriggerFlow execution is
resumed through the normal interrupt ledger.

Expected key output from one real run:
[PENDING] approval pending demo-exchange-1
[RESOLVED] responded True
[STATE] True finance approved
"""

from __future__ import annotations

import asyncio
from typing import Any

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently.base import execution_exchange
from agently.types.data import ExecutionExchangeProviderResult, ExecutionExchangeRequest


class MemoryApprovalProvider:
    """Tiny host transport that records published approval requests."""

    def __init__(self):
        self.published: list[dict[str, Any]] = []

    def publish_request(
        self,
        execution_id: str,
        request: ExecutionExchangeRequest,
        *,
        interrupt: dict[str, Any],
    ) -> ExecutionExchangeProviderResult:
        exchange_id = f"demo-exchange-{len(self.published) + 1}"
        self.published.append(
            {
                "exchange_id": exchange_id,
                "execution_id": execution_id,
                "interrupt_id": interrupt.get("id"),
                "request": dict(request),
            }
        )
        return {
            "exchange_id": exchange_id,
            "provider_metadata": {"transport": "memory"},
        }


async def request_finance_approval(data: TriggerFlowRuntimeData):
    return await data.async_pause_for(
        type="exchange",
        exchange_kind="approval",
        payload={
            "subject": "refund:T-100",
            "question": f"Approve refund for ticket {data.input}?",
        },
        interrupt_id="refund-approval",
        resume_to="next",
        provider_id="memory-approval",
        wait_mode="disconnected",
        audit_metadata={"source": "finance_example"},
    )


async def apply_finance_decision(data: TriggerFlowRuntimeData):
    decision = data.input if isinstance(data.input, dict) else {}
    await data.async_set_state("approved", bool(decision.get("approved")))
    await data.async_set_state("approval_reason", str(decision.get("reason") or ""))


async def main():
    provider = MemoryApprovalProvider()
    execution_exchange.register_provider("memory-approval", provider, replace=True)

    flow = TriggerFlow(name="execution-exchange-provider-demo")
    flow.to(request_finance_approval).to(apply_finance_decision)

    try:
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("T-100")

        pending = execution_exchange.project_pending_exchanges(execution)
        pending_view = pending[0]
        print(
            "[PENDING]",
            pending_view.get("kind"),
            pending_view.get("status"),
            pending_view.get("exchange_id"),
        )

        await execution.async_continue_with(
            str(pending_view.get("interrupt_id")),
            {"approved": True, "reason": "finance approved"},
            resume_request_id="demo-approval-1",
            actor="finance-reviewer",
        )

        resolved = execution_exchange.project_execution_exchanges(execution)
        resolved_view = resolved[0]
        print(
            "[RESOLVED]",
            resolved_view.get("status"),
            resolved_view.get("response", {}).get("approved"),
        )

        snapshot = await execution.async_close()
        print("[STATE]", snapshot["approved"], snapshot["approval_reason"])
    finally:
        execution_exchange.unregister_provider("memory-approval")


if __name__ == "__main__":
    asyncio.run(main())
