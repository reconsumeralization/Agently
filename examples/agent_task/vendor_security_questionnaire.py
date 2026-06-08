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


TASK_ID = "vendor_security_questionnaire"
OUTPUT_FILE = "outputs/security_questionnaire_answer.md"

SECURITY_CONTEXT: dict[str, Any] = {
    "customer_request": {
        "request_id": "SECQ-77",
        "customer": "Blue Harbor Analytics",
        "questions": [
            "Do you have SOC 2 Type II?",
            "Where is customer data stored?",
            "Do you retain prompts after service delivery?",
            "List subprocessors used for hosted inference.",
            "Do you have ISO 27001 certification?",
        ],
    },
    "internal_security_docs": [
        {
            "title": "SOC 2 status note",
            "content": "SOC 2 Type II audit is scheduled for Q3 2026. A Type I readiness review was completed in April 2026.",
        },
        {
            "title": "Data residency note",
            "content": "Default production storage is us-east-1. Enterprise customers may request EU storage during onboarding.",
        },
        {
            "title": "Prompt retention note",
            "content": "Prompt and response logs are retained for 30 days for abuse monitoring unless the customer signs the zero-retention addendum.",
        },
        {
            "title": "Hosted inference subprocessors",
            "content": "Hosted inference may use OpenAI, Anthropic, or a customer-selected OpenAI-compatible endpoint depending on model profile configuration.",
        },
    ],
    "sales_context": {
        "deal_stage": "security review",
        "requested_due_date": "2026-06-10",
        "preferred_tone": "precise and non-overpromising",
    },
}

JUDGE_RULES = [
    "The answer responds to each customer question.",
    "The answer distinguishes completed controls from planned or unavailable certifications.",
    "The answer does not claim ISO 27001 or SOC 2 Type II completion when the context does not support it.",
    "The answer names known storage, retention, and subprocessor facts from the context.",
    "The answer is precise, customer-facing, and avoids unsupported security claims.",
]


async def main() -> None:
    workspace_dir = default_workspace("vendor-security-questionnaire")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agent = Agently.create_agent("agent-task-vendor-security-questionnaire").use_workspace(workspace_dir)
    provider = configure_agent_model_pool(agent, temperature=0.0)
    workspace = agent.workspace
    if workspace is None:
        raise RuntimeError("Workspace was not initialized.")

    agent.enable_workspace_file_actions(read=True, write=True, expose_to_model=True)

    @agent.action_func
    def fetch_security_context(request_id: str) -> dict[str, Any]:
        """Return customer questionnaire, security-doc, and sales-context facts for a request."""
        _ = request_id
        return SECURITY_CONTEXT

    agent.use_actions(fetch_security_context)
    await workspace.ingest(
        content=SECURITY_CONTEXT,
        collection="observations",
        kind="vendor_security_context",
        summary="Customer security questionnaire and internal security context for SECQ-77",
        scope={"task_id": TASK_ID},
        source={"type": "mock_business_system", "name": "security_context"},
    )

    print("[SETUP] Vendor security questionnaire")
    print(f"[SETUP] Workspace: {workspace_dir}")
    print(f"[SETUP] Provider: {provider}, model_key={TASK_MODEL_KEY}")

    execution = agent.create_task(
        task_id=TASK_ID,
        goal=(
            "Prepare a customer-facing answer for security questionnaire SECQ-77. Use fetch_security_context "
            "and Workspace context as factual evidence, then write the final answer to "
            f"{OUTPUT_FILE}. The mock security system provides source facts only; the model owns whether the answer is supportable."
        ),
        success_criteria=[
            "The final Markdown answer exists in Workspace.",
            "The answer responds to each customer question using the supplied security context.",
            "The answer distinguishes completed, scheduled, optional, and unknown security facts.",
            "The answer avoids unsupported security claims.",
            "The execution evidence includes reading back or checking the final answer content.",
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
    stream_trace_path = workspace_dir / "outputs" / "vendor_security_questionnaire_stream.jsonl"
    stream_trace_path.parent.mkdir(parents=True, exist_ok=True)
    with stream_trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator():
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
        scenario="Vendor security questionnaire answer from partial security documentation.",
        artifact_text=artifact_text,
        business_context=SECURITY_CONTEXT,
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
# model_judge_passed=true, example_accepted=true, replan_count=2,
# first_verification_failed=true.
# The mock security system returns source facts only; the model owns
# supportability judgment and final acceptance.
