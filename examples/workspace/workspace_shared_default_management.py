import asyncio
import tempfile
from pathlib import Path
from typing import Any, cast

from agently import Agently, TriggerFlow


async def main():
    with tempfile.TemporaryDirectory(prefix="agently-workspace-defaults-") as temp_dir:
        workspace_root = Path(temp_dir).resolve()
        first_agent = Agently.create_agent("workspace-default-a").use_workspace(workspace_root)
        second_agent = Agently.create_agent("workspace-default-b").use_workspace(workspace_root)

        await first_agent.workspace.put(
            "first agent durable observation",
            collection="observations",
            kind="shared_root_probe",
        )
        await second_agent.workspace.put(
            "second agent durable observation",
            collection="observations",
            kind="shared_root_probe",
        )

        flow = TriggerFlow(name="workspace-shared-root-demo")
        first_execution = flow.create_execution(workspace=first_agent.workspace)
        second_execution = flow.create_execution(workspace=second_agent.workspace)
        first_execution_workspace = cast(Any, first_execution.require_runtime_resource("workspace"))
        second_execution_workspace = cast(Any, second_execution.require_runtime_resource("workspace"))

        await first_execution_workspace.put(
            "first execution durable observation",
            collection="observations",
            kind="shared_root_probe",
        )
        await second_execution_workspace.put(
            "second execution durable observation",
            collection="observations",
            kind="shared_root_probe",
        )
        await first_execution_workspace.write_file("outputs/probe.txt", "first")
        await second_execution_workspace.write_file("outputs/probe.txt", "second")

        db_count = len(list((workspace_root / ".agently").glob("workspace.db")))
        search_results = await first_agent.workspace.search(
            "durable observation",
            filters={"kind": "shared_root_probe"},
        )

        return {
            "physical_db_count": db_count,
            "agents_share_direct_root": first_agent.workspace.root == second_agent.workspace.root == workspace_root,
            "executions_share_direct_root": first_execution_workspace.root == second_execution_workspace.root,
            "execution_fallback_files_isolated": (
                first_execution_workspace.resolve_file_path("outputs/probe.txt")
                != second_execution_workspace.resolve_file_path("outputs/probe.txt")
            ),
            "shared_record_count": len(search_results),
        }


if __name__ == "__main__":
    print(asyncio.run(main()))


# Expected key output:
# {'physical_db_count': 1, 'agents_share_direct_root': True, 'executions_share_direct_root': True, 'execution_fallback_files_isolated': True, 'shared_record_count': 4}
#
# This infrastructure-only probe does not call a model. It demonstrates the
# direct-root Workspace contract: explicitly bound Agents and TriggerFlow
# executions share one lazily created private database, while read-only new-file
# products are isolated under `.agently/files/<execution-id>/`.
