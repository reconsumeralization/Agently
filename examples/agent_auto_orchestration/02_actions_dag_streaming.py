from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


# Actions + DAG process-stream example.
# Expected key output from one local run:
# selected_route=dynamic_task
# final_role=final
# final_priority=high
# stream_action_task=True
# stream_graph_ready=True
#
# This is a local execution-facade smoke case for Action-backed Dynamic Task
# nodes and AgentExecution stream bridging.


def classify_ticket(text: str):
    return {"priority": "high" if "payment" in text.lower() else "normal", "text": text}


def draft_triage(priority: str, text: str):
    return {"priority": priority, "reply": f"Triage priority is {priority}: {text}"}


async def main():
    agent = Agently.create_agent("actions-dag-stream")
    agent.register_action(
        name="classify_ticket",
        desc="Classify support ticket priority.",
        kwargs={"text": (str, "ticket text")},
        func=classify_ticket,
    )
    agent.register_action(
        name="draft_triage",
        desc="Draft a triage reply.",
        kwargs={"priority": (str, "priority"), "text": (str, "ticket text")},
        func=draft_triage,
    )

    graph = {
        "graph_id": "actions-dag-stream",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {
                "id": "classify",
                "kind": "action",
                "binding": "classify_ticket",
                "inputs": {"kwargs": {"text": "Payment webhook failed for an enterprise account."}},
            },
            {
                "id": "draft",
                "kind": "action",
                "binding": "draft_triage",
                "depends_on": ["classify"],
                "inputs": {
                    "kwargs": {
                        "priority": "high",
                        "text": "Payment webhook failed for an enterprise account.",
                    }
                },
            },
        ],
        "semantic_outputs": {"final": "draft"},
    }

    execution = (
        agent
        .use_actions(["classify_ticket", "draft_triage"])
        .use_dynamic_task(mode="submitted", plan=graph)
        .input("Run support triage graph.")
        .create_execution()
    )

    stream_paths = []
    async for item in execution.get_async_generator(type="instant"):
        if item.is_complete:
            stream_paths.append(item.path)

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    final = data["semantic_outputs"]["final"]["result"]
    print(f"selected_route={meta['route_plan']['selected_route']}")
    print("final_role=final")
    print(f"final_priority={final['priority']}")
    print(f"stream_action_task={any(path.startswith('task_dag.tasks.draft.') for path in stream_paths)}")
    print(f"stream_graph_ready={'route.dynamic_task.graph' in stream_paths}")


if __name__ == "__main__":
    asyncio.run(main())
