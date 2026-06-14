# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Single-Skill Blocks example: policy-safe support reply with model judge."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping, cast
import sys

EXAMPLE_DIR = Path(__file__).resolve().parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from _business_ladder_runtime import (
    BusinessCase,
    compile_case,
    emit,
    generate_model_artifact,
    model_judge,
    output_for,
    run_business_cases,
)


SKILL_CONTRACTS: dict[str, dict[str, Any]] = {
    "support-response-skill": {
        "skill_id": "support-response-skill",
        "card": {
            "name": "Support Response Skill",
            "description": "Guidance for a policy-safe customer support reply.",
        },
        "guidance": {
            "body": (
                "Acknowledge the customer issue, reference known invoice facts, "
                "do not promise a refund before policy review, and give one clear next step."
            )
        },
        "resource_index": {
            "references/refund-policy.md": {
                "kind": "reference",
                "summary": "Refund policy and escalation checklist",
                "size": 280,
            }
        },
    },
}

SUPPORT_TICKET_CONTEXT: dict[str, Any] = {
    "customer_message": "I was charged twice for invoice INV-8821. Please refund one charge.",
    "ticket_id": "SUP-1007",
    "invoice_id": "INV-8821",
    "amount_usd": 1280.40,
    "policy": "Refund requires invoice verification and finance approval above USD 1000.",
    "known_gap": "Finance approval has not happened yet.",
}


async def support_reply_from_single_skill(context: Mapping[str, Any]) -> dict[str, Any]:
    guidance = SKILL_CONTRACTS["support-response-skill"]["guidance"]["body"]
    return await generate_model_artifact(
        context,
        artifact="support_reply",
        business_context={"ticket": SUPPORT_TICKET_CONTEXT, "skill_guidance": guidance},
        instructions=[
            "Draft a customer support reply.",
            "The reply must be concise and policy-safe.",
        ],
        output_schema={
            "reply": (str, "Customer-facing reply.", True),
            "internal_note": (str, "Short internal note for the support team.", True),
        },
    )


async def judge_support_reply(context: Mapping[str, Any]) -> dict[str, Any]:
    output = cast(dict[str, Any], output_for(context, "draft_reply") or {})
    candidate = cast(dict[str, Any], output.get("content") or {})
    judged = await model_judge(
        scenario="single_skill_support_reply",
        candidate=candidate,
        business_context={
            "ticket": SUPPORT_TICKET_CONTEXT,
            "skill_guidance": SKILL_CONTRACTS["support-response-skill"]["guidance"]["body"],
        },
        rules=[
            "The reply acknowledges the duplicate charge concern.",
            "The reply does not promise a refund before policy review.",
            "The reply mentions a concrete next step.",
            "The internal note is separate from the customer-facing reply.",
        ],
    )
    await emit(context, {"type": "business.validation", "scenario": "single_skill_support_reply", "accepted": judged.get("accepted")})
    ok = bool(judged.get("accepted")) and not judged.get("unsupported_claims")
    return {"ok": ok, "model_judge": judged}


HANDLERS = {
    "support_reply_from_single_skill": support_reply_from_single_skill,
    "judge_support_reply": judge_support_reply,
}


def build_case() -> BusinessCase:
    return {
        "case_id": "04_single_skill",
        "title": "One Skill activates guidance before model-owned support reply",
        "graph": compile_case(
            "blocks-business-single-skill",
            [
                {
                    "id": "activate_support",
                    "plan_block_id": "skill_activation",
                    "kind": "skill_activation",
                    "bound_inputs": {
                        "skill_id": "support-response-skill",
                        "task": "draft policy-safe support reply",
                    },
                },
                {"id": "draft_reply", "plan_block_id": "model_request", "kind": "model_request", "runtime_preferences": {"handler": "support_reply_from_single_skill"}},
                {"id": "judge_reply", "plan_block_id": "validation", "kind": "validation", "runtime_preferences": {"handler": "judge_support_reply"}},
            ],
            [
                {"from": "activate_support", "to": "draft_reply"},
                {"from": "draft_reply", "to": "judge_reply"},
            ],
        ),
        "handlers": HANDLERS,
        "skill_contracts": SKILL_CONTRACTS,
        "needs_model": True,
    }


async def main() -> None:
    await run_business_cases([build_case()])


if __name__ == "__main__":
    asyncio.run(main())
