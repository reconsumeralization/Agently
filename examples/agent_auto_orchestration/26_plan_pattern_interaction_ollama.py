"""Built-in beta plan Pattern with request-local connected clarification.

Run:
    python examples/agent_auto_orchestration/26_plan_pattern_interaction_ollama.py

Environment:
    Local Ollama at OLLAMA_BASE_URL (default http://127.0.0.1:11434/v1).
    AGENT_PATTERN_OLLAMA_MODEL or OLLAMA_DEFAULT_MODEL (default qwen3.5:9b).

The readiness stage must ask for the deliberately omitted delivery format. The
request-local interaction handler answers the resulting ExecutionExchange, and
the Pattern then returns an actionable plan through the caller's output contract.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from agently.types.data import (  # noqa: E402
    ExecutionExchangeView,
    OutputValidateResult,
)
from examples.agent_auto_orchestration._ollama_qwen import (  # noqa: E402
    configure_ollama_qwen,
)


WORKSHOP_FACTS: dict[str, Any] = {
    "topic": "safe rollout of model-backed support routing",
    "duration_minutes": 120,
    "audience": "18 support engineers in Shanghai and Singapore",
    "timezone": "all participants are in UTC+8; there is no time-zone difference",
    "target_date": "2026-09-18",
    "success_metric": "participants correctly triage at least 4 of 5 practice cases",
    "practice_cases": [
        {
            "case_id": "case-1-policy-match",
            "summary": "a routine request clearly covered by the approved support policy",
        },
        {
            "case_id": "case-2-account-access",
            "summary": "an account-access request that requires identity verification",
        },
        {
            "case_id": "case-3-billing-dispute",
            "summary": "a billing dispute that must be escalated to the billing queue",
        },
        {
            "case_id": "case-4-sensitive-data",
            "summary": "a request containing sensitive data that must not enter the model prompt",
        },
        {
            "case_id": "case-5-ambiguous-intent",
            "summary": "an ambiguous request that needs human clarification before routing",
        },
    ],
    "known_missing_constraint": (
        "delivery format is intentionally absent; it materially changes venue/platform, "
        "facilitation, rehearsal, and contingency work, so clarify it before planning"
    ),
}

RUNTIME_ROOT = (
    ROOT
    / ".example_runtime"
    / "agent_auto_orchestration"
    / "plan_pattern_interaction"
)


def validate_plan(result: dict[str, Any], _context: object) -> OutputValidateResult:
    delivery_format = result.get("delivery_format")
    agenda = result.get("session_agenda", [])
    agenda_minutes = sum(
        item.get("duration_minutes", 0)
        for item in agenda
        if isinstance(item, dict)
        and isinstance(item.get("duration_minutes"), int)
        and not isinstance(item.get("duration_minutes"), bool)
    )
    steps = result.get("steps", [])
    step_orders = [
        item.get("order") for item in steps if isinstance(item, dict)
    ]
    issues: list[str] = []
    offered_case_ids = {
        str(item["case_id"]) for item in WORKSHOP_FACTS["practice_cases"]
    }
    planned_case_ids = [
        str(case_id)
        for item in agenda
        if isinstance(item, dict)
        for case_id in item.get("case_ids", [])
    ]
    if delivery_format != "remote_zoom":
        issues.append("delivery_format must be remote_zoom after clarification")
    if agenda_minutes != WORKSHOP_FACTS["duration_minutes"]:
        issues.append(
            "session_agenda duration_minutes must sum to exactly 120 minutes"
        )
    if step_orders != list(range(1, len(step_orders) + 1)):
        issues.append("steps must use consecutive one-based order values")
    if set(planned_case_ids) != offered_case_ids:
        issues.append(
            "session_agenda case_ids must collectively cover every supplied case_id "
            "and contain no other case id"
        )
    if issues:
        return {"ok": False, "reason": "; ".join(issues)}
    return True


async def main() -> None:
    model = configure_ollama_qwen(max_tokens=2200)
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)
    clarification_views: list[ExecutionExchangeView] = []

    async def answer_clarification(
        exchange: ExecutionExchangeView,
    ) -> dict[str, object]:
        clarification_views.append(exchange)
        questions = exchange["payload"].get("questions", [])
        return {
            "delivery_format": "remote workshop using Zoom breakout rooms",
            "recording_allowed": False,
            "answers": [
                {
                    "question": item.get("question", ""),
                    "answer": (
                        "Run it remotely in Zoom with breakout rooms. Do not record. "
                        "Use the existing company Zoom tenant; no new procurement is needed."
                    ),
                }
                for item in questions
                if isinstance(item, dict)
            ],
        }

    agent = Agently.create_agent(
        "plan-pattern-interaction-ollama"
    ).use_task_workspace(RUNTIME_ROOT, mode="read_write")
    agent.set_settings("plugins.AgentPattern.plan.max_questions_per_round", 2)
    agent.set_settings("plugins.AgentPattern.plan.max_clarification_rounds", 2)
    execution = (
        agent.input({"workshop_request": WORKSHOP_FACTS})
        .instruct(
            "Create the implementation plan, not the workshop materials. Ask for any supplied "
            "constraint explicitly marked as materially missing before finalizing. Treat the "
            "provided practice-case summaries and UTC+8 fact as authoritative: schedule all five "
            "case ids, do not invent replacement cases or a time-zone difference, and make each "
            "acceptance check observable without assuming a perfect participant outcome."
        )
        .output(
            {
                "objective": (str, "Concrete planning objective.", "not_null"),
                "delivery_format": (
                    str,
                    "Exactly remote_zoom, matching the connected clarification response.",
                    True,
                ),
                "assumptions": [(str, "Explicit accepted assumption or clarification.")],
                "session_agenda": [
                    {
                        "activity": (str, "Reader-facing workshop activity.", True),
                        "duration_minutes": (
                            int,
                            "Positive whole minutes; all agenda items must sum to exactly 120.",
                            True,
                        ),
                        "case_ids": [
                            (
                                str,
                                "Zero or more supplied practice case_id values used in this activity; "
                                "across the agenda all five supplied ids must be covered and no new id is allowed.",
                            )
                        ],
                    }
                ],
                "steps": [
                    {
                        "order": (int, "One-based execution order.", True),
                        "owner": (str, "Accountable role.", True),
                        "work": (str, "Concrete planned work.", True),
                        "acceptance_check": (str, "Observable completion check.", True),
                    }
                ],
                "risks": [(str, "Plan risk and mitigation.")],
            },
            format="json",
        )
        .validate(validate_plan)
        .interact(answer_clarification)
        .pattern("plan")
        .artifact("reports/workshop-plan.json")
    )

    plan = await execution.async_get_data()
    meta = await execution.async_get_meta()
    pattern_run = meta["diagnostics"].get("pattern_run")
    if pattern_run is None:
        raise RuntimeError("Expected plan Pattern diagnostics.")
    artifact_ref = meta["logs"]["artifact_refs"][0]
    artifact_plan = json.loads(
        (RUNTIME_ROOT / artifact_ref["path"]).read_text(encoding="utf-8")
    )

    print(f"model={model}")
    print(f"interaction_calls={len(clarification_views)}")
    print(
        "interaction_kind="
        f"{clarification_views[0]['kind'] if clarification_views else None}"
    )
    print(f"pattern_name={meta['pattern']['name']}")
    print(f"pattern_status={meta['pattern']['status']}")
    print(f"clarification_rounds={pattern_run['clarification_rounds']}")
    print(f"model_request_count={pattern_run['model_request_count']}")
    print(f"plan_step_count={len(plan['steps'])}")
    print(f"delivery_format={plan['delivery_format']}")
    print(
        "agenda_minutes="
        f"{sum(item['duration_minutes'] for item in plan['session_agenda'])}"
    )
    print(f"artifact_path={artifact_ref['path']}")
    print(f"artifact_readback_matches={artifact_plan == plan}")


if __name__ == "__main__":
    asyncio.run(main())


# Expected key output from one real local qwen3.5:9b run on 2026-09-05:
# model=qwen3.5:9b
# interaction_calls=1
# interaction_kind=clarification
# pattern_name=plan
# pattern_status=completed
# clarification_rounds=1
# model_request_count=3
# plan_step_count=5
# delivery_format=remote_zoom
# agenda_minutes=120
# artifact_path=reports/workshop-plan.json
# artifact_readback_matches=True
#
# Plan wording and step count remain model-owned. The handler answers the
# connected exchange; Host validation owns duration arithmetic and case-id
# membership before TaskWorkspace accepts the artifact.
