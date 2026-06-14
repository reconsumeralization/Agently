from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

EXAMPLE_DIR = Path(__file__).resolve().parent
ROOT = EXAMPLE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from agently import Agently
from legacy_agently_script_upgrade import TASK_MODEL_KEY, configure_agent_model_pool


TASK_ROOT = Path(".agently/tasks/goal-effort-public-stream").resolve()


def print_stream_item(item: Any) -> None:
    meta = item.meta or {}
    stream_kind = meta.get("stream_kind")
    if stream_kind == "progress_delta":
        print(item.delta or "", end="", flush=True)
        return
    if stream_kind == "progress":
        message = item.value.get("message") if isinstance(item.value, dict) else ""
        print(f"\n[progress:{meta.get('stage', 'task')}] {message}", flush=True)
        return
    if stream_kind == "snapshot":
        value = item.value if isinstance(item.value, dict) else {}
        print(f"\n[snapshot:{value.get('stage', 'task')}] {value.get('message', '')}", flush=True)
        return
    if stream_kind == "phase":
        print(f"\n[phase] {item.path}", flush=True)
        return
    if item.path == "result":
        print("\n[result] task stream emitted terminal result", flush=True)


async def main() -> None:
    workspace_dir = Path(os.getenv("AGENT_TASK_PUBLIC_STREAM_WORKSPACE", str(TASK_ROOT))).resolve()
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = workspace_dir / "outputs" / "goal_effort_public_stream.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    progress_language = os.getenv("AGENTLY_PROGRESS_LANGUAGE", "zh-CN")
    agent = Agently.create_agent("goal-effort-public-stream").use_workspace(workspace_dir)
    provider = configure_agent_model_pool(agent, temperature=0.0)
    agent.settings.set("agent_task.progress.language", progress_language)

    incident_facts = {
        "incident_id": "INC-4242",
        "severity": "SEV2",
        "customer_tier": "enterprise",
        "current_status": "database failover completed; backlog is draining",
        "known_risk": "delayed invoice synchronization for east-region customers",
        "required_next_action": "confirm invoice queue depth after the next scheduled sync",
    }

    execution = (
        agent
        .goal(
            "Prepare an operator handoff from the caller-provided incident facts.",
            [
                "The final result uses the incident_id supplied through execution input.",
                "The final result uses severity, customer tier, current status, risk, and next action from execution input.",
                "The final result is suitable for an operations handoff and does not invent additional incident facts.",
            ],
        )
        .effort(
            "medium",
            budget={"iteration_limit": 2, "model_call_limit": 8, "wall_time_seconds": 180},
            planning={"depth": "bounded", "max_plan_items": 4},
            verification={"strictness": "strict"},
            execution={"step_plan": "direct"},
            progress={"detail": "natural_language"},
        )
        .input(incident_facts)
        .output(
            {
                "handoff": (str, "Concise operator handoff.", True),
                "risk": (str, "One grounded risk.", True),
                "next_action": (str, "One grounded next action.", True),
            },
            format="json",
        )
        .strategy(
            "task",
            task_id="goal_effort_public_stream",
            workspace=workspace_dir,
            limits={"max_model_requests": 10, "max_seconds": 180, "max_no_progress_seconds": 80},
            options={
                "agent_task": {
                    "request_timeout_seconds": 60,
                    "stream_progress": True,
                    "stream_snapshots": True,
                    "progress_model_key": TASK_MODEL_KEY,
                    "progress_language": progress_language,
                    "progress_timeout_seconds": 30,
                }
            },
        )
    )

    stream_items: list[Any] = []
    print("[setup] top-level AgentExecution Goal Pursuit stream")
    print(f"[setup] provider={provider} progress_language={progress_language}")
    print(f"[setup] workspace={workspace_dir}")
    with trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator():
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
            trace_file.flush()
            print_stream_item(item)

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    task = getattr(execution, "task_record", None)
    task_options = getattr(task, "options", {}) if task is not None else {}
    execution_prompt = task_options.get("execution_prompt_snapshot", {}) if isinstance(task_options, dict) else {}
    final_result = str(result.get("final_result") or "")
    progress_delta_text = "".join(
        str(item.delta or "")
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "progress_delta"
    )
    host_checks = {
        "route_is_agent_task": meta.get("route", {}).get("selected_route") == "agent_task",
        "task_completed": result.get("status") == "completed" and bool(result.get("accepted")),
        "execution_input_reached_task_loop": (
            isinstance(execution_prompt, dict)
            and isinstance(execution_prompt.get("input"), dict)
            and execution_prompt["input"].get("incident_id") == incident_facts["incident_id"]
        ),
        "execution_output_contract_reached_task_loop": isinstance(execution_prompt, dict) and "output" in execution_prompt,
        "progress_delta_streamed": bool(progress_delta_text.strip()),
        "progress_language_observed": any(
            (item.meta or {}).get("progress_language") == progress_language
            for item in stream_items
            if (item.meta or {}).get("stream_kind") in {"progress", "progress_delta"}
        ),
        "final_result_uses_incident_id": incident_facts["incident_id"] in final_result,
    }
    summary = {
        "provider": provider,
        "task_status": result.get("status"),
        "accepted": bool(result.get("accepted")),
        "artifact_status": result.get("artifact_status"),
        "route": meta.get("route", {}),
        "task_refs": meta.get("task_refs", {}),
        "stream_counts": {
            "progress_delta": sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "progress_delta"),
            "progress": sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"),
            "snapshot": sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "snapshot"),
        },
        "host_checks": host_checks,
        "final_result": final_result,
        "stream_trace_file": str(trace_path),
    }
    print("\n[summary]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not all(host_checks.values()):
        raise AssertionError(f"Top-level Goal Pursuit stream failed host checks: {host_checks}")


if __name__ == "__main__":
    asyncio.run(main())
