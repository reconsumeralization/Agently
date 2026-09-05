"""Built-in beta long-content Pattern with verified artifact delivery.

Run:
    python examples/agent_auto_orchestration/27_long_content_pattern_artifact_ollama.py

Environment:
    Local Ollama at OLLAMA_BASE_URL (default http://127.0.0.1:11434/v1).
    AGENT_PATTERN_OLLAMA_MODEL or OLLAMA_DEFAULT_MODEL (default qwen3.5:9b).

The Pattern owns one section-plan request plus one request per planned section.
The host assembles the Markdown in plan order, then TaskWorkspace writes and
physically reads back the declared artifact before the advisory review runs.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from examples.agent_auto_orchestration._ollama_qwen import (  # noqa: E402
    configure_ollama_qwen,
)

RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "long_content_pattern_artifact"

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
    model = configure_ollama_qwen(max_tokens=2400)
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)

    agent = Agently.create_agent("long-content-pattern-artifact-ollama").use_task_workspace(
        RUNTIME_ROOT, mode="read_write"
    )
    agent.set_settings("plugins.AgentPattern.long_content.max_sections", 3)
    agent.set_settings("plugins.AgentPattern.long_content.continuity_chars", 800)
    agent.set_settings("debug", True)
    execution = (
        agent.input({"migration_facts": SOURCE_FACTS})
        .instruct(
            "Write a concise migration runbook with exactly three complementary sections: "
            "scope and invariants, rollout and rollback, and acceptance evidence. Use only the "
            "supplied facts; label any necessary assumption and do not invent observed results."
        )
        .pattern("long_content")
        .artifact("reports/locale-migration-runbook.md")
        .review()
    )

    document = await execution.async_get_data()
    meta = await execution.async_get_meta()
    pattern_run = meta["diagnostics"].get("pattern_run")
    if pattern_run is None:
        raise RuntimeError("Expected long-content Pattern diagnostics.")
    artifact_ref = meta["logs"]["artifact_refs"][0]
    artifact_text = (RUNTIME_ROOT / artifact_ref["path"]).read_text(encoding="utf-8")

    print(f"model={model}")
    print(f"pattern_name={meta['pattern']['name']}")
    print(f"pattern_status={meta['pattern']['status']}")
    print(f"section_count={pattern_run['section_count']}")
    print(f"model_request_count={pattern_run['model_request_count']}")
    print(f"assembly={pattern_run['assembly']}")
    print(f"artifact_path={artifact_ref['path']}")
    print(f"artifact_readback_matches={artifact_text == document}")
    reviews = meta.get("reviews", [])
    if len(reviews) != 1:
        raise RuntimeError("Expected one model review.")
    print(f"review_source={reviews[0]['source']}")
    print(f"review_passed={reviews[0]['passed']}")


if __name__ == "__main__":
    asyncio.run(main())


# Expected key output from one real local qwen3.5:9b run on 2026-09-05:
# model=qwen3.5:9b
# pattern_name=long_content
# pattern_status=completed
# section_count=3
# model_request_count=4
# assembly=host_ordered
# artifact_path=reports/locale-migration-runbook.md
# artifact_readback_matches=True
# review_source=model
# review_passed=True
#
# Document prose remains model-owned. Section count, host ordering, verified
# artifact readback, and review source are the stable framework evidence.
