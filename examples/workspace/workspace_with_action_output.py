import asyncio
from tempfile import TemporaryDirectory
from typing import Any, cast

from agently import Agently


async def main():
    with TemporaryDirectory() as temp_dir:
        agent = Agently.create_agent("workspace-action-output-example").use_workspace(
            temp_dir,
            mode="read_write",
        )
        workspace = agent.workspace
        assert workspace is not None

        agent.enable_workspace_file_actions(write=True, expose_to_model=False)
        agent.enable_shell(
            commands=["cat"],
            action_id="inspect_workspace_files",
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
            "inspect_workspace_files",
            {
                "cmd": "cat notes/runtime.txt",
                "workdir": str(workspace.root),
            },
        )
        shell_data = cast(dict[str, Any], shell_result.get("data"))
        assert shell_result.get("status") == "success"
        assert shell_data["stdout"] == "route fallback fixed after provider returned no route candidate"

        observation = {
            "action_id": shell_result.get("action_id"),
            "status": shell_result.get("status"),
            "stdout": shell_data["stdout"],
            "workspace_file": "notes/runtime.txt",
            "artifact_count": len(shell_result.get("artifact_refs", [])),
        }
        observation_ref = await workspace.put(
            content=observation,
            collection="observations",
            kind="action_output",
            summary="shell inspected route fallback workspace note",
            scope={"task_id": "issue-456"},
            source={"type": "action", "name": "inspect_workspace_files"},
        )

        context_pack = await workspace.build_context(
            goal="route fallback action output",
            scope={"task_id": "issue-456"},
            budget={"chars": 1200},
            profile="auto",
        )
        context_refs = [item["ref"]["id"] for item in context_pack["items"]]
        summary = {
            "stdout": observation["stdout"],
            "workspace_file": observation["workspace_file"],
            "context_item_count": len(context_pack["items"]),
            "contains_action_output": observation_ref["id"] in context_refs,
        }

        print(summary)
        assert summary == {
            "stdout": "route fallback fixed after provider returned no route candidate",
            "workspace_file": "notes/runtime.txt",
            "context_item_count": 1,
            "contains_action_output": True,
        }


asyncio.run(main())

# Expected key output:
# {'stdout': 'route fallback fixed after provider returned no route candidate', 'workspace_file': 'notes/runtime.txt', 'context_item_count': 1, 'contains_action_output': True}
#
# This is an infrastructure composition smoke, not a model-owned WorkLoop.
# `use_workspace(...)` binds one direct ordinary file root. This example grants
# read-write access because the shell must read the new external file directly.
# Durable records are created lazily under `.agently/workspace.db` only when
# `workspace.put(...)` is called.
#
# `enable_workspace_file_actions(...)` exposes file actions over that root.
# The shell action output does not become memory automatically. Application code
# explicitly ingests the action result as a Workspace observation, then
# ContextBuilder packages it into a ContextPackage through `workspace.build_context(...)`.
#
# Flow:
# use_workspace(temp_dir)
#   |
#   v
# enable_workspace_file_actions(write=True) -> write_file("notes/runtime.txt")
#   |
#   v
# enable_shell(..., action_id="inspect_workspace_files")
#   |
#   v
# run_bash("cat notes/runtime.txt", workdir=workspace.root)
#   |
#   v
# workspace.put(action output as observation)
#   |
#   v
# workspace.build_context(...) -> ContextPackage contains the action-output record
