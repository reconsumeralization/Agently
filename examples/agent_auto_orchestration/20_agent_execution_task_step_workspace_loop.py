"""AgentExecution task-step loop with explicit Workspace observations.

Run:
    python examples/agent_auto_orchestration/20_agent_execution_task_step_workspace_loop.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

This example demonstrates the 4.1.3.2 AgentExecution step contract. The host
owns the two-step loop. Each Agent call is one bounded `mode="task_step"`
execution with explicit lineage and model-request limits. AgentExecution
receives the Agent's Workspace binding; the host explicitly asks the execution
to store observations/checkpoints in Workspace, then calls
`workspace.build_context(...)` before the next step.

Expected key output from one real DeepSeek run on 2026-05-31:
    provider=deepseek
    first_execution_mode=task_step
    first_budget_used=1
    first_result_has_root_cause=True
    context_item_count=1
    second_parent_matches=True
    second_budget_used=1
    second_result_has_next_action=True
    stream_lineage_ok=True
    checkpoint_count=2
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from agently.utils import DataFormatter
from examples.dynamic_task._shared import configure_model


RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "task_step_workspace_loop"

ISSUE = {
    "issue_id": "AG-4132-ROUTE",
    "symptom": "AgentExecution stream items cannot be correlated across a developer-owned loop.",
    "expected": "Each step should expose execution id, lineage, budget diagnostics, and route metadata.",
    "observed": "Only route data is visible; the next step cannot cite the previous execution reliably.",
    "constraints": [
        "Do not implement AgentTaskLoop in this slice.",
        "Workspace writes must be explicit host-owned operations.",
        "Keep route planning in AgentOrchestrator.",
    ],
}


async def collect_stream_flags(execution, *, task_id: str) -> dict[str, bool]:
    flags = {
        "route_selected": False,
        "lineage_ok": True,
    }
    async for item in execution.get_async_generator(type="instant"):
        if item.path == "route.selected" and item.is_complete:
            flags["route_selected"] = True
        meta = item.meta or {}
        if meta.get("execution_mode") != "task_step":
            flags["lineage_ok"] = False
        if (meta.get("lineage") or {}).get("task_id") != task_id:
            flags["lineage_ok"] = False
    return flags


async def main():
    provider = configure_model(temperature=0.1)
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)
    agent = Agently.create_agent("task-step-workspace-loop").use_workspace(RUNTIME_ROOT)
    assert agent.workspace is not None

    task_id = ISSUE["issue_id"]
    first = (
        agent
        .input({"issue": ISSUE})
        .instruct(
            "Analyze the issue as one bounded engineering step. Return concise, stable fields. "
            "Do not claim the full AgentTask loop is implemented."
        )
        .output(
            {
                "root_cause": (str, "Likely root cause or design gap", True),
                "proposed_fix": (str, "One concrete implementation step", True),
                "confidence": (str, "One of: high, medium, low", True),
            },
            format="json",
        )
        .create_execution(
            mode="task_step",
            lineage={"task_id": task_id, "iteration_id": "iter-1", "step_id": "analyze"},
            limits={"max_model_requests": 1},
        )
    )

    first_stream_task = asyncio.create_task(collect_stream_flags(first, task_id=task_id))
    first_result = await first.async_get_data()
    first_stream = await first_stream_task
    first_meta = await first.async_get_meta()

    first_workspace_record = await first.async_record_workspace(
        content={
            "result": first_result,
            "diagnostics": first_meta["diagnostics"],
        },
        collection="observations",
        kind="agent_execution_observation",
        summary=f"{task_id} first task-step analysis",
        scope={"task_id": task_id},
        source={"step": "analyze"},
        checkpoint=True,
    )
    observation_ref = first_workspace_record["record"]

    context_pack = await agent.workspace.build_context(
        # Scope owns this recall in the example; an empty goal exercises
        # ContextPack construction without invoking FTS query syntax.
        goal="",
        scope={"task_id": task_id},
        budget={"chars": 1200},
        profile="software_dev",
    )

    second = (
        agent
        .input(
            {
                "issue": ISSUE,
                "previous_observation_ref": observation_ref,
                "context_pack": DataFormatter.sanitize(context_pack),
            }
        )
        .instruct(
            "Use the ContextPack as explicit prior evidence. Choose the next concrete action and "
            "state a verification check. Do not invent Workspace writes; the host owns them."
        )
        .output(
            {
                "next_action": (str, "Concrete next action", True),
                "acceptance_check": (str, "How the host should verify the next action", True),
                "risk": (str, "One implementation risk", True),
            },
            format="json",
        )
        .create_execution(
            mode="task_step",
            lineage={
                "task_id": task_id,
                "iteration_id": "iter-2",
                "step_id": "choose-next-action",
                "parent_execution_id": first_meta["execution_id"],
            },
            limits={"max_model_requests": 1},
        )
    )

    second_stream_task = asyncio.create_task(collect_stream_flags(second, task_id=task_id))
    second_result = await second.async_get_data()
    second_stream = await second_stream_task
    second_meta = await second.async_get_meta()

    second_workspace_record = await second.async_record_workspace(
        content={
            "result": second_result,
            "diagnostics": second_meta["diagnostics"],
        },
        collection="decisions",
        kind="agent_execution_decision",
        summary=f"{task_id} second task-step decision",
        scope={"task_id": task_id},
        source={"step": "choose-next-action"},
        checkpoint=True,
    )
    decision_ref = second_workspace_record["record"]
    await agent.workspace.link(decision_ref, observation_ref, relation="uses_observation")

    checkpoints = await agent.workspace.checkpoint_history(task_id)
    context_items = context_pack.get("items", []) if isinstance(context_pack, dict) else []
    stream_lineage_ok = first_stream["lineage_ok"] and second_stream["lineage_ok"]

    print(f"provider={provider}")
    print(f"first_execution_mode={first_meta['execution_mode']}")
    print(f"first_budget_used={first_meta['diagnostics']['budget']['model_requests_used']}")
    print(f"first_result_has_root_cause={bool(first_result.get('root_cause'))}")
    print(f"context_item_count={len(context_items)}")
    print(f"second_parent_matches={second_meta['lineage']['parent_execution_id'] == first_meta['execution_id']}")
    print(f"second_budget_used={second_meta['diagnostics']['budget']['model_requests_used']}")
    print(f"second_result_has_next_action={bool(second_result.get('next_action'))}")
    print(f"stream_lineage_ok={stream_lineage_ok}")
    print(f"checkpoint_count={len(checkpoints)}")


if __name__ == "__main__":
    asyncio.run(main())
