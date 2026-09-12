"""The public example must use the actual record owner before any model call."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agently import Agently


@pytest.mark.asyncio
async def test_unified_example_stores_observation_in_record_store(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "examples/agent_auto_orchestration/22_unified_agent_execution_result.py"
    spec = importlib.util.spec_from_file_location("unified_example_storage", path)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    agent = Agently.create_agent().use_task_workspace(tmp_path / "files")
    agent.use_record_store(tmp_path / "records", mode="read_write")

    class ReachedTask(Exception):
        pass

    def stop_before_model(**kwargs):
        assert kwargs["limits"]["max_seconds"] is None
        raise ReachedTask

    monkeypatch.setattr(agent, "create_task_loop", stop_before_model)
    with pytest.raises(ReachedTask):
        await example.run_task_strategy(agent)
    records = await agent.record_store.search("renewal", filters={"kind": "account_signal"})
    assert len(records) == 1
    assert await agent.record_store.get_data(records[0]) == example.ACCOUNT_SIGNAL
