# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tool-composition Blocks example: ticket, invoice, and policy review."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, cast
import sys

EXAMPLE_DIR = Path(__file__).resolve().parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from _business_ladder_runtime import BusinessCase, all_outputs, compile_case, emit, output_for, run_business_cases


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
        "action_evidence": [{"action_id": "support_ticket_lookup", "status": "success", "ticket_id": ticket["ticket_id"]}],
    }


async def invoice_lookup(context: Mapping[str, Any]) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": "Reading invoice ledger rows."})
    return {
        "system": "mock_invoice_ledger",
        "invoice_id": "INV-8821",
        "charges": [1280.40, 1280.40],
        "currency": "USD",
        "posted_count": 2,
        "action_evidence": [{"action_id": "invoice_lookup", "status": "success"}],
    }


async def policy_review(context: Mapping[str, Any]) -> dict[str, Any]:
    ticket = cast(dict[str, Any], output_for(context, "ticket") or {})
    invoice = cast(dict[str, Any], output_for(context, "invoice") or {})
    duplicate_total = sum(cast(list[float], invoice.get("charges") or [])) - float(ticket.get("amount_usd", 0))
    await emit(
        context,
        {
            "type": "business.progress",
            "message": f"Combining ticket and invoice evidence; duplicate exposure is USD {duplicate_total:.2f}.",
        },
    )
    return {
        "case_id": ticket.get("ticket_id"),
        "duplicate_exposure_usd": round(duplicate_total, 2),
        "requires_finance_approval": duplicate_total > 1000,
        "recommended_next_step": "Verify invoice ownership, then route to finance approval.",
        "action_evidence": [{"action_id": "policy_review", "status": "success"}],
    }


async def deterministic_validation(context: Mapping[str, Any]) -> dict[str, Any]:
    outputs = all_outputs(context)
    review = cast(dict[str, Any], outputs.get("review") or {})
    ok = review.get("requires_finance_approval") is True and review.get("duplicate_exposure_usd") == 1280.40
    reason = "composed tools produced finance approval decision"
    await emit(context, {"type": "business.validation", "scenario": "validate_combo", "accepted": ok, "reason": reason})
    return {"ok": ok, "reason": reason, "validation_results": [{"validator": "validate_combo", "ok": ok, "reason": reason}]}


HANDLERS = {
    "support_ticket_lookup": support_ticket_lookup,
    "invoice_lookup": invoice_lookup,
    "policy_review": policy_review,
    "deterministic_validation": deterministic_validation,
}


def build_case() -> BusinessCase:
    return {
        "case_id": "02_tool_composition",
        "title": "Ticket, invoice, and policy tools composed into one decision",
        "graph": compile_case(
            "blocks-business-tool-composition",
            [
                {"id": "ticket", "plan_block_id": "action_call", "kind": "action_call", "runtime_preferences": {"handler": "support_ticket_lookup"}},
                {"id": "invoice", "plan_block_id": "action_call", "kind": "action_call", "runtime_preferences": {"handler": "invoice_lookup"}},
                {"id": "review", "plan_block_id": "action_call", "kind": "action_call", "runtime_preferences": {"handler": "policy_review"}},
                {"id": "validate_combo", "plan_block_id": "validation", "kind": "validation", "runtime_preferences": {"handler": "deterministic_validation"}},
            ],
            [
                {"from": "ticket", "to": "invoice"},
                {"from": "invoice", "to": "review"},
                {"from": "review", "to": "validate_combo"},
            ],
        ),
        "handlers": HANDLERS,
    }


async def main() -> None:
    await run_business_cases([build_case()])


if __name__ == "__main__":
    asyncio.run(main())
