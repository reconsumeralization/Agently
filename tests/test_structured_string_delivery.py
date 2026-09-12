"""Synthetic native graph/storage acceptance, not observed model generation."""

import json

import pytest
from pydantic import BaseModel, Field, RootModel
from test_builtin_agent_executions import (
    ScriptedExecutionRequester,
    create_execution_agent,
)

from agently.builtins.plugins.AgentExecution.modules import long_output as native
from agently.builtins.plugins.AgentExecution.modules.execution import AgentExecution


class Initial:
    id = "synthetic-prefix"
    response_id = id

    def __init__(self, raw):
        self.raw = raw

    async def async_get_text(self):
        return self.raw


async def delivery(tmp_path, schema, raw, responses=(), validator=None):
    execution = create_execution_agent(
        tmp_path, "integrated-probe", list(responses)
    ).input("Complete the declared result.")
    if schema is not None:
        execution.output(schema, format="json")
    assert isinstance(execution, AgentExecution)
    owner = native.LongOutputDelivery(
        execution,
        ensure_keys=None,
        ensure_all_keys=True,
        validate_handler=validator,
        key_style="dot",
        max_retries=0,
        raise_ensure_failure=True,
    )
    owner.preflight()
    await owner.accept_initial(Initial(raw), streaming_events=[])
    return owner


def packet(updates, completion="complete"):
    return {
        "updates": {
            key: {"value": value, "is_complete": closed}
            for key, (value, closed) in updates.items()
        },
        "completion": completion,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema,raw,key,suffix,expected",
    [
        (RootModel[str], '"pre', "p0:$", "fix", "prefix"),
        (
            {"a.b": {"x[0]": str}},
            '{"a.b":{"x[0]":"pre',
            "p0:a.b.x[0]",
            "fix",
            {"a.b": {"x[0]": "prefix"}},
        ),
        (
            {"items": [str]},
            '{"items":["one","tw',
            "p0:items[1]",
            "o",
            {"items": ["one", "two"]},
        ),
    ],
)
async def test_native_graph_and_verified_replay(
    tmp_path, schema, raw, key, suffix, expected
):
    owner = await delivery(tmp_path, schema, raw, [packet({key: (suffix, True)})])
    assert await owner.run_continuation_flow() == expected
    assert owner.replayed_unit_count == 2
    assert owner.meta["request_count"] == 2
    assert owner.meta["transport_complete"]
    assert await owner._read_verified_ref(owner.final_ref, label="final") == (
        native._canonical_json(expected) if owner.structured else expected
    )


@pytest.mark.asyncio
async def test_multiblock_and_independent_fields(tmp_path):
    class Document(BaseModel):
        body: str = Field(min_length=9000, max_length=10000)
        note: str

    owner = await delivery(
        tmp_path,
        Document,
        '{"body":"' + "a" * 3000,
        [
            packet(
                {"p0:body": ("b" * 3000, False), "p1:note": ("done", True)},
                "incomplete",
            ),
            packet({"p0:body": ("c" * 3000, True)}),
        ],
    )
    result = await owner.run_continuation_flow()
    assert result == {"body": "a" * 3000 + "b" * 3000 + "c" * 3000, "note": "done"}
    assert len(ScriptedExecutionRequester.requests) == 2


@pytest.mark.asyncio
async def test_bad_packet_recovery_is_bounded_and_preserves_prefix(tmp_path):
    bad = json.dumps(packet({"p0:body": ("discard", True)})) + "\n</think>"
    owner = await delivery(
        tmp_path,
        {"body": str},
        '{"body":"pre',
        [bad, packet({"p0:body": ("fix", True)})],
    )
    assert await owner.run_continuation_flow() == {"body": "prefix"}
    assert len(owner.units) == 2
    assert owner.meta["no_progress_event_count"] == 1
    assert len(ScriptedExecutionRequester.requests) == 2


