"""Pinned AgentExecution result-reader and lifecycle usage.

Run:
    python examples/release_pinned_usage/01_agent_execution_result_lifecycle.py

Expected key output:
    quick_data_reply=attempt=1; input=first
    full_data_has_reply=True
    object_reply=attempt=1; input=first
    prompt_before_has_input=True
    prompt_after_has_input=True
    lifecycle_fail_fast=True
    fresh_execution_reply=attempt=2; input=second
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.release_pinned_usage._local_requesters import (
    PinnedUsageStructuredRequester,
    create_structured_agent,
)


def main() -> None:
    PinnedUsageStructuredRequester.reset()
    agent = create_structured_agent("release-pinned-agent-execution-result")
    agent.system("Reply with the requested structured field.", always=True)

    execution = agent.input("first").output({"reply": (str,)}, format="json")
    prompt_before = execution.get_prompt_text()
    result = execution.get_result()
    quick_data = result.get_data(ensure_keys=["reply"])
    full_data = result.get_full_data(ensure_keys=["reply"])
    data_object = result.get_data_object(ensure_keys=["reply"])
    prompt_after = execution.get_prompt_text()

    lifecycle_fail_fast = False
    try:
        execution.input("second")
    except RuntimeError as error:
        lifecycle_fail_fast = "one independent run" in str(error)

    fresh_data = (
        execution.create_execution()
        .input("second")
        .output({"reply": (str,)}, format="json")
        .start()
    )

    print(f"quick_data_reply={quick_data.get('reply')}")
    print(f"full_data_has_reply={isinstance(full_data, dict) and 'reply' in full_data}")
    print(f"object_reply={data_object.model_dump().get('reply') if data_object else None}")
    print(f"prompt_before_has_input={'first' in prompt_before}")
    print(f"prompt_after_has_input={'first' in prompt_after}")
    print(f"lifecycle_fail_fast={lifecycle_fail_fast}")
    print(f"fresh_execution_reply={fresh_data.get('reply')}")


if __name__ == "__main__":
    main()
