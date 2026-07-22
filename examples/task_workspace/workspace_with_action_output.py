from __future__ import annotations

import asyncio
from tempfile import TemporaryDirectory
from typing import Any, cast

from agently import Agently
from agently.core.context import TaskContext
from agently.core.storage import RecordStoreContextSource
from agently.types.data import ContextReadIntent


async def main() -> None:
    with TemporaryDirectory() as temp_dir:
        agent = (
            Agently.create_agent("task-workspace-action-output-example")
            .use_task_workspace(temp_dir, mode="read_write")
            .use_record_store(temp_dir, mode="read_write")
        )
        task_workspace = agent.task_workspace
        record_store = agent.record_store

        agent.enable_task_workspace_file_actions(write=True, expose_to_model=False)
        agent.enable_shell(
            commands=["cat"],
            action_id="inspect_task_workspace_files",
            expose_to_model=False,
            sandbox="trusted_local",
        )

        write_result = agent.action.execute_action(
            "write_file",
            {
                "path": "notes/runtime.txt",
                "content": "route fallback fixed after provider returned no route candidate",
            },
        )
        assert write_result.get("status") == "success"

        shell_result = agent.action.execute_action(
            "inspect_task_workspace_files",
            {
                "cmd": "cat notes/runtime.txt",
                "workdir": str(task_workspace.root),
            },
        )
        shell_data = cast(dict[str, Any], shell_result.get("data"))
        assert shell_result.get("status") == "success"

        observation_ref = await record_store.put(
            content={
                "action_id": shell_result.get("action_id"),
                "status": shell_result.get("status"),
                "stdout": shell_data["stdout"],
                "task_workspace_file": "notes/runtime.txt",
            },
            collection="observations",
            kind="action_output",
            summary="shell inspected a TaskWorkspace note",
            scope={"task_id": "issue-456"},
            source={"type": "action", "name": "inspect_task_workspace_files"},
        )

        task_context = TaskContext("issue-456")
        task_context.attach(
            RecordStoreContextSource(record_store),
            binding_id="record-store:issue-456",
            scope="task",
        )
        context_package = await task_context.reader(
            consumer="issue-planner",
            phase="planning",
        ).async_read(
            ContextReadIntent(
                query="route fallback action output",
                explicit_refs=(observation_ref["id"],),
                filters={"source_kinds": ["record_store"]},
            )
        )

        summary = {
            "stdout": shell_data["stdout"],
            "task_workspace_file": "notes/runtime.txt",
            "context_block_count": len(context_package.blocks),
            "contains_action_output": any(
                block.source_ref == observation_ref["id"]
                for block in context_package.blocks
            ),
        }
        print(summary)
        assert summary["context_block_count"] == 1
        assert summary["contains_action_output"] is True


if __name__ == "__main__":
    asyncio.run(main())


# TaskWorkspace owns file containment, mutation, and readback. RecordStore owns
# the durable observation. TaskContext binds the selected sources, and its
# consumer-bound reader returns the bounded ContextPackage.
