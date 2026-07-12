from __future__ import annotations

import json
from typing import Any

import pytest

from agently import Agently


@pytest.mark.asyncio
async def test_large_action_output_stays_in_memory_and_runtime_event_is_bounded() -> None:
    large_body = "x" * (1024 * 1024)
    agent = Agently.create_agent("action-artifact-retention-large-output")
    record = agent.action._finalize_action_result(
        {
            "action_call_id": "large-action-call",
            "action_id": "large_action_output",
            "status": "success",
            "success": True,
            "ok": True,
            "result": {"body": large_body},
            "data": {"body": large_body},
        }
    )
    artifact_refs = record["artifact_refs"]
    assert len(artifact_refs) == 1
    artifact_id = artifact_refs[0]["artifact_id"]
    assert len(agent.action._artifact_manager._artifacts) == 1
    assert agent.action._artifact_manager.get_artifact_value(artifact_id) == {"body": large_body}
    assert large_body not in json.dumps(record, ensure_ascii=False)

    visible_record = agent.action._to_model_visible_record(record)
    assert large_body not in json.dumps(visible_record, ensure_ascii=False)
    assert visible_record["artifact_refs"][0]["artifact_id"] == artifact_id
    assert visible_record["result"]["result_preview_meta"]["truncated"] is True

    captured: list[Any] = []
    hook_name = "test_action_artifact_retention.large_action_output"
    Agently.event_center.register_hook(
        lambda event: captured.append(event),
        event_types="action.completed",
        hook_name=hook_name,
    )
    try:
        await agent.action._flow_controller.async_emit_action_flow_observation(
            {
                "kind": "action_completed",
                "source": "ActionFlow",
                "payload": {"record": record},
            }
        )
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert len(captured) == 1
    event_payload = captured[0].payload
    event_record = event_payload["record"]
    assert large_body not in json.dumps(event_payload, ensure_ascii=False)
    assert event_record["artifact_refs"][0]["artifact_id"] == artifact_id
    assert event_record["result"]["result_preview_meta"]["truncated"] is True
