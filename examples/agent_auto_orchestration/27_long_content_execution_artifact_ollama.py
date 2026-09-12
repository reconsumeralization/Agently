"""Built-in long-content Execution with verified artifact delivery.

Run:
    python examples/agent_auto_orchestration/27_long_content_execution_artifact_ollama.py

Environment:
    Local Ollama at OLLAMA_BASE_URL (default http://127.0.0.1:11434/v1).
    AGENT_EXECUTION_OLLAMA_MODEL or OLLAMA_DEFAULT_MODEL (default qwen).

The Execution plans once, writes each chapter, and summarizes each non-final
chapter once for its successors. Continuation is conditional, not a fixed node.
The host assembles the Markdown in plan order, then TaskWorkspace writes and
physically reads back the declared artifact before the advisory review runs.
"""

from __future__ import annotations

import asyncio
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from examples.agent_auto_orchestration._ollama_qwen import (  # noqa: E402
    configure_ollama_qwen,
)

RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "long_content_execution_artifact"

SOURCE_FACTS = {
    "current_state": "three services each parse customer locale independently",
    "target_state": "one versioned locale-normalization library used by all services",
    "constraints": [
        "migration must be reversible for seven days",
        "no customer identifier may appear in validation logs",
        "the billing service cannot deploy on Fridays",
    ],
    "acceptance_evidence": [
        "contract tests pass for the 12 supported locales",
        "shadow comparison reports zero normalization differences for 24 hours",
        "rollback rehearsal restores the previous parser within 15 minutes",
    ],
}


async def main() -> None:
    model = configure_ollama_qwen(max_tokens=None)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    runtime_root = Path(tempfile.mkdtemp(prefix="run-", dir=RUNTIME_ROOT))

    agent = Agently.create_agent("long-content-execution-artifact-ollama").use_task_workspace(
        runtime_root, mode="read_write"
    )
    agent.set_settings("plugins.AgentExecution.long_content.max_sections", 3)
    agent.set_settings("debug", True)
    execution = (
        agent.create_execution("long_content").input({"migration_facts": SOURCE_FACTS})
        .instruct(
            "Write a concise migration runbook with exactly three complementary sections: "
            "scope and invariants, rollout and rollback, and acceptance evidence. Use only the "
            "supplied facts; label any necessary assumption and do not invent observed results."
        )
        .artifact("reports/locale-migration-runbook.md")
        .review()
    )

    document = await execution.async_get_data()
    meta = await execution.async_get_meta()
    execution_run = meta["diagnostics"].get("execution_run")
    if execution_run is None:
        raise RuntimeError("Expected long-content Execution diagnostics.")
    artifact_ref = meta["logs"]["artifact_refs"][0]
    artifact_text = (runtime_root / artifact_ref["path"]).read_text(encoding="utf-8")

    print(f"model={model}")
    print(f"execution_name={meta['plugin']}")
    print(f"execution_status={meta['status']}")
    print(f"section_count={execution_run['section_count']}")
    print(f"model_request_count={execution_run['model_request_count']}")
    print(f"assembly={execution_run['assembly']}")
    print(f"artifact_path={artifact_ref['path']}")
    print(f"runtime_root={runtime_root}")
    print(f"artifact_readback_matches={artifact_text == document}")
    reviews = meta.get("reviews", [])
    if len(reviews) != 1:
        raise RuntimeError("Expected one model review.")
    print(f"review_source={reviews[0]['source']}")
    print(f"review_passed={reviews[0]['passed']}")


if __name__ == "__main__":
    asyncio.run(main())


# Expected key output, local qwen3.8:27b-mlx run (2026-09-10):
# model=qwen3.8:27b-mlx
# execution_name=long_content
# execution_status=success
# section_count=3
# model_request_count=6
# assembly=host_ordered
# artifact_path=reports/locale-migration-runbook.md
# artifact_readback_matches=True
# review_source=model
# review_passed=True
#
# Document prose remains model-owned. Section count, host ordering, verified
# artifact readback, and review source are the stable framework evidence.

# The six production requests exclude the separately configured review request;
# seven provider requests were observed in total, without natural truncation.
# Model review passed, but direct inspection still found unsupported operational
# requirements. Advisory review success is not final semantic acceptance.
