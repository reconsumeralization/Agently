"""City day-trip planner — remote Travel Planner Skill + real AMap MCP.

Run:
    python examples/agent_auto_orchestration/18_amap_mcp_trip_planner.py
    python examples/agent_auto_orchestration/18_amap_mcp_trip_planner.py 成都
    AGENTLY_TRIP_CITY="Hangzhou 杭州" python examples/agent_auto_orchestration/18_amap_mcp_trip_planner.py

Environment:
    DEEPSEEK_API_KEY  — the reasoning model (shell or .env).
    AMAP_API_KEY      — a real AMap (高德) key for the remote MCP server.
                        Get one free at https://lbs.amap.com/ . The example
                        skips gracefully if it is missing.

What this demonstrates
----------------------
This is the remote public-Skill + remote MCP path: the travel planning guidance
comes from `ZawYePhyo/travel-planner-skill`, while live city facts come from the
real AMap Streamable HTTP MCP server through the agent's Action runtime.

Phase 1 — Field research (model-generated AMap MCP actions).
    The model generates concrete AMap action calls against registered MCP tool
    schemas; ActionRuntime executes them and records real observations.

Phase 2 — Itinerary synthesis (remote Travel Planner Skill).
    The remote travel-planner Skill turns the gathered REAL observations into a
    structured one-day itinerary via ``output``. The HOST writes the
    markdown artifact (the only side effect).

Model calls and MCP data are BOTH real. The weather forecast, attraction names,
and addresses come from AMap, not from the model's memory.

Expected key output from one real run (city=杭州, 2026-05-24):
    amap_tools_registered=15
    → tool round 1: 3 call(s)
    research_tool_calls=3
    weather_observed=True   (forecast: 杭州 大雨, 24-30°C)
    pois_observed=10        (e.g. 杭州西湖风景名胜区, 清河坊历史文化特色街区)
    itinerary_blocks=3      (weather-aware: rain → sheltered/indoor picks)
    plan written: .../amap_trip_planner/plans/trip_plan_杭州.md
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model

RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "amap_trip_planner"

TRAVEL_PLANNER_SKILL = {
    "source": "ZawYePhyo/travel-planner-skill",
    "trust_level": "remote",
}

# ── Phase 2 Skill: prompt-only synthesis into a structured itinerary ──────────

ITINERARY_OUTPUTS: dict[str, Any] = {
    "city": (str, "City the plan is for.", True),
    "weather_summary": (str, "One-line summary of the forecast actually provided.", True),
    "blocks": [
        (
            {
                "part_of_day": (str, "morning | afternoon | evening", True),
                "activity": (str, "What to do, using a real place name from the research.", True),
                "place": (str, "Real place name from the research data.", True),
                "why": (str, "Why this fits (incl. weather adaptation when relevant).", True),
            },
            "Itinerary block.",
            True,
        )
    ],
    "dining": (str, "A real restaurant suggestion from the research.", True),
    "tips": ([str], "Practical tips, weather-aware.", True),
}

def _summarize_observations(history: list[dict[str, Any]]) -> str:
    """Flatten AMap observation records into compact text for synthesis."""
    lines: list[str] = []
    for obs in history:
        name = obs.get("action_id", obs.get("name", "tool"))
        result = obs.get("result")
        if result in (None, "", {}):
            continue
        lines.append(f"### {name}\n{json.dumps(result, ensure_ascii=False)[:2000]}")
    return "\n\n".join(lines)


async def _execute_model_owned_amap_actions(agent: Any, *, city: str) -> list[dict[str, Any]]:
    agent.input(
        (
            f"Use the registered AMap MCP actions to collect one-day trip facts for {city}. "
            "Generate action calls only for maps_weather and maps_text_search. "
            "Call maps_weather once, maps_text_search for 景点, and maps_text_search for 餐厅. "
            "Do not answer from memory."
        )
    )
    action_calls = await agent.async_generate_action_call(max_rounds=1)
    if not action_calls:
        raise RuntimeError("The model did not generate AMap MCP action calls.")

    records: list[dict[str, Any]] = []
    for call in action_calls:
        if not isinstance(call, dict):
            continue
        action_id = str(call.get("action_id") or call.get("tool_name") or call.get("name") or "")
        if action_id not in {"maps_weather", "maps_text_search"}:
            continue
        action_input = call.get("action_input") or call.get("tool_kwargs") or call.get("kwargs") or {}
        if not isinstance(action_input, dict):
            action_input = {}
        result = await agent.action.async_execute_action(
            action_id,
            action_input,
            purpose=str(call.get("purpose") or "Collect AMap travel facts."),
            source_protocol="example_model_generated_mcp",
        )
        records.append({
            "action_id": action_id,
            "action_input": action_input,
            "status": result.get("status"),
            "result": result.get("data", result.get("result", result.get("error"))),
        })
    if not records:
        raise RuntimeError(f"The model generated no executable AMap actions: { action_calls }")
    return records


async def main() -> None:
    provider = configure_model(temperature=0.2)
    amap_key = os.getenv("AMAP_API_KEY")
    if not amap_key:
        print("AMAP_API_KEY not set; skipping (this example needs a real AMap MCP key).")
        return

    city = " ".join(a for a in sys.argv[1:] if not a.startswith("-")).strip()
    city = city or os.getenv("AGENTLY_TRIP_CITY", "").strip() or "杭州"

    divider = "=" * 64
    print(divider)
    print(f"AMap MCP Trip Planner — real remote MCP + Skills   ·   provider={provider}")
    print(f"City: {city}")
    print(divider)

    Agently.skills_executor.configure(
        registry_root=str(RUNTIME_ROOT / "registry"),
        allowed_trust_levels=["local", "remote"],
    )

    agent = Agently.create_agent("amap-trip-planner")
    agent.use_skills([TRAVEL_PLANNER_SKILL], mode="required")
    # Wire the REAL remote AMap Streamable HTTP MCP server into Action runtime.
    agent.use_mcp(f"https://mcp.amap.com/mcp?key={amap_key}")
    amap_tools = [t["name"] for t in agent.action.get_tool_info().values() if str(t["name"]).startswith("maps_")]
    print(f"amap_tools_registered={len(amap_tools)}")

    # ── Phase 1: model generates and ActionRuntime executes real AMap MCP calls ──
    print("\n[Phase 1] Field research via AMap MCP actions…")
    history = await _execute_model_owned_amap_actions(agent, city=city)
    research_calls = sum(1 for h in history if isinstance(h, dict) and h.get("result") not in (None, "", {}))

    weather_observed = any("weather" in str(h.get("action_id", h.get("name", ""))) and h.get("result") for h in history)
    poi_names: list[str] = []
    for h in history:
        result = h.get("result")
        if isinstance(result, dict):
            for poi in (result.get("pois") or [])[:5]:
                name = poi.get("name")
                if name and name not in poi_names:
                    poi_names.append(name)
    print(f"research_tool_calls={research_calls}")
    print(f"weather_observed={weather_observed}")
    print(f"pois_observed={len(poi_names)}  e.g. {', '.join(poi_names[:3])}")

    observations = _summarize_observations(history)
    if not observations.strip():
        print("No tool observations gathered; aborting synthesis.")
        return

    # ── Phase 2: remote Travel Planner Skill synthesizes a structured itinerary ──
    print("\n[Phase 2] Itinerary synthesis (remote travel-planner Skill)…")
    synthesis = await agent.async_run_skills_task(
        f"Design a one-day itinerary for {city} from this researched data:\n\n{observations}",
        mode="required",
        effort="normal",
        output=ITINERARY_OUTPUTS,
    )
    if synthesis.status != "success":
        print("synthesis failed:", synthesis.output)
        return
    plan = synthesis.output or {}
    blocks = plan.get("blocks", []) or []

    # ── Host side effect: write the plan artifact ──
    out_dir = RUNTIME_ROOT / "plans"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"trip_plan_{city.split()[0]}.md"
    lines = [f"# One-Day Trip Plan — {plan.get('city', city)}\n",
             f"**Weather:** {plan.get('weather_summary', '—')}\n"]
    for b in blocks:
        lines.append(f"## {b.get('part_of_day', '').title()} — {b.get('place', '')}\n"
                     f"{b.get('activity', '')}\n\n*{b.get('why', '')}*\n")
    lines.append(f"## Dining\n{plan.get('dining', '—')}\n")
    if plan.get("tips"):
        lines.append("## Tips\n" + "\n".join(f"- {t}" for t in plan["tips"]))
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n{divider}")
    print(f"weather_summary={plan.get('weather_summary', '—')}")
    print(f"itinerary_blocks={len(blocks)}")
    for b in blocks:
        print(f"  · {b.get('part_of_day', '')}: {b.get('place', '')}")
    print(f"dining={plan.get('dining', '—')}")
    print(f"plan written: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
