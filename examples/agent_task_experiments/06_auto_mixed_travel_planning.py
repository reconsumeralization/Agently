from __future__ import annotations

import asyncio
from pathlib import Path

from _shared import (
    amap_mcp_transport,
    async_run_and_print,
    create_task_agent,
    enable_workspace_report_actions,
    install_local_skill,
    require_mcp_runtime,
    sanitized_proxy_env,
    stream_options,
)


SKILL_DIR = Path(__file__).resolve().parent / "skills" / "travel-planner"


async def main() -> None:
    require_mcp_runtime()
    agent, provider, workspace = create_task_agent(
        "agent-task-example-mixed-travel",
        workspace_prefix="mixed-travel",
    )
    enable_workspace_report_actions(agent)
    agent.set_action_loop(max_rounds=6)
    skill_id = install_local_skill(SKILL_DIR, registry_root=workspace / "skills_registry")
    agent.require_skills([skill_id], always=True)

    @agent.action_func
    def get_travel_policy() -> dict[str, object]:
        """Return host-side business travel policy constraints for this example."""

        return {
            "source": "example_action.get_travel_policy",
            "traveler": "APAC partner success lead",
            "objective": "arrive rested for a Hangzhou client workshop and dinner",
            "fixed_commitments": [
                "arrive at Hangzhou East Railway Station / 杭州东站 before 12:30",
                "client workshop near Qianjiang New City / 钱江新城 starts at 14:00",
                "client dinner near West Lake Hubin business district / 西湖湖滨商圈 starts at 19:00",
            ],
            "policy": [
                "prefer rail plus short taxi over long taxi transfers",
                "keep contingency time before client-facing commitments",
                "do not present example MCP data as live booking availability",
            ],
        }

    agent.use_actions(get_travel_policy, always=True)

    options = stream_options()
    options["capability_evidence_requirements"] = [
        {"capability_id": skill_id, "capability_kind": "skill", "kind": "capability_used"},
        {"capability_id": "get_travel_policy", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "maps_geo", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "read_file", "capability_kind": "action", "kind": "action_succeeded"},
    ]

    with sanitized_proxy_env():
        await agent.async_use_mcp(amap_mcp_transport())
        execution = (
            agent.goal(
                "Create a concise Hangzhou business travel plan for the traveler. "
                "Use the host travel policy action, the installed travel-planner Skill guidance, "
                "and the real AMap MCP tools for city, weather, POI, geocode, or route facts. "
                "Write the operator-ready plan to final.md in the Workspace. "
                "The final answer should summarize the chosen plan and point to final.md.",
                success_criteria=[
                    "The plan uses get_travel_policy evidence for objective, timing, and policy constraints.",
                    "The plan uses real AMap MCP evidence for at least one Hangzhou location, weather, POI, geocode, or route fact.",
                    "The plan follows the installed travel-planner Skill guidance.",
                    "The plan separates fixed commitments, recommended choices, and a contingency.",
                    "The final report is written to final.md and does not claim live booking availability.",
                ],
            )
            .strategy("auto", options=options)
        )
        await async_run_and_print(execution, provider=provider, workspace=workspace)


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output:
# real LONGCAT run 2026-07-02:
# status completed, accepted true, execution_strategy auto;
# final.md written and read back at 3426 bytes with sha256
# c5aee9dcb92ff99a9f15dda5aced627fc5aa29f95d0c9319c732d39fb34e2287;
# delta stream shows get_travel_policy, AMap maps_geo/maps_weather, write_file,
# and read_file activity. AMap evidence includes Hangzhou East Railway Station
# 120.212600,30.290851, Qianjiang New City 120.213988,30.250397,
# Hangzhou weather 中雨/小雨, and a West Lake Hubin fallback via 杭州西湖.
