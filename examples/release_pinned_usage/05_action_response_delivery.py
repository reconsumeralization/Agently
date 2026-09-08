"""Pinned ActionLoop response-delivery and fluent-chain behavior.

Run:
    python examples/release_pinned_usage/05_action_response_delivery.py

Expected key output:
    result=The sum is 5.
    model_request_count=2
    fluent_chain_same_execution=True
    info_present_in_each_round=True
    action_result_present_in_second_round=True
    simple_action_decision_hidden=True
    simple_response_once=True
"""

from __future__ import annotations

import json
import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.release_pinned_usage._local_requesters import (  # noqa: E402
    PinnedUsageActionResponseRequester,
    create_action_response_agent,
)


def main() -> None:
    PinnedUsageActionResponseRequester.reset(
        [
            json.dumps(
                {
                    "next_action": "execute",
                    "execution_commands": [
                        {
                            "purpose": "Add the values",
                            "action_id": "add",
                            "action_input": {"a": 2, "b": 3},
                            "todo_suggestion": "Return the observed result.",
                        }
                    ],
                    "response": None,
                }
            ),
            json.dumps(
                {
                    "next_action": "response",
                    "execution_commands": [],
                    "response": "The sum is 5.",
                }
            ),
        ]
    )
    agent = create_action_response_agent("release-pinned-action-response")
    agent.set_settings("debug", True)

    @agent.action_func
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    input_execution = agent.input("Add 2 and 3 with the available Action.")
    info_execution = input_execution.info("RELEASE_PINNED_ACTION_INFO")
    action_execution = info_execution.use_action(add)
    console_output = io.StringIO()
    with redirect_stdout(console_output):
        result = action_execution.start()
    rendered_console = re.sub(r"\x1b\[[0-9;]*m", "", console_output.getvalue())

    prompts = PinnedUsageActionResponseRequester.prompt_texts
    print(f"result={result}")
    print(f"model_request_count={PinnedUsageActionResponseRequester.request_count}")
    print(
        "fluent_chain_same_execution="
        f"{input_execution is info_execution is action_execution}"
    )
    print(
        "info_present_in_each_round="
        f"{len(prompts) == 2 and all('RELEASE_PINNED_ACTION_INFO' in prompt for prompt in prompts)}"
    )
    print(
        "action_result_present_in_second_round="
        f"{len(prompts) == 2 and '5' in prompts[1]}"
    )
    print(f"simple_action_decision_hidden={'next_action' not in rendered_console}")
    print(f"simple_response_once={rendered_console.count('The sum is 5.') == 1}")


if __name__ == "__main__":
    main()
