"""Goal Pursuit example: generate an Agently architecture diagram.

Run:
    python examples/agent_task/agently_architecture_diagram_task.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set AGENT_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

This example exercises the 4.1.3.8 AgentExecution-backed task strategy on a
longer architecture-document task:

    goal + effort + task strategy
        -> AgentExecution route: agent_task
        -> task step calls fetch_agently_architecture_sources as repository evidence
        -> task writes a readable Mermaid architecture document to TaskWorkspace
        -> independent model judge reviews whether the artifact fits the scenario

The source-gathering Action only returns repository facts and excerpts. Diagram
composition, layering, edge naming, and quality judgement are model-owned.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from agently import Agently

from _business_example_common import (
    TASK_MODEL_KEY,
    configure_agent_model_pool,
    default_workspace,
    judge_business_artifact,
    print_stream_item,
    resolve_result_artifact_path,
    write_summary,
)


TASK_ID = "agently_architecture_diagram"
OUTPUT_FILE = "outputs/agently_architecture_diagram.md"
SUMMARY_FILE = "outputs/agently_architecture_diagram_summary.json"

ARCHITECTURE_DIAGRAM_SKILL_GUIDANCE = """
Architecture diagram skill guidance:
- Identify purpose, audience, and whether the document describes current state,
  target state, or migration state.
- Separate ownership boundaries from implementation details. Show who owns
  lifecycle, execution, state, policy, evidence, and extension points.
- Include at least one top-level diagram and one flow/detail diagram when the
  system has multiple layers or runtime paths.
- Prefer Mermaid flowchart TD for layered architecture and sequenceDiagram for
  request/loop interactions.
- Make diagrams inspectable: stable node labels, semantic edge labels, no
  decorative styling, and no overloaded single diagram.
- Pair every diagram with text explaining the contract behind each edge.
- Record repositioning explicitly when current module names are kept but their
  future responsibility changes.
