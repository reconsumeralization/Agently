import asyncio
from typing import Any

import pytest

from agently import Agently
from agently.base import execution_exchange, settings as global_settings


class ApprovingProvider:
    def __init__(self, response):
        self.response = response
        self.published: list[dict[str, Any]] = []

    def publish_request(self, execution_id, request, *, interrupt):
        self.published.append({"execution_id": execution_id, "request": dict(request)})
        return {"exchange_id": f"loop-ex-{ len(self.published) }"}

    async def await_response(self, request):
        await asyncio.sleep(0.01)
        return self.response


class RecordingOnlyProvider:
    def __init__(self):
        self.published: list[dict[str, Any]] = []

    def publish_request(self, execution_id, request, *, interrupt):
        self.published.append({"execution_id": execution_id, "request": dict(request)})
        return {"exchange_id": f"cold-ex-{ len(self.published) }"}


def _build_agent_with_guarded_action(calls: list[dict[str, Any]]):
    agent = Agently.create_agent()

    def delete_report(path: str) -> dict[str, Any]:
        calls.append({"path": path})
        return {"deleted": True, "path": path}

    agent.action.register_action(
        action_id="delete_report",
        desc="Delete a generated report file.",
        kwargs={"path": ("str", "file path to delete")},
        func=delete_report,
        tags=["exchange-test"],
        side_effect_level="write",
        approval_required=True,
    )
    return agent


async def _planning_handler(context, request):
    if context.get("round_index") == 0:
        return {
            "next_action": "execute",
            "use_action": True,
            "action_calls": [
                {
                    "purpose": "Delete the stale report",
                    "action_id": "delete_report",
                    "action_input": {"path": "./tmp/report.md"},
                    "todo_suggestion": "respond",
                }
            ],
        }
    return {"next_action": "response", "action_calls": []}


def _interaction_settings(**values):
    keys = ("mode", "exchange_provider", "hot_wait_timeout")
    old = {key: global_settings.get(f"interaction.{ key }", None) for key in keys}
    for key in keys:
        global_settings.set(f"interaction.{ key }", values.get(key, old[key]))
    return old


def _restore_interaction_settings(old):
    for key, value in old.items():
        global_settings.set(f"interaction.{ key }", value)


@pytest.mark.asyncio
async def test_action_loop_hot_wait_approval_executes_action_in_place():
    calls: list[dict[str, Any]] = []
    agent = _build_agent_with_guarded_action(calls)
    provider = ApprovingProvider({"status": "approved", "approved": True, "reason": "operator approved"})
    execution_exchange.register_provider("loop-hot", provider, replace=True)
    old = _interaction_settings(mode="hot", exchange_provider="loop-hot", hot_wait_timeout=5)
    old_handler = global_settings.get("policy_approval.handler", None)
    global_settings.set("policy_approval.handler", "fail_closed")
    try:
        prompt = Agently.create_prompt()
        prompt.set("input", "clean up the stale report")
        records = await agent.action.async_plan_and_execute(
            prompt=prompt,
            settings=agent.settings,
            action_list=agent.action.get_action_list(tags=["exchange-test"]),
            agent_name=agent.name,
            planning_handler=_planning_handler,
            max_rounds=3,
        )
        assert len(provider.published) == 1
        assert calls == [{"path": "./tmp/report.md"}]
        executed = [record for record in records if record.get("action_id") == "delete_report"]
        assert executed and executed[0]["status"] == "success"
        assert executed[0]["data"]["deleted"] is True
    finally:
        global_settings.set("policy_approval.handler", old_handler)
        _restore_interaction_settings(old)
        execution_exchange.unregister_provider("loop-hot")


@pytest.mark.asyncio
async def test_action_loop_durable_mode_returns_paused_records_instead_of_raising():
    calls: list[dict[str, Any]] = []
    agent = _build_agent_with_guarded_action(calls)
    provider = RecordingOnlyProvider()
    execution_exchange.register_provider("loop-cold", provider, replace=True)
    old = _interaction_settings(mode="durable", exchange_provider="loop-cold")
    old_handler = global_settings.get("policy_approval.handler", None)
    global_settings.set("policy_approval.handler", "fail_closed")
    try:
        prompt = Agently.create_prompt()
        prompt.set("input", "clean up the stale report")
        records = await agent.action.async_plan_and_execute(
            prompt=prompt,
            settings=agent.settings,
            action_list=agent.action.get_action_list(tags=["exchange-test"]),
            agent_name=agent.name,
            planning_handler=_planning_handler,
            max_rounds=3,
        )
        assert calls == []
        assert len(provider.published) == 1
        paused = [record for record in records if record.get("status") == "approval_required"]
        assert len(paused) == 1
        exchange_meta = paused[0]["meta"]["exchange"]
        assert exchange_meta["pending"][0]["kind"] == "approval"
        assert exchange_meta["pending"][0]["status"] == "pending"
        assert exchange_meta["respond_keys"]
        assert exchange_meta["execution_id"]

        # The paused execution stays live: respond in process and let the
        # loop continue to execute the approved action.
        respond_key = exchange_meta["respond_keys"][0]
        view = await execution_exchange.async_respond(
            respond_key,
            {"status": "approved", "approved": True, "reason": "late approval"},
            actor="unit-test",
        )
        assert view["status"] == "responded"
        await asyncio.sleep(0.1)
        assert calls == [{"path": "./tmp/report.md"}]
    finally:
        global_settings.set("policy_approval.handler", old_handler)
        _restore_interaction_settings(old)
        execution_exchange.unregister_provider("loop-cold")


@pytest.mark.asyncio
async def test_action_loop_without_provider_denies_instead_of_hanging_or_raising():
    calls: list[dict[str, Any]] = []
    agent = _build_agent_with_guarded_action(calls)
    old = _interaction_settings(mode="hot", exchange_provider=None, hot_wait_timeout=5)
    old_handler = global_settings.get("policy_approval.handler", None)
    global_settings.set("policy_approval.handler", "fail_closed")
    try:
        prompt = Agently.create_prompt()
        prompt.set("input", "clean up the stale report")
        records = await agent.action.async_plan_and_execute(
            prompt=prompt,
            settings=agent.settings,
            action_list=agent.action.get_action_list(tags=["exchange-test"]),
            agent_name=agent.name,
            planning_handler=_planning_handler,
            max_rounds=3,
        )
        assert calls == []
        blocked = [record for record in records if record.get("status") == "blocked"]
        assert len(blocked) == 1
        assert "No ExecutionExchange provider" in str(blocked[0].get("error", ""))
    finally:
        global_settings.set("policy_approval.handler", old_handler)
        _restore_interaction_settings(old)
