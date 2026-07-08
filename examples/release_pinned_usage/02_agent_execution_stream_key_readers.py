"""Pinned AgentExecution stream and key-reader usage.

Run:
    python examples/release_pinned_usage/02_agent_execution_stream_key_readers.py

Expected key output:
    key_result=attempt=1; input=key
    wait_keys=[('reply', 'attempt=2; input=wait')]
    handler_result=[('reply', 'attempt=3; input=handler', 'ATTEMPT=3; INPUT=HANDLER')]
    specific_events=['reasoning_delta', 'delta', 'tool_calls', 'done']
    streaming_print_contains_answer=True
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.release_pinned_usage._local_requesters import (
    PinnedUsageStructuredRequester,
    create_specific_stream_agent,
    create_structured_agent,
)


def main() -> None:
    PinnedUsageStructuredRequester.reset()
    agent = create_structured_agent("release-pinned-agent-execution-key-readers")

    key_result = agent.input("key").output({"reply": (str,)}, format="json").get_key_result("reply")
    wait_keys = list(agent.input("wait").output({"reply": (str,)}, format="json").wait_keys(["reply"]))
    handler_result = (
        agent.input("handler")
        .output({"reply": (str,)}, format="json")
        .when_key("reply", lambda value: str(value).upper())
        .start_waiter()
    )

    stream_agent = create_specific_stream_agent("release-pinned-agent-execution-stream")
    specific_events = [
        event
        for event, _data in stream_agent.input("stream").get_generator(type="specific")
    ]
    stream_buffer = io.StringIO()
    with contextlib.redirect_stdout(stream_buffer):
        stream_agent.input("streaming_print").streaming_print()

    print(f"key_result={key_result}")
    print(f"wait_keys={wait_keys}")
    print(f"handler_result={handler_result}")
    print(f"specific_events={specific_events}")
    print(f"streaming_print_contains_answer={'answer' in stream_buffer.getvalue()}")


if __name__ == "__main__":
    main()
