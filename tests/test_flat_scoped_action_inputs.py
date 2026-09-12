"""Real request/Block transport checks; synthetic responses prove no model quality."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading
from typing import Any

import pytest

from agently import Agently
from agently.core import PluginManager, TaskContext
from agently.core.application.AgentTask import AgentTask
from agently.core.storage import RecordStoreContextSource
from agently.types.data import AgentlyRequestData
from agently.utils import Settings


@pytest.fixture
def scoped_setup(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    captured: list[dict[str, Any]] = []
    events: list[str] = []
    control: dict[str, Any] = {"defer": False, "ready": False, "pause": False,
        "captured_event": threading.Event(), "values": []}

    class CaptureRequester:
        name = "FlatScopedCaptureRequester"
        DEFAULT_SETTINGS: dict[str, Any] = {}

        def __init__(self, prompt: Any, settings: Any) -> None:
            self.prompt = prompt

        @staticmethod
        def _on_register() -> None:
            pass

        @staticmethod
        def _on_unregister() -> None:
            pass

        def generate_request_data(self) -> AgentlyRequestData:
            return AgentlyRequestData(client_options={}, headers={}, data={
                "messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
                request_options={"stream": True}, request_url="synthetic://scoped-input")

        async def request_model(self, request_data: Any) -> Any:
            if "selected_keys" in (self.prompt.get("output") or {}):
                events.append("context_selector")
                infos = self.prompt.get("info") or []
                infos = infos if isinstance(infos, list) else [infos]
                cards = next((item["offered_context_blocks"] for item in infos
                    if "offered_context_blocks" in item), [])
                yield "message", json.dumps({"selected_keys": [item["block_key"] for item in cards]})
                return
            events.append("narrow_request")
            captured.append({"prompt": deepcopy(self.prompt.get()), "text": self.prompt.to_text()})
            if control["pause"]:
                control["captured_event"].set()
                await asyncio.Future()
            commands = [{"purpose": "Exercise transport only", "action_id": "observe_input",
                "action_input": {"value": "PROTOCOL_VALUE"}}] if control["ready"] else []
            yield "message", json.dumps({"requires_observation": control["defer"], "action_commands": commands})

        async def broadcast_response(self, response_generator: Any) -> Any:
            text = ""
            async for _event, data in response_generator:
                text += str(data)
                yield "delta", str(data)
            yield "done", text

    settings = Settings(name="scoped-input-settings", parent=Agently.settings)
    plugins = PluginManager(settings, parent=Agently.plugin_manager, name="scoped-input-plugins")
    plugins.register("ModelRequester", CaptureRequester, activate=True)
    agent = Agently.AgentType(plugins, parent_settings=settings, name="scoped-input-test")
    agent.use_task_workspace(tmp_path / "files").use_record_store(tmp_path / "records", mode="read_write")

    def forbidden_action(value: str) -> None:
        assert control["ready"], "Only the explicit ready-command protocol test may dispatch"
        control["values"].append(value)
        events.append("protocol_action")

    agent.action.register_action(action_id="observe_input", desc="Transport test only",
        kwargs={"value": (str, "Observed value", True)}, func=forbidden_action)
    agent.use_actions("observe_input")
    context = TaskContext(task_id="scoped-input-test")
    source = RecordStoreContextSource(agent.record_store)
    original_read = source.async_read_exact

    async def read(*args: Any, **kwargs: Any) -> Any:
        events.append("source_read")
        return await original_read(*args, **kwargs)

    monkeypatch.setattr(source, "async_read_exact", read)
    context.attach(source, binding_id="cold-source", metadata={"disclosure_mode": "explicit_retrieval"})
    task = AgentTask(agent, task_id="scoped-input-test", goal="Inspect source values.",
        success_criteria=["Read before use."], execution="flat", task_context=context,
        context_budget={"chars": 10000, "optional_selection": "none"})
    plan = {"execution_shape": "actions", "required_action_ids": ["observe_input"],
        "step_instruction": "Inspect the source value.", "scoped_retrieval": {"query_groups": [{
            "query": "source", "source_kinds": ["record_store"], "expected_role": "evidence_snippet",
            "filters": {"collection": "input-source"}, "max_results": 1, "snippet_limit": 500}]}}
    legacy = {"goal": task.goal, "items": [], "omitted": [], "diagnostics": {}, "profile": "none"}
    child_inputs: list[dict[str, Any]] = []

    async def child(*_args: Any, **kwargs: Any) -> Any:
        events.append("child_enter")
        child_inputs.append(deepcopy(kwargs))
        return {"status": "failed"}, {"status": "failed", "execution_id": "synthetic-child-boundary"}

    monkeypatch.setattr(task, "_run_bounded_agent_execution_step", child)
    return task, plan, legacy, captured, events, control, child_inputs, source


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["body", "empty", "ref_only", "truncated", "deferred"])
async def test_current_scoped_read_reaches_real_narrow_request(scoped_setup: Any, mode: str) -> None:
    task, plan, legacy, captured, events, control, child_inputs, _source = scoped_setup
    if mode != "empty":
        await task.record_store.put(content="SCOPED_TRANSPORT_BODY " * (100 if mode == "truncated" else 1),
            collection="input-source", kind="note", summary="Source body remains cold until read.")
    if mode == "ref_only":
        plan["scoped_retrieval"]["query_groups"][0]["expected_role"] = "locator_ref"
    if mode == "truncated":
        plan["scoped_retrieval"]["query_groups"][0]["snippet_limit"] = 60
    control["defer"] = mode == "deferred"
    await task._execute_step(1, plan, legacy)
    assert len(captured) == 1
    inputs = captured[0]["prompt"]["input"]
    assert inputs["task_id"] == task.id
    assert inputs["goal"] == task.goal
    assert inputs["success_criteria"] == task.success_criteria
    assert inputs["iteration"] == 1
    assert inputs["bounded_step_plan"]["required_action_ids"] == ["observe_input"]
    assert "context_pack" in inputs and "repair_context" in inputs
    assert "Do not infer source content from failed, empty, or ref_only" in captured[0]["prompt"]["instruct"]
    assert "Assess [output.requires_observation]" in captured[0]["prompt"]["instruct"]
    assert "exact contracts in [info.available_actions]" in captured[0]["prompt"]["instruct"]
    assert "Do not execute Actions, guess missing values" in captured[0]["prompt"]["instruct"]
    assert inputs["scoped_retrieval_results"]
    assert "evidence_ledger" in inputs
    if mode not in {"empty", "ref_only"}:
        assert "SCOPED_TRANSPORT_BODY" in str(inputs["scoped_retrieval_results"])
        assert events.index("source_read") < events.index("narrow_request")
    if mode == "ref_only":
        assert "SCOPED_TRANSPORT_BODY" not in str(inputs["scoped_retrieval_results"])
        assert "ref_only" in str(inputs["evidence_ledger"])
    if mode == "truncated":
        assert "truncated" in str(inputs["evidence_ledger"])
        assert "SCOPED_TRANSPORT_BODY " * 100 not in captured[0]["text"]
    if mode == "empty":
        assert "SCOPED_TRANSPORT_BODY" not in captured[0]["text"]
    if mode == "deferred":
        assert len(child_inputs) == 1
        assert child_inputs[0]["scoped_retrieval_results"] == inputs["scoped_retrieval_results"]
        assert child_inputs[0]["evidence_ledger"] == inputs["evidence_ledger"]
        assert child_inputs[0]["require_step_actions"] is True
    else:
        assert child_inputs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [False, True])
async def test_no_current_read_does_not_project_or_register_ledger(
    scoped_setup: Any, monkeypatch: pytest.MonkeyPatch, prior: bool
) -> None:
    task, plan, legacy, captured, events, _control, _child_inputs, _source = scoped_setup
    plan.pop("scoped_retrieval")
    if prior:
        monkeypatch.setattr(task, "_cumulative_evidence_ledger", lambda *_a, **_k: {
            "items": [{"id": "prior", "body_state": "content", "status": "ok", "preview": "PRIOR_BODY"}]})

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("No current read must not project/register evidence before narrow")

    monkeypatch.setattr(task, "_flat_step_evidence_ledger", forbidden)
    await task._execute_step(1, plan, legacy)
    assert len(captured) == 1
    assert events == ["narrow_request"]
    inputs = captured[0]["prompt"]["input"]
    assert "scoped_retrieval_results" not in inputs
    assert "evidence_ledger" not in inputs
    assert "PRIOR_BODY" not in captured[0]["text"]


@pytest.mark.asyncio
async def test_read_cancellation_never_starts_argument_request(
    scoped_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, plan, legacy, captured, _events, _control, child_inputs, source = scoped_setup
    await task.record_store.put(content="CANCEL_BODY", collection="input-source", kind="note")

    async def cancelled(*_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    monkeypatch.setattr(source, "async_read_exact", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await task._execute_step(1, plan, legacy)
    assert captured == child_inputs == []


@pytest.mark.asyncio
async def test_failed_read_is_disclosed_without_body(
    scoped_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, plan, legacy, captured, _events, _control, child_inputs, source = scoped_setup
    await task.record_store.put(content="UNREAD_BODY", collection="input-source", kind="note")

    async def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("Synthetic source unavailable")

    monkeypatch.setattr(source, "async_read_exact", unavailable)
    await task._execute_step(1, plan, legacy)
    assert len(captured) == 1
    inputs = captured[0]["prompt"]["input"]
    assert inputs["scoped_retrieval_results"]
    assert "failed" in str(inputs["evidence_ledger"])
    assert "UNREAD_BODY" not in captured[0]["text"]
    assert child_inputs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("preplanned", [False, True])
async def test_direct_commands_retain_block_evidence_and_fixed_inputs(
    scoped_setup: Any, preplanned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, plan, legacy, captured, events, control, child_inputs, _source = scoped_setup
    await task.record_store.put(content="SCOPED_DIRECT_BODY", collection="input-source", kind="note")
    control["ready"] = True
    if preplanned:
        plan["action_commands"] = [{"purpose": "Exercise fixed command protocol", "action_id": "observe_input",
            "action_input": {"value": "FIXED_INPUT"}}]
        def forbidden_ledger(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Fixed commands must not project a parameter-request ledger")
        monkeypatch.setattr(task, "_flat_step_evidence_ledger", forbidden_ledger)
    result, meta = await task._execute_step(1, plan, legacy)
    assert result["status"] == "completed"
    assert events.count("protocol_action") == 1
    assert control["values"] == ["FIXED_INPUT" if preplanned else "PROTOCOL_VALUE"]
    assert len(captured) == (0 if preplanned else 1)
    assert child_inputs == []
    ledger = task._evidence_ledger_from_execution_meta(meta)
    assert "SCOPED_DIRECT_BODY" in str(ledger)
    if not preplanned:
        offered = captured[0]["prompt"]["input"]["evidence_ledger"]
        canonical = task._model_evidence_ledger_projection(task._cumulative_evidence_ledger(meta), max_items=64)
        ids = {item["reference_id"] for item in canonical["items"]}
        assert {item["reference_id"] for item in offered["items"]} <= ids


@pytest.mark.asyncio
async def test_no_read_deferred_ledger_keeps_post_narrow_timing(
    scoped_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, plan, legacy, captured, events, control, child_inputs, _source = scoped_setup
    plan.pop("scoped_retrieval")
    control["defer"] = True
    original = task._flat_step_evidence_ledger

    def ledger(context: Any) -> Any:
        events.append("ledger_projection")
        return original(context)

    monkeypatch.setattr(task, "_flat_step_evidence_ledger", ledger)
    await task._execute_step(1, plan, legacy)
    assert events == ["narrow_request", "ledger_projection", "child_enter"]
    assert "evidence_ledger" not in captured[0]["prompt"]["input"]
    assert len(child_inputs) == 1


@pytest.mark.asyncio
async def test_narrow_cancellation_after_read_does_not_start_child(scoped_setup: Any) -> None:
    task, plan, legacy, captured, events, control, child_inputs, _source = scoped_setup
    await task.record_store.put(content="CANCEL_NARROW_BODY", collection="input-source", kind="note")
    control["pause"] = True
    pending = asyncio.create_task(task._execute_step(1, plan, legacy))
    async def wait_capture() -> None:
        while not control["captured_event"].is_set():
            await asyncio.sleep(0.001)
    try:
        await asyncio.wait_for(wait_capture(), timeout=10)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert len(captured) == 1
    assert "CANCEL_NARROW_BODY" in str(captured[0]["prompt"]["input"]["scoped_retrieval_results"])
    assert "protocol_action" not in events
    assert child_inputs == []
