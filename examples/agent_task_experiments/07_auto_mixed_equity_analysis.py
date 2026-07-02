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
SKILL_DIR = Path(__file__).resolve().parent / "skills" / "equity-risk-reviewer"


async def main() -> None:
    require_mcp_runtime()
    agent, provider, workspace = create_task_agent(
        "agent-task-example-mixed-equity",
        workspace_prefix="mixed-equity",
    )
    enable_workspace_report_actions(agent)
    agent.set_action_loop(max_rounds=6)
    skill_id = install_local_skill(SKILL_DIR, registry_root=workspace / "skills_registry")
    agent.require_skills([skill_id], always=True)
    await agent.async_use_mcp(str(MCP_SERVER))

    @agent.action_func
    def get_portfolio_mandate() -> dict[str, object]:
        """Return host-side portfolio mandate facts for this example."""

        return {
            "source": "example_action.get_portfolio_mandate",
            "audience": "weekly risk meeting",
            "coverage": ["NVDA", "AMD", "AVGO"],
            "mandate": {
                "horizon": "next 30-60 days",
                "risk_limit": "do not recommend trade actions or personalized advice",
                "focus": "concentration, catalyst, and downside-watchpoint framing",
            },
            "current_exposure": {
                "NVDA": "largest single-name semiconductor exposure",
                "AMD": "medium exposure with execution sensitivity",
                "AVGO": "diversifier across custom silicon and infrastructure software",
            },
        }

    agent.use_actions(get_portfolio_mandate, always=True)

    options = stream_options()
    options["capability_evidence_requirements"] = [
        {"capability_id": skill_id, "capability_kind": "skill", "kind": "capability_used"},
        {"capability_id": "get_portfolio_mandate", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "equity_market_snapshot", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "equity_news_digest", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "read_file", "capability_kind": "action", "kind": "action_succeeded"},
    ]
    execution = (
        agent.goal(
            "Prepare a portfolio-facing semiconductor risk brief. "
            "Use get_portfolio_mandate, the local MCP equity market and news digest tools, "
            "and the installed equity-risk-reviewer Skill guidance. "
            "Write the brief to final.md in the Workspace and return a compact final summary. "
            "Treat MCP market data as example non-live data, not investment advice.",
            success_criteria=[
                "The brief covers NVDA, AMD, and AVGO.",
                "The brief uses get_portfolio_mandate evidence for audience, mandate, and exposure context.",
                "The brief uses MCP market snapshot and news digest evidence for each covered ticker.",
                "The brief follows the installed equity-risk-reviewer Skill guidance.",
                "The final report is written to final.md and includes a non-investment-advice boundary.",
            ],
        )
        .create_execution(options=options)
    )
    await async_run_and_print(execution, provider=provider, workspace=workspace)


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output:
# real LONGCAT run 2026-07-02:
# status completed, accepted true, execution_strategy auto;
# final.md written and read back at 2564 bytes with sha256
# 5634dec1c4d47e72f1e9618215e99e2498e6ab2748885ea68f3f0b72e85f7929;
# delta stream shows get_portfolio_mandate, equity_market_snapshot,
# equity_news_digest, write_file, and read_file activity. The final brief covers
# NVDA, AMD, and AVGO, includes market facts and news digest sections, and
# states that the example MCP data is not live data or investment advice.
