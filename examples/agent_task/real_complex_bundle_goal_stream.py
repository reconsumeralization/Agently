# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""High-level Goal Pursuit example with Search, AMap MCP, and CocoonAI Skill.

Run:
    python examples/agent_task/real_complex_bundle_goal_stream.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file, or set
    AGENT_TASK_MODEL_PROVIDER=ollama for local Ollama.
    AMAP_API_KEY is required for the real AMap MCP branch.

This is the top-level companion to
`examples/blocks/07_real_complex_bundle_stream.py`. The Blocks example proves
the lower-level lifecycle substrate. This script proves the public task API:

    agent.use_actions(...)
    await agent.async_use_mcp(...)
    agent.use_skills(...)
    agent.goal(...).effort(...).input(...).output(...).get_async_generator(type="instant")

No final business artifact is canned in this file. The model must use the
mounted capabilities and satisfy the semantic judge.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, cast

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from agently import Agently
from agently.builtins.actions import Search

from _business_example_common import (
    TASK_MODEL_KEY,
    configure_agent_model_pool,
    default_workspace,
    judge_business_artifact,
    write_summary,
)


TASK_ID = "real_complex_bundle_goal_stream"
OUTPUT_DAILY_REPORT = "outputs/operator_daily_report.md"
OUTPUT_TRAVELOGUE = "outputs/hangzhou_business_travelogue.md"
OUTPUT_ARCHITECTURE = "outputs/agently_blocks_architecture.html"
OUTPUT_SUMMARY = "outputs/real_complex_bundle_goal_summary.json"

COCOON_SKILL_SOURCE = "Cocoon-AI/architecture-diagram-generator"
COCOON_SKILL_SUBPATH = "architecture-diagram"
COCOON_SKILL_ID = "architecture-diagram"
SKILLS_ARTIFACT_EFFORT = "real_complex_bundle_artifact_react"

SOURCE_PATHS = [
    "spec/implemented/architecture/COMPLEX_TASK_EXECUTION_LIFECYCLE_BLOCKS_PLUGIN_SPEC.md",
    "docs/en/reference/blocks-lifecycle.md",
    "docs/en/reference/execution-layer-selection.md",
    "agently/builtins/plugins/Blocks/AgentlyBlocks.py",
    "agently/core/application/AgentTask/Task.py",
]

REQUIRED_ACTIONS = [
    "search",
    "maps_weather",
    "maps_geo",
    "maps_text_search",
    "maps_direction_transit_integrated",
    "fetch_agently_architecture_sources",
    "write_file",
    "read_file",
]

JUDGE_RULES = [
    "The result contains an operator daily report, a Hangzhou business travelogue, and an architecture diagram deliverable.",
    "The daily report separates completed work, evidence, risks, and next actions.",
    "The travelogue grounds factual location, weather, POI, or route claims in AMap MCP evidence.",
    "The architecture deliverable is a single-file HTML/SVG artifact and uses repository evidence for Agently ownership boundaries.",
    "The result describes capability evidence from Search, AMap MCP, and the installed architecture-diagram Skill.",
    "The result avoids unsupported factual claims beyond the supplied task brief, repository sources, Search results, AMap MCP results, or Skill guidance.",
]


@contextmanager
def sanitized_proxy_env():
    """Avoid httpx proxy parsing failures caused by local IPv6 CIDR no_proxy entries."""

    names = ("NO_PROXY", "no_proxy")
    original = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            value = os.environ.get(name)
            if value:
                os.environ[name] = ",".join(part for part in value.split(",") if part.strip() != "::1/128")
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def amap_transport() -> str:
    load_dotenv(find_dotenv(usecwd=True))
    amap_key = os.getenv("AMAP_API_KEY")
    if not amap_key:
        raise RuntimeError("real_complex_bundle_goal_stream.py requires AMAP_API_KEY for AMap MCP.")
    return f"https://mcp.amap.com/mcp?key={amap_key}"