@pytest.mark.asyncio
async def test_three_invalid_packets_stop(tmp_path):
    owner = await delivery(tmp_path, {"body": str}, '{"body":"pre', ["invalid"] * 3)
    with pytest.raises(Exception, match="no durable progress"):
        await owner.run_continuation_flow()
    assert owner.value == {"body": "pre"}
    assert len(owner.units) == 1
    assert len(ScriptedExecutionRequester.requests) == 3


@pytest.mark.asyncio
async def test_empty_final_confirmation(tmp_path):
    owner = await delivery(
        tmp_path, {"body": str}, '{"body":"done', [packet({"p0:body": ("", True)})]
    )
    assert await owner.run_continuation_flow() == {"body": "done"}
    assert not owner.execution.diagnostics.get("long_output_no_progress")


@pytest.mark.asyncio
async def test_stopped_partial_envelope_discards_untrusted_increment(tmp_path):
    partial = (
        '{"updates":{"p0:body":{"value":"fix","is_complete":false}},"completion":"incom'
    )
    owner = await delivery(
        tmp_path,
        {"body": str},
        '{"body":"pre',
        [partial, packet({"p0:body": (" done", True)})],
    )
    assert await owner.run_continuation_flow() == {"body": "pre done"}
    assert len(owner.units) == 2


@pytest.mark.asyncio
async def test_early_completion_does_not_finish_open_field(tmp_path):
    owner = await delivery(
        tmp_path, {"body": str}, '{"body":"pre', [packet({"p0:body": ("fix", False)})]
    )
    with pytest.raises(native._FinalValidationError, match="Open fields"):
        await owner.run_continuation_flow()


@pytest.mark.asyncio
async def test_closed_field_is_not_reopened(tmp_path):
    owner = await delivery(tmp_path, {"a": str, "b": str}, '{"a":"closed","b":"pre')
    assert owner._field_continuation is not None
    assert owner._field_continuation.field_state is not None
    assert all(
        slot.path != ("a",) for slot in owner._field_continuation.field_state.slots
    )


@pytest.mark.asyncio
async def test_state_tampering_is_not_accepted_as_replay(tmp_path):
    owner = await delivery(
        tmp_path, {"body": str}, '{"body":"pre', [packet({"p0:body": ("fix", True)})]
    )
    await owner.request_and_commit_next()
    assert owner._field_continuation is not None
    assert owner._field_continuation.field_state is not None
    owner._field_continuation.field_state.closed.clear()
    with pytest.raises(native.LongOutputError, match="In-memory state"):
        await owner.materialize_and_validate()


@pytest.mark.asyncio
async def test_corrupt_readback_never_succeeds(tmp_path):
    owner = await delivery(
        tmp_path, {"body": str}, '{"body":"pre', [packet({"p0:body": ("fix", True)})]
    )
    await owner.request_and_commit_next()
    ref = owner.units[0]["ref"]
    await owner.execution.task_workspace.write_file(ref["path"], "corrupt")
    with pytest.raises(native.LongOutputError, match="replay mismatch"):
        await owner.materialize_and_validate()


@pytest.mark.asyncio
async def test_original_custom_validation_still_runs(tmp_path):
    observed = []

    def validate(data, context):
        observed.append(data)
        return True

    owner = await delivery(
        tmp_path,
        {"body": str},
        '{"body":"pre',
        [packet({"p0:body": ("fix", True)})],
        validate,
    )
    assert await owner.run_continuation_flow() == {"body": "prefix"}
    assert observed == [{"body": "prefix"}]


@pytest.mark.asyncio
async def test_undetermined_stops_without_redundant_request(tmp_path):
    owner = await delivery(
        tmp_path, {"body": str}, '{"body":"pre', [packet({}, "undetermined")]
    )
    with pytest.raises(native.LongOutputError, match="undetermined"):
        await owner.run_continuation_flow()
    assert owner.value == {"body": "pre"}
    assert len(ScriptedExecutionRequester.requests) == 1
    assert len(owner.units) == 1
