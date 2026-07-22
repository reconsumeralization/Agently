from __future__ import annotations

from _shared import create_task_agent, run_and_print, stream_options


SOURCE_PACKET = """
Week: 2026-06-24 to 2026-07-01

Engineering signals:
- [S1] Agent teams are moving from single prompt demos toward observable task
  execution, action logs, and recoverable workspace artifacts.
- [S2] Long-output reliability depends on writing artifacts to a workspace and
  verifying bounded readbacks instead of pushing every byte through final text.
- [S3] Search and browse tools should expose partial success and bounded evidence
  when concurrent sources fail.
- [S4] Code-writing agents need runtime preflight before selecting Python, Node.js,
  Go, or C++.
- [S5] Event-driven frontier scheduling can reduce wait time when independent
  branches finish at different times.
"""


def main() -> None:
    agent, provider, workspace = create_task_agent(
        "agent-task-example-engineering-weekly",
        workspace_prefix="agent-engineering-weekly",
    )
    execution = agent.create_task(
        goal=(
            "Write a short Agent engineering weekly for software developers. "
            "Use the source packet as the only evidence. Group the signals by theme, "
            "explain why each theme matters to an engineering team, and end with three "
            "practical next actions.\n\n"
            f"{SOURCE_PACKET}"
        ),
        success_criteria=[
            "The weekly states the covered date window.",
            "The weekly groups 5 source signals by useful engineering themes.",
            "Each implication is grounded in a source id from the packet.",
            "The next actions are practical for developers building agents.",
        ],
        options=stream_options(),
    )
    run_and_print(execution, provider=provider, task_workspace=workspace)


if __name__ == "__main__":
    main()

# Expected key output:
# prints a [DELTA_STREAM] section from get_async_generator(type="delta");
# status is completed, accepted is true, execution_strategy is selected by
# AgentTask auto, and the delta stream includes the covered week plus source ids
# such as [S2], [S3], [S4], or [S5].
