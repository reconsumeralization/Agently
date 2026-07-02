from __future__ import annotations

import asyncio
from pathlib import Path

from _shared import (
    async_run_and_print,
    create_task_agent,
    enable_workspace_report_actions,
    install_local_skill,
    require_mcp_runtime,
    stream_options,
)


MCP_SERVER = Path(__file__).with_name("_mixed_business_mcp_server.py")
SKILL_DIR = Path(__file__).resolve().parent / "skills" / "market-entry-analyst"


async def main() -> None:
    require_mcp_runtime()
    agent, provider, workspace = create_task_agent(
        "agent-task-example-mixed-business",
        workspace_prefix="mixed-business",
    )
    enable_workspace_report_actions(agent)
    agent.set_action_loop(max_rounds=6)
    skill_id = install_local_skill(SKILL_DIR, registry_root=workspace / "skills_registry")
    agent.require_skills([skill_id], always=True)
    await agent.async_use_mcp(str(MCP_SERVER))

    @agent.action_func
    def get_board_question_packet() -> dict[str, object]:
        """Return host-side executive decision context for this example."""

        return {
            "source": "example_action.get_board_question_packet",
            "decision": "whether to fund a 90-day market-entry sprint",
            "segment": "mid-market healthcare operations",
            "constraints": {
                "budget_cap_usd": 180000,
                "team": "2 solutions engineers, 1 product marketer, 1 account executive",
                "must_answer": [
                    "which segment wedge is strongest",
                    "what risk could invalidate the sprint",
                    "what experiments should start first",
                ],
            },
            "current_assets": [
                "workflow automation templates",
                "SOC2-ready deployment posture",
                "limited EHR integration proof",
            ],
        }

    agent.use_actions(get_board_question_packet, always=True)

    options = stream_options()
    options["capability_evidence_requirements"] = [
        {"capability_id": skill_id, "capability_kind": "skill", "kind": "capability_used"},
        {"capability_id": "get_board_question_packet", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "crm_pipeline_snapshot", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "competitor_signal_snapshot", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "read_file", "capability_kind": "action", "kind": "action_succeeded"},
    ]
    execution = (
        agent.goal(
            "Write a board-ready market-entry memo for the mid-market healthcare operations segment. "
            "Use get_board_question_packet, the local MCP CRM and competitor signal tools, "
            "and the installed market-entry-analyst Skill guidance. "
            "Write the memo to final.md in the Workspace and return a concise final summary. "
            "Treat MCP CRM and competitor data as example business-system data, not a production export.",
            success_criteria=[
                "The memo states the board decision it supports.",
                "The memo uses get_board_question_packet evidence for constraints and required questions.",
                "The memo uses MCP CRM pipeline evidence and MCP competitive signal evidence.",
                "The memo follows the installed market-entry-analyst Skill guidance.",
                "The final report is written to final.md and clearly labels example MCP data boundaries.",
            ],
        )
        .create_execution(options=options)
    )
    await async_run_and_print(execution, provider=provider, workspace=workspace)


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output:
# real LONGCAT run on 2026-07-02 completed with accepted=true and
# execution_strategy=auto. Auto selected TaskBoard, used the native
# get_board_question_packet action, local stdio MCP crm_pipeline_snapshot and
# competitor_signal_snapshot tools, the installed market-entry-analyst Skill,
# and Workspace write_file/read_file. The run produced final.md
# (5335 bytes, sha256
# e1531a4c44bf01179ba1a61773c450d8de37706a7ad6db2feae547b82c73d838)
# with a board decision, required question answers, CRM/competitor evidence,
# assumptions, missing information, and explicit example-data boundaries.
