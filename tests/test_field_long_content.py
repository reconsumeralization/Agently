"""Synthetic producer/consumer handoff tests, not model prose evaluation."""

import json

import pytest
from pydantic import BaseModel, Field
from test_builtin_agent_executions import (
    ScriptedExecutionRequester,
    create_execution_agent,
)

LONG = (str, "Write the requested long-form field.", True, {"long_content": True})


def plan(title="Field", chapters=1):
    return {
        "document_title": title,
        "part_plan": [
            {"part_title": f"Chapter {index}", "part_brief": f"Contribution {index}"}
            for index in range(chapters)
        ],
    }


def test_field_body_then_summary_uses_actual_prose(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "field-actual",
        [
            {"title": "Fixed title"},
            plan(chapters=2),
            {"body": "First actual paragraph."},
            {"summary": "Actual first contribution."},
            {"body": "Second actual paragraph."},
            {"summary": "Final business summary."},
        ],
    )
    seen = []
    execution = agent.input("Write a report.").output(
        {"title": str, "body": LONG, "summary": str}
    )
    execution.validate(lambda data, context: seen.append(data) or True)
    expected = {
        "title": "Fixed title",
        "body": "First actual paragraph.\n\nSecond actual paragraph.",
        "summary": "Final business summary.",
    }
    assert execution.get_data() == expected
    assert seen == [expected]
    assert json.loads(execution.get_text()) == expected
    typed = execution.get_data_object()
    assert typed is not None
    assert typed.model_dump() == expected
    assert len(ScriptedExecutionRequester.requests) == 6
    assert (
        ScriptedExecutionRequester.requests[-1]["input"]["accepted_fields"]["body"]
        == expected["body"]
    )
    assert ScriptedExecutionRequester.requests[-1]["output"] == {"summary": str}
    assert len(execution.diagnostics["long_content_fields"]) == 1


def test_pydantic_metadata_preserves_original_constraints_and_return_type(tmp_path):
    class Report(BaseModel):
        body: str = Field(min_length=5, json_schema_extra={"long_content": True})
        summary: str

    agent = create_execution_agent(
        tmp_path,
        "field-pydantic",
        [plan(), {"body": "Actual content."}, {"summary": "Actual summary."}],
    )
    execution = agent.input("Write.").output(Report)
    assert isinstance(execution.get_data_object(), Report)
    assert execution.get_data()["body"] == "Actual content."
    assert len(ScriptedExecutionRequester.requests) == 3


def test_nested_arrays_bind_actual_indices_and_completed_siblings(tmp_path):
    class Item(BaseModel):
        body: str = Field(json_schema_extra={"long_content": True})
        summary: str

    class Report(BaseModel):
        items: list[Item] = Field(min_length=2, max_length=2)

    responses = [
        {"items": ["First item scope", "Second item scope"]},
        plan("First"),
        {"body": "First body"},
        {"summary": "First summary"},
        plan("Second"),
        {"body": "Second body"},
        {"summary": "Second summary"},
    ]
    execution = (
        create_execution_agent(tmp_path, "field-array", responses)
        .input("Write two items.")
        .output(Report)
    )
    assert execution.get_data() == {
        "items": [
            {"body": "First body", "summary": "First summary"},
            {"body": "Second body", "summary": "Second summary"},
        ]
    }
    inputs = [request["input"] for request in ScriptedExecutionRequester.requests]
    assert inputs[4]["array_item_plans"][0]["index"] == 1
    assert inputs[4]["accepted_fields"]["items"][0]["body"] == "First body"
    refs = [
        record["content"]["drafts"][0]["body_ref"]["path"]
        for record in execution.diagnostics["long_content_fields"]
    ]
    assert len(set(refs)) == 2


@pytest.mark.parametrize(
    "declaration",
    [(int, "", True, {"long_content": True}), (str, "", True, {"long_content": "yes"})],
)
def test_bad_marker_rejected_before_dispatch(tmp_path, declaration):
    execution = (
        create_execution_agent(tmp_path, "bad-field", [])
        .input("Write.")
        .output({"body": declaration})
    )
    with pytest.raises(ValueError, match="long_content"):
        execution.get_data()
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_plain_string_remains_one_request(tmp_path):
    execution = (
        create_execution_agent(tmp_path, "plain-field", [{"body": "ordinary"}])
        .input("Write.")
        .output({"body": str})
    )
    assert execution.get_data() == {"body": "ordinary"}
    assert ScriptedExecutionRequester.model_dispatches == 1


def test_field_original_constraint_is_not_silently_relaxed(tmp_path):
    class Report(BaseModel):
        body: str = Field(min_length=40, json_schema_extra={"long_content": True})

    execution = (
        create_execution_agent(
            tmp_path, "short-field", [plan(), {"body": "Too short."}]
        )
        .input("Write.")
        .output(Report)
    )
    with pytest.raises(ValueError, match="at least 40"):
        execution.get_data()
    assert ScriptedExecutionRequester.model_dispatches == 2


def test_empty_array_does_not_generate_phantom_field(tmp_path):
    execution = (
        create_execution_agent(tmp_path, "empty-field-array", [{"items": []}])
        .input("No items needed.")
        .output({"items": [{"body": LONG}]})
    )
    assert execution.get_data() == {"items": []}
    assert ScriptedExecutionRequester.model_dispatches == 1
