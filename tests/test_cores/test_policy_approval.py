import pytest

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently.core.orchestration.TriggerFlow.Control import TriggerFlowPauseSignal


@pytest.mark.asyncio
async def test_policy_approval_default_input_timeout_fail_denies_non_interactive():
    Agently.configure_policy_approval(handler="input_timeout_fail")

    decision = await Agently.policy_approval.async_resolve(
        {
            "source": "action",
            "capability": "dangerous_action",
            "subject": "Dangerous Action",
        }
    )

    assert decision.get("status") == "denied"
    assert decision.get("approved") is False
    assert decision.get("wait_strategy") == "input_timeout_fail"


@pytest.mark.asyncio
async def test_policy_approval_auto_approve_handler():
    Agently.configure_policy_approval(handler="auto_approve")
    try:
        decision = await Agently.policy_approval.async_resolve(
            {
                "source": "skills_capability",
                "capability": "web_search",
                "subject": "web_search",
            }
        )
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")

    assert decision.get("status") == "approved"
    assert decision.get("approved") is True


@pytest.mark.asyncio
async def test_policy_approval_triggerflow_gate_pending_and_resume():
    Agently.configure_policy_approval(handler="fail_closed")
    try:
        flow = TriggerFlow(name="policy-approval-gate-test")

        async def gate(data: TriggerFlowRuntimeData):
            result = await Agently.policy_approval.async_gate(
                data,
                {
                    "request_id": "test-approval",
                    "source": "triggerflow",
                    "capability": "write_file",
                    "subject": "Write interview file",
                },
                resume_to="self",
            )
            if isinstance(result, TriggerFlowPauseSignal):
                return result
            if result.get("status") == "approved":
                final = {"approved": True, "reason": result.get("reason", "")}
            else:
                final = {"approved": False, "reason": result.get("reason", "")}
            data.execution._system_runtime_data.set("result", final)
            result_ready = data.execution._system_runtime_data.get("result_ready")
            if result_ready is not None:
                result_ready.set()
            return final

        flow.to(gate)
        execution = await flow.async_start_execution(None, wait_for_result=False)
        pending = execution.get_pending_interrupts()

        assert execution.get_status() == "waiting"
        assert "policy:test-approval" in pending
        assert pending["policy:test-approval"]["type"] == "policy_approval"

        await execution.async_continue_with("policy:test-approval", {"status": "approved", "reason": "ok"})
        result = await execution.result.async_get_final_result(timeout=1)
        await execution.async_close()

        assert result == {"approved": True, "reason": "ok"}
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")


@pytest.mark.asyncio
async def test_policy_approval_gate_loads_from_workspace_provider_snapshot(tmp_path):
    Agently.configure_policy_approval(handler="fail_closed")
    try:
        agent = Agently.create_agent("policy-approval-provider-snapshot").use_workspace(tmp_path / "run")
        workspace = agent.workspace
        assert workspace is not None
        flow = TriggerFlow(name="policy-approval-provider-snapshot")

        async def gate(data: TriggerFlowRuntimeData):
            result = await Agently.policy_approval.async_gate(
                data,
                {
                    "request_id": "provider-approval",
                    "source": "triggerflow",
                    "capability": "write_file",
                    "subject": "Write provider snapshot proof",
                },
                resume_to="self",
            )
            if isinstance(result, TriggerFlowPauseSignal):
                return result
            await data.async_set_state("policy_decision", result, emit=False)

        flow.to(gate)
        execution = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
        await execution.async_start(None)
        assert "policy:provider-approval" in execution.get_pending_interrupts()

        snapshot_ref = await execution.async_save(step_id="policy-pending")
        snapshot_state = await workspace.get_data(snapshot_ref)
        assert snapshot_state["interrupts"]["policy:provider-approval"]["status"] == "waiting"

        restored = flow.create_execution(auto_close=False, runtime_resources={"workspace": workspace})
        load = await restored.async_load(
            snapshot_state,
            runtime_resources={"workspace": workspace},
        )
        assert load["ready"] is True
        await restored.async_continue_with(
            "policy:provider-approval",
            {"status": "approved", "reason": "ok"},
            resume_request_id="approval-callback-1",
            actor="policy-service",
        )
        snapshot = await restored.async_close()
        resumed_ref = await restored.async_save(step_id="policy-approved")
        resumed_state = await workspace.get_data(resumed_ref)
        runtime_events = await workspace.query_runtime_events(restored.id)
        event_types = [event["event_type"] for event in runtime_events]

        assert snapshot["policy_decision"]["approved"] is True
        assert snapshot["policy_decision"]["reason"] == "ok"
        assert (
            resumed_state["resume_ledger"]["policy:provider-approval"]["approval-callback-1"][
                "status"
            ]
            == "completed"
        )
        assert "triggerflow.resume_request_accepted" in event_types
        assert "triggerflow.resume_dispatched" in event_types
        assert "triggerflow.resume_completed" in event_types
        assert "triggerflow.interrupt_raised" in event_types
        assert "triggerflow.execution_resumed" in event_types
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")
