from __future__ import annotations

import asyncio
from tempfile import TemporaryDirectory

from agently import TaskWorkspace, TriggerFlow
from agently.core.context import TaskContext
from agently.core.storage import RecordStore, RecordStoreContextSource
from agently.types.data import ContextReadIntent


def build_flow(record_store: RecordStore, task_workspace: TaskWorkspace) -> TriggerFlow:
    flow = TriggerFlow(name="task-context-loop-foundation")

    async def start(data):
        data.emit_nowait("ATTEMPT", {"task_id": data.input["task_id"], "attempt": 1})

    async def run_attempt(data):
        task_id = data.input["task_id"]
        attempt = int(data.input["attempt"])
        status = "fixed" if attempt >= 2 else "failed"
        observation_ref = await record_store.put(
            {
                "attempt": attempt,
                "status": status,
                "evidence": "fallback selected" if status == "fixed" else "no route candidate",
            },
            collection="observations",
            kind="loop_observation",
            scope={"task_id": task_id},
        )

        task_context = TaskContext(task_id)
        task_context.attach(
            RecordStoreContextSource(record_store),
            binding_id=f"record-store:{task_id}",
            scope="task",
        )
        package = await task_context.reader(
            consumer="loop-controller",
            phase="decision",
        ).async_read(
            ContextReadIntent(
                query="route fallback",
                explicit_refs=(observation_ref["id"],),
                filters={"source_kinds": ["record_store"]},
            )
        )
        decision_ref = await record_store.put(
            {
                "attempt": attempt,
                "next": "stop" if status == "fixed" else "retry",
                "context_block_count": len(package.blocks),
            },
            collection="decisions",
            kind="loop_decision",
            scope={"task_id": task_id},
        )
        await record_store.link(decision_ref, observation_ref, relation="responds_to")
        await record_store.checkpoint(
            task_id,
            {"attempt": attempt, "status": status},
            step_id=f"attempt-{attempt}",
        )

        if status == "fixed":
            await task_workspace.write_file(
                "outputs/result.txt",
                "route fallback fixed after two attempts",
            )
            await data.async_set_state(
                "summary",
                {
                    "status": status,
                    "checkpoint_count": len(await record_store.checkpoint_history(task_id)),
                    "link_count": len(await record_store.links(relation="responds_to")),
                    "result_file": "outputs/result.txt",
                },
                emit=False,
            )
        else:
            data.emit_nowait("ATTEMPT", {"task_id": task_id, "attempt": attempt + 1})

    flow.to(start)
    flow.when("ATTEMPT").to(run_attempt)
    return flow


async def main() -> None:
    with TemporaryDirectory() as temp_dir:
        task_workspace = TaskWorkspace(temp_dir, mode="read_write")
        record_store = RecordStore(temp_dir, mode="read_write")
        flow = build_flow(record_store, task_workspace)
        execution = flow.create_execution(
            auto_close=False,
            record_store=record_store,
            runtime_resources={"runtime_event_store": record_store},
        )
        await execution.async_start({"task_id": "issue-123"})
        state = await execution.async_close()
        runtime_events = await record_store.query_runtime_events(execution.id)
        result = await task_workspace.read_file("outputs/result.txt")
        summary = {
            **state["summary"],
            "result": result.content,
            "runtime_event_count": len(runtime_events),
        }
        print(summary)
        assert summary["status"] == "fixed"
        assert summary["checkpoint_count"] == 2
        assert summary["result"] == "route fallback fixed after two attempts"


if __name__ == "__main__":
    asyncio.run(main())


# TriggerFlow owns progression; RecordStore owns observations, links,
# checkpoints, and runtime events; TaskWorkspace owns the final file;
# TaskContext owns bounded information delivery to the loop consumer.
