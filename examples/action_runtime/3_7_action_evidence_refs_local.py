from __future__ import annotations

import json

from agently import Agently
from agently.core import Action
from agently.core.runtime import bind_runtime_context


agent = Agently.create_agent()
execution = agent.input("Inspect bounded Action evidence.").create_execution().strategy("direct")
artifact_scope = {"kind": "agent_execution", "id": execution.id}
stdout = "alpha-line\n" * 1500
stderr = "warning-line\n" * 700


def produce_large_output():
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": 0,
    }


agent.action.register_action(
    action_id="produce_large_output",
    desc="Produce large command-like output.",
    kwargs={},
    func=produce_large_output,
    expose_to_model=False,
)

record = agent.action.execute_action(
    "produce_large_output",
    {},
    artifact_scope=artifact_scope,
)
visible = Action.to_action_results([record])
visible_digest = next(iter(visible.values()))
artifact_refs = record.get("artifact_refs", [])
output_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_output")

with bind_runtime_context(agent_execution_context=execution.execution_context):
    raw = agent.action.read_action_artifact(
        selection_key=str(output_ref.get("selection_key", "")),
    )
agent.action._release_artifact_scope(artifact_scope)

deduped = agent.action._artifact_manager.normalize_execution_records(
    [
        {
            "action_call_id": "act_call_duplicate",
            "status": "success",
            "success": True,
            "action_id": "duplicate_probe",
            "purpose": "Duplicate probe",
            "data": "first",
        },
        {
            "action_call_id": "act_call_duplicate",
            "status": "success",
            "success": True,
            "action_id": "duplicate_probe",
            "purpose": "Duplicate probe",
            "data": "second",
        },
    ],
    [],
)

summary = {
    "record_status": record.get("status"),
    "raw_stdout_complete": raw.get("value", {}).get("stdout") == stdout,
    "raw_stderr_complete": raw.get("value", {}).get("stderr") == stderr,
    "artifact_preview_truncated": output_ref.get("truncated"),
    "selection_key_present": bool(output_ref.get("selection_key")),
    "visible_has_digest": isinstance(visible_digest, dict) and "result_preview_meta" in visible_digest,
    "visible_preview_truncated": visible_digest.get("result_preview_meta", {}).get("truncated"),
    "deduped_record_count": len(deduped),
}

print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))

assert summary["record_status"] == "success"
assert summary["raw_stdout_complete"] is True
assert summary["raw_stderr_complete"] is True
assert summary["artifact_preview_truncated"] is True
assert summary["selection_key_present"] is True
assert summary["visible_has_digest"] is True
assert summary["visible_preview_truncated"] is True
assert summary["deduped_record_count"] == 1

# Expected key output:
# {
#   "artifact_preview_truncated": true,
#   "deduped_record_count": 1,
#   "raw_stderr_complete": true,
#   "raw_stdout_complete": true,
#   "record_status": "success",
#   "selection_key_present": true,
#   "visible_preview_truncated": true
# }
