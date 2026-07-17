from __future__ import annotations

import asyncio

from agently import Agently
from agently.core.context import TaskContext
from agently.types.data import ContextBudget, ContextConsumer


async def main() -> None:
    task_context = TaskContext("blocks-context-smoke")
    task_context.put(
        role="instruction",
        content="Escalate refunds above USD 1000 to finance approval.",
        source_ref="policy/refunds",
        required=True,
    )
    reader = task_context.reader(
        consumer=ContextConsumer("blocks:refund-review"),
        phase="execution",
        budget=ContextBudget(max_chars=2000, max_blocks=8, max_block_chars=1000),
    )

    graph = Agently.blocks.compile(
        {
            "plan_id": "blocks-context-read-smoke",
            "plan_blocks": [
                {
                    "id": "policy_context",
                    "plan_block_id": "context_read",
                    "kind": "context_read",
                    "intent": "Read the refund approval policy.",
                    "bound_inputs": {
                        "operation": "read",
                        "query": "refund approval policy",
                        "explicit_refs": ["policy/refunds"],
                    },
                },
                {
                    "id": "validate_context",
                    "plan_block_id": "validation",
                    "kind": "validation",
                    "runtime_preferences": {"handler": "validate_context"},
                },
            ],
            "edges": [{"from": "policy_context", "to": "validate_context"}],
        }
    )

    async def validate_context(context):
        results = context["state"].get("execution_block_results", [])
        package = results[-1]["output"]["context_package"] if results else {}
        blocks = package.get("blocks", [])
        return {
            "ok": bool(blocks and "USD 1000" in str(blocks[0].get("content"))),
            "context_block_count": len(blocks),
        }

    execution = Agently.blocks.bind_runtime(graph).create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={
            "context_reader": reader,
            "blocks.handlers": {"validate_context": validate_context},
        },
    )
    await execution.async_start({"case": "refund-review"})
    snapshot = await execution.async_close(timeout=5)
    result = Agently.blocks.map_result(graph, snapshot)
    terminal = list(result["semantic_outputs"].values())[-1]
    print(terminal)
    assert terminal == {"ok": True, "context_block_count": 1}


if __name__ == "__main__":
    asyncio.run(main())


# `context_read` consumes one caller-bound ContextReader. It never installs or
# executes Skills and never performs file or persistence side effects.
