"""Select long_task explicitly and let the model interpret its missing contract.

Run from the repository root with local Ollama/Qwen. The external scenario is
synthetic; goal interpretation, planning, production and judgment use the model.
No Actions or external services are granted. Missing goal does not choose the
producer: create_execution("long_task") does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.agent_auto_orchestration._ollama_qwen import configure_ollama_qwen

RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "missing_goal_preparation"


async def main() -> None:
    model = configure_ollama_qwen(max_tokens=2600)
    agent = Agently.create_agent("missing-goal-preparation").use_task_workspace(RUNTIME_ROOT)
    execution = (
        agent.create_execution("long_task", limits={"max_model_requests": 10, "max_seconds": 210})
        .input({
            "request": "Recommend an approach for an internal daily CSV report; provide a decision, not implementation.",
            "constraints": ["Raw records must remain inside the company network.", "No real deployment is requested."],
            "options": {
                "hosted": "Managed cloud reporting; requires uploading raw records outside the company network.",
                "local": "Python process inside the company network; team must own packaging and scheduled execution.",
            },
        })
        .instruct("Use only the supplied facts. Explain the tradeoff and remaining operational risks without claiming tests or deployment occurred.")
        .output({
            "recommendation": (str, "One supplied option key: hosted or local.", True),
            "rationale": (str, "Explain the choice using the supplied constraints and option facts.", True),
            "risks": [(str, "Remaining risk or responsibility supported by the supplied facts.")],
        })
        .strategy("flat", max_iterations=2)
    )
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    print(f"model={model}")
    print(f"plugin={meta['plugin']}")
    print(f"status={meta['status']}")
    print(f"goal_declared={'goal' in execution.prompt_snapshot}")
    print(f"goal_preparation_source={meta['diagnostics'].get('goal_preparation', {}).get('source')}")
    print(f"effective_goal_count={len(execution.goal_items)}")
    print(f"effective_criterion_count={len(execution.success_criteria_items)}")
    print(f"result={result}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output from one local qwen3.8:27b-mlx run (2026-09-06):
# plugin=long_task
# status=success
# goal_declared=False
# goal_preparation_source=model
# effective_goal_count=2
# effective_criterion_count=4
# result.recommendation=local
# Four model requests completed in about 168 seconds, within the original
# 210-second execution budget. Goal wording/counts remain model-owned.
# Earlier qwen3.5:9b runs timed out or were blocked without an accepted final
# deliverable. This successful 27B sample is not a stability or release claim.
