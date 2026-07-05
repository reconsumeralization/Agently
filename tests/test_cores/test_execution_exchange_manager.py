import asyncio
import threading

import pytest

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently.base import execution_exchange, policy_approval, settings


class RecordingProvider:
    def __init__(self, response=None):
        self.published = []
        self.response = response
        self.await_calls = 0

    def publish_request(self, execution_id, request, *, interrupt):
        self.published.append({"execution_id": execution_id, "request": dict(request)})
        return {
            "exchange_id": f"ex-{ len(self.published) }",
            "request_ref": {"collection": "test_exchanges", "id": str(len(self.published))},
            "provider_metadata": {"provider": "recording"},
        }


class HotProvider(RecordingProvider):
    async def await_response(self, request):
        self.await_calls += 1
        await asyncio.sleep(0.01)
        return self.response


def _approval_flow(**gate_kwargs):
    flow = TriggerFlow()
    outcome: dict[str, object] = {}

    async def guarded(data: TriggerFlowRuntimeData):
        decision = await policy_approval.async_gate(
            data,
            {
                "source": "action",
                "capability": "delete_file",
                "subject": "delete ./report.md",
                "risk": "destructive",
            },
            handler="fail_closed",
            resume_to="next",
            interrupt_id="approval-gate",
            **gate_kwargs,
        )
        return decision

    async def finish(data: TriggerFlowRuntimeData):
        outcome["decision"] = data.value
        return data.value

    flow.to(guarded).to(finish)
    return flow, outcome


@pytest.mark.asyncio
async def test_policy_gate_passes_exchange_metadata_into_interrupt_envelope():
    provider = RecordingProvider()
    flow, _ = _approval_flow(
        channel_id="test-channel",
        provider_id="unit-provider",
        wait_mode="connected_then_disconnected",
        hot_wait_timeout=12.5,
        cold_persistence_policy="persist",
        request_payload_schema={"type": "object"},
        response_payload_schema={"type": "object", "required": ["approved"]},
        audit_metadata={"case": "metadata-passthrough"},
    )
    execution_exchange.register_provider("unit-provider", provider, replace=True)
    try:
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")

        interrupt = execution.get_interrupt("approval-gate")
        assert interrupt is not None and interrupt["status"] == "waiting"
        envelope = interrupt["external_wait_request"]
        assert envelope["channel_id"] == "test-channel"
        assert envelope["provider_id"] == "unit-provider"
        assert envelope["wait_mode"] == "connected_then_disconnected"
        assert envelope["hot_wait_timeout"] == 12.5
        assert envelope["cold_persistence_policy"] == "persist"
        assert envelope["request_payload_schema"] == {"type": "object"}
        assert envelope["response_payload_schema"] == {"type": "object", "required": ["approved"]}
        assert envelope["audit_metadata"]["case"] == "metadata-passthrough"
        assert envelope["audit_metadata"]["subject"] == "delete ./report.md"
        assert envelope["exchange_id"] == "ex-1"
        assert len(provider.published) == 1

        await execution.async_continue_with("approval-gate", {"approved": True})
        await execution.async_close()
    finally:
        execution_exchange.unregister_provider("unit-provider")


@pytest.mark.asyncio
async def test_policy_gate_routes_metadata_from_interaction_posture():
    provider = RecordingProvider()
    execution_exchange.register_provider("posture-provider", provider, replace=True)
    old_mode = settings.get("interaction.mode", None)
    old_provider = settings.get("interaction.exchange_provider", None)
    settings.set("interaction.mode", "auto")
    settings.set("interaction.exchange_provider", "posture-provider")
    try:
        flow, _ = _approval_flow()
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")

        interrupt = execution.get_interrupt("approval-gate")
        envelope = interrupt["external_wait_request"]
        assert envelope["wait_mode"] == "connected_then_disconnected"
        assert envelope["provider_id"] == "posture-provider"
        assert len(provider.published) == 1

        await execution.async_continue_with("approval-gate", {"approved": False, "reason": "denied"})
        await execution.async_close()
    finally:
        settings.set("interaction.mode", old_mode)
        settings.set("interaction.exchange_provider", old_provider)
        execution_exchange.unregister_provider("posture-provider")


@pytest.mark.asyncio
async def test_hot_wait_resolves_through_provider_await_response():
    provider = HotProvider(response={"approved": True, "reason": "provider approved"})
    execution_exchange.register_provider("hot-provider", provider, replace=True)
    try:
        flow, outcome = _approval_flow(provider_id="hot-provider", wait_mode="connected")
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")
        assert execution.get_pending_interrupts()

        resolved = await execution_exchange.async_hot_wait_pending(execution, timeout=5)
        assert resolved is True
        assert provider.await_calls == 1
        assert not execution.get_pending_interrupts()
        result = await execution.async_close()
        assert outcome["decision"]["approved"] is True
    finally:
        execution_exchange.unregister_provider("hot-provider")


