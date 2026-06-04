from __future__ import annotations

import asyncio
import json
from typing import Any

from agently import Agently

from _business_example_common import (
    TASK_MODEL_KEY,
    configure_agent_model_pool,
    default_workspace,
    judge_business_artifact,
    print_stream_item,
    write_summary,
)


TASK_ID = "subscription_usage_risk_report"
OUTPUT_FILE = "outputs/subscription_risk_report.md"

USAGE_CONTEXT: dict[str, Any] = {
    "account": {
        "account_id": "acct-northwind-ai",
        "plan": "enterprise",
        "renewal_date": "2026-07-15",
        "contract_value_usd": 84000,
    },
    "monthly_usage": [
        {"month": "2026-01", "active_seats": 48, "api_calls": 930000, "critical_incidents": 0},
        {"month": "2026-02", "active_seats": 51, "api_calls": 1010000, "critical_incidents": 0},
        {"month": "2026-03", "active_seats": 49, "api_calls": 980000, "critical_incidents": 1},
        {"month": "2026-04", "active_seats": 31, "api_calls": 610000, "critical_incidents": 0},
        {"month": "2026-05", "active_seats": 22, "api_calls": 390000, "critical_incidents": 2},
    ],
    "customer_signals": [
        {"date": "2026-05-12", "source": "CSM note", "text": "Champion moved to another team."},
        {"date": "2026-05-18", "source": "Support", "text": "Asked whether lower-tier plan supports audit logs."},
        {"date": "2026-05-23", "source": "Product analytics", "text": "Only one workspace used advanced workflows in the last 30 days."},
    ],
    "playbook_excerpts": [
        "Renewal reports should separate observed facts from recommended actions.",
        "When usage drops, mention exact observed trend and avoid assigning motive without evidence.",
        "Recommended actions should be tied to the customer signals and renewal date.",
    ],
}

JUDGE_RULES = [
    "The report uses the supplied usage rows and customer signals without inventing motives.",
    "The report separates observed facts from recommendations.",
    "The report explains the usage trend in concrete terms.",
    "The report ties recommended actions to the renewal date or customer signals.",
    "The report is useful for a customer-success or revenue team reviewing renewal risk.",
]


async def main() -> None:
    workspace_dir = default_workspace("subscription-usage-risk-report")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agent = Agently.create_agent("agent-task-subscription-usage-risk-report").use_workspace(workspace_dir)
    provider = configure_agent_model_pool(agent, temperature=0.0)
    workspace = agent.workspace
    if workspace is None:
        raise RuntimeError("Workspace was not initialized.")

    agent.enable_workspace_file_actions(read=True, write=True, expose_to_model=True)

    @agent.action_func
    def fetch_subscription_usage(account_id: str) -> dict[str, Any]:
        """Return subscription, usage, customer-signal, and playbook facts for an account."""
        _ = account_id
        return USAGE_CONTEXT

    agent.use_actions(fetch_subscription_usage)
    await workspace.ingest(
        content=USAGE_CONTEXT,
        collection="observations",
        kind="subscription_usage_context",
        summary="Subscription usage and customer-signal facts for acct-northwind-ai",
        scope={"task_id": TASK_ID},
        source={"type": "mock_business_system", "name": "subscription_usage"},
    )

    print("[SETUP] Subscription usage risk report")
    print(f"[SETUP] Workspace: {workspace_dir}")
    print(f"[SETUP] Provider: {provider}, model_key={TASK_MODEL_KEY}")

    task = agent.create_task(
        task_id=TASK_ID,
        goal=(
            "Prepare a renewal-risk analysis report for account acct-northwind-ai. Use fetch_subscription_usage "
            "and Workspace context as factual evidence, then write the final report to "
            f"{OUTPUT_FILE}. The business system provides observed facts only; the model owns risk interpretation."
        ),
        success_criteria=[
            "The final Markdown report exists in Workspace.",
            "The report uses the supplied usage rows, customer signals, and playbook excerpts.",
            "The report separates observed facts from model-owned interpretation or recommendations.",
            "The report does not invent customer motives or unsupported operational facts.",
            "The execution evidence includes reading back or checking the final report content.",
        ],
        workspace=workspace_dir,
        max_iterations=3,
        limits={"max_model_requests": 12, "max_seconds": 240, "max_no_progress_seconds": 90},
        options={
            "agent_task": {
                "request_timeout_seconds": 60,
                "stream_progress": True,
                "stream_snapshots": True,
            },
            "routes": {"model_request": {"action_loop": {"max_rounds": 5}}},
        },
    )

    stream_items = []
    stream_trace_path = workspace_dir / "outputs" / "subscription_usage_risk_report_stream.jsonl"
    stream_trace_path.parent.mkdir(parents=True, exist_ok=True)
    with stream_trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in task.stream():
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
            trace_file.flush()
            print_stream_item(item)

    result = await task.run()
    meta = await task.meta()
    output_path = workspace.files_root / OUTPUT_FILE
    artifact_text = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
    model_judge = await judge_business_artifact(
        agent,
        scenario="Subscription usage and renewal-risk report.",
        artifact_text=artifact_text,
        business_context=USAGE_CONTEXT,
        rules=JUDGE_RULES,
    )
    summary = {
        "provider": provider,
        "task_status": result["status"],
        "accepted": bool(result.get("accepted", result.get("status") == "completed")),
        "example_accepted": bool(result.get("accepted", result.get("status") == "completed") and model_judge.get("accepted")),
        "artifact_status": str(result.get("artifact_status") or ("accepted" if result.get("status") == "completed" else "partial")),
        "output_file_exists": output_path.is_file(),
        "model_judge_passed": bool(model_judge.get("accepted")),
        "model_judge": model_judge,
        "replan_count": sum(1 for item in stream_items if item.path.endswith(".replan")),
        "first_verification_failed": any(
            item.path == "agent_task.iteration.1.verification"
            and isinstance(item.value, dict)
            and isinstance(item.value.get("verification"), dict)
            and item.value["verification"].get("is_complete") is False
            for item in stream_items
        ),
        "workspace_checkpoint_count": len(await workspace.checkpoint_history(TASK_ID)),
        "workspace_decision_count": len(meta["workspace_refs"]["decisions"]),
        "stream_trace_file": str(stream_trace_path),
        "output_file": str(output_path),
    }
    write_summary(summary)


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output from a real DeepSeek run on 2026-06-04:
# task_status="completed", accepted=true, output_file_exists=true,
# model_judge_passed=false, example_accepted=false, replan_count=0,
# first_verification_failed=false.
# This is an intentional quality-gate example: TaskLoop accepted the artifact,
# but the independent model judge rejected unsupported motive inference. The
# mock business system returns observed facts only; the model owns renewal-risk
# interpretation and final judgment.
