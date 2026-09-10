"""Synthetic API/transport checks; these do not evaluate model prose quality."""

import inspect
import json
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel

from agently.builtins.plugins.AgentExecution import AgentExecution as BuiltinExecution
from agently.builtins.plugins.AgentExecution.modules.snapshot import _fingerprint
from agently.core.application.AgentExecution import AgentExecutionPaused
from agently.types.plugins import AgentExecution
from test_agent_execution_compatibility import (
    MockAgentExecutionLongOutputRequester,
    _create_long_output_test_agent,
    _get_long_output_meta,
)
from test_builtin_agent_executions import ScriptedExecutionRequester, create_execution_agent


Spelling = Literal["auto_continue", "ensure_long_output"]


def configure(execution: AgentExecution, spelling: Spelling, enabled: bool = True) -> AgentExecution:
    if spelling == "auto_continue":
        return execution.auto_continue(enabled)
    return execution.ensure_long_output(enabled)


def test_auto_continue_defaults_off_and_aliases_share_one_draft_flag(tmp_path: Path) -> None:
    agent = create_execution_agent(tmp_path, "continuation-policy", [])
    draft = agent.input("Write.")
    assert not cast(BuiltinExecution, draft)._ensure_long_output_enabled
    assert draft.auto_continue() is draft
    assert cast(BuiltinExecution, draft)._ensure_long_output_enabled
    assert draft.ensure_long_output(False) is draft
    assert not cast(BuiltinExecution, draft)._ensure_long_output_enabled
    assert draft.ensure_long_output() is draft
    assert draft.auto_continue(False) is draft
    assert not cast(BuiltinExecution, draft)._ensure_long_output_enabled
    draft.auto_continue()
    assert not cast(BuiltinExecution, agent.input("New request."))._ensure_long_output_enabled
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_legacy_spelling_delegates_to_canonical_method(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = create_execution_agent(tmp_path, "continuation-alias", []).input("Write.")
    calls: list[bool] = []

    def replacement(self: BuiltinExecution, enabled: bool = True) -> BuiltinExecution:
        calls.append(enabled)
        return self

    monkeypatch.setattr(BuiltinExecution, "auto_continue", replacement)
    assert draft.ensure_long_output(False) is draft
    assert draft.ensure_long_output() is draft
    assert calls == [False, True]


@pytest.mark.parametrize("spelling", ["auto_continue", "ensure_long_output"])
def test_normal_completion_and_all_readers_share_one_request(tmp_path: Path, spelling: Spelling) -> None:
    class Reply(BaseModel):
        reply: str

    agent = create_execution_agent(tmp_path, "continuation-fast-path", [{"reply": "done"}])
    draft = configure(agent.input("Write.").output(Reply), spelling)
    assert draft.get_data() == {"reply": "done"}
    assert json.loads(draft.get_text()) == {"reply": "done"}
    assert isinstance(draft.get_data_object(), Reply)
    assert draft.get_full_data() == {"reply": "done"}
    list(draft.get_generator(type="delta"))
    assert _get_long_output_meta(draft)["request_count"] == 1
    assert ScriptedExecutionRequester.model_dispatches == 1
    for method in (draft.auto_continue, draft.ensure_long_output):
        with pytest.raises(RuntimeError, match="one independent run"):
            method(False)


@pytest.mark.parametrize("spelling", ["auto_continue", "ensure_long_output"])
def test_both_spellings_use_existing_lossless_continuation(tmp_path: Path, spelling: Spelling) -> None:
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester, "continuation-shared-flow"
    ).use_task_workspace(tmp_path)
    draft = configure(agent.input("Write a document."), spelling)
    assert draft.get_text() == "alpha-omega"
    assert draft.get_data() == "alpha-omega"
    assert "base_revision" not in "".join(draft.get_generator(type="delta"))
    meta = _get_long_output_meta(draft)
    assert meta["request_count"] == 2
    assert meta["replayed_unit_count"] == 2
    assert meta["transport_complete"] is True
    assert MockAgentExecutionLongOutputRequester.attempts == 2


def test_spelling_does_not_change_snapshot_fingerprint(tmp_path: Path) -> None:
    draft = create_execution_agent(tmp_path, "continuation-snapshot", []).input("Write.")
    owner = cast(BuiltinExecution, draft)
    disabled = _fingerprint(owner)
    draft.ensure_long_output()
    enabled = _fingerprint(owner)
    assert enabled != disabled
    draft.auto_continue(False)
    assert _fingerprint(owner) == disabled
    draft.auto_continue()
    assert _fingerprint(owner) == enabled


def test_auto_continue_has_an_explicit_protocol_and_typed_fluent_signature() -> None:
    for owner in (AgentExecution, BuiltinExecution):
        signature = inspect.signature(owner.auto_continue)
        assert signature.parameters["enabled"].default is True
        assert signature.parameters["enabled"].annotation in (bool, "bool")
        assert signature.return_annotation is not inspect.Signature.empty


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["auto_continue", "ensure_long_output"])
async def test_safe_pause_snapshot_loads_with_other_spelling(tmp_path: Path, source: Spelling) -> None:
    agent = create_execution_agent(tmp_path, "continuation-pause", [{"reply": "done"}])
    original = configure(agent.input("Write.").output({"reply": str}), source)
    await original.async_pause()
    try:
        with pytest.raises(AgentExecutionPaused):
            await original.async_run()
        snapshot = json.loads(json.dumps(original.save()))
        target: Spelling = "ensure_long_output" if source == "auto_continue" else "auto_continue"
        restored = configure(agent.input("Write.").output({"reply": str}), target)
        restored.load(snapshot)
        assert restored.id == original.id
        assert ScriptedExecutionRequester.model_dispatches == 0
        await original.async_close(pending="cancel")
        assert await restored.async_resume() == {"reply": "done"}
        assert _get_long_output_meta(restored)["request_count"] == 1
        assert ScriptedExecutionRequester.model_dispatches == 1
    finally:
        if original.status == "paused":
            await original.async_close(pending="cancel")
