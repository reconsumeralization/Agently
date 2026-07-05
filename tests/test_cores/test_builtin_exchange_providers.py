import asyncio

import pytest

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently.base import execution_exchange, policy_approval
from agently.builtins.exchange_providers import ConsoleExchangeProvider, HostCallbackExchangeProvider


def _gate_flow(**gate_kwargs):
    flow = TriggerFlow()
    outcome: dict[str, object] = {}

    async def guarded(data: TriggerFlowRuntimeData):
        return await policy_approval.async_gate(
            data,
            {
                "source": "action",
                "capability": "send_mail",
                "subject": "send quarterly report",
                "risk": "write",
            },
            handler="fail_closed",
            resume_to="next",
            interrupt_id="mail-approval",
            **gate_kwargs,
        )

    async def finish(data: TriggerFlowRuntimeData):
        outcome["decision"] = data.value
        return data.value

    flow.to(guarded).to(finish)
    return flow, outcome


@pytest.mark.asyncio
async def test_console_provider_renders_card_and_maps_yes_to_approval():
    printed: list[str] = []
    provider = ConsoleExchangeProvider(
        input_func=lambda prompt: "y",
        print_func=printed.append,
    )
    execution_exchange.register_provider("console-test", provider, replace=True)
    try:
        flow, outcome = _gate_flow(provider_id="console-test", wait_mode="connected")
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")
        assert printed and "APPROVAL required" in printed[0]
        assert "send quarterly report" in printed[0]

        resolved = await execution_exchange.async_hot_wait_pending(execution, timeout=5)
        assert resolved is True
        await execution.async_close()
        assert outcome["decision"]["approved"] is True
    finally:
        execution_exchange.unregister_provider("console-test")


@pytest.mark.asyncio
async def test_console_provider_maps_other_input_to_denial():
    provider = ConsoleExchangeProvider(input_func=lambda prompt: "no way", print_func=lambda text: None)
    execution_exchange.register_provider("console-deny", provider, replace=True)
    try:
        flow, outcome = _gate_flow(provider_id="console-deny", wait_mode="connected")
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")
        resolved = await execution_exchange.async_hot_wait_pending(execution, timeout=5)
        assert resolved is True
        await execution.async_close()
        assert outcome["decision"]["approved"] is False
    finally:
        execution_exchange.unregister_provider("console-deny")


@pytest.mark.asyncio
async def test_host_callback_provider_publishes_and_resumes_on_host_approval():
    published: list[dict] = []
    provider = HostCallbackExchangeProvider(on_publish=published.append)
    execution_exchange.register_provider("host-test", provider, replace=True)
    try:
        flow, outcome = _gate_flow(provider_id="host-test", wait_mode="connected")
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")
        assert len(published) == 1
        exchange_id = published[0]["exchange_id"]
        assert published[0]["exchange_kind"] == "approval"
        assert provider.pending_views() and provider.pending_views()[0]["exchange_id"] == exchange_id

        async def host_approves():
            await asyncio.sleep(0.05)
            assert provider.approve(exchange_id, reason="host endpoint approved") is True

        approve_task = asyncio.create_task(host_approves())
        resolved = await execution_exchange.async_hot_wait_pending(execution, timeout=5)
        await approve_task
        assert resolved is True
        await execution.async_close()
        assert outcome["decision"]["approved"] is True
        assert provider.pending_views() == []
    finally:
        execution_exchange.unregister_provider("host-test")


@pytest.mark.asyncio
async def test_host_callback_provider_deny_produces_denied_decision():
    provider = HostCallbackExchangeProvider()
    execution_exchange.register_provider("host-deny", provider, replace=True)
    try:
        flow, outcome = _gate_flow(provider_id="host-deny", wait_mode="connected")
        execution = flow.create_execution(auto_close=False)
        await execution.async_start("go")
        pending = await provider.list_pending()
        exchange_id = str(pending[0]["exchange_id"])

        async def host_denies():
            await asyncio.sleep(0.05)
            provider.deny(exchange_id, reason="not allowed")

        deny_task = asyncio.create_task(host_denies())
        resolved = await execution_exchange.async_hot_wait_pending(execution, timeout=5)
        await deny_task
        assert resolved is True
        await execution.async_close()
        assert outcome["decision"]["approved"] is False
        assert "not allowed" in str(outcome["decision"].get("reason", ""))
    finally:
        execution_exchange.unregister_provider("host-deny")
