from __future__ import annotations

from _shared import create_task_agent, run_and_print, stream_options


SOURCE_PACKET = """
Repository snapshot:
- [R1] Repo name: sample-agent-runtime
- [R2] README states the project wraps model calls, tools, and workspace records
  into one observable task execution.
- [R3] File tree: README.md, pyproject.toml, sample_runtime/agent.py,
  sample_runtime/actions.py, sample_runtime/workspace.py, examples/support_triage.py.
- [R4] sample_runtime/agent.py exposes create_task(goal, success_criteria) and
  returns an execution object.
- [R5] sample_runtime/actions.py registers search_docs and update_ticket actions
  with structured action logs.
- [R6] sample_runtime/workspace.py persists artifacts and readback snippets for
  later verification.
- [R7] The snapshot does not include tests, CI config, or packaging metadata beyond
  pyproject.toml.
"""


def main() -> None:
    agent, provider, workspace = create_task_agent(
        "agent-task-example-repo-reading",
        workspace_prefix="repo-reading",
    )
    execution = agent.create_task(
        goal=(
            "Produce a source-grounded repository reading note for developers. "
            "Use only the repository snapshot below. Explain purpose, architecture, "
            "interesting implementation points, likely use cases, and limitations. "
            "Distinguish direct evidence from inference.\n\n"
            f"{SOURCE_PACKET}"
        ),
        success_criteria=[
            "The note names the repo purpose and core modules.",
            "Direct claims cite source ids from the packet.",
            "Inferences are labeled as inferences.",
            "Limitations include missing tests or CI when relevant.",
        ],
        options=stream_options(),
    )
    run_and_print(execution, provider=provider, workspace=workspace)


if __name__ == "__main__":
    main()

# Expected key output:
# prints a [DELTA_STREAM] section from get_async_generator(type="delta");
# status is completed, accepted is true, execution_strategy is selected by
# AgentTask auto, and the delta stream describes purpose, modules, use cases, and
# limitations with citations such as [R2], [R4], [R5], and [R7].
