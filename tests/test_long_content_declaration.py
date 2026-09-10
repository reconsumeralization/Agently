"""Typed and legacy declaration forms share one output production contract."""

from typing import Annotated

import pytest
from pydantic import BaseModel, Field, TypeAdapter
from test_builtin_agent_executions import (
    ScriptedExecutionRequester,
    create_execution_agent,
)
from test_field_long_content import plan

from agently import Agently, LongContent
from agently.types import LongContent as NamespacedLongContent
from agently.types.data import LongContent as DataLongContent


def test_public_type_is_string_annotation_and_has_consistent_exports():
    assert LongContent is NamespacedLongContent is DataLongContent
    assert TypeAdapter(LongContent).validate_python("text") == "text"
    assert TypeAdapter(LongContent).json_schema()["type"] == "string"


def test_typed_and_legacy_nested_forms_render_identically_without_mutation():
    snapshots = []
    for kind in [LongContent, "long_content"]:
        declaration = {
            "items": [{"body": (kind, "正文", "not_null")}],
            "note": (str, "long_content is just description text"),
        }
        original = repr(declaration)
        prompt = Agently.create_agent().create_request().output(declaration).prompt
        snapshots.append(
            (prompt.to_text(), prompt.to_output_model().model_json_schema())
        )
        assert repr(declaration) == original
        assert "FieldInfo" not in prompt.to_text()
        assert "<str>" in prompt.to_text()
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("kind", [LongContent, "long_content"])
@pytest.mark.parametrize("root", [False, True])
def test_public_production_supports_root_and_nested_declarations(tmp_path, kind, root):
    execution = create_execution_agent(
        tmp_path, "typed-long-content", [plan(), {"body": "Actual prose."}]
    ).input("Write.")
    execution.output(
        (kind, "Writing requirements")
        if root
        else {"body": (kind, "Writing requirements")}
    )
    expected = "Actual prose." if root else {"body": "Actual prose."}
    assert execution.get_data() == expected
    assert ScriptedExecutionRequester.model_dispatches == 2
    result_object = execution.get_data_object()
    assert result_object is not None
    assert result_object.model_dump() == expected


def test_pydantic_annotation_preserves_constraints_and_model_class(tmp_path):
    class Report(BaseModel):
        body: LongContent = Field(
            description="Actual prose.", min_length=5, max_length=30
        )

    execution = (
        create_execution_agent(
            tmp_path, "typed-model", [plan(), {"body": "Actual prose."}]
        )
        .input("Write.")
        .output(Report)
    )
    result = execution.get_data_object()
    assert isinstance(result, Report)
    assert type(result.body) is str
    assert result.body == "Actual prose."


def test_annotated_constraints_are_not_discarded():
    bounded = Annotated[LongContent, Field(min_length=10)]
    prompt = (
        Agently.create_agent()
        .create_request()
        .output({"body": (bounded, "Body")})
        .prompt
    )
    with pytest.raises(ValueError):
        prompt.to_output_model().model_validate({"body": "short"})


def test_description_and_field_name_do_not_select_long_content():
    from agently.builtins.plugins.AgentExecution.modules.field_long_content import (
        has_long_content,
    )

    prompt = (
        Agently.create_agent()
        .create_request()
        .output({"long_content": (str, "long_content")})
        .prompt
    )
    assert not has_long_content(prompt.to_prompt_object().output)


def test_prompt_config_serializes_the_canonical_string_tag():
    import json

    prompt = (
        Agently.create_agent()
        .create_request()
        .output({"body": (LongContent, "正文", True)})
        .prompt
    )
    data = json.loads(prompt.to_json_prompt())
    assert data["output"]["body"] == {
        "$type": "long_content",
        "$desc": "正文",
        "$ensure": True,
    }
    assert "FieldInfo" not in prompt.to_json_prompt()


def test_nested_prompt_config_round_trip_preserves_long_content():
    import json

    declaration = {"items": [{"body": (LongContent, "正文", True)}]}
    prompt = Agently.create_agent().create_request().output(declaration).prompt
    original = repr(declaration)
    serialized = json.loads(prompt.to_json_prompt())
    assert serialized["output"]["items"][0]["body"]["$type"] == "long_content"
    assert repr(declaration) == original
    restored = Agently.create_agent()
    restored.load_json_prompt(json.dumps({".execution": serialized}))
    assert restored.request_prompt.to_text() == prompt.to_text()
    assert (
        restored.request_prompt.to_output_model().model_json_schema()
        == prompt.to_output_model().model_json_schema()
    )


def test_bare_type_selects_root_long_content(tmp_path):
    execution = (
        create_execution_agent(
            tmp_path, "bare-long-content", [plan(), {"body": "Actual prose."}]
        )
        .input("Write.")
        .output(LongContent)
    )
    assert execution.get_data() == "Actual prose."
    assert ScriptedExecutionRequester.model_dispatches == 2


def test_pydantic_array_item_annotation_keeps_production_marker(tmp_path):
    from agently.builtins.plugins.AgentExecution.modules.field_long_content import (
        has_long_content,
    )

    class Report(BaseModel):
        bodies: list[LongContent]

    prompt = Agently.create_agent().create_request().output(Report).prompt
    assert has_long_content(prompt.to_prompt_object().output)
    validated = prompt.to_output_model().model_validate({"bodies": ["text"]})
    assert isinstance(validated, Report)
    assert validated.bodies == ["text"]
    execution = (
        create_execution_agent(
            tmp_path,
            "array-long-content",
            [{"items": ["One article"]}, plan(), {"body": "Actual prose."}],
        )
        .input("Write one article.")
        .output(Report)
    )
    result = execution.get_data_object()
    assert isinstance(result, Report)
    assert result.bodies == ["Actual prose."]
    assert ScriptedExecutionRequester.model_dispatches == 3
