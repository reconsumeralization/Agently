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
        assert executed and executed[0].get("status") == "success"
        executed_data = executed[0].get("data")
        assert isinstance(executed_data, dict)
        assert executed_data.get("deleted") is True
    finally:
        global_settings.set("policy_approval.handler", old_handler)
        _restore_interaction_settings(old)
        execution_exchange.unregister_provider("loop-hot")


@pytest.mark.asyncio
async def test_action_loop_durable_mode_returns_paused_records_instead_of_raising(monkeypatch):
    calls: list[dict[str, Any]] = []
    agent = _build_agent_with_guarded_action(calls)
    provider = RecordingOnlyProvider()
    execution_exchange.register_provider("loop-cold", provider, replace=True)
    old = _interaction_settings(mode="durable", exchange_provider="loop-cold")
    old_handler = global_settings.get("policy_approval.handler", None)
    global_settings.set("policy_approval.handler", "fail_closed")
    original_normalize = agent.action._normalize_execution_records
    original_release = agent.action._release_artifact_scope
    registered_scope: dict[str, str] | None = None
    release_calls: list[dict[str, str]] = []

    def normalize_with_temporary_artifact(records, commands, *, artifact_scope=None):
        nonlocal registered_scope
        if registered_scope is None and artifact_scope is not None:
            registered_scope = dict(artifact_scope)
            agent.action._artifact_manager.register_execution_artifact(
                action_call_id="pre-pause-output",
                artifact_type="action_output",
                label="Temporary pre-pause output",
                value={"body": "p" * (1024 * 1024)},
                artifact_scope=artifact_scope,
            )
        return original_normalize(records, commands, artifact_scope=artifact_scope)

    def record_release(artifact_scope):
        release_calls.append(dict(artifact_scope))
        return original_release(artifact_scope)

    monkeypatch.setattr(agent.action, "_normalize_execution_records", normalize_with_temporary_artifact)
    monkeypatch.setattr(agent.action, "_release_artifact_scope", record_release)
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
        paused_meta = paused[0].get("meta")
        assert isinstance(paused_meta, dict)
        exchange_meta = paused_meta["exchange"]
        assert exchange_meta["pending"][0]["kind"] == "approval"
        assert exchange_meta["pending"][0]["status"] == "pending"
        assert exchange_meta["respond_keys"]
        assert exchange_meta["execution_id"]
        assert agent.action._artifact_manager._artifacts
        assert release_calls == []

        # The paused execution stays live: respond in process and let the
        # loop continue to execute the approved action.
        respond_key = exchange_meta["respond_keys"][0]
        view = await execution_exchange.async_respond(
            respond_key,
            {"status": "approved", "approved": True, "reason": "late approval"},
            actor="unit-test",
        )
        assert view.get("status") == "responded"
        for _ in range(50):
            if not agent.action._artifact_manager._artifacts:
                break
            await asyncio.sleep(0.01)
        assert calls == [{"path": "./tmp/report.md"}]
        assert agent.action._artifact_manager._artifacts == {}
        assert release_calls == [registered_scope]
    finally:
        global_settings.set("policy_approval.handler", old_handler)
        _restore_interaction_settings(old)
        execution_exchange.unregister_provider("loop-cold")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_path", ["abandon", "direct_close"])
async def test_action_loop_durable_abandon_releases_paused_artifact_scope(monkeypatch, terminal_path):
    calls: list[dict[str, Any]] = []
    agent = _build_agent_with_guarded_action(calls)
    provider = RecordingOnlyProvider()
    execution_exchange.register_provider("loop-abandon", provider, replace=True)
    old = _interaction_settings(mode="durable", exchange_provider="loop-abandon")
    old_handler = global_settings.get("policy_approval.handler", None)
    global_settings.set("policy_approval.handler", "fail_closed")
    original_normalize = agent.action._normalize_execution_records
    original_release = agent.action._release_artifact_scope
    release_calls: list[dict[str, str]] = []
    registered = False

    def normalize_with_temporary_artifact(records, commands, *, artifact_scope=None):
        nonlocal registered
        if not registered and artifact_scope is not None:
            registered = True
            agent.action._artifact_manager.register_execution_artifact(
                action_call_id="pre-abandon-output",
                artifact_type="action_output",
                label="Temporary pre-abandon output",
                value={"body": "a" * (1024 * 1024)},
                artifact_scope=artifact_scope,
            )
        return original_normalize(records, commands, artifact_scope=artifact_scope)

    def record_release(artifact_scope):
        release_calls.append(dict(artifact_scope))
        return original_release(artifact_scope)

    monkeypatch.setattr(agent.action, "_normalize_execution_records", normalize_with_temporary_artifact)
    monkeypatch.setattr(agent.action, "_release_artifact_scope", record_release)
    try:
        records = await agent.action.async_plan_and_execute(
            prompt=Agently.create_prompt().set("input", "clean up the stale report"),
            settings=agent.settings,
            action_list=agent.action.get_action_list(tags=["exchange-test"]),
            agent_name=agent.name,
            planning_handler=_planning_handler,
            max_rounds=3,
        )
        paused = next(record for record in records if record.get("status") == "approval_required")
        exchange_meta = paused.get("meta", {})["exchange"]
        respond_key = exchange_meta["respond_keys"][0]
        assert agent.action._artifact_manager._artifacts

        if terminal_path == "abandon":
            await execution_exchange.async_abandon(respond_key, reason="unit-test abandonment")
        else:
            live_wait = execution_exchange.get_live_wait(respond_key)
            assert live_wait is not None
            await live_wait["execution"].async_close(
                reason="unit-test direct close",
                pending_interrupts="cancel",
            )
        for _ in range(50):
            if not agent.action._artifact_manager._artifacts:
                break
            await asyncio.sleep(0.01)

        assert agent.action._artifact_manager._artifacts == {}
        assert len(release_calls) == 1
        assert execution_exchange.get_live_wait(respond_key) is None
        assert calls == []
    finally:
        global_settings.set("policy_approval.handler", old_handler)
        _restore_interaction_settings(old)
        execution_exchange.unregister_provider("loop-abandon")


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


