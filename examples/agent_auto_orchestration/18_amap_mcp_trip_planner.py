"""City day-trip planner — real remote MCP (AMap) + Skills, two-phase agent.

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
This is the "complex" combined path: a **real remote MCP server** wired into an
agent's Action runtime, driven **from inside a Skill** — not by host glue code.

Phase 1 — Field research (react Skill + AMap MCP).
    A standard ``SKILL.md`` with ``execution: react`` and ``allowed-tools``
    listing AMap tool names. The react loop reasons → calls a real AMap tool →
    observes, gathering live weather and points-of-interest for the city. The
    tools resolve through the agent's ActionRuntime (``agent.use_mcp(...)``),
    and the react planner surfaces each tool's real argument schema to the
    model, so calls use correct parameter names.

Phase 2 — Itinerary synthesis (single_shot Skill).
    A prompt-only ``SKILL.md`` turns the gathered REAL observations into a
    structured one-day itinerary via ``semantic_outputs``. The HOST writes the
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

# ── Phase 1 Skill: react loop that calls the real AMap MCP tools ──────────────
FIELD_RESEARCH_SKILL = """---
name: City Field Researcher
description: Gather grounded local facts (weather and points of interest) for a city using AMap map tools.
keywords: [travel, city, weather, attractions, restaurants, 行程, 景点]
execution: react
allowed-tools: [maps_weather, maps_text_search]
max-steps: 6
---

# City Field Researcher

You research a city for a one-day visit using the available map tools. Use ONLY
data returned by the tools — never invent weather, place names, or addresses.

Work ONE tool call per step and read its result before the next step:
1. Call `maps_weather` with the city name to get the forecast.
2. Call `maps_text_search` with `keywords="景点"` and `city=<the city>` to find
   top attractions.
3. Call `maps_text_search` with `keywords="餐厅"` and `city=<the city>` to find
   well-known local restaurants.

When you have the weather plus attractions plus restaurants, set `final: true`.
Always pass the exact argument names the tools declare.
"""

# ── Phase 2 Skill: prompt-only synthesis into a structured itinerary ──────────
ITINERARY_SKILL = """---
name: Itinerary Designer
description: Turn researched city facts into a structured, weather-aware one-day itinerary.
keywords: [itinerary, trip plan, schedule, travel]
---

# Itinerary Designer

Given REAL researched facts about a city (weather forecast, attractions,
restaurants), design a practical one-day itinerary.

- Adapt to the weather: if rain or extreme heat is forecast, prefer sheltered or
  indoor options and say so.
- Use only the provided place names and addresses; do not invent new ones.
- Sequence morning → afternoon → evening with a dining suggestion, and keep it
  realistic for a single day.
"""

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


def install_skill(skill_md: str, slug: str) -> str:
    src = RUNTIME_ROOT / "skills" / slug
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(skill_md, encoding="utf-8")
    contract = Agently.skills_executor.install_skills(src, trust_level="local", update=True)
    return str(contract["skill_id"])


def _summarize_observations(history: list[dict[str, Any]]) -> str:
    """Flatten the react observation trail into compact text for synthesis."""
    lines: list[str] = []
    for obs in history:
        name = obs.get("name", "tool")
        result = obs.get("result")
        if result in (None, "", {}):
            continue
        lines.append(f"### {name}\n{json.dumps(result, ensure_ascii=False)[:2000]}")
    return "\n\n".join(lines)


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
        allowed_trust_levels=["local"],
    )
    research_skill = install_skill(FIELD_RESEARCH_SKILL, "city-field-researcher")
    itinerary_skill = install_skill(ITINERARY_SKILL, "itinerary-designer")

    agent = Agently.create_agent("amap-trip-planner")
    # Wire the REAL remote AMap MCP server into the agent's Action runtime.
    agent.use_mcp(f"https://mcp.amap.com/mcp?key={amap_key}")
    amap_tools = [t["name"] for t in agent.action.get_tool_info().values() if str(t["name"]).startswith("maps_")]
    print(f"amap_tools_registered={len(amap_tools)}")

    # ── Phase 1: react Skill gathers real data through AMap MCP ──
    print("\n[Phase 1] Field research via AMap MCP (react)…")
    tool_rounds = 0

    async def on_stream(item: dict[str, Any]) -> None:
        if item.get("type") == "skills.react.action_runtime_round":
            nonlocal tool_rounds
            count = (item.get("payload") or {}).get("action_count", 0)
            if count:
                tool_rounds += 1
                print(f"  → tool round {tool_rounds}: {count} call(s)")

    research = await agent.async_run_skills_task(
        f"Research a one-day visit to {city}. Gather weather, attractions, and restaurants.",
        skills=[research_skill],
        mode="required",
        stream_handler=on_stream,
    )
    history = (research.output or {}).get("history", []) if isinstance(research.output, dict) else []
    research_calls = sum(1 for h in history if isinstance(h, dict) and h.get("result") not in (None, "", {}))

    weather_observed = any("weather" in str(h.get("name", "")) and h.get("result") for h in history)
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

    # ── Phase 2: prompt-only Skill synthesizes a structured itinerary ──
    print("\n[Phase 2] Itinerary synthesis (single_shot)…")
    synthesis = await agent.async_run_skills_task(
        f"Design a one-day itinerary for {city} from this researched data:\n\n{observations}",
        skills=[itinerary_skill],
        mode="required",
        semantic_outputs=ITINERARY_OUTPUTS,
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
