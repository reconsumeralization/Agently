import asyncio
import tempfile
from pathlib import Path

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData


async def triggerflow_durable_recovery_demo():
    with tempfile.TemporaryDirectory(prefix="agently-triggerflow-recovery-") as workspace_dir:
        workspace = Agently.create_workspace(Path(workspace_dir))
        flow = TriggerFlow(name="durable-approval-recovery")

        async def draft_request(data: TriggerFlowRuntimeData):
            await data.async_set_state("draft", {"topic": data.value}, emit=False)
            return await data.async_pause_for(
                type="exchange", exchange_kind="approval",
                payload={"question": "Approve this pricing update?"},
                interrupt_id="approval",
                resume_to="next",
                channel_id="ops-approval",
                provider_id="approval-router",
                wait_mode="connected_then_disconnected",
                hot_wait_timeout=30.0,
                cold_persistence_policy="persist",
                request_payload_schema={"type": "object", "required": ["question"]},
                response_payload_schema={"type": "object", "required": ["approved"]},
                audit_metadata={"exchange_id": "approval-exchange-42"},
            )

        async def finalize(data: TriggerFlowRuntimeData):
            approvals = list(data.get_state("approvals", []) or [])
            approvals.append(data.value)
            await data.async_set_state("approvals", approvals, emit=False)
            await data.async_set_state(
                "final",
                {
                    "draft": data.get_state("draft"),
                    "approval": data.value,
                },
                emit=False,
            )

        flow.to(draft_request).to(finalize)

        execution = flow.create_execution(
            auto_close=False,
            runtime_resources={"workspace": workspace},
        )
        await execution.async_start("pricing")
        snapshot_ref = await execution.async_save(step_id="waiting-approval")
        saved_snapshot = await workspace.get_data(snapshot_ref)

        recovered = flow.create_execution(
            auto_close=False,
            runtime_resources={"workspace": workspace},
        )
        load = await recovered.async_load(
            saved_snapshot,
            runtime_resources={"workspace": workspace},
        )
        assert load["ready"] is True

        await recovered.async_continue_with(
            "approval",
            {"approved": True},
            resume_request_id="approval-webhook-42",
            actor="reviewer",
        )

        resumed_state = recovered.save()
        replay = flow.create_execution(
            auto_close=False,
            runtime_resources={"workspace": workspace},
        )
        replay.load(resumed_state, runtime_resources={"workspace": workspace})
        duplicate_result = await replay.async_continue_with(
            "approval",
            {"approved": True},
            resume_request_id="approval-webhook-42",
            actor="reviewer",
        )
        snapshot = await replay.async_close()
        final_state = replay.save()
        runtime_events = await workspace.query_runtime_events(replay.id)

        resume_record = final_state["resume_ledger"]["approval"]["approval-webhook-42"]
        wait_request = final_state["interrupts"]["approval"]["external_wait_request"]

        assert snapshot["final"]["approval"]["approved"] is True
        assert snapshot["approvals"] == [{"approved": True}]
        assert resume_record["status"] == "completed"
        assert wait_request["dispatch_state"] == "completed"
        assert wait_request["callback_idempotency_key"] == "approval-webhook-42"
        assert duplicate_result is not None
        assert duplicate_result["resume_request_id"] == "approval-webhook-42"

        key_output = {
            "load_ready": load["ready"],
            "approval_count": len(snapshot["approvals"]),
            "resume_status": resume_record["status"],
            "wait_dispatch_state": wait_request["dispatch_state"],
            "has_resume_completed_event": any(
                event["event_type"] == "triggerflow.resume_completed"
                for event in runtime_events
            ),
        }
        print(key_output)


if __name__ == "__main__":
    asyncio.run(triggerflow_durable_recovery_demo())

# Expected key output:
# {
#     'load_ready': True,
#     'approval_count': 1,
#     'resume_status': 'completed',
#     'wait_dispatch_state': 'completed',
#     'has_resume_completed_event': True,
# }
#
# How it works:
# The first execution pauses at a durable ExternalWait approval request and
# persists a Workspace-backed execution snapshot. A fresh execution loads
# that snapshot, accepts the approval with a stable resume_request_id, then a second
# fresh execution receives the same callback id again.  The duplicate callback
# returns the completed interrupt record instead of running finalize twice.
#
# Flow:
# async_start("pricing")
#   |
#   v
# draft_request -> pause_for(type="exchange", exchange_kind="approval", resume_to="next")
#   |
# async_save(step_id="waiting-approval")
#   |
# [--- process/restart boundary represented by a fresh execution ---]
#   |
# async_load(saved_snapshot)
# async_continue_with("approval", ..., resume_request_id="approval-webhook-42")
#   |
#   v
# finalize -> state["approvals"] has exactly one approval
#   |
# [--- duplicate callback delivery represented by another fresh execution ---]
#   |
# async_continue_with(... same resume_request_id ...) -> idempotent replay
# async_close() -> close snapshot with one approval and completed resume ledger
