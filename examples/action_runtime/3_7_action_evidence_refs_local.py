from __future__ import annotations

import json

from agently import Agently
from agently.core import Action


agent = Agently.create_agent()
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

record = agent.action.execute_action("produce_large_output", {})
visible = Action.to_action_results([record])
visible_digest = next(iter(visible.values()))
artifact_refs = record.get("artifact_refs", [])
output_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_output")

raw = agent.action.read_action_artifact(
    artifact_id=str(output_ref.get("artifact_id", "")),
    action_call_id=str(output_ref.get("action_call_id", "")),
)

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
    "artifact_bytes_positive": output_ref.get("bytes", 0) > 0,
    "artifact_preview_truncated": output_ref.get("truncated"),
    "artifact_sha256_length": len(str(output_ref.get("sha256", ""))),
    "visible_has_digest": isinstance(visible_digest, dict) and "result_preview_meta" in visible_digest,
    "visible_preview_truncated": visible_digest.get("result_preview_meta", {}).get("truncated"),
    "deduped_record_count": len(deduped),
}

print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))

assert summary["record_status"] == "success"
assert summary["raw_stdout_complete"] is True
assert summary["raw_stderr_complete"] is True
assert summary["artifact_bytes_positive"] is True
assert summary["artifact_preview_truncated"] is True
assert summary["artifact_sha256_length"] == 64
assert summary["visible_has_digest"] is True
assert summary["visible_preview_truncated"] is True
assert summary["deduped_record_count"] == 1

# Expected key output:
# {
#   "artifact_preview_truncated": true,
#   "artifact_sha256_length": 64,
#   "deduped_record_count": 1,
#   "raw_stderr_complete": true,
#   "raw_stdout_complete": true,
#   "record_status": "success",
#   "visible_preview_truncated": true
# }
