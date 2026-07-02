"""Independent Dynamic Task DAG smoke.

Run:
    python examples/agent_auto_orchestration/24_independent_dynamic_task_dag.py

This is an infrastructure smoke, not a model-app quality example. It verifies
that a submitted TaskDAG still runs as an independent Dynamic Task workflow.
The graph uses a local handler and does not call a model.

Expected key output from one local run on 2026-06-27:
    execution_entry=dynamic_task
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
    graph = {
        "graph_id": "independent-dynamic-task-example",
        "task_schema_version": "task_dag/v1",
        "tasks": [
            {"id": "extract", "kind": "local", "binding": "extract_value_handler"},
        ],
        "semantic_outputs": {"final": "extract"},
    }

    task = Agently.create_dynamic_task(
        target="Run the submitted graph as an independent Dynamic Task.",
        plan=graph,
        handlers={"extract_value_handler": extract_value},
    )
    execution = task.compile(graph).create_execution(auto_close=False)

    stream_paths: list[str] = []
    async for item in execution.get_async_runtime_stream({"value": "ok"}, timeout=None):
        if isinstance(item, dict) and item.get("type") == "task_dag.task":
            stream_paths.append(f"task_dag.tasks.{ item.get('task_id') }.{ item.get('action') }")
        if isinstance(item, dict) and item.get("type") == "task_dag.graph" and item.get("action") == "complete":
            break

    data = await execution.async_close(timeout=10)
    final_value = data["semantic_outputs"]["final"]["result"]["value"]

    print("execution_entry=dynamic_task")
    print(f"final_value={final_value}")
    print(f"stream_extract={any(path.startswith('task_dag.tasks.extract') for path in stream_paths)}")


if __name__ == "__main__":
    asyncio.run(main())