""".strip()

SOURCE_PATHS = [
    "docs/en/reference/execution-layer-selection.md",
    "spec/planned/architecture/CONCEPT_REGISTRY.md",
    "spec/planned/architecture/UNIFIED_AGENT_EXECUTION_IMPLEMENTATION_SPEC.md",
    "spec/planned/architecture/AGENT_TASK_LOOP_4_1_3_7_CLOSEOUT_SPEC.md",
    "spec/planned/agent_task/AGENT_TASK_LOOP_LAYERED_DAG_STEP_EXECUTION_OPTIMIZATION_SPEC.md",
    "agently/core/Agent.py",
    "agently/core/model/ModelRequest.py",
    "agently/core/application/AgentExecution/__init__.py",
    "agently/core/application/AgentExecution/Result.py",
    "agently/core/orchestration/TaskDAG/TaskDAGExecutor.py",
    "agently/core/orchestration/TriggerFlow/TriggerFlow.py",
    "agently/core/TaskWorkspace/TaskWorkspace.py",
    "agently/core/application/SkillsExecutor/SkillsExecutor.py",
    "agently/builtins/plugins/ActionRuntime/AgentlyActionRuntime.py",
]

EXCERPT_TERMS = [
    "AgentExecution",
    "AgentTask",
    "AgentTaskLoop",
    "TaskDAG",
    "DAG",
    "DynamicTask",
    "TriggerFlow",
    "ModelRequest",
    "ModelRequestResult",
    "TaskWorkspace",
    "Action",
    "ActionRuntime",
    "SkillsExecutor",
    "ExecutionResource",
    "RuntimeEvent",
    "EventCenter",
    "goal",
    "effort",
    "provider",
    "strategy",
    "verification",
    "evidence",
    "deferred",
]

JUDGE_RULES = [
    "The artifact would help an engineer understand Agently's architecture at a high level without reading the whole repository.",
    "The diagrams are readable, layered, and not just decorative; at least one diagram should show structure and one should clarify a runtime or detail relationship.",
    "The artifact chooses and explains important boundaries in its own way, instead of only listing module names.",
    "The artifact is reasonably grounded in the supplied repository evidence and avoids major contradictions or overconfident claims about deferred work.",
    "The artifact is suitable as a design-review conversation starter, even if a maintainer might later refine the exact labels or grouping.",
]


def _line_excerpt(text: str, *, max_chars: int = 5600) -> str:
    lines = text.splitlines()
    if len(text) <= max_chars:
        return text
    lowered_terms = [term.lower() for term in EXCERPT_TERMS]
    selected: set[int] = set()
    for index, line in enumerate(lines):
        lower = line.lower()
        if any(term in lower for term in lowered_terms):
            for offset in range(-2, 5):
                candidate = index + offset
                if 0 <= candidate < len(lines):
                    selected.add(candidate)
    chunks: list[str] = []
    last = -10
    for index in sorted(selected):
        if index > last + 1:
            chunks.append("...")
        chunks.append(f"{index + 1}: {lines[index]}")
        last = index
        if sum(len(chunk) + 1 for chunk in chunks) >= max_chars:
            chunks.append("...")
            break
    return "\n".join(chunks)


def _count_mermaid_blocks(text: str) -> int:
    return len(re.findall(r"```mermaid\s+.*?```", text, flags=re.S | re.I))


async def main() -> None:
    os.environ.setdefault("AGENT_TASK_JUDGE_TIMEOUT_SECONDS", "180")
    workspace_dir = default_workspace("agently-architecture-diagram")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agent = Agently.create_agent("agent-task-agently-architecture-diagram").use_task_workspace(workspace_dir).use_record_store(workspace_dir, mode="read_write")
    provider = configure_agent_model_pool(agent, temperature=0.0)
    task_workspace = agent.task_workspace
    record_store = agent.record_store

    agent.enable_task_workspace_file_actions(read=True, write=True, expose_to_model=True)

    @agent.action_func
    def fetch_agently_architecture_sources() -> dict[str, Any]:
        """Return bounded Agently architecture source excerpts from docs, specs, and implementation files."""
        sources: list[dict[str, Any]] = []
        for relative in SOURCE_PATHS:
            path = (ROOT / relative).resolve()
            if not path.is_file():
                sources.append({"path": relative, "status": "missing"})
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            sources.append(
                {
                    "path": relative,
                    "status": "ok",
                    "excerpt": _line_excerpt(text),
                }
            )
        return {
            "status": "ok",
            "architecture_diagram_skill_guidance": ARCHITECTURE_DIAGRAM_SKILL_GUIDANCE,
            "sources": sources,
        }

    agent.use_actions(fetch_agently_architecture_sources)
    await record_store.put(
        content={
            "task": TASK_ID,
            "output_file": OUTPUT_FILE,
            "skill_guidance": ARCHITECTURE_DIAGRAM_SKILL_GUIDANCE,
            "source_paths": SOURCE_PATHS,
            "judge_rules": JUDGE_RULES,
        },
        collection="observations",
        kind="architecture_diagram_task_brief",
        summary="Goal Pursuit task brief for generating an Agently architecture diagram.",
        scope={"task_id": TASK_ID, "execution_id": TASK_ID},
        source={"type": "example_script", "name": "agently_architecture_diagram_task"},
    )

    print("[SETUP] Agently architecture diagram Goal Pursuit experiment")
    print(f"[SETUP] TaskWorkspace: {workspace_dir}")
    print(f"[SETUP] Provider: {provider}, model_key={TASK_MODEL_KEY}")

    goal = (
        "Use the architecture diagram skill guidance and repository source evidence to produce a design-review-ready "
        "Agently architecture document. First call fetch_agently_architecture_sources, then write the final Markdown "
        f"document to `{OUTPUT_FILE}`. Choose the structure yourself: prioritize a readable architecture view over "
        "covering every implementation detail, and call out current versus deferred capabilities when the evidence "
        "makes that distinction important."
    )
    success_criteria = [
        f"The final Markdown architecture document exists at `{OUTPUT_FILE}` in TaskWorkspace.",
        "The document uses the architecture-diagram guidance and includes readable Mermaid diagrams.",
        "The document gives engineers a clear high-level view of Agently's major layers, ownership boundaries, and runtime path.",
        "The document handles uncertain, planned, or deferred areas responsibly instead of presenting everything as already landed.",
        "The execution evidence includes source collection and a readback or check of the final document.",
    ]

    execution = (
        agent
        .goal(goal, success_criteria)
        .effort(
            "high",
            budget={"iteration_limit": 4, "model_call_limit": 16, "wall_time_seconds": 360},
            planning={"depth": "deep", "require_source_collection": True},
            execution={"step_plan": "auto"},
            verification={"strength": "strong", "require_artifact_readback": True},
            replan={"on_missing_criteria": True},
            progress={"stream": True, "snapshots": True},
        )
        .strategy(
            "task",
            task_id=TASK_ID,
            task_workspace=workspace_dir,
            limits={"max_model_requests": 16, "max_seconds": 360, "max_no_progress_seconds": 120},
            options={
                "agent_task": {
                    "request_timeout_seconds": 90,
                    "stream_progress": True,
                    "stream_snapshots": True,
                },
                "routes": {"model_request": {"action_loop": {"max_rounds": 8}}},
            },
        )
    )

    stream_items = []
    stream_trace_path = workspace_dir / "outputs" / "agently_architecture_diagram_stream.jsonl"
    stream_trace_path.parent.mkdir(parents=True, exist_ok=True)
    with stream_trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator(type="instant"):
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
            trace_file.flush()
            print_stream_item(item)

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    output_path = resolve_result_artifact_path(task_workspace, result, OUTPUT_FILE)
    artifact_text = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
    model_judge = await judge_business_artifact(
        agent,
        scenario="Agently architecture diagram design-review artifact.",
        artifact_text=artifact_text,
        business_context={
            "architecture_diagram_skill_guidance": ARCHITECTURE_DIAGRAM_SKILL_GUIDANCE,
            "source_paths": SOURCE_PATHS,
            "expected_output_file": OUTPUT_FILE,
            "current_release_slice": "4.1.3.8 AgentExecution-backed AgentTaskLoop hardening",
        },
        rules=JUDGE_RULES,
    )
    structural_smoke = {
        "output_file_exists": output_path.is_file(),
        "mermaid_block_count": _count_mermaid_blocks(artifact_text),
        "has_multiple_mermaid_blocks": _count_mermaid_blocks(artifact_text) >= 2,
        "artifact_char_count": len(artifact_text),
    }
    summary = {
        "provider": provider,
        "task_status": result.get("status"),
        "task_accepted": bool(result.get("accepted", result.get("status") == "completed")),
        "artifact_status": str(result.get("artifact_status") or ("accepted" if result.get("status") == "completed" else "partial")),
        "iterations": result.get("iterations"),
        "model_judge_passed": bool(model_judge.get("accepted")),
        "example_accepted": bool(
            result.get("accepted", result.get("status") == "completed")
            and model_judge.get("accepted")
            and structural_smoke["has_multiple_mermaid_blocks"]
        ),
        "model_judge": model_judge,
        "structural_smoke": structural_smoke,
        "replan_count": sum(1 for item in stream_items if item.path.endswith(".replan")),
        "record_store_recovery_ref_count": len(meta.get("record_refs", {}).get("checkpoints", [])),
        "workspace_process_record_count": len(meta.get("record_refs", {}).get("decisions", [])),
        "task_refs": result.get("task_refs") or meta.get("task_refs"),
        "stream_trace_file": str(stream_trace_path),
        "output_file": str(output_path),
    }
    summary_path = task_workspace.root / SUMMARY_FILE
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(summary)


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output from a real DeepSeek run on 2026-06-12:
# provider="deepseek"
# task_status="completed"
# task_accepted=true
# artifact_status="accepted"
# iterations=1
# model_judge_passed=true
# example_accepted=true
# structural_smoke.output_file_exists=true
# structural_smoke.mermaid_block_count=2
# record_store_recovery_ref_count=0 (default task process state stays in memory/logs)
# task_refs.strategy="task"
# output_file ends with files/outputs/agently_architecture_diagram.md
#
# The acceptance result requires both AgentTaskLoop verification and the
# independent model judge. The judge rules are intentionally broad because this
# example is a scenario simulation, not a fixed-answer architecture exam.
# Structural smoke checks only file presence and Mermaid block count; semantic
# acceptance remains model-owned.
