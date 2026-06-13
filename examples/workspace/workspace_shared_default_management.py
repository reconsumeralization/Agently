import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, cast

from agently import Agently, TriggerFlow
from agently.types.data import RunContext


async def main():
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="agently-workspace-defaults-") as temp_dir:
        os.chdir(temp_dir)
        try:
            session_id = "workspace-demo"
            first_agent = Agently.create_agent("workspace-default-a").activate_session(session_id=session_id)
            second_agent = Agently.create_agent("workspace-default-b").activate_session(session_id=session_id)

            await first_agent.workspace.put(
                "first agent durable observation",
                collection="observations",
                kind="shared_default_probe",
            )
            await second_agent.workspace.put(
                "second agent durable observation",
                collection="observations",
                kind="shared_default_probe",
            )

            flow = TriggerFlow(name="workspace-shared-default-demo")
            parent_run = RunContext.create(run_kind="agent_execution", session_id=session_id)
            first_execution = flow.create_execution(parent_run_context=parent_run)
            second_execution = flow.create_execution(parent_run_context=parent_run)
            first_execution_workspace = cast(Any, first_execution.require_runtime_resource("workspace"))
            second_execution_workspace = cast(Any, second_execution.require_runtime_resource("workspace"))

            await first_execution_workspace.put(
                "first execution durable observation",
                collection="observations",
                kind="shared_default_probe",
            )
            await second_execution_workspace.put(
                "second execution durable observation",
                collection="observations",
                kind="shared_default_probe",
            )

            session_root = Path(".agently") / "workspaces" / "sessions" / session_id
            db_count = len(list(session_root.glob("workspace.db")))
            search_results = await first_agent.workspace.search(
                "durable observation",
                filters={"kind": "shared_default_probe"},
            )

            return {
                "physical_db_count": db_count,
                "agent_roots_share_db": first_agent.workspace.root == second_agent.workspace.root == session_root.resolve(),
                "execution_roots_share_db": first_execution_workspace.root == second_execution_workspace.root,
                "execution_file_roots_isolated": (
                    first_execution_workspace.files_root != second_execution_workspace.files_root
                ),
                "session_search_count": len(search_results),
            }
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    print(asyncio.run(main()))


# Expected key output:
# {'physical_db_count': 1, 'agent_roots_share_db': True, 'execution_roots_share_db': True, 'execution_file_roots_isolated': True, 'session_search_count': 4}
#
# This infrastructure-only probe does not call a model. It demonstrates the
# default Workspace management contract: an active session maps default Agents
# and TriggerFlow executions to one physical Workspace database, while each
# execution receives an isolated editable file root under files/executions/.