def print_stream_item(item: Any) -> None:
    meta = item.meta or {}
    stream_kind = meta.get("stream_kind")
    if stream_kind == "progress_delta":
        print(item.delta or "", end="", flush=True)
        return
    if stream_kind == "progress":
        value = item.value if isinstance(item.value, dict) else {}
        print(f"\n[progress:{meta.get('stage', 'task')}] {value.get('message', '')}", flush=True)
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


def install_cocoon_skill(registry_root: Path) -> dict[str, Any]:
    Agently.skills_executor.configure(
        registry_root=str(registry_root),
        allowed_trust_levels=["local", "remote"],
    )
    record = Agently.skills_executor.install_skills_pack(
        source=COCOON_SKILL_SOURCE,
        subpath=COCOON_SKILL_SUBPATH,
        fetch=True,
        trust_level="remote",
        update=True,
    )
    installed_skills = record.get("installed_skills", []) if isinstance(record, dict) else []
    if COCOON_SKILL_ID not in installed_skills:
        raise RuntimeError(f"CocoonAI Skill install did not register {COCOON_SKILL_ID!r}: {installed_skills!r}")
    return record


def configure_skills_stage_models(agent: Any) -> None:
    configured_pool = agent.settings.get("model_pool", {}) or {}
    model_pool = cast(dict[str, Any], dict(configured_pool) if isinstance(configured_pool, dict) else {})
    task_profile = model_pool.get(TASK_MODEL_KEY)
    if task_profile is None:
        return
    for stage_key in (
        "planner",
        "research",
        "reason",
        "reason_fast",
        "executor",
        "verifier",
        "reflector",
        "finalizer",
    ):
        model_pool.setdefault(stage_key, task_profile)
    agent.settings.set("model_pool", model_pool)


def line_excerpt(text: str, *, max_chars: int = 5200) -> str:
    needles = (
        "AgentExecution",
        "AgentTaskLoop",
        "ExecutionPlan",
        "PlanBlock",
        "Blocks",
        "TriggerFlow",
        "Skills",
        "Action",
    )
    selected: list[str] = []
    for index, line in enumerate(text.splitlines()):
        if any(needle in line for needle in needles):
            selected.append(f"{index + 1}: {line}")
        if sum(len(item) for item in selected) > max_chars:
            break
    return "\n".join(selected)[:max_chars]


def action_ids(agent: Any) -> set[str]:
    items = agent.action.get_action_list(tags=[f"agent-{agent.name}"])
    return {str(item.get("action_id") or item.get("name") or "") for item in items if isinstance(item, dict)}


