# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Blocks external-capability proof with Search, AMap MCP, and CocoonAI Skill.

This is a Blocks-level substrate probe, not the recommended high-level business
entry point. Use `examples/agent_task/real_complex_bundle_goal_stream.py` to
exercise the encapsulated `agent.use_actions()`, `agent.use_mcp()` /
`agent.async_use_mcp()`, `agent.use_skills()`, and
`.goal().effort().input().output()` chain.

This example is intentionally not a mock-data bundle. It fails closed when the
real lower-level capabilities it claims are unavailable:

- Search uses the built-in `Search` action package backed by ddgs.
- AMap data comes from the remote AMap MCP server and requires `AMAP_API_KEY`.
- The architecture diagram branch installs and activates the public
  `Cocoon-AI/architecture-diagram-generator` Skill at runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, cast
import sys

from dotenv import find_dotenv, load_dotenv

EXAMPLE_DIR = Path(__file__).resolve().parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from _business_ladder_runtime import (
    ARTIFACTS_DIR,
    ROOT,
    BusinessCase,
    all_outputs,
    compile_case,
    emit,
    generate_model_artifact,
    model_judge,
    output_for,
    run_business_cases,
)
from agently import Agently
from agently.builtins.actions import Search
from agently.builtins.plugins.ActionExecutor.MCPActionExecutor import MCPActionExecutor


COCOON_SKILL_SOURCE = "Cocoon-AI/architecture-diagram-generator"
COCOON_SKILL_SUBPATH = "architecture-diagram"
COCOON_SKILL_ID = "architecture-diagram"
COCOON_REGISTRY_ROOT = ARTIFACTS_DIR / "cocoon_skills_registry"

