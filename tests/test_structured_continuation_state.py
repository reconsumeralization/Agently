"""D13 synthetic protocol preflight; no semantic model evidence."""

import json

import pytest
from pydantic import BaseModel, Field, RootModel
from test_builtin_agent_executions import create_execution_agent

from agently.builtins.plugins.AgentExecution.modules import long_output as native
from agently.builtins.plugins.AgentExecution.modules.execution import AgentExecution
from agently.builtins.plugins.AgentExecution.modules.structured_continuation_state import (
    StructuredContinuationState as GeneralProbe,
)


def probe(tmp_path, schema, raw):
    execution = create_execution_agent(tmp_path, "general-probe", []).input(
        "Complete the declared result."
    )
    if schema is not None:
        execution.output(schema, format="json")
    assert isinstance(execution, AgentExecution)
    owner = native.LongOutputDelivery(
        execution,
        ensure_keys=None,
        ensure_all_keys=True,
        validate_handler=None,
        key_style="dot",
        max_retries=0,
        raise_ensure_failure=True,
    )
    owner.preflight()
    return GeneralProbe(owner, raw)


def update(owner, values, completion="complete"):
    owner.request()
    keys = {slot.path: slot.key for slot in owner.slots}
    return owner.accept(
        {
            "updates": {
                keys[path]: {"value": value, "is_complete": closed}
                for path, (value, closed) in values.items()
            },
            "completion": completion,
        }
    )


@pytest.mark.parametrize(
    "schema,raw,values,expected",
    [
        (None, "pre", {(): ("fix", True)}, "prefix"),
        (str, '"pre', {(): ("fix", True)}, "prefix"),
        (RootModel[str], '"pre', {(): ("fix", True)}, "prefix"),
        (
            {"a.b": {"x[0]": str}},
            '{"a.b":{"x[0]":"pre',
            {("a.b", "x[0]"): ("fix", True)},
            {"a.b": {"x[0]": "prefix"}},
        ),
        (
            {"items": [str]},
            '{"items":["first","se',
            {("items", 1): ("cond", True)},
            {"items": ["first", "second"]},
        ),
        (
            {"items": [{"title": str, "body": str}]},
            '{"items":[{"title":"first","body":"pre',
            {("items", 0, "body"): ("fix", True)},
            {"items": [{"title": "first", "body": "prefix"}]},
        ),
        ({"items": [str]}, "{", {("items",): ([], True)}, {"items": []}),
    ],
)
def test_shapes(tmp_path, schema, raw, values, expected):
    owner = probe(tmp_path, schema, raw)
    assert update(owner, values)["state"] == expected


def test_new_array_items_after_partial_item(tmp_path):
    class Item(BaseModel):
        body: str = Field(min_length=5)
        ready: bool

    class Document(BaseModel):
        items: list[Item] = Field(min_length=2, max_length=2)
        note: str

    owner = probe(tmp_path, Document, '{"items":[{"body":"pre')
    update(
        owner,
        {("items", 0, "body"): ("fix", True), ("items", 0, "ready"): (True, True)},
        "incomplete",
    )
    update(
        owner,
        {
            ("items", 1, "body"): ("second", True),
            ("items", 1, "ready"): (False, True),
            ("note",): ("done", True),
        },
    )
    assert owner.state == {
        "items": [
            {"body": "prefix", "ready": True},
            {"body": "second", "ready": False},
        ],
        "note": "done",
    }
    owner.request()
    assert not owner.slots


@pytest.mark.parametrize("char", ['"', "\\", "/", "\n", "\t", "\b", "中", "😀", "A"])
def test_every_escape_cut(tmp_path, char):
    encoded = json.dumps(char, ensure_ascii=True)[1:-1]
    if char == "A":
        encoded = "\\u0041"
    for end in range(1, len(encoded)):
        owner = probe(tmp_path, {"body": str}, '{"body":"pre' + encoded[:end])
        assert update(owner, {("body",): (char + "fix", True)})["state"] == {
            "body": "pre" + char + "fix"
        }


def test_bad_escape_never_changes_state(tmp_path):
    owner = probe(tmp_path, {"body": str}, '{"body":"pre\\u4f')
    before = owner.snapshot()
    with pytest.raises(ValueError, match="escape"):
        update(owner, {("body",): ("错", True)})
    assert owner.snapshot() == before


def test_multiple_long_fields(tmp_path):
    class Document(BaseModel):
        a: str = Field(min_length=9000, max_length=10000, pattern="^甲")
        b: str

    owner = probe(tmp_path, Document, '{"a":"甲' + "乙" * 2999)
    update(owner, {("a",): ("丙" * 3000, False), ("b",): ("done", True)}, "incomplete")
    owner.request()
    assert [slot.path for slot in owner.slots] == [("a",)]
    update(owner, {("a",): ("丁" * 3000, True)})
    assert isinstance(owner.state, dict)
    assert len(owner.state["a"]) == 9000


def test_array_index_never_skips_partial(tmp_path):
    owner = probe(tmp_path, {"items": [{"body": str}]}, '{"items":[{"body":"pre')
    owner.request()
    assert [slot.path for slot in owner.slots] == [("items", 0, "body")]


def test_replayed_response_rejected(tmp_path):
    owner = probe(tmp_path, {"body": str}, '{"body":"pre')
    owner.request()
    payload = {
        "updates": {owner.slots[0].key: {"value": "fix", "is_complete": True}},
        "completion": "complete",
    }
    owner.accept(payload)
    with pytest.raises(ValueError, match="request-bound"):
        owner.accept(payload)


def test_completion_without_added_text(tmp_path):
    owner = probe(tmp_path, {"body": str}, '{"body":"Already complete.')
    assert update(owner, {("body",): ("", True)})["is_final"]


def test_incomplete_field_blocks_global_complete(tmp_path):
    owner = probe(tmp_path, {"body": str}, '{"body":"pre')
    with pytest.raises(ValueError, match="Open fields"):
        update(owner, {("body",): ("fix", False)})


def test_stale_snapshot_rejected(tmp_path):
    owner = probe(tmp_path, {"body": str}, '{"body":"pre')
    owner.request()
    owner.pending[("body",)] = "\\"
    with pytest.raises(ValueError, match="Stale"):
        owner.accept({"updates": {}, "completion": "complete"})


def test_missing_array_initialization_precedes_item_updates(tmp_path):
    owner = probe(tmp_path, {"items": [str]}, "{")
    update(owner, {("items", 0): ("one", True), ("items",): ([], True)})
    assert owner.state == {"items": ["one"]}
    owner.request()
    assert all(slot.path != ("items",) for slot in owner.slots)