async def main() -> None:
    os.environ.setdefault("AGENT_TASK_JUDGE_TIMEOUT_SECONDS", "180")
    workspace_dir = default_workspace("real-complex-bundle-goal")
    workspace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = workspace_dir / "outputs" / "real_complex_bundle_goal_stream.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    progress_language = os.getenv("AGENTLY_PROGRESS_LANGUAGE", "zh-CN")
    install_record = install_cocoon_skill(workspace_dir / "skills_registry")

    agent = Agently.create_agent("real-complex-bundle-goal-stream").use_workspace(workspace_dir)
    provider = configure_agent_model_pool(agent, temperature=0.0)
    configure_skills_stage_models(agent)
    agent.settings.set("agent_task.progress.language", progress_language)
    agent.set_settings("action.stage_idle_timeout", 240)
    agent.set_settings("tool.stage_idle_timeout", 240)

    raw_effort_presets = agent.settings.get("effort_presets", {})
    effort_presets = cast(dict[str, Any], dict(raw_effort_presets) if isinstance(raw_effort_presets, dict) else {})
    effort_presets[SKILLS_ARTIFACT_EFFORT] = {
        "strategy": "react",
        "step_budget": 4,
        "artifact_inline_limit": 180000,
        "action_concurrency": 1,
        "allowed_actions": ["write_file", "read_file"],
        "required_actions": ["write_file", "read_file"],
    }
    agent.set_settings("effort_presets", effort_presets)

    agent.use_actions(Search(timeout=20, backend="auto", region="us-en"), always=True)
    with sanitized_proxy_env():
        await agent.async_use_mcp(amap_transport())
    agent.enable_workspace_file_actions(read=True, write=True, expose_to_model=True)
    agent.use_skills([COCOON_SKILL_ID], mode="required", always=True)
    agent.configure_skill_capabilities(
        auto_load={
            "web_search": "allow",
            "web_browse": "allow",
            "http_request": "allow",
        }
    )

    @agent.action_func
    def fetch_agently_architecture_sources() -> dict[str, Any]:
        """Return bounded Agently architecture source excerpts from local repository evidence."""

        sources: list[dict[str, Any]] = []
        for relative in SOURCE_PATHS:
            path = ROOT / relative
            if not path.is_file():
                sources.append({"path": relative, "status": "missing"})
                continue
            sources.append(
                {
                    "path": relative,
                    "status": "ok",
                    "excerpt": line_excerpt(path.read_text(encoding="utf-8", errors="replace")),
                }
            )
        return {
            "status": "ok",
            "sources": sources,
            "architecture_focus": [
                "AgentExecution owns the public task-facing run surface.",
                "AgentTaskLoop owns bounded complex-task plan, execute, observe, verify, and replan behavior.",
                "Blocks are the internal lifecycle lowering substrate, not the public business API.",
                "Actions, MCP tools, Skills, and Workspace file actions are capability adapters attached to the task run.",
            ],
        }

    agent.use_actions(fetch_agently_architecture_sources, always=True)
    agent.require_actions(REQUIRED_ACTIONS, always=True)
    agent.require_skills([COCOON_SKILL_ID], always=True)

    workspace = agent.workspace
    if workspace is None:
        raise RuntimeError("Workspace was not initialized.")
    await workspace.put(
        content={
            "task": TASK_ID,
            "outputs": {
                "daily_report": OUTPUT_DAILY_REPORT,
                "travelogue": OUTPUT_TRAVELOGUE,
                "architecture": OUTPUT_ARCHITECTURE,
            },
            "city": "Hangzhou",
            "amap_targets": ["杭州东站", "西湖"],
            "skill": f"{COCOON_SKILL_SOURCE}#{COCOON_SKILL_SUBPATH}",
            "source_paths": SOURCE_PATHS,
        },
        collection="observations",
        kind="real_complex_bundle_task_brief",
        summary="High-level Goal Pursuit task brief using Search, AMap MCP, and CocoonAI Skill.",
        scope={"task_id": TASK_ID},
        source={"type": "example_script", "name": "real_complex_bundle_goal_stream"},
    )

    task_input = {
        "business_context": {
            "run_purpose": "example-level usability proof for the complex task execution lifecycle",
            "city": "Hangzhou",
            "travel_start": "杭州东站",
            "travel_destination": "西湖",
            "public_research_queries": [
                "Agently AgentEra GitHub AI agent framework",
                "OpenAI Codex Skills Agent Skills specification",
            ],
            "architecture_source_paths": SOURCE_PATHS,
            "deliverable_files": {
                "daily_report": OUTPUT_DAILY_REPORT,
                "travelogue": OUTPUT_TRAVELOGUE,
                "architecture": OUTPUT_ARCHITECTURE,
            },
        },
        "capability_requirements": {
            "search": "Use public Search for framework and skill context.",
            "amap_mcp": "Use AMap MCP for Hangzhou weather, POI, geocode, and transit route evidence.",
            "architecture_skill": "Use the installed CocoonAI architecture-diagram Skill for the HTML/SVG diagram style and artifact guidance.",
            "workspace": "Write the three deliverables to Workspace files and read them back before finalizing.",
        },
    }

    goal = (
        "Produce a real complex task bundle for an operator: an execution daily report, "
        "a Hangzhou business travelogue, and an Agently Blocks lifecycle architecture diagram. "
        "Use the mounted Search action, AMap MCP tools, repository-source action, workspace file actions, "
        f"and the installed `{COCOON_SKILL_ID}` Skill. Write the deliverables to the requested Workspace files "
        "and return a concise final summary with source trace and any unresolved risks."
    )
    success_criteria = [
        f"The daily report is written to `{OUTPUT_DAILY_REPORT}` and separates completed work, evidence, risks, and next actions.",
        f"The travelogue is written to `{OUTPUT_TRAVELOGUE}` and grounds factual location, weather, POI, or route claims in AMap MCP evidence.",
        f"The architecture diagram is written to `{OUTPUT_ARCHITECTURE}` as single-file HTML with inline SVG.",
        "The architecture diagram uses repository evidence for AgentExecution, AgentTaskLoop, Blocks, Actions, MCP, Skills, and Workspace boundaries.",
        "Execution evidence includes Search, AMap MCP, repository-source collection, workspace write/readback, and architecture-diagram Skill usage.",
        "The final summary calls out unsupported or incomplete external evidence instead of filling gaps with invented facts.",
    ]

    execution = (
        agent.goal(goal, success_criteria)
        .effort(
            "high",
            budget={"iteration_limit": 5, "model_call_limit": 30, "wall_time_seconds": 900},
            planning={"depth": "deep", "require_source_collection": True},
            execution={"step_plan": "direct"},
            verification={"strength": "strong", "require_artifact_readback": True},
            replan={"on_missing_criteria": True},
            progress={"detail": "natural_language", "stream": True, "snapshots": True},
        )
        .input(task_input)
        .output(
            {
                "daily_report_file": (str, "Workspace-relative daily report path.", True),
                "travelogue_file": (str, "Workspace-relative travelogue path.", True),
                "architecture_file": (str, "Workspace-relative architecture diagram path.", True),
                "source_trace": ([str], "Concise source trace across Search, AMap MCP, repository evidence, and Skill usage.", True),
                "risks": ([str], "Unresolved external-data or artifact risks.", True),
            },
            format="json",
        )
        .strategy(
            "task",
            task_id=TASK_ID,
            workspace=workspace_dir,
            limits={"max_model_requests": 30, "max_seconds": 900, "max_no_progress_seconds": 240},
            options={
                "agent_task": {
                    "request_timeout_seconds": 180,
                    "stream_progress": True,
                    "stream_snapshots": True,
                    "progress_model_key": TASK_MODEL_KEY,
                    "progress_language": progress_language,
                    "progress_timeout_seconds": 45,
                },
                "routes": {
                    "model_request": {"action_loop": {"max_rounds": 10}},
                    "skills": {"effort": SKILLS_ARTIFACT_EFFORT, "output_format": "yaml_literal"},
                },
                "capability_evidence_requirements": [
                    {"capability_id": "search", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "maps_weather", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "maps_geo", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "maps_text_search", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "maps_direction_transit_integrated", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "fetch_agently_architecture_sources", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": "read_file", "capability_kind": "action", "kind": "action_succeeded"},
                    {"capability_id": COCOON_SKILL_ID, "capability_kind": "skill", "kind": "capability_used"},
                ],
            },
        )
    )

    stream_items: list[Any] = []
    print("[setup] high-level real complex Goal Pursuit stream")
    print(f"[setup] provider={provider} progress_language={progress_language}")
    print(f"[setup] workspace={workspace_dir}")
    print(f"[setup] installed_skill={COCOON_SKILL_ID} source={COCOON_SKILL_SOURCE}")
    print(f"[setup] registered_actions={sorted(action_ids(agent))}")
    with trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator(type="instant"):
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
            trace_file.flush()
            print_stream_item(item)

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    final_result = result.get("final_result")
    final_text = final_result if isinstance(final_result, str) else json.dumps(final_result, ensure_ascii=False, indent=2)
    model_judge = await judge_business_artifact(
        agent,
        scenario="High-level real complex Goal Pursuit bundle using Search, AMap MCP, and CocoonAI Skill.",
        artifact_text=final_text,
        business_context={
            "task_input": task_input,
            "success_criteria": success_criteria,
            "installed_skill": COCOON_SKILL_ID,
            "skill_source": f"{COCOON_SKILL_SOURCE}#{COCOON_SKILL_SUBPATH}",
            "required_actions": REQUIRED_ACTIONS,
        },
        rules=JUDGE_RULES,
    )

    files_root = workspace.files_root
    output_files = {
        "daily_report": files_root / OUTPUT_DAILY_REPORT,
        "travelogue": files_root / OUTPUT_TRAVELOGUE,
        "architecture": files_root / OUTPUT_ARCHITECTURE,
    }
    registered_actions = action_ids(agent)
    required_skill_ids = execution.required_skill_ids()
    required_action_ids = execution.required_action_ids()
    progress_delta_text = "".join(
        str(item.delta or "")
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "progress_delta"
    )
    architecture_text = (
        output_files["architecture"].read_text(encoding="utf-8", errors="replace")
        if output_files["architecture"].is_file()
        else ""
    )
    host_checks = {
        "route_is_agent_task": meta.get("route", {}).get("selected_route") == "agent_task",
        "task_completed": result.get("status") == "completed" and bool(result.get("accepted")),
        "search_action_registered": "search" in registered_actions,
        "amap_mcp_tools_registered": {"maps_weather", "maps_geo", "maps_text_search"}.issubset(registered_actions),
        "required_actions_visible": set(REQUIRED_ACTIONS).issubset(set(required_action_ids)),
        "required_skill_visible": COCOON_SKILL_ID in required_skill_ids,
        "progress_delta_streamed": bool(progress_delta_text.strip()),
        "progress_language_observed": any(
            (item.meta or {}).get("progress_language") == progress_language
            for item in stream_items
            if (item.meta or {}).get("stream_kind") in {"progress", "progress_delta"}
        ),
        "daily_report_written": output_files["daily_report"].is_file(),
        "travelogue_written": output_files["travelogue"].is_file(),
        "architecture_html_svg_written": output_files["architecture"].is_file()
        and "<html" in architecture_text.lower()
        and "<svg" in architecture_text.lower(),
        "model_judge_passed": bool(model_judge.get("accepted")),
    }
    summary = {
        "provider": provider,
        "installed_skill": COCOON_SKILL_ID,
        "skill_source_url": install_record.get("source_url") if isinstance(install_record, dict) else None,
        "task_status": result.get("status"),
        "accepted": bool(result.get("accepted")),
        "artifact_status": result.get("artifact_status"),
        "route": meta.get("route", {}),
        "stream_counts": {
            "progress_delta": sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "progress_delta"),
            "progress": sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"),
            "snapshot": sum(1 for item in stream_items if (item.meta or {}).get("stream_kind") == "snapshot"),
        },
        "registered_actions": sorted(registered_actions),
        "required_actions": required_action_ids,
        "required_skills": required_skill_ids,
        "host_checks": host_checks,
        "model_judge": model_judge,
        "task_refs": result.get("task_refs") or meta.get("task_refs"),
        "output_files": {name: str(path) for name, path in output_files.items()},
        "stream_trace_file": str(trace_path),
        "final_result": final_text,
    }
    summary_path = files_root / OUTPUT_SUMMARY
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(summary)
    if not all(host_checks.values()):
        raise AssertionError(f"High-level real complex Goal Pursuit example failed host checks: {host_checks}")


if __name__ == "__main__":
    asyncio.run(main())
