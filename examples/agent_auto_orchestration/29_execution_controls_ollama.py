"""A paused execution handed off through a data-only snapshot to a fresh handle.

Run with local Ollama; AGENT_EXECUTION_OLLAMA_MODEL selects the model.
Two logical model requests, no automatic retry, 120-second cumulative deadline.

Flow: draft -> safe pause -> JSON snapshot -> explicit resource rebinding ->
      resume -> real model summary -> rework in the same execution -> retained revisions -> close.
This demonstrates execution lifecycle, not long-task rework or provider checkpointing.
Observed with qwen3.8:27b-mlx: 0 calls before pause, 2 calls cumulatively,
revision 1, restored identity true, and retained original summary readable.
Revised summary: "Production deployment remains pending operator approval,
though the data import has passed staging validation."
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from agently.core.application.AgentExecution import AgentExecutionPaused  # noqa: E402
from examples.agent_auto_orchestration._ollama_qwen import configure_ollama_qwen  # noqa: E402


async def main() -> None:
    model = configure_ollama_qwen(max_tokens=600)
    with TemporaryDirectory(prefix="agently-execution-controls-") as workspace:
        agent = Agently.create_agent("execution-controls").use_task_workspace(workspace, mode="read_write")
        original = (
            agent.create_execution("request", limits={"max_model_requests": 2, "max_seconds": 120})
            .input({"notes": ["The data import passed staging validation.",
                              "Production deployment is waiting for the operator's approval."]})
            .instruct("Summarize the supplied status in one sentence. Do not imply production deployment occurred.")
        )
        await original.async_pause()
        try:
            await original.async_run(max_retries=0)
        except AgentExecutionPaused:
            snapshot = json.loads(json.dumps(original.save()))
        else:
            raise AssertionError("The requested pre-production pause was not reached.")
        restored = (
            agent.create_execution("request", limits={"max_model_requests": 2, "max_seconds": 120})
            .input({"notes": ["The data import passed staging validation.",
                              "Production deployment is waiting for the operator's approval."]})
            .instruct("Summarize the supplied status in one sentence. Do not imply production deployment occurred.")
        )
        restored.load(snapshot)
        assert restored.id == original.id
        # Transfer ownership: retire the old in-memory continuation before running the new one.
        await original.async_cancel(reason="handoff")
        previous = restored.get_result()
        result = await restored.async_resume()
        assert await restored.async_get_data() == result
        revised = await restored.async_rework("Keep one sentence, but lead with the pending production approval.", max_reworks=1)
        assert restored.revision == 1 and previous.revision == 0
        assert await previous.async_get_data() == result
        await restored.async_close()
        assert await restored.async_get_data() == revised
        print(json.dumps({"model": model, "paused_before_calls": snapshot["model_requests_used"],
                          "restored_identity": restored.id == original.id,
                          "model_calls": restored.execution_context.model_request_count,
                          "closed": True, "revision": restored.revision, "summary": result, "revised_summary": revised}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
