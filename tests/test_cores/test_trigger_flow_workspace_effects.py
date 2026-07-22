# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

import pytest

from agently import TaskWorkspace, TriggerFlow, TriggerFlowRuntimeData
from agently.core.storage import RecordStore


def _tables(root: Path) -> set[str]:
    database = root / ".agently" / "records" / "records.db"
    if not database.exists():
        return set()
    with sqlite3.connect(database) as conn:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(row[0]).startswith("sqlite_")
        }


def test_triggerflow_default_record_store_is_lazy_and_pure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_dir = tmp_path / "application"
    monkeypatch.setattr(
        sys.modules["__main__"],
        "__file__",
        str(entry_dir / "main.py"),
        raising=False,
    )

    execution = TriggerFlow(name="direct-default-record_store").create_execution()
    record_store = execution.require_runtime_resource("record_store")

    assert isinstance(record_store, RecordStore)
    assert record_store.root == entry_dir.resolve()
    assert record_store.mode == "read_only"
    assert record_store.execution_id == execution.id
    assert not hasattr(record_store, "files_root")
    assert not entry_dir.exists()


@pytest.mark.asyncio
async def test_finite_triggerflow_task_workspace_read_creates_zero_private_state(
    tmp_path: Path,
) -> None:
    (tmp_path / "input.txt").write_text("direct project input", encoding="utf-8")
    task_workspace = TaskWorkspace(tmp_path)
    flow = TriggerFlow(name="finite-record_store-read")

    async def read_input(data: TriggerFlowRuntimeData) -> None:
        bound = data.require_resource("task_workspace")
        read = await bound.read_file("input.txt")
        await data.async_set_state("body", read["content"])

    flow.to(read_input)
    execution = flow.create_execution(
        auto_close=True,
        auto_close_timeout=0,
        runtime_resources={"task_workspace": task_workspace},
        record_store=False,
    )

    result = await execution.async_start(None)

    assert result["body"] == "direct project input"
    assert execution._snapshot_store is None
    assert execution._runtime_event_store is None
    assert not (tmp_path / ".agently").exists()


@pytest.mark.asyncio
async def test_finite_triggerflow_leaves_task_workspace_retention_to_its_owner(
    tmp_path: Path,
) -> None:
    task_workspace = TaskWorkspace(tmp_path, mode="read_write")
    flow = TriggerFlow(name="finite-record_store-product")

    async def produce(data: TriggerFlowRuntimeData) -> None:
        bound = data.require_resource("task_workspace")
        await bound.write_file("working/draft.md", "discard")
        final = await bound.write_file("deliverables/final.md", "retain")
        await data.async_set_state("product", final.to_dict())

    flow.to(produce)
    execution = flow.create_execution(
        auto_close=True,
        auto_close_timeout=0,
        runtime_resources={"task_workspace": task_workspace},
        record_store=False,
    )

    result = await execution.async_start(None)

    product = result["product"]
    assert (tmp_path / product["path"]).read_text(encoding="utf-8") == "retain"
    assert (tmp_path / "working" / "draft.md").read_text(encoding="utf-8") == "discard"
    assert not (tmp_path / ".agently" / "records.db").exists()


@pytest.mark.asyncio
async def test_triggerflow_record_store_audit_is_persisted_only_when_explicitly_bound(
    tmp_path: Path,
) -> None:
    record_store = RecordStore(tmp_path, mode="read_write")
    flow = TriggerFlow(name="explicit-record_store-audit")

    async def remember(data: TriggerFlowRuntimeData) -> None:
        await data.async_set_state("value", "done")

    flow.to(remember)
    execution = flow.create_execution(
        auto_close=True,
        auto_close_timeout=0,
        runtime_resources={
            "record_store": record_store,
            "runtime_event_store": record_store,
        },
    )

    result = await execution.async_start(None)
    events = await record_store.query_runtime_events(execution.id)

    assert result["value"] == "done"
    assert events
    assert _tables(tmp_path) == {"runtime_events"}


@pytest.mark.asyncio
async def test_triggerflow_pause_uses_record_store_snapshot_without_enabling_audit(
    tmp_path: Path,
) -> None:
    record_store = RecordStore(tmp_path, mode="read_write")
    flow = TriggerFlow(name="record_store-recovery-only")

    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    flow.to(pause)
    execution = flow.create_execution(
        auto_close=False,
        runtime_resources={"record_store": record_store},
    )

    await execution.async_start("draft")

    assert execution.get_status() == "waiting"
    assert execution._snapshot_store is execution.require_runtime_resource("record_store")
    assert execution._runtime_event_store is None
    assert await record_store.latest_snapshot(execution.run_context.run_id) is not None
    assert _tables(tmp_path) == {"records", "checkpoints", "manifests"}
    await execution.async_close(pending_interrupts="cancel")
    assert await record_store.latest_snapshot(execution.run_context.run_id) is None
    assert not (tmp_path / ".agently" / "records" / "records.db").exists()
    private_files = {
        path.relative_to(tmp_path).as_posix()
        for path in (tmp_path / ".agently").rglob("*")
        if path.is_file()
    }
    assert private_files == {
        ".agently/records/identity/state.json",
        ".agently/records/identity/state.lock",
    }