@pytest.mark.asyncio
async def test_respond_from_another_thread_resumes_live_wait():
    provider = RecordingProvider()
    execution_exchange.register_provider("thread-provider", provider, replace=True)
    try:
        flow, outcome = _approval_flow(provider_id="thread-provider", wait_mode="connected")
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")
        interrupt = execution.get_interrupt("approval-gate")
        wait_key = f"{ execution.id }:approval-gate"

        def respond_from_thread():
            execution_exchange.respond(
                "ex-1",
                {"approved": True, "reason": "thread approved"},
                actor="thread-tester",
            )

        hot_wait_task = asyncio.create_task(
            execution_exchange.async_hot_wait(execution, interrupt, timeout=5)
        )
        await asyncio.sleep(0.05)
        assert execution_exchange.get_live_wait(wait_key) is not None
        assert execution_exchange.get_live_wait("ex-1") is not None
        thread = threading.Thread(target=respond_from_thread)
        thread.start()
        resolved = await hot_wait_task
        thread.join(timeout=5)

        assert resolved is True
        resumed = execution.get_interrupt("approval-gate")
        assert resumed["status"] == "resumed"
        assert resumed["resumed_by"] == "thread-tester"
        await execution.async_close()
        assert outcome["decision"]["approved"] is True
    finally:
        execution_exchange.unregister_provider("thread-provider")


@pytest.mark.asyncio
async def test_project_pending_exchanges_view_shape():
    provider = RecordingProvider()
    execution_exchange.register_provider("view-provider", provider, replace=True)
    try:
        flow, _ = _approval_flow(provider_id="view-provider", wait_mode="disconnected")
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")

        views = execution_exchange.project_pending_exchanges(execution)
        assert len(views) == 1
        view = views[0]
        assert view["status"] == "pending"
        assert view["kind"] == "approval"
        assert view["interrupt_id"] == "approval-gate"
        assert view["execution_id"] == execution.id
        assert view["exchange_id"] == "ex-1"
        assert view["subject"] == "delete ./report.md"

        await execution.async_continue_with("approval-gate", {"approved": True})
        views = execution_exchange.project_execution_exchanges(execution)
        assert views[0]["status"] == "responded"
        assert views[0]["response"] == {"approved": True}
        await execution.async_close()
    finally:
        execution_exchange.unregister_provider("view-provider")


@pytest.mark.asyncio
async def test_routing_reads_agent_scoped_settings_not_only_global():
    from agently import Agently

    agent = Agently.create_agent()
    agent.settings.set("interaction.mode", "hot")
    agent.settings.set("interaction.exchange_provider", "scoped-prov")

    routing = await execution_exchange.async_route(
        {"exchange_kind": "approval"}, settings=agent.settings
    )
    assert routing is not None
    assert routing["provider_id"] == "scoped-prov"
    assert routing["wait_mode"] == "connected"

    # Unset agent-local mode falls back to global/default without leaking the
    # provider from another scope.
    fresh = Agently.create_agent()
    routing_default = await execution_exchange.async_route(
        {"exchange_kind": "approval"}, settings=fresh.settings
    )
    assert routing_default is not None
    assert routing_default["provider_id"] is None


def _two_gate_chunk_flow():
    """One chunk containing two DIFFERENT sequential approval gates.

    Models the reviewed defect pair: a plan-approval gate followed by a
    permission gate inside the same resumed chunk. Each gate must claim only
    its own resumed interrupt, and the second gate must pause with a fresh
    self-resume budget instead of inheriting the first gate's count.
    """
    flow = TriggerFlow()
    outcome: dict[str, object] = {}

    async def double_gated(data: TriggerFlowRuntimeData):
        plan_decision = await policy_approval.async_gate(
            data,
            {
                "source": "action_plan",
                "capability": "plan",
                "subject": "approve the action plan",
                "risk": "low",
            },
            handler="fail_closed",
            resume_to="self",
        )
        if data.execution.is_waiting():
            return plan_decision
        outcome["plan_decision"] = plan_decision
        permission_decision = await policy_approval.async_gate(
            data,
            {
                "source": "action",
                "capability": "run_shell",
                "subject": "run a high-risk shell command",
                "risk": "destructive",
            },
            handler="fail_closed",
            resume_to="self",
        )
        if data.execution.is_waiting():
            return permission_decision
        outcome["permission_decision"] = permission_decision
        return permission_decision

    flow.to(double_gated)
    return flow, outcome


@pytest.mark.asyncio
async def test_second_gate_does_not_consume_first_gate_resume_and_gets_fresh_budget():
    flow, outcome = _two_gate_chunk_flow()
    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)

    pending = execution.get_pending_interrupts()
    assert len(pending) == 1
    plan_interrupt_id = next(iter(pending))
    plan_payload = pending[plan_interrupt_id]["payload"]["request"]
    assert plan_payload["source"] == "action_plan"

    # Approving the plan gate must NOT auto-approve the permission gate: the
    # replayed chunk claims the plan resume for the plan gate only, then the
    # permission gate pauses as a new interrupt with a fresh self-resume budget.
    await execution.async_continue_with(plan_interrupt_id, {"approved": True, "reason": "plan ok"})

    pending = execution.get_pending_interrupts()
    assert len(pending) == 1
    permission_interrupt_id = next(iter(pending))
    assert permission_interrupt_id != plan_interrupt_id
    permission_payload = pending[permission_interrupt_id]["payload"]["request"]
    assert permission_payload["source"] == "action"
    assert outcome["plan_decision"]["approved"] is True

    await execution.async_continue_with(
        permission_interrupt_id, {"approved": False, "reason": "denied by reviewer"}
    )
    assert outcome["permission_decision"]["approved"] is False
    # Replays must keep each gate bound to its own resumed interrupt.
    assert outcome["plan_decision"]["approved"] is True
    await execution.async_close()
