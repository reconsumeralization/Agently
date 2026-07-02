from __future__ import annotations

import asyncio
import json
from pathlib import Path
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


TASK_ID = "support_ticket_policy_reply"
OUTPUT_FILE = "outputs/customer_reply.md"

SUPPORT_TICKET_CONTEXT: dict[str, Any] = {
    "ticket": {
        "ticket_id": "SUP-1842",
        "customer_id": "cust-river-17",
        "customer_message": (
            "We were charged twice for our May workspace subscription. Please refund one charge today. "
            "If you cannot refund it, explain exactly what happens next."
        ),
        "customer_tone": "frustrated but factual",
        "account_plan": "growth",
        "region": "US",
    },
    "billing_records": [
        {
            "invoice_id": "INV-10091",
            "period": "2026-05",
            "amount_usd": 240,
            "status": "paid",
            "payment_reference": "ch_4Vt_primary",
            "created_at": "2026-05-01T09:12:00Z",
        },
        {
            "invoice_id": "INV-10094",
            "period": "2026-05",
            "amount_usd": 240,
            "status": "paid",
            "payment_reference": "ch_4Vt_retry",
            "created_at": "2026-05-01T09:19:00Z",
        },
    ],
    "support_policy_excerpts": [
        "Customer replies must not promise a refund before finance review creates a refund case.",
        "A duplicate-charge case should name the invoice ids, amount, and expected next operational step.",
        "When billing evidence is incomplete, ask for the billing contact confirmation instead of inventing missing account facts.",
        "Replies should include a concise apology and a timeline for the next internal action.",
    ],
    "finance_queue": {
        "next_review_window": "same business day",
        "required_case_fields": ["customer_id", "invoice_ids", "amount_usd", "payment_references"],
    },
}

JUDGE_RULES = [
    "The reply uses the ticket and billing facts without inventing new facts.",
    "The reply does not promise a refund before finance review.",
    "The reply names the relevant invoice ids or clearly references the duplicate charge evidence.",
    "The reply gives a concrete next step and timeline.",
    "The reply is suitable for a frustrated customer and avoids internal-only jargon.",
]


async def main() -> None:
    workspace_dir = default_workspace("support-ticket-policy-reply")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agent = Agently.create_agent("agent-task-support-ticket-policy-reply").use_workspace(workspace_dir)
    provider = configure_agent_model_pool(agent, temperature=0.0)
    workspace = agent.workspace
    if workspace is None:
        raise RuntimeError("Workspace was not initialized.")

    agent.enable_workspace_file_actions(read=True, write=True, expose_to_model=True)

    @agent.action_func
    def lookup_ticket_context(ticket_id: str) -> dict[str, Any]:
        """Return ticket, billing, and policy facts for a support ticket."""
        _ = ticket_id
        return SUPPORT_TICKET_CONTEXT

    agent.use_actions(lookup_ticket_context)
    await workspace.ingest(
        content=SUPPORT_TICKET_CONTEXT,
        collection="observations",
        kind="support_ticket_business_context",
        summary="Support ticket, billing records, and policy excerpts for SUP-1842",
        scope={"task_id": TASK_ID},
        source={"type": "mock_business_system", "name": "support_ticket_context"},
    )

    print("[SETUP] Support ticket policy reply")
    print(f"[SETUP] Workspace: {workspace_dir}")
    print(f"[SETUP] Provider: {provider}, model_key={TASK_MODEL_KEY}")

    execution = agent.create_task(
        task_id=TASK_ID,
        goal=(
            "Prepare a customer-facing support reply for ticket SUP-1842. Use lookup_ticket_context and the "
            "Workspace business context as factual evidence. Write the final reply to "
            f"{OUTPUT_FILE}. The business system provides facts only; the model must judge whether the reply "
            "responsibly handles the policy and billing context."
        ),
        success_criteria=[
            "The final Markdown reply file exists in Workspace.",
            "The reply is grounded in the ticket, billing records, and policy excerpts.",
            "The reply does not invent facts or promise an operational outcome not supported by the business context.",
            "The reply gives the customer a concrete next step and appropriate tone.",
            "The execution evidence includes reading back or checking the final file content.",
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
    stream_trace_path = workspace_dir / "outputs" / "support_ticket_policy_reply_stream.jsonl"
    stream_trace_path.parent.mkdir(parents=True, exist_ok=True)
    with stream_trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator(type="instant"):
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
            trace_file.flush()
            print_stream_item(item)

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    output_path = workspace.files_root / OUTPUT_FILE
    artifact_text = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
    model_judge = await judge_business_artifact(
        agent,
        scenario="Support ticket reply with billing facts and policy constraints.",
        artifact_text=artifact_text,
        business_context=SUPPORT_TICKET_CONTEXT,
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
# model_judge_passed=true, example_accepted=true, replan_count=0,
# first_verification_failed=false.
# The mock business system returns facts only; AgentTask verification and the
# final model_judge decide whether the reply satisfies the business rules.
