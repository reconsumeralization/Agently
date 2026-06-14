# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Multi-Skill Blocks example: travel memo with expense-policy review."""

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
    "travelogue-writer": {
        "skill_id": "travelogue-writer",
        "card": {
            "name": "Travelogue Writer",
            "description": "Narrative travel writing guidance with concrete route details.",
        },
        "guidance": {
            "body": (
                "Write first-person travel notes grounded in provided stops, timing, "
                "and observations. Keep practical notes separate from the story."
            )
        },
        "resource_index": {
            "references/narrative-structure.md": {
                "kind": "reference",
                "summary": "Scene, route, and reflection structure",
                "size": 320,
            }
        },
    },
    "expense-policy-reviewer": {
        "skill_id": "expense-policy-reviewer",
        "card": {
            "name": "Expense Policy Reviewer",
            "description": "Travel expense policy checks for business trip notes.",
        },
        "guidance": {
            "body": (
                "Flag reimbursable, needs-receipt, and personal-expense items. "
                "Do not invent receipts or approvals."
            )
        },
        "resource_index": {
            "references/expense-rules.md": {
                "kind": "reference",
                "summary": "Receipt and reimbursement policy",
                "size": 220,
            }
        },
    },
}

TRIP_CONTEXT: dict[str, Any] = {
    "traveler": "Product lead",
    "route": [
        {"city": "Shanghai", "known_fact": "starting city"},
        {"city": "Suzhou", "known_fact": "customer research stop"},
        {"city": "Hangzhou", "known_fact": "partner workshop stop and final recorded stop"},
    ],
    "route_record_status": "The supplied trip record ends at the Hangzhou partner workshop; no return leg or post-trip destination is recorded.",
    "purpose": "Customer research and partner workshop",
    "observations": [
        "Suzhou customer asked for offline export support.",
        "Hangzhou partner requested a tighter integration roadmap.",
    ],
    "expense_evidence": {
        "available_receipts": ["train receipts"],
        "missing_receipts": ["one dinner receipt"],
        "approval_status": "not provided",
    },
    "expense_policy": {
        "train_receipts": "business train travel with a receipt is reimbursable evidence.",
        "missing_receipt": "a missing receipt requires a follow-up explanation or replacement receipt before reimbursement can be assessed.",
        "personal_expense_review": "the supplied evidence does not label any item as personal; final approval is not provided.",
    },
    "unknowns": [
        "exact dates",
        "departure and arrival times",
        "meal locations",
        "ride quality",
        "approval outcome",
        "return leg or post-trip destination",
        "meeting productivity or relationship quality",
    ],
}


async def travel_memo_from_multi_skills(context: Mapping[str, Any]) -> dict[str, Any]:
    guidance = {
        skill_id: SKILL_CONTRACTS[skill_id]["guidance"]["body"]
        for skill_id in ("travelogue-writer", "expense-policy-reviewer")
    }
    return await generate_model_artifact(
        context,
        artifact="travel_memo",
        business_context={"trip": TRIP_CONTEXT, "skill_guidance": guidance},
        instructions=[
            "Write a travel memo with a short narrative section and a separate expense-policy section.",
            "Use both activated Skills: narrative travel writing and expense policy review.",
            "Ground business, route, customer, partner, receipt, and approval claims in the supplied trip context.",
            "Treat the supplied route entries as the complete recorded route.",
            "Use a factual travel-memo style; omit qualitative assessments unless they are supplied as observations.",
            "When a fact is not present in the trip context, either omit it or state that it was not provided.",
            "Attach a fact_trace entry for the main factual claims in the memo.",
        ],
        output_schema={
            "title": (str, "Memo title.", True),
            "travelogue": (str, "Narrative travelogue grounded in the route and observations.", True),
            "business_findings": ([str], "Business findings from the trip.", True),
            "expense_policy_notes": ([str], "Expense policy notes and missing evidence.", True),
            "fact_trace": [
                {
                    "claim": (str, "A factual claim from the memo.", True),
                    "context_field": (str, "Trip context field supporting the claim.", True),
                    "skill_influence": (str, "Skill guidance reflected in this claim.", True),
                }
            ],
        },
    )


async def judge_travel_memo(context: Mapping[str, Any]) -> dict[str, Any]:
    output = cast(dict[str, Any], output_for(context, "draft_travel_memo") or {})
    candidate = cast(dict[str, Any], output.get("content") or {})
    judged = await model_judge(
        scenario="multi_skill_travel_memo",
        candidate=candidate,
        business_context=TRIP_CONTEXT,
        rules=[
            "The travelogue is grounded in the supplied route and observations.",
            "Business findings are separated from narrative travel writing.",
            "Expense policy notes flag the missing dinner receipt without inventing approval.",
            "Both Skills influence the final memo.",
            "Main factual claims include fact_trace entries that point to supplied trip context fields.",
            "No customer, partner, receipt, approval, route, or logistics fact is introduced beyond the supplied trip context.",
        ],
    )
    await emit(context, {"type": "business.validation", "scenario": "multi_skill_travel_memo", "accepted": judged.get("accepted")})
    ok = bool(judged.get("accepted")) and not judged.get("unsupported_claims")
    return {"ok": ok, "model_judge": judged}


HANDLERS = {
    "travel_memo_from_multi_skills": travel_memo_from_multi_skills,
    "judge_travel_memo": judge_travel_memo,
}


def build_case() -> BusinessCase:
    return {
        "case_id": "05_multi_skills",
        "title": "Two Skills guide one travel memo with policy notes",
        "graph": compile_case(
            "blocks-business-multi-skills",
            [
                {
                    "id": "activate_trip_skills",
                    "plan_block_id": "skill_activation",
                    "kind": "skill_activation",
                    "bound_inputs": {
                        "skill_ids": ["travelogue-writer", "expense-policy-reviewer"],
                        "task": "write business travel memo with expense policy notes",
                    },
                },
                {"id": "draft_travel_memo", "plan_block_id": "model_request", "kind": "model_request", "runtime_preferences": {"handler": "travel_memo_from_multi_skills"}},
                {"id": "judge_travel_memo", "plan_block_id": "validation", "kind": "validation", "runtime_preferences": {"handler": "judge_travel_memo"}},
            ],
            [
                {"from": "activate_trip_skills", "to": "draft_travel_memo"},
                {"from": "draft_travel_memo", "to": "judge_travel_memo"},
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
