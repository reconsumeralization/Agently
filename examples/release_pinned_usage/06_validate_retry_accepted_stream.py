"""Pinned validation-retry and accepted instant-stream reconciliation usage.

Run:
    python examples/release_pinned_usage/06_validate_retry_accepted_stream.py

Expected key output:
    model_request_first_status=attempt=1; input=retry-stream
    model_request_final_status=attempt=2; input=retry-stream
    model_request_reopened_status=attempt=2; input=retry-stream
    agent_execution_statuses=['attempt=1; input=retry-stream', 'attempt=2; input=retry-stream']
    agent_execution_attempt_indexes=[1, 2]

This is an infrastructure probe, not a model-quality example. The scripted
requester deliberately rejects its first structured result so the public stream
consumer can reconcile provisional UI state with the accepted retry attempt.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently.types.data import OutputValidateResult  # noqa: E402
from examples.release_pinned_usage._local_requesters import (  # noqa: E402
    PinnedUsageStructuredRequester,
    create_structured_agent,
)


def accept_second_attempt(
    result: dict[str, Any],
    _context: object,
) -> OutputValidateResult:
    if str(result.get("status", "")).startswith("attempt=2;"):
        return True
    return {
        "ok": False,
        "reason": "The accepted status must come from the declared second attempt.",
    }


def completed_status_values(items: list[Any]) -> list[str]:
    return [
        str(item.value)
        for item in items
        if item.path == "status" and item.is_complete
    ]


async def model_request_probe() -> tuple[str, str, str]:
    PinnedUsageStructuredRequester.reset()
    agent = create_structured_agent("release-pinned-retry-model-request")
    result = (
        agent.create_request(name="release-pinned-retry-request")
        .input("retry-stream")
        .instruct(
            "Return the declared status. The validation rule requires the accepted replacement "
            "attempt to satisfy the same structured contract."
        )
        .output({"status": (str, "Structured UI status.", True)}, format="json")
        .validate(accept_second_attempt)
        .get_result()
    )

    first_items = [item async for item in result.get_async_generator(type="instant")]
    final_data = await result.async_get_data(max_retries=1)
    reopened_items = [
        item async for item in result.get_async_generator(type="instant")
    ]
    return (
        completed_status_values(first_items)[-1],
        str(final_data["status"]),
        completed_status_values(reopened_items)[-1],
    )


async def agent_execution_probe() -> tuple[list[str], list[int | None]]:
    PinnedUsageStructuredRequester.reset()
    agent = create_structured_agent("release-pinned-retry-agent-execution")
    execution = (
        agent.input("retry-stream")
        .instruct(
            "Return the declared status. The validation rule requires the accepted replacement "
            "attempt to satisfy the same structured contract."
        )
        .output({"status": (str, "Structured UI status.", True)}, format="json")
        .validate(accept_second_attempt)
    )

    items = [item async for item in execution.get_async_generator(type="instant")]
    await execution.async_get_data(max_retries=1)
    completed = [
        item for item in items if item.path == "status" and item.is_complete
    ]
    return (
        [str(item.value) for item in completed],
        [item.meta.get("attempt_index") if item.meta else None for item in completed],
    )


async def main() -> None:
    first, final, reopened = await model_request_probe()
    statuses, attempt_indexes = await agent_execution_probe()

    print(f"model_request_first_status={first}")
    print(f"model_request_final_status={final}")
    print(f"model_request_reopened_status={reopened}")
    print(f"agent_execution_statuses={statuses}")
    print(f"agent_execution_attempt_indexes={attempt_indexes}")


if __name__ == "__main__":
    asyncio.run(main())