@pytest.mark.asyncio
async def test_action_loop_projects_exchange_items_onto_owning_agent_execution():
    """Slice F: pending/resolved exchanges reach the AgentExecution stream.

    The action loop notifies the bound AgentExecutionContext at each exchange
    moment; the orchestrator republishes them as instant stream items with
    meta.stream_kind="exchange" carrying normalized ExecutionExchangeView
    payloads.
    """
    from agently.core.application.AgentExecution import AgentExecutionContext
    from agently.core.runtime.RuntimeContext import bind_runtime_context

    calls: list[dict[str, Any]] = []
    agent = _build_agent_with_guarded_action(calls)
    provider = ApprovingProvider({"status": "approved", "approved": True, "reason": "operator approved"})
    execution_exchange.register_provider("loop-projection", provider, replace=True)
    old = _interaction_settings(mode="hot", exchange_provider="loop-projection", hot_wait_timeout=5)
    old_handler = global_settings.get("policy_approval.handler", None)
    global_settings.set("policy_approval.handler", "fail_closed")

    notifications: list[dict[str, Any]] = []
    context = AgentExecutionContext(execution_id="exchange-projection", lineage={}, limits={})

    async def record_exchange(action: str, exchanges: list[dict[str, Any]], meta: dict[str, Any]):
        notifications.append({"action": action, "exchanges": exchanges, "meta": meta})

    context.set_exchange_callback(record_exchange)
    try:
        prompt = Agently.create_prompt()
        prompt.set("input", "clean up the stale report")
        with bind_runtime_context(agent_execution_context=context):
            records = await agent.action.async_plan_and_execute(
                prompt=prompt,
                settings=agent.settings,
                action_list=agent.action.get_action_list(tags=["exchange-test"]),
                agent_name=agent.name,
                planning_handler=_planning_handler,
                max_rounds=3,
            )
        executed = [record for record in records if record.get("action_id") == "delete_report"]
        assert executed and executed[0].get("status") == "success"

        actions = [item["action"] for item in notifications]
        assert actions == ["pending", "resolved"]
        pending_views = notifications[0]["exchanges"]
        assert pending_views and pending_views[0].get("kind") == "approval"
        assert pending_views[0].get("status") == "pending"
        resolved_views = notifications[1]["exchanges"]
        assert resolved_views and resolved_views[0].get("status") == "responded"
        assert notifications[1]["meta"].get("interrupt_id") == resolved_views[0].get("interrupt_id")
    finally:
        global_settings.set("policy_approval.handler", old_handler)
        _restore_interaction_settings(old)
        execution_exchange.unregister_provider("loop-projection")


@pytest.mark.asyncio
async def test_agent_execution_publishes_exchange_stream_items():
    """Orchestrator wiring: the exchange callback lands typed stream items."""
    agent = Agently.create_agent()
    execution = agent.create_execution().input("noop")
    view = {
        "exchange_id": "ex-1",
        "interrupt_id": "policy:demo",
        "execution_id": "flow-1",
        "kind": "approval",
        "status": "pending",
    }
    await execution.execution_context.async_notify_exchange("pending", [view])
    exchange_items = [
        item
        for item in execution.stream.items
        if (item.meta or {}).get("stream_kind") == "exchange"
    ]
    assert len(exchange_items) == 1
    item = exchange_items[0]
    assert item.path == "exchange.pending"
    assert item.value["action"] == "pending"
    assert item.value["exchanges"] == [view]
    assert (item.meta or {}).get("exchange_action") == "pending"
