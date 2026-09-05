"""AgentExecution artifact delivery, advisory review, and blocking handler review.

Run:
    python examples/agent_auto_orchestration/25_agent_execution_delivery_review_ollama.py

Environment:
    Local Ollama at OLLAMA_BASE_URL (default http://127.0.0.1:11434/v1).
    AGENT_PATTERN_OLLAMA_MODEL or OLLAMA_DEFAULT_MODEL (default qwen).

The model produces a release-risk brief and performs the advisory review. The
host-owned handler review checks only the declared delivery invariant: the
TaskWorkspace artifact must have a trusted physical readback before the run can
finish successfully.
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
from agently.types.data import AgentReviewContext  # noqa: E402
from examples.agent_auto_orchestration._ollama_qwen import (  # noqa: E402
    configure_ollama_qwen,
)


RUNTIME_ROOT = (
    ROOT
    / ".example_runtime"
    / "agent_auto_orchestration"
    / "agent_execution_delivery_review"
)

RELEASE_FACTS: dict[str, Any] = {
    "change_id": "checkout-cache-v2",
    "traffic_plan": "10% -> 50% -> 100%",
    "rollback_slo": "restore the previous cache path within 10 minutes",
    "observations": [
        "staging p95 checkout latency improved from 820 ms to 510 ms",
        "one stale-price incident occurred when invalidation was delayed by 47 seconds",
        "the rollback command passed the staging rehearsal",
    ],
}


def check_artifact_delivery(
    _result: object,
    context: AgentReviewContext,
) -> dict[str, object]:
    verified_refs = [
        ref
        for ref in context.artifact_refs
        if ref.get("role") == "artifact"
        and ref.get("complete_readback_verified") is True
        and bool(ref.get("sha256"))
    ]
    passed = len(verified_refs) == 1
    return {
        "passed": passed,
        "summary": (
            "The declared release brief has one trusted TaskWorkspace readback."
            if passed
            else "The declared release brief is missing its trusted TaskWorkspace readback."
        ),
        "issues": [] if passed else [{
            "criterion": "Exactly one readback-verified artifact.",
            "finding": "The declared artifact is missing verified readback.",
            "evidence": str(context.artifact_refs),
            "suggestions": ["Inspect the artifact delivery failure."],
        }],
        "overall_suggestions": [],
    }


async def main() -> None:
    model = configure_ollama_qwen(max_tokens=2600)
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)

    agent = Agently.create_agent(
        "agent-execution-delivery-review-ollama"
    ).use_task_workspace(RUNTIME_ROOT, mode="read_write")
    execution = (
        agent.input({"release_facts": RELEASE_FACTS})
        .info(
            {
                "decision_policy": [
                    "recommend hold when an observed correctness risk has no mitigation",
                    "recommend proceed_with_guardrails when rollback and monitoring bound the risk",
                    "recommend proceed only when no material observed risk remains",
                ]
            }
        )
        .instruct(
            "Prepare a concise operator-facing release decision. Ground every risk and action "
            "in the supplied facts, distinguish observations from recommendations, and do not "
            "invent test results or approvals."
        )
        .output(
            {
                "decision": (
                    str,
                    "Exactly one of: hold, proceed_with_guardrails, proceed.",
                    True,
                ),
                "summary": (str, "Two or three grounded sentences.", "not_null"),
                "risks": [(str, "Observed release risk and its factual basis.")],
                "next_actions": [
                    {
                        "owner": (str, "Accountable role, not a person's invented name.", True),
                        "action": (str, "Concrete action tied to an observed risk.", True),
                    }
                ],
            },
            format="json",
        )
        .artifact("reports/release-risk.json")
        .review(rules=["Check that observed risks are distinguished from proposed mitigations."])
        .review(check_artifact_delivery, on_fail="block")
    )

    data = await execution.async_get_data(max_retries=0)
    meta = await execution.async_get_meta()
    artifact_ref = meta["logs"]["artifact_refs"][0]
    artifact_data = json.loads(
        (RUNTIME_ROOT / artifact_ref["path"]).read_text(encoding="utf-8")
    )
    reviews = meta.get("reviews", [])
    if len(reviews) != 2:
        raise RuntimeError("Expected one model review and one blocking handler review.")

    print(f"model={model}")
    print(f"decision={data['decision']}")
    print(f"artifact_path={artifact_ref['path']}")
    print(
        "artifact_readback_verified="
        f"{artifact_ref['complete_readback_verified'] is True}"
    )
    print(f"artifact_preserves_business_result={artifact_data == data}")
    print(f"review_source={reviews[0]['source']}")
    print(f"review_passed={reviews[0]['passed']}")
    print(f"handler_review_source={reviews[1]['source']}")
    print(f"handler_review_passed={reviews[1]['passed']}")
    print("result=" + json.dumps(data, ensure_ascii=False))
    print("reviews=" + json.dumps(reviews, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())


# Expected key output from one real local qwen run on 2026-09-05:
# model=qwen
# decision=proceed_with_guardrails
# artifact_path=reports/release-risk.json
# artifact_readback_verified=True
# artifact_preserves_business_result=True
# review_source=model
# review_passed=True
# handler_review_source=handler
# handler_review_passed=True
#
# Decisions, prose, and quality judgments remain model-owned, not fixed answers.
# This recorded run demonstrates delivery and review; it is not a real release approval.
