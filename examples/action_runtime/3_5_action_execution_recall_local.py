from pathlib import Path
from pprint import pprint

from agently import Agently
from agently.core import Action
from agently.core.runtime import bind_runtime_context


agent = Agently.create_agent()
workspace = Path(__file__).resolve().parent
execution = agent.input("Inspect the example TaskWorkspace.").create_execution().strategy("direct")
artifact_scope = {"kind": "agent_execution", "id": execution.id}

agent.enable_shell(
    root=workspace,
    commands=["pwd"],
    action_id="inspect_workspace",
)

with bind_runtime_context(agent_execution_context=execution.execution_context):
    records = agent.action.execute_action(
        "inspect_workspace",
        {"cmd": "pwd", "workdir": str(workspace)},
        purpose="Inspect workspace path",
        artifact_scope=artifact_scope,
    )

print("RAW ACTION RECORD")
pprint(records)

print("\nMODEL-VISIBLE ACTION RESULTS")
pprint(Action.to_action_results([records]))

artifact_refs = records.get("artifact_refs") or []
assert artifact_refs
artifact_ref = artifact_refs[0]
selection_key = artifact_ref.get("selection_key")
assert selection_key is not None
with bind_runtime_context(agent_execution_context=execution.execution_context):
    raw_artifact = agent.action.read_action_artifact(
        selection_key=selection_key,
    )
agent.action._release_artifact_scope(artifact_scope)

print("\nRECALLED RAW ARTIFACT")
pprint(raw_artifact)

# Expected key output:
# RAW ACTION RECORD contains model_digest and artifact_refs.
# MODEL-VISIBLE ACTION RESULTS contains the compact digest, not only raw stdout.
# RECALLED RAW ARTIFACT returns the saved action input for inspect_workspace.

# How it works:
# agent.action.execute_action() bypasses model planning entirely and calls the action
# directly with explicit arguments — useful for testing or scripted workflows.
# enable_shell() registers a bash action restricted to the "pwd" command.
# The raw ActionResult contains model_digest (a compact summary the model can read)
# and artifact_refs (pointers to full stored artifacts).
# agent.action.read_action_artifact() resolves only the host-issued selection_key
# in the currently bound AgentExecution scope.
#
# Flow:
# agent.enable_shell(root=workspace, commands=["pwd"], action_id="inspect_workspace")
#   |
#   v
# agent.action.execute_action("inspect_workspace", {"cmd":"pwd","workdir":workspace})
#   | (no model call — direct execution)
#   v
# BashSandboxActionExecutor -> stdout = str(workspace)
# ActionResult { model_digest: ..., artifact_refs: [{ selection_key, ...bounded facts }] }
#   |
#   v
# Action.to_action_results([records]) -> compact model-visible dict
# agent.action.read_action_artifact(selection_key=...) -> raw artifact
