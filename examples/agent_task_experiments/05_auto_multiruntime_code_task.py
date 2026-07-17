from __future__ import annotations

from _shared import create_task_agent, enable_coding_workspace, run_and_print, stream_options
from agently.builtins.actions import RuntimePreflight


ORDER_PACKET = """
Orders:
- O-1001 | customer=Ada | gross=120.00 | discount=10.00 | shipping=5.00 | tax_rate=0.0825 | paid=124.08
- O-1002 | customer=Ben | gross=75.50 | discount=0.00 | shipping=0.00 | tax_rate=0.0825 | paid=81.73
- O-1005 | customer=Eli | gross=99.99 | discount=20.00 | shipping=4.99 | tax_rate=0.0825 | paid=91.61

Refunds:
- R-9001 | order_id=O-1002 | amount=20.00 | reason=damaged
- R-9002 | order_id=O-1005 | amount=10.00 | reason=coupon_error

Rules:
- expected_total = round((gross - discount) * (1 + tax_rate) + shipping, 2)
- net_revenue = expected_total - refunds for that order
- A payment mismatch is material when abs(paid - expected_total) > 0.02
"""


def main() -> None:
    agent, provider, workspace = create_task_agent(
        "agent-task-example-multiruntime-code",
        workspace_prefix="multiruntime-code",
    )
    enable_coding_workspace(agent)
    runtime_preflight = RuntimePreflight().inspect(include_unavailable=True)
    selected_runtime_id = runtime_preflight["selected_runtime_hint"]
    selected_runtime = next(
        candidate for candidate in runtime_preflight["candidates"] if candidate["runtime_id"] == selected_runtime_id
    )
    selected_language = selected_runtime["language"]
    source_file = selected_runtime["source_file"]
    run_command = selected_runtime["run_commands"][0]
    options = stream_options()
    options["routes"] = {"model_request": {"action_loop": {"max_rounds": 4}}}
    options["capability_evidence_requirements"] = [
        {"capability_id": "write_file", "capability_kind": "action", "kind": "action_succeeded"},
        {"capability_id": "run_bash", "capability_kind": "action", "kind": "action_succeeded"},
    ]
    execution = agent.create_task(
        goal=(
            "Write and run a tiny stdlib-only reconciliation program in the TaskWorkspace. "
            "The framework runtime preflight has already inspected Python, Node.js, Go, and C++ before this task. "
            f"Use the selected runtime facts: runtime_id={selected_runtime_id}, language={selected_language}, "
            f"source_file={source_file}, run_command={run_command}. "
            f"Create {source_file} with write_file, without reading it first because this run starts with no source file. "
            f"Execute exactly `{run_command}` with run_bash. "
            "Deliver the compact final report to final.md as a TaskWorkspace artifact. "
            "The final report must include the selected runtime, implementation file path, run command, output summary, payment mismatches, refund impact, and validation notes. "
            "Do not install runtimes, compilers, package managers, or third-party packages. "
            "Do not run runtime version checks or environment installation commands.\n\n"
            f"Runtime preflight result:\n{runtime_preflight}\n\n"
            f"{ORDER_PACKET}"
        ),
        success_criteria=[
            "The generated program uses the preflight-selected runtime, source file, and run command.",
            "The reconciliation logic is written to a TaskWorkspace source file with write_file.",
            "The generated program is executed successfully with run_bash.",
            "The final report is written to final.md in the TaskWorkspace.",
            "The final report cites the selected runtime, implementation file path, and command output.",
            "The reported totals are consistent with executed program output.",
        ],
        options=options,
    )
    run_and_print(execution, provider=provider, task_workspace=workspace)


if __name__ == "__main__":
    main()

# Expected key output:
# prints a [DELTA_STREAM] section from get_async_generator(type="delta");
# status is completed, accepted is true, execution_strategy is selected by
# AgentTask auto, and the delta stream shows the TaskWorkspace source file, run
# command, payment mismatch findings, refund impact, and final.md delivery.
