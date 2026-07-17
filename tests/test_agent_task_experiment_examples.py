from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from examples.agent_task_experiments._shared import async_run_and_print


@pytest.mark.asyncio
async def test_shared_example_summary_uses_terminal_envelope_for_task_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    terminal_result = {
        "status": "completed",
        "accepted": True,
        "artifact_status": "degraded",
        "missing_criteria": [],
        "execution_strategy": "taskboard",
        "final_result": "TaskWorkspace artifact delivered at final.md",
    }

    class FakeResult:
        async def async_get_data(self) -> str:
            return "TaskWorkspace artifact delivered at final.md"

        async def async_get_full_data(self) -> dict[str, Any]:
            return terminal_result

        async def async_get_meta(self) -> dict[str, Any]:
            return {"task_refs": {}, "logs": {"route_logs": {}}}

    class FakeExecution:
        async def get_async_generator(
            self,
            *,
            type: str,
        ) -> AsyncGenerator[str, None]:
            assert type == "delta"
            if False:
                yield ""

        def get_result(self) -> FakeResult:
            return FakeResult()

    summary = await async_run_and_print(
        FakeExecution(),
        provider="deepseek",
        task_workspace=tmp_path,
    )

    assert summary["status"] == "completed"
    assert summary["accepted"] is True
    assert summary["artifact_status"] == "degraded"
    assert summary["missing_criteria"] == []
    assert summary["execution_strategy"] == "taskboard"
    assert summary["final_preview"] == "TaskWorkspace artifact delivered at final.md"
    assert '"status": "completed"' in capsys.readouterr().out
