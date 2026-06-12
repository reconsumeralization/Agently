from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from agently import Agently

from legacy_agently_script_upgrade import configure_agent_model_pool


TASK_ROOT = Path(".agently/tasks/goal-pursuit-acceptance-matrix").resolve()


async def _run_goal_pursuit_case(
    *,
    agent_name: str,
    workspace_dir: Path,
    task_id: str,
    goal: str,
    success_criteria: list[str],
    max_iterations: int,
) -> dict[str, Any]:
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    agent = Agently.create_agent(agent_name).use_workspace(workspace_dir)
    provider = configure_agent_model_pool(agent, temperature=0.0)
    execution = (
        agent
        .goals(goal, success_criteria)
        .effort("low", budget={"iteration_limit": max_iterations})
        .strategy(
            "task",
            task_id=task_id,
            workspace=workspace_dir,
            limits={"max_model_requests": 8, "max_seconds": 180, "max_no_progress_seconds": 80},
            options={
                "agent_task": {
                    "request_timeout_seconds": 80,
                    "stream_progress": True,
                    "stream_snapshots": True,
                }
            },
        )
    )

    stream_items = []
    trace_path = workspace_dir / "outputs" / f"{task_id}_stream.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator():
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    verifications = [
        item.value.get("verification")
        for item in stream_items
        if item.path.endswith(".verification") and isinstance(item.value, dict)
    ]
    final_result = str(
        result.get("final_result")
        or (result.get("verification") or {}).get("final_result")
        or ""
    )
    return {
        "provider": provider,
        "status": result.get("status"),
        "accepted": bool(result.get("accepted")),
        "artifact_status": result.get("artifact_status"),
        "iterations": result.get("iterations"),
        "replan_count": sum(1 for item in stream_items if item.path.endswith(".replan")),
        "final_result": final_result,
        "last_verification": verifications[-1] if verifications else {},
        "phase_names": [item.get("phase") for item in meta.get("diagnostics", {}).get("phases", [])],
        "trace_file": str(trace_path),
    }


async def main() -> None:
    accepted = await _run_goal_pursuit_case(
        agent_name="goal-pursuit-accepted-example",
        workspace_dir=TASK_ROOT / "accepted",
        task_id="goal_pursuit_accepted",
        goal=(
            "Write a concise release-note paragraph from these facts: AgentExecution now owns prompt, "
            "skills, actions, goals, and effort as one execution draft."
        ),
        success_criteria=[
            "The final result mentions AgentExecution.",
            "The final result mentions effort as a strategy control.",
            "The final result is one concise paragraph.",
        ],
        max_iterations=2,
    )
    partial = await _run_goal_pursuit_case(
        agent_name="goal-pursuit-partial-example",
        workspace_dir=TASK_ROOT / "partial",
        task_id="goal_pursuit_partial",
        goal=(
            "Prepare a release artifact for a product website. No file Actions are available in this run; "
            "if the required file-write/readback evidence is missing, report that the task cannot be accepted yet."
        ),
        success_criteria=[
            "Execution evidence includes a write_file Action record for outputs/site.md.",
            "Execution evidence includes a read_file Action record for outputs/site.md.",
            "The final result clearly states whether the artifact is accepted or still missing evidence.",
        ],
        max_iterations=1,
    )

    summary = {
        "accepted": {
            "provider": accepted["provider"],
            "status": accepted["status"],
            "accepted": accepted["accepted"],
            "artifact_status": accepted["artifact_status"],
            "iterations": accepted["iterations"],
            "final_result_mentions_agent_execution": "AgentExecution" in accepted["final_result"],
            "final_result_mentions_effort": "effort" in accepted["final_result"].lower(),
            "trace_file": accepted["trace_file"],
        },
        "partial": {
            "provider": partial["provider"],
            "status": partial["status"],
            "accepted": partial["accepted"],
            "artifact_status": partial["artifact_status"],
            "iterations": partial["iterations"],
            "missing_criteria": partial["last_verification"].get("missing_criteria", []),
            "guard_reasons": partial["last_verification"].get("guard_reasons", []),
            "trace_file": partial["trace_file"],
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output from one real local Ollama run with qwen2.5:7b on 2026-06-12
# using AGENT_TASK_MODEL_PROVIDER=ollama:
# accepted.provider="ollama"
# accepted.status="completed"
# accepted.accepted=true
# accepted.artifact_status="accepted"
# accepted.iterations=1
# accepted.final_result_mentions_agent_execution=true
# accepted.final_result_mentions_effort=true
# partial.provider="ollama"
# partial.status="max_iterations"
# partial.accepted=false
# partial.artifact_status="partial"
# partial.iterations=1
# partial.missing_criteria includes "Execution evidence includes a read_file Action record for outputs/site.md."
# partial.guard_reasons includes "missing_criteria_present"
#
# The accepted and partial verdicts come from model-owned task planning and
# verification plus host guards. Stricter providers such as DeepSeek may classify
# the same missing Action evidence as blocked instead of max_iterations/partial;
# that is still a verifier-owned terminal decision. The example does not use
# keyword or substring checks as the task acceptance mechanism; the printed
# booleans are only post-run smoke evidence for the documented real run.
