"""DAG model-field delta streaming, similar to coding-agent progress output.

Run:
    python examples/agent_auto_orchestration/05_model_field_delta_streaming.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

This example focuses on the AgentExecution stream contract. It uses a submitted
Dynamic Task DAG with multiple model nodes and prints selected structured fields
as they stream:

    task_dag.tasks.prethink.fields.prethinking
    task_dag.tasks.reply.fields.tool_call_note
    task_dag.tasks.reply.fields.reply
    task_dag.tasks.review.fields.reflection

The CLI consumes `item.delta` with `print(delta, end="", flush=True)` so the
operator sees process notes and final reply text while each field is still being
generated, before the task-level `.complete` event fires.

Expected key output from one local Ollama run with qwen2.5:7b on 2026-05-22:
    selected_route=dynamic_task
    stream_prethinking_delta=True
    stream_tool_call_note_delta=True
    stream_reply_delta=True
    stream_reflection_delta=True
    reply_task_completed=True
    Final reply excerpt starts with: Dear valued customer,
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model


SUPPORT_CONTEXT = {
    "ticket_id": "ENT-48291",
    "customer": "Northstar Bank",
    "plan": "Enterprise",
    "arr": "$420K",
    "incident": "API invoice export returns 504 after the nightly billing deploy.",
    "impact": "Finance team cannot close month-end reconciliation.",
    "observed_signals": [
        "p95 invoice export latency rose from 1.8s to 38s",
        "database CPU is normal",
        "new PDF renderer worker queue is saturated",
        "rollback is available but would pause new invoice templates",
    ],
    "safe_actions": [
        "acknowledge business impact",
        "explain current investigation path",
        "offer temporary CSV export workaround",
        "avoid promising an exact fix time",
    ],
}


FIELD_LABELS = {
    "task_dag.tasks.prethink.fields.prethinking": "\n\n[prethinking]\n",
    "task_dag.tasks.reply.fields.tool_call_note": "\n\n[tool-call note]\n",
    "task_dag.tasks.reply.fields.reply": "\n\n[reply]\n",
    "task_dag.tasks.review.fields.reflection": "\n\n[reflection]\n",
}


async def provide_context(_context):
    await asyncio.sleep(0.2)
    return SUPPORT_CONTEXT


def build_graph() -> dict[str, Any]:
    return {
        "graph_id": "operator-visible-field-delta",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {
                "id": "context",
                "kind": "local",
                "binding": "context_handler",
                "title": "Load ticket context",
            },
            {
                "id": "prethink",
                "kind": "model",
                "depends_on": ["context"],
                "title": "Prepare operator-visible reasoning before drafting",
                "purpose": (
                    "Explain how you will reason about the customer impact, likely technical path, "
                    "safe claims, and reply constraints. This is process visibility for an operator."
                ),
                "inputs": {
                    "output_schema": {
                        "prethinking": (
                            str,
                            "Short operator-visible reasoning note. Mention what evidence matters and what not to overpromise.",
                            True,
                        ),
                    },
                    "ensure_keys": ["prethinking"],
                },
            },
            {
                "id": "reply",
                "kind": "model",
                "depends_on": ["prethink"],
                "title": "Draft transparent customer reply",
                "purpose": (
                    "Use the prethinking and ticket context to draft an enterprise support reply. "
                    "Also explain which internal capability or data source you would consult next."
                ),
                "inputs": {
                    "output_schema": {
                        "tool_call_note": (
                            str,
                            "Operator-visible note describing the next internal lookup or tool action.",
                            True,
                        ),
                        "reply": (
                            str,
                            "Customer-facing reply. Acknowledge impact, explain investigation path, include workaround, avoid exact ETA.",
                            True,
                        ),
                    },
                    "ensure_keys": ["tool_call_note", "reply"],
                },
            },
            {
                "id": "review",
                "kind": "model",
                "depends_on": ["reply"],
                "title": "Reflect on reply quality",
                "purpose": (
                    "Review whether the reply is safe, specific, and useful. Explain one improvement if needed."
                ),
                "inputs": {
                    "output_schema": {
                        "reflection": (
                            str,
                            "Operator-visible reflection about accuracy, empathy, and overpromise risk.",
                            True,
                        ),
                        "ready_to_send": (bool, "Whether the reply is safe to send.", True),
                    },
                    "ensure_keys": ["reflection", "ready_to_send"],
                },
            },
        ],
        "semantic_outputs": {
            "customer_reply": "reply",
            "quality_review": "review",
        },
    }


async def main():
    provider = configure_model(temperature=0.2)
    print(f"Provider: {provider}")
    print("Streaming selected DAG model fields as deltas...")

    agent = Agently.create_agent("field-delta-stream-agent")
    execution = (
        agent
        .use_dynamic_task(
            mode="submitted",
            plan=build_graph(),
            handlers={"context_handler": provide_context},
        )
        .input("Handle the enterprise billing export incident transparently.")
        .create_execution()
    )

    seen_labels: set[str] = set()
    flags = {
        "stream_prethinking_delta": False,
        "stream_tool_call_note_delta": False,
        "stream_reply_delta": False,
        "stream_reflection_delta": False,
        "reply_task_completed": False,
    }

    async for item in execution.get_async_generator(type="instant"):
        if item.path == "route.selected" and item.is_complete:
            print(f"\n[route] selected: {(item.value or {}).get('selected_route')}")
            continue

        if item.path == "task_dag.tasks.reply.complete" and item.is_complete:
            flags["reply_task_completed"] = True
            continue

        if item.event_type != "delta" or not item.delta:
            continue
        if item.path not in FIELD_LABELS:
            continue

        if item.path not in seen_labels:
            print(FIELD_LABELS[item.path], end="", flush=True)
            seen_labels.add(item.path)
        print(item.delta, end="", flush=True)

        if item.path.endswith(".prethinking"):
            flags["stream_prethinking_delta"] = True
        elif item.path.endswith(".tool_call_note"):
            flags["stream_tool_call_note_delta"] = True
        elif item.path.endswith(".reply"):
            flags["stream_reply_delta"] = True
        elif item.path.endswith(".reflection"):
            flags["stream_reflection_delta"] = True

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    print("\n\n---")
    print("selected_route=", meta["route_plan"]["selected_route"], sep="")
    for key, value in flags.items():
        print(f"{key}={value}")

    semantic_outputs = data.get("semantic_outputs", {}) if isinstance(data, dict) else {}
    reply_result = semantic_outputs.get("customer_reply", {}).get("result", {})
    review_result = semantic_outputs.get("quality_review", {}).get("result", {})
    print("\nFinal reply excerpt:")
    print(str(reply_result.get("reply", ""))[:500])
    print("\nReview:")
    print(str(review_result.get("reflection", ""))[:300])


if __name__ == "__main__":
    asyncio.run(main())
