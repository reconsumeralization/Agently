"""Use auto_continue() to continue unfinished output, not to force long writing.

This example uses a real OpenAI-compatible model. Configure QWEN_API_KEY,
QWEN_BASE_URL, and QWEN_MODEL in the environment or a local .env file.

Expected key output (content values are model-owned):
{
  "component_count": 75,
  "first_refdes": "C001",
  "last_refdes": "C075",
  "request_count": 1,
  "accepted_unit_count": 0,
  "replayed_unit_count": 0,
  "transport_complete": true,
  "schema_complete": true,
  "validation_repair_count": 0,
  "semantic_exhaustiveness": "not_claimed"
}

Observed with local qwen3.8:27b-mlx on 2026-09-10, without a max_tokens override.
The 75 items fit in one request, so no continuation manifest was needed: zero
accepted/replayed units describes private staging, not missing business items.
Request and repair counts depend on model output and may vary. Enabling this
policy does not require continuation to occur.

With this policy enabled, a normal provider stop still requires one complete
JSON carrier. Multiple objects, trailing material and unfinished JSON fail;
they are not merged, silently cropped, or classified as length continuation.
If a necessary continuation confirms that nothing remains, a complete, normally
stopped and correctly correlated empty acknowledgement proceeds to the original
validation without adding filler. The real run recorded above did not exercise
that branch; it remains a normal-completion example, not truncation evidence.

How it works:
original ModelRequest
-> ordinary completion: validate and return (no continuation)
-> provider length terminal: TriggerFlow continuation requests
-> TaskWorkspace manifest replay and digest verification
-> original schema plus declared 75-item coverage validation
-> one AgentExecution result
"""

from __future__ import annotations

import asyncio
import json
import os

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.types.data import OutputValidateResultDict


EXPECTED_COMPONENT_COUNT = 75


def validate_component_inventory(value, _context) -> OutputValidateResultDict:
    components = value["components"]
    expected_indexes = list(range(1, EXPECTED_COMPONENT_COUNT + 1))
    expected_refdes = [f"C{index:03d}" for index in expected_indexes]
    actual_indexes = [component["index"] for component in components]
    actual_refdes = [component["refdes"] for component in components]
    return {
        "ok": (
            len(components) == EXPECTED_COMPONENT_COUNT
            and actual_indexes == expected_indexes
            and actual_refdes == expected_refdes
        ),
        "reason": (
            "The final candidate must contain exactly 75 components in index "
            "order, with refdes C001 through C075."
        ),
    }


async def main() -> None:
    load_dotenv(find_dotenv())
    api_key = os.getenv("QWEN_API_KEY")
    if not api_key:
        raise RuntimeError("QWEN_API_KEY is required for this real-model example.")

    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv(
                "QWEN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            "model": os.getenv("QWEN_MODEL", "qwen-plus"),
            "model_type": "chat",
            "auth": api_key,
            "stream_idle_timeout": 60,
            "request_retry": {"max_attempts": 2, "after_output": True},
        },
    )
    agent = Agently.create_agent()
    execution = (
        agent.create_execution(
            limits={
                "max_model_requests": 18,
                "max_seconds": 600,
            }
        )
        .input(
            {
                "component_count": EXPECTED_COMPONENT_COUNT,
                "index_range": [1, EXPECTED_COMPONENT_COUNT],
                "refdes_format": "C followed by a zero-padded three-digit index",
            }
        )
        .info(
            {
                "inventory_contract": (
                    "Return every index from 1 through 75 exactly once, in order. "
                    "The matching refdes values are C001 through C075."
                )
            }
        )
        .instruct(
            "Generate the complete synthetic component index. Do not omit, "
            "duplicate, reorder, summarize, or group an index."
        )
        .output(
            {
                "components": [
                    {
                        "index": (int, "one-based component index", True),
                        "refdes": (str, "C plus a zero-padded three-digit index", True),
                    }
                ],
                "summary": (str, "short inventory summary", True),
            },
            format="json",
        )
        .set_prompt_options({"temperature": 0.1})
        .validate(validate_component_inventory)
        .auto_continue()
    )

    async for _source, item in execution.get_async_generator(type="all"):
        if item.path in {
            "long_output.initial_committed",
            "long_output.segment_committed",
        }:
            print(
                "[long-output progress]",
                json.dumps(item.value, ensure_ascii=False),
                flush=True,
            )

    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    long_output = meta.get("long_output")
    if not isinstance(long_output, dict):
        raise RuntimeError("Long-output completion metadata is missing.")
    print(
        json.dumps(
            {
                "component_count": len(result["components"]),
                "first_refdes": result["components"][0]["refdes"],
                "last_refdes": result["components"][-1]["refdes"],
                "request_count": long_output["request_count"],
                "accepted_unit_count": long_output.get("accepted_unit_count", 0),
                "replayed_unit_count": long_output.get("replayed_unit_count", 0),
                "transport_complete": long_output["transport_complete"],
                "schema_complete": long_output["schema_complete"],
                "validation_repair_count": long_output.get("validation_repair_count", 0),
                "semantic_exhaustiveness": long_output["semantic_exhaustiveness"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
