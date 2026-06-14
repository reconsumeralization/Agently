# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Single-tool Blocks example: support ticket lookup with streamed progress."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, cast
import sys

EXAMPLE_DIR = Path(__file__).resolve().parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from _business_ladder_runtime import BusinessCase, all_outputs, compile_case, emit, run_business_cases


async def support_ticket_lookup(context: Mapping[str, Any]) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": "Looking up the support ticket."})
    ticket = {
        "system": "mock_support_system",
        "ticket_id": "SUP-1007",
        "customer": "Northwind Traders",
        "invoice_id": "INV-8821",
        "issue": "duplicate card charge",
        "amount_usd": 1280.40,
        "status": "needs_policy_review",
        "policy_note": "Refund requires invoice verification and finance approval above USD 1000.",
    }
    await emit(context, {"type": "business.progress", "message": "Ticket lookup returned invoice and policy facts."})
    return {
        **ticket,
        "action_evidence": [
            {
                "action_id": "support_ticket_lookup",
                "status": "success",
                "business_system": ticket["system"],
                "ticket_id": ticket["ticket_id"],
            }
        ],
    }


async def deterministic_validation(context: Mapping[str, Any]) -> dict[str, Any]:
    outputs = all_outputs(context)
    ticket = cast(dict[str, Any], outputs.get("lookup") or {})
    ok = ticket.get("ticket_id") == "SUP-1007" and ticket.get("status") == "needs_policy_review"
    reason = "ticket lookup returned required business facts"
    await emit(context, {"type": "business.validation", "scenario": "validate_ticket", "accepted": ok, "reason": reason})
    return {"ok": ok, "reason": reason, "validation_results": [{"validator": "validate_ticket", "ok": ok, "reason": reason}]}


HANDLERS = {
    "support_ticket_lookup": support_ticket_lookup,
    "deterministic_validation": deterministic_validation,
}


def build_case() -> BusinessCase:
    return {
        "case_id": "01_single_tool",
        "title": "Support ticket lookup through one Action-like tool block",
        "graph": compile_case(
            "blocks-business-single-tool",
            [
                {
                    "id": "lookup",
                    "plan_block_id": "action_call",
                    "kind": "action_call",
                    "runtime_preferences": {"handler": "support_ticket_lookup"},
                },
                {
                    "id": "validate_ticket",
                    "plan_block_id": "validation",
                    "kind": "validation",
                    "runtime_preferences": {"handler": "deterministic_validation"},
                },
            ],
            [{"from": "lookup", "to": "validate_ticket"}],
        ),
        "handlers": HANDLERS,
    }


async def main() -> None:
    await run_business_cases([build_case()])


if __name__ == "__main__":
    asyncio.run(main())