ARCHITECTURE_SOURCE_PATHS = [
    "spec/implemented/architecture/COMPLEX_TASK_EXECUTION_LIFECYCLE_BLOCKS_PLUGIN_SPEC.md",
    "docs/en/reference/blocks-lifecycle.md",
    "docs/en/reference/execution-layer-selection.md",
    "agently/builtins/plugins/Blocks/AgentlyBlocks.py",
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
        raise RuntimeError("07_real_complex_bundle_stream.py requires AMAP_API_KEY for the real AMap MCP branch.")
    return f"https://mcp.amap.com/mcp?key={amap_key}"


async def call_amap_tool(tool_name: str, action_input: Mapping[str, Any]) -> Any:
    with sanitized_proxy_env():
        return await MCPActionExecutor(tool_name, amap_transport()).execute(
            spec={"action_id": tool_name},
            action_call={"action_input": dict(action_input)},
            policy={},
            settings=Agently.settings,
        )


def compact_search_results(results: list[Mapping[str, Any]], *, limit: int = 5) -> list[dict[str, str]]:
    compacted: list[dict[str, str]] = []
    for result in results[:limit]:
        compacted.append(
            {
                "title": str(result.get("title") or ""),
                "href": str(result.get("href") or result.get("url") or ""),
                "body": str(result.get("body") or result.get("snippet") or "")[:500],
            }
        )
    return compacted


async def search_public_context(context: Mapping[str, Any]) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": "Searching public context with the built-in Search action."})
    search = Search(timeout=20, backend="auto", region="us-en")
    queries = [
        "Agently AgentEra GitHub AI agent framework",
        "OpenAI Codex Skills Agent Skills specification",
    ]
    query_results: list[dict[str, Any]] = []
    for query in queries:
        results = await search.search(query=query, max_results=5)
        compacted = compact_search_results(cast(list[Mapping[str, Any]], results))
        if not compacted:
            raise RuntimeError(f"Search returned no usable results for query: {query}")
        query_results.append({"query": query, "results": compacted})
        await emit(context, {"type": "business.progress", "message": f"Search returned {len(compacted)} results for: {query}"})
    return {
        "source": "agently.builtins.actions.Search",
        "queries": query_results,
        "action_evidence": [{"action_id": "search", "status": "success", "query_count": len(query_results)}],
    }


async def amap_trip_context(context: Mapping[str, Any]) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": "Calling real AMap MCP tools for Hangzhou trip context."})
    weather = await call_amap_tool("maps_weather", {"city": "杭州"})
    west_lake_geo = await call_amap_tool("maps_geo", {"address": "西湖", "city": "杭州"})
    hangzhou_east_geo = await call_amap_tool("maps_geo", {"address": "杭州东站", "city": "杭州"})
    poi_search = await call_amap_tool("maps_text_search", {"keywords": "西湖", "city": "杭州", "citylimit": True})

    west_lake_location = (
        cast(dict[str, Any], cast(dict[str, Any], west_lake_geo).get("results", [{}])[0]).get("location")
        if isinstance(west_lake_geo, dict)
        else None
    )
    hangzhou_east_location = (
        cast(dict[str, Any], cast(dict[str, Any], hangzhou_east_geo).get("results", [{}])[0]).get("location")
        if isinstance(hangzhou_east_geo, dict)
        else None
    )
    transit = None
    if west_lake_location and hangzhou_east_location:
        transit = await call_amap_tool(
            "maps_direction_transit_integrated",
            {
                "origin": str(hangzhou_east_location),
                "destination": str(west_lake_location),
                "city": "杭州",
                "cityd": "杭州",
            },
        )

    await emit(context, {"type": "business.progress", "message": "AMap MCP returned weather, geocode, POI, and route data."})
    return {
        "source": "AMap MCP",
        "city": "杭州",
        "weather": weather,
        "west_lake_geo": west_lake_geo,
        "hangzhou_east_geo": hangzhou_east_geo,
        "west_lake_pois": poi_search,
        "transit_from_hangzhou_east_to_west_lake": transit,
        "action_evidence": [
            {"action_id": "maps_weather", "status": "success"},
            {"action_id": "maps_geo", "status": "success", "target": "西湖"},
            {"action_id": "maps_geo", "status": "success", "target": "杭州东站"},
            {"action_id": "maps_text_search", "status": "success"},
            {"action_id": "maps_direction_transit_integrated", "status": "success" if transit is not None else "skipped"},
        ],
    }


async def install_cocoon_skill(context: Mapping[str, Any]) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": "Installing the public CocoonAI architecture-diagram Skill."})
    Agently.skills_executor.configure(
        registry_root=str(COCOON_REGISTRY_ROOT),
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
    await emit(context, {"type": "business.progress", "message": "CocoonAI architecture-diagram Skill is installed."})
    return {
        "source": COCOON_SKILL_SOURCE,
        "subpath": COCOON_SKILL_SUBPATH,
        "install_record": record,
        "action_evidence": [{"action_id": "install_cocoon_skill", "status": "success", "skill_id": COCOON_SKILL_ID}],
    }


def excerpt_file(path: Path, *, max_chars: int = 4200) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    needles = ("AgentExecution", "AgentTaskLoop", "ExecutionPlan", "PlanBlock", "Blocks", "TriggerFlow", "EvidenceEnvelope")
    lines = text.splitlines()
    selected: list[str] = []
    for index, line in enumerate(lines):
        if any(needle in line for needle in needles):
            selected.append(f"{index + 1}: {line}")
        if sum(len(item) for item in selected) > max_chars:
            break
    return "\n".join(selected)[:max_chars]


async def repo_architecture_sources(context: Mapping[str, Any]) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": "Collecting local repository architecture evidence."})
    sources: list[dict[str, Any]] = []
    for relative in ARCHITECTURE_SOURCE_PATHS:
        path = ROOT / relative
        if path.is_file():
            sources.append({"path": relative, "status": "ok", "excerpt": excerpt_file(path)})
        else:
            sources.append({"path": relative, "status": "missing"})
    if not any(item["status"] == "ok" for item in sources):
        raise RuntimeError("No repository architecture evidence files were available.")
    return {
        "source": "local_repository",
        "sources": sources,
        "action_evidence": [{"action_id": "repo_architecture_sources", "status": "success", "source_count": len(sources)}],
    }


async def daily_report(context: Mapping[str, Any]) -> dict[str, Any]:
    public_context = output_for(context, "search_context") or {}
    amap_context = output_for(context, "amap_context") or {}
    return await generate_model_artifact(
        context,
        artifact="daily_report",
        business_context={"public_context": public_context, "amap_context": amap_context},
        instructions=[
            "Write an operator-facing daily report for a real capability validation run.",
            "Separate completed work, external evidence, risks, and next actions.",
            "Ground public references in Search results and trip/location facts in AMap MCP results.",
        ],
        output_schema={
            "title": (str, "Report title.", True),
            "completed": ([str], "Completed validation work.", True),
            "external_evidence": ([str], "Evidence from Search and AMap MCP.", True),
            "risks": ([str], "Current risks.", True),
            "next_actions": ([str], "Concrete next actions.", True),
        },
    )


async def travelogue(context: Mapping[str, Any]) -> dict[str, Any]:
    amap_context = output_for(context, "amap_context") or {}
    return await generate_model_artifact(
        context,
        artifact="travelogue",
        business_context={"amap_context": amap_context},
        instructions=[
            "Write a concise business travelogue for arriving at Hangzhou East Railway Station and visiting West Lake.",
            "Use only AMap MCP weather, geocode, POI, and route facts for factual claims.",
            "Include source_trace items that identify the AMap tool behind the main factual claims.",
        ],
        output_schema={
            "title": (str, "Travelogue title.", True),
            "story": (str, "Narrative travelogue grounded in AMap MCP results.", True),
            "practical_notes": ([str], "Practical notes grounded in AMap MCP results.", True),
            "source_trace": [
                {
                    "claim": (str, "A main factual claim from the travelogue.", True),
                    "source_tools": ([str], "AMap MCP tool names supporting the claim.", True),
                }
            ],
        },
    )


async def architecture_diagram(context: Mapping[str, Any]) -> dict[str, Any]:
    skill_activation = output_for(context, "activate_cocoon") or {}
    repo_sources = output_for(context, "repo_sources") or {}
    return await generate_model_artifact(
        context,
        artifact="architecture_diagram",
        business_context={"skill_activation": skill_activation, "repo_sources": repo_sources},
        instructions=[
            "Create a single-file HTML architecture diagram using the activated CocoonAI architecture-diagram Skill guidance.",
            "The artifact must include inline SVG and show the Blocks lifecycle ownership boundaries.",
            "Use the supplied repository source excerpts for architecture facts.",
        ],
        output_schema={
            "title": (str, "Diagram title.", True),
            "html": (str, "Single-file HTML with inline SVG architecture diagram.", True),
            "source_notes": ([str], "Repository source notes used for the diagram.", True),
        },
    )


async def judge_complex_bundle(context: Mapping[str, Any]) -> dict[str, Any]:
    outputs = all_outputs(context)
    bundle = {
        "daily_report": cast(dict[str, Any], outputs.get("daily_report") or {}).get("content"),
        "travelogue": cast(dict[str, Any], outputs.get("travelogue") or {}).get("content"),
        "architecture_diagram": cast(dict[str, Any], outputs.get("architecture") or {}).get("content"),
    }
    host_checks = {
        "search_results_present": bool(cast(dict[str, Any], outputs.get("search_context") or {}).get("queries")),
        "amap_weather_present": bool(cast(dict[str, Any], outputs.get("amap_context") or {}).get("weather")),
        "amap_poi_present": bool(cast(dict[str, Any], outputs.get("amap_context") or {}).get("west_lake_pois")),
        "cocoon_install_present": COCOON_SKILL_ID in cast(dict[str, Any], cast(dict[str, Any], outputs.get("install_cocoon") or {}).get("install_record") or {}).get("installed_skills", []),
        "cocoon_activation_present": bool(cast(dict[str, Any], outputs.get("activate_cocoon") or {}).get("activations")),
        "architecture_html_svg_present": "<svg" in str(cast(dict[str, Any], bundle.get("architecture_diagram") or {}).get("html") or "").lower(),
    }
    judged = await model_judge(
        scenario="real_complex_bundle_search_amap_cocoon",
        candidate=bundle,
        business_context={
            "search_context": outputs.get("search_context"),
            "amap_context": outputs.get("amap_context"),
            "cocoon_install": outputs.get("install_cocoon"),
            "cocoon_activation": outputs.get("activate_cocoon"),
            "repo_sources": outputs.get("repo_sources"),
            "host_checks": host_checks,
        },
        rules=[
            "The daily report separates completed work, external evidence, risks, and next actions.",
            "The travelogue is grounded in AMap MCP weather, geocode, POI, or route data.",
            "The travelogue includes source_trace items naming AMap MCP tools for factual claims.",
            "The architecture diagram is an HTML/SVG artifact that follows the activated architecture-diagram Skill guidance at a high level.",
            "The architecture content reflects Blocks lifecycle ownership from repository source excerpts.",
            "The bundle does not treat progress narration or artifact existence as acceptance evidence.",
            "No public, trip, route, weather, POI, or architecture fact is introduced beyond the supplied Search, AMap MCP, Cocoon Skill, or repository evidence.",
        ],
    )
    ok = bool(judged.get("accepted")) and not judged.get("unsupported_claims") and all(host_checks.values())
    await emit(context, {"type": "business.validation", "scenario": "real_complex_bundle", "accepted": ok})
    return {"ok": ok, "model_judge": judged, "host_checks": host_checks, "bundle": bundle}


HANDLERS = {
    "search_public_context": search_public_context,
    "amap_trip_context": amap_trip_context,
    "install_cocoon_skill": install_cocoon_skill,
    "repo_architecture_sources": repo_architecture_sources,
    "daily_report": daily_report,
    "travelogue": travelogue,
    "architecture_diagram": architecture_diagram,
    "judge_complex_bundle": judge_complex_bundle,
}


def build_case() -> BusinessCase:
    return {
        "case_id": "06_real_complex_bundle",
        "title": "Blocks external Search, AMap MCP, and CocoonAI Skill branches",
        "graph": compile_case(
            "blocks-business-real-complex-bundle",
            [
                {"id": "search_context", "plan_block_id": "action_call", "kind": "action_call", "runtime_preferences": {"handler": "search_public_context"}},
                {
                    "id": "amap_context",
                    "plan_block_id": "mcp_tool_call",
                    "kind": "mcp_tool_call",
                    "capability_requirements": [{"need": "mcp"}],
                    "runtime_preferences": {"handler": "amap_trip_context"},
                },
                {"id": "install_cocoon", "plan_block_id": "action_call", "kind": "action_call", "runtime_preferences": {"handler": "install_cocoon_skill"}},
                {
                    "id": "activate_cocoon",
                    "plan_block_id": "skill_activation",
                    "kind": "skill_activation",
                    "bound_inputs": {
                        "skill_id": COCOON_SKILL_ID,
                        "task": "render a Blocks lifecycle architecture diagram as HTML/SVG",
                        "budget_chars": 10000,
                    },
                },
                {"id": "repo_sources", "plan_block_id": "action_call", "kind": "action_call", "runtime_preferences": {"handler": "repo_architecture_sources"}},
                {"id": "daily_report", "plan_block_id": "model_request", "kind": "model_request", "runtime_preferences": {"handler": "daily_report"}},
                {"id": "travelogue", "plan_block_id": "model_request", "kind": "model_request", "runtime_preferences": {"handler": "travelogue"}},
                {"id": "architecture", "plan_block_id": "model_request", "kind": "model_request", "runtime_preferences": {"handler": "architecture_diagram"}},
                {"id": "judge_bundle", "plan_block_id": "validation", "kind": "validation", "runtime_preferences": {"handler": "judge_complex_bundle"}},
            ],
            [
                {"from": "install_cocoon", "to": "activate_cocoon"},
                {"from": "activate_cocoon", "to": "architecture"},
                {"from": "repo_sources", "to": "architecture"},
                {"from": "search_context", "to": "daily_report"},
                {"from": "amap_context", "to": "daily_report"},
                {"from": "amap_context", "to": "travelogue"},
                {"from": "daily_report", "to": "judge_bundle"},
                {"from": "travelogue", "to": "judge_bundle"},
                {"from": "architecture", "to": "judge_bundle"},
            ],
            capability_resolution={"allowed_capabilities": ["mcp"]},
        ),
        "handlers": HANDLERS,
        "runtime_resources": {"skills.executor": Agently.skills_executor},
        "needs_model": True,
    }


async def main() -> None:
    await run_business_cases([build_case()])


if __name__ == "__main__":
    asyncio.run(main())
