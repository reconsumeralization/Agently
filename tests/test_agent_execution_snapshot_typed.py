"""Data-only recovery probes; scripted transport is not model-quality evidence."""

import json
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest
from pydantic import BaseModel, Field, RootModel, field_validator
from test_builtin_agent_executions import (
    ScriptedExecutionRequester,
    create_execution_agent,
)
from test_field_long_content import plan

from agently import LongContent
from agently.builtins.plugins.AgentExecution import AgentExecution
from agently.core.application.AgentExecution import AgentExecutionPaused


class ChapterReport(BaseModel):
    body: LongContent
    summary: str


class Entry(BaseModel):
    day: date
    amount: Decimal


class TypedReport(BaseModel):
    validation_calls: ClassVar[int] = 0
    entries: list[Entry]
    label: str = Field(alias="heading")

    @field_validator("label")
    @classmethod
    def transform_label(cls, value: str) -> str:
        cls.validation_calls += 1
        return value + "!"


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("schema,payload", [
    (RootModel[str], "plain root"),
    (TypedReport, {"entries": [{"day": "2026-09-10", "amount": "12.50"}], "heading": "A"}),
])
async def test_snapshot_reconstructs_original_schema_once(tmp_path, monkeypatch, continuation, schema, payload):
    agent = create_execution_agent(tmp_path, "typed-restore", [json.dumps(payload)])
    policy_calls = []

    def review(value, context):
        policy_calls.append(value)
        return True

    def draft():
        result = agent.input("Return the declared output.").output(schema).auto_continue(continuation).review(review)
        assert isinstance(result, AgentExecution)
        return result

    run = draft()
    produce = run._async_produce

    async def paused_producer(options):
        candidate = await produce(options)
        await run.async_pause()
        return candidate

    monkeypatch.setattr(run, "_async_produce", paused_producer)
    with pytest.raises(AgentExecutionPaused):
        await run.async_get_data()
    snapshot = json.loads(json.dumps(run.save()))
    calls_before_load = TypedReport.validation_calls
    restored = draft()
    restored.load(snapshot)
    assert TypedReport.validation_calls == calls_before_load
    assert policy_calls == []
    assert restored._producer_result_object is None
    assert ScriptedExecutionRequester.model_dispatches == 1
    await run.async_close(pending="cancel")
    assert await restored.async_resume() == payload
    typed = await restored.async_get_data_object()
    assert isinstance(typed, schema)
    if isinstance(typed, TypedReport):
        assert typed.label == "A!"  # Restore from original data, not transformed model_dump.
        assert isinstance(typed.entries[0], Entry)
        assert typed.entries[0].day == date(2026, 9, 10)
        assert typed.entries[0].amount == Decimal("12.50")
        assert TypedReport.validation_calls == calls_before_load + 1
    else:
        assert typed.root == payload
    assert await restored.async_get_data_object() is typed
    if isinstance(typed, TypedReport):
        assert TypedReport.validation_calls == calls_before_load + 1
    assert len(policy_calls) == 1
    assert ScriptedExecutionRequester.model_dispatches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", [ChapterReport, {"body": (LongContent, "Write the body."), "summary": str}])
async def test_field_snapshot_retains_current_and_historical_typed_views(tmp_path, monkeypatch, schema):
    agent = create_execution_agent(tmp_path, "field-restore", [
        plan(), {"body": "first"}, {"summary": "first summary"},
        plan(), {"body": "second"}, {"summary": "second summary"},
    ])

    def draft():
        result = agent.input("Write a report.").output(schema).auto_continue()
        assert isinstance(result, AgentExecution)
        return result

    run = draft()
    first = await run.async_get_data()
    produce = run._async_produce

    async def paused_producer(options):
        candidate = await produce(options)
        await run.async_pause()
        return candidate

    monkeypatch.setattr(run, "_async_produce", paused_producer)
    with pytest.raises(AgentExecutionPaused):
        await run.async_rework("Revise the report.")
    snapshot = json.loads(json.dumps(run.save()))
    restored = draft()
    restored.load(snapshot)
    await run.async_close(pending="cancel")
    assert await restored.async_resume() == {"body": "second", "summary": "second summary"}
    current = await restored.async_get_data_object()
    old = await restored.get_result(revision=0).async_get_data_object()
    assert isinstance(current, BaseModel) and current.model_dump()["body"] == "second"
    assert isinstance(old, BaseModel) and old.model_dump() == first
    if schema is ChapterReport:
        assert isinstance(current, ChapterReport) and isinstance(old, ChapterReport)
    assert old is not current
    assert await restored.get_result(revision=0).async_get_data_object() is old
    assert ScriptedExecutionRequester.model_dispatches == 6
