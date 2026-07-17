from __future__ import annotations

from _shared import create_task_agent, run_and_print, stream_options


SOURCE_PACKET = """
Run date: 2026-07-02

Source facts:
- [Q1] NVDA, AMD, and AVGO are the covered semiconductor tickers.
- [Q2] The desk has only delayed quote snapshots in this packet, so the brief must not
  claim live market prices.
- [N1] AI accelerator demand remains the strongest common growth theme.
- [N2] Supply concentration, export controls, customer capex timing, and valuation
  sensitivity are common downside risks.
- [N3] Broadcom has a more diversified mix across custom silicon, networking, and
  infrastructure software than NVDA or AMD in this packet.
- [P1] Compliance policy: do not issue buy/sell/hold calls, target prices, or
  personalized investment advice.
"""


def main() -> None:
    agent, provider, workspace = create_task_agent(
        "agent-task-example-stock-risk",
        workspace_prefix="stock-risk",
    )
    execution = agent.create_task(
        goal=(
            "Prepare a concise semiconductor stock risk brief for a portfolio meeting. "
            "Use only the source packet below as evidence. Separate facts, interpretation, "
            "watchpoints, and non-investment-advice boundary.\n\n"
            f"{SOURCE_PACKET}"
        ),
        success_criteria=[
            "The brief covers NVDA, AMD, and AVGO.",
            "The brief separates observed source facts from interpretation.",
            "The brief includes risks, watchpoints, and a non-investment-advice boundary.",
            "Every material claim cites the packet ids such as [Q1] or [N2].",
        ],
        options=stream_options(),
    )
    run_and_print(execution, provider=provider, task_workspace=workspace)


if __name__ == "__main__":
    main()

# Expected key output:
# prints a [DELTA_STREAM] section from get_async_generator(type="delta");
# provider is longcat/deepseek/ollama, status is completed, accepted is true,
# execution_strategy is selected by AgentTask auto, and the delta stream cites
# packet ids such as [Q1], [N1], [N2], and [P1].
