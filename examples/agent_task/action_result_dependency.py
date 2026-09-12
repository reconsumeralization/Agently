"""Real-model Flat task: use a lookup result as the next Action's argument.

Set MODEL_BASE_URL, MODEL_API_KEY and MODEL_NAME for an OpenAI-compatible service.
Optional MODEL_REQUEST_OPTIONS is a JSON object of provider request options.
The ticket backend below is an in-memory business-system simulation, not a
replacement for model planning, argument selection, or final verification.
No fixed wall-clock task limit is imposed: local model throughput varies.
The explicit model-request and iteration budgets remain in effect.
Flow: model plans -> lookup_ticket -> observed revision -> acknowledge_revision
-> ordinary task verification -> final response. No manual result substitution.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from agently import Agent, Agently


def configure_model(agent: Agent) -> None:
    agent.set_settings("plugins.ModelRequester.OpenAICompatible", {
        "base_url": os.environ["MODEL_BASE_URL"],
        "auth": os.environ["MODEL_API_KEY"],
        "model": os.environ["MODEL_NAME"],
        "request_retry": {"max_attempts": 1},
        "request_options": {"temperature": 0.3, **json.loads(os.getenv("MODEL_REQUEST_OPTIONS", "{}"))},
    })


async def run_example() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agently-action-dependency-") as directory:
        agent = Agently.create_agent("ticket-revision-handoff").use_task_workspace(Path(directory) / "files")
        configure_model(agent)
        current: dict[str, str] = {}
        observed_calls: list[dict[str, Any]] = []

        @agent.action_func
        def lookup_ticket(ticket_id: str) -> dict[str, str]:
            """Read the ticket and its current revision. The revision is known only after this call."""
            revision = secrets.token_hex(8)
            current[ticket_id] = revision
            result = {"ticket_id": ticket_id, "revision": revision, "status": "ready"}
            observed_calls.append({"action": "lookup_ticket", "result": result})
            return result

        @agent.action_func
        def acknowledge_revision(ticket_id: str, revision: str) -> dict[str, Any]:
            """Acknowledge only the exact current revision returned by lookup_ticket; guesses are rejected."""
            matched = ticket_id in current and current[ticket_id] == revision
            result = {"ticket_id": ticket_id, "revision": revision, "acknowledged": matched}
            observed_calls.append({"action": "acknowledge_revision", "result": result})
            if not matched:
                raise ValueError("The supplied revision is not the observed current ticket revision.")
            return result

        execution = (
            agent.goal(
                "Read ticket T-208 with lookup_ticket, then use the returned revision to acknowledge it "
                "with acknowledge_revision. Report the observed acknowledgement outcome.",
                success_criteria=[
                    "The acknowledgement uses exactly the revision returned by lookup_ticket.",
                    "The final response accurately reports the actual acknowledgement outcome.",
                ],
            )
            .require_actions([lookup_ticket, acknowledge_revision])
            .strategy("flat", limits={"max_seconds": None, "max_model_requests": 20}, max_iterations=4)
        )
        result = await execution.async_start()
        meta = await execution.async_get_meta()
        return {"result": result, "observed_calls": observed_calls, "meta": meta}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), ensure_ascii=False, default=str, indent=2))

# Historical output with the previous 600-second budget (not acceptance):
# observed_calls[1]["result"]["acknowledged"] == True; both calls used ticket_id "T-208"
# and the same revision. result["status"] == "timed_out", result["accepted"] == False.
# The 600-second run reached final verification after correct Actions and a final
# response draft, but did not finish verification. Action success is not task acceptance.
# Latest acceptance observation (no wall-clock task limit; not a passing example):
# Both Actions succeeded with the same observed revision. After 12 model requests
# and four iterations, status was "max_iterations", accepted was False. A correct
# generated answer was blocked by task-wide required Actions inherited by the
# answer-only child execution. The release acceptance gap remains open.
