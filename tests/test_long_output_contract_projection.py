"""Continuation contract projection; deterministic rules, not model-quality scoring."""
from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from agently import Agently
from agently.builtins.plugins.AgentExecution.modules.execution import AgentExecution
from agently.builtins.plugins.AgentExecution.modules.long_output import LongOutputDelivery, LongOutputError

from agently.builtins.plugins.PromptGenerator.modules.output_contract import (
    output_schema_to_json_schema,
    pydantic_model_to_output_schema,
)


class ProjectionItem(BaseModel):
    body: str = Field(min_length=8, max_length=16, pattern=r"^正文")
    weight: float = Field(gt=0, le=10, multiple_of=0.5)


class ProjectionDocument(BaseModel):
    title: str = Field(min_length=3, max_length=12, description="文档标题")
    optional_note: str | None = Field(default=None, max_length=20)
    items: list[ProjectionItem] = Field(min_length=1, max_length=3)
    tags: list[Annotated[str, Field(min_length=2, max_length=5)]]


@pytest.mark.parametrize("strict", [False, True])
def test_pydantic_contract_projection_retains_nested_constraints(strict: bool) -> None:
    projected = output_schema_to_json_schema(
        pydantic_model_to_output_schema(ProjectionDocument), strict_output=strict,
    )
    fields = projected["properties"]
    assert fields["title"]["minLength"] == 3
    assert fields["title"]["maxLength"] == 12
    assert fields["title"]["description"] == "文档标题"
    assert fields["items"]["minItems"] == 1
    assert fields["items"]["maxItems"] == 3
    item = fields["items"]["items"]["properties"]
    assert item["body"]["minLength"] == 8
    assert item["body"]["maxLength"] == 16
    assert item["body"]["pattern"] == "^正文"
    assert item["weight"]["exclusiveMinimum"] == 0
    assert item["weight"]["maximum"] == 10
    assert item["weight"]["multipleOf"] == 0.5
    assert fields["tags"]["items"]["minLength"] == 2
    assert fields["tags"]["items"]["maxLength"] == 5
    assert fields["optional_note"]["anyOf"] == [
        {"type": "string", "maxLength": 20}, {"type": "null"},
    ]
    assert ("optional_note" in projected["required"]) is strict


def test_tuple_declaration_without_pydantic_metadata_is_unchanged() -> None:
    assert output_schema_to_json_schema({"body": (str, "完整正文", True)}) == {
        "type": "object", "properties": {"body": {"type": "string", "description": "完整正文"}},
        "required": ["body"], "additionalProperties": True,
    }


def test_continuation_projects_the_same_rule_its_slot_validator_enforces() -> None:
    execution = Agently.create_agent("projection-contract-test").create_execution().output(
        ProjectionDocument, format="json",
    )
    assert isinstance(execution, AgentExecution)
    owner = LongOutputDelivery(
        execution, ensure_keys=None, ensure_all_keys=True, validate_handler=None,
        key_style="dot", max_retries=0, raise_ensure_failure=True,
    )
    owner.preflight()
    slots = {slot.path: slot for slot in owner.slots}
    title = slots[("title",)]
    assert owner._slot_value_contract(title)["maxLength"] == 12
    assert owner._validate_slot_value(title, "有效标题") == "有效标题"
    with pytest.raises(LongOutputError, match="at most 12"):
        owner._validate_slot_value(title, "过" * 13)
    items = slots[("items",)]
    contract = owner._slot_value_contract(items)
    assert contract["properties"]["body"]["maxLength"] == 16
    assert contract["properties"]["weight"]["multipleOf"] == 0.5
    with pytest.raises(LongOutputError, match="at most 16"):
        owner._validate_slot_value(items, {"body": "正文" + "长" * 15, "weight": 1})
    note = slots[("optional_note",)]
    assert {"type": "null"} in owner._slot_value_contract(note)["anyOf"]
    assert owner._validate_slot_value(note, None) is None
