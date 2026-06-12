"""Execution-local Dynamic Task candidate smoke.

Run:
    python examples/agent_auto_orchestration/24_execution_local_dynamic_task_candidate.py

This is an infrastructure smoke, not a model-app quality example. It verifies
that ``execution.use_dynamic_task(...)`` attaches a submitted TaskDAG candidate
only to the captured AgentExecution draft. The route uses a local handler and
does not call a model.

Expected key output from one local run:
    selected_route=dynamic_task
    local_candidate_count=1
    global_candidate_count=0
    final_value=ok
    stream_extract=True
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


async def extract_value(context):
    return {
        "task_id": context.task.id,
        "value": context.graph_input["value"],
    }


async def main() -> None:
    agent = Agently.create_agent("execution-local-dynamic-task-example")
    graph = {
        "graph_id": "execution-local-dynamic-task-example",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {"id": "extract", "kind": "local", "binding": "extract_value_handler"},
        ],
        "semantic_outputs": {"final": "extract"},
    }

    execution = (
        agent
        .create_execution()
        .input("Run the submitted graph for this execution only.")
        .use_dynamic_task(
            mode="submitted",
            plan=graph,
            handlers={"extract_value_handler": extract_value},
            graph_input={"value": "ok"},
        )
    )

    stream_paths: list[str] = []
    async for item in execution.get_async_generator(type="instant"):
        if item.is_complete:
            stream_paths.append(item.path)

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    final_value = data["semantic_outputs"]["final"]["result"]["value"]

    print(f"selected_route={meta['route_plan']['selected_route']}")
    print(f"local_candidate_count={len(execution.local_dynamic_task_candidates)}")
    print(f"global_candidate_count={len(getattr(agent, '_dynamic_task_candidates', []))}")
    print(f"final_value={final_value}")
    print(f"stream_extract={any(path.startswith('task_dag.tasks.extract') for path in stream_paths)}")


if __name__ == "__main__":
    asyncio.run(main())
