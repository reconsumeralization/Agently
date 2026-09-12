"""Root output contracts must not gain an artificial object wrapper."""
from typing import Annotated

import pytest
from pydantic import Field, RootModel

from agently import Agently
from agently.builtins.plugins.PromptGenerator.modules.output_contract import (
    output_schema_to_json_schema,
    pydantic_model_to_output_schema,
)
from test_builtin_agent_executions import create_execution_agent, ScriptedExecutionRequester
from pathlib import Path


@pytest.mark.parametrize('model', [
    RootModel[str],
    RootModel[list[str]],
    RootModel[Annotated[str, Field(min_length=2, max_length=8)]],
])
def test_root_schema_preserves_original_json_kind(model: type[RootModel]) -> None:
    projected = output_schema_to_json_schema(pydantic_model_to_output_schema(model), strict_output=True)
    original = model.model_json_schema()
    for key in ('type', 'items', 'minLength', 'maxLength'):
        if key in original:
            assert projected[key] == original[key]
    assert 'properties' not in projected


def test_root_prompt_and_original_validation_agree() -> None:
    model = RootModel[Annotated[str, Field(min_length=2, max_length=8)]]
    request = Agently.create_agent('root-contract').create_request().input('Write a short label.').output(model, format='json')
    assert request.prompt.to_output_model() is model
    validated = request.prompt.to_output_model().model_validate('valid')
    assert isinstance(validated, RootModel)
    assert validated.root == 'valid'
    projected = output_schema_to_json_schema(request.prompt.to_prompt_object().output, strict_output=True)
    assert projected['type'] == 'string'
    assert projected['maxLength'] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['valid', 'text with [brackets] and {braces}', '', '引号"和\\转义'])
async def test_ordinary_root_string_request(tmp_path: Path, value: str) -> None:
    import json
    agent = create_execution_agent(tmp_path, 'root-string-request', [json.dumps(value)])
    execution = agent.create_execution().input('Return the declared string.').output(RootModel[str], format='json')
    assert await execution.async_get_data(max_retries=0) == value
    assert ScriptedExecutionRequester.model_dispatches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['"unterminated', '"valid" trailing', 'not JSON', '42'])
async def test_invalid_root_string_never_returns_success(tmp_path: Path, raw: str) -> None:
    agent = create_execution_agent(tmp_path, 'root-string-invalid', [raw])
    execution = agent.create_execution().input('Return the declared string.').output(RootModel[str], format='json')
    with pytest.raises(Exception):
        await execution.async_get_data(max_retries=0)
    assert ScriptedExecutionRequester.model_dispatches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('model,value', [(RootModel[list[str]], ['a', 'b']), (RootModel[int], 12),
    (RootModel[bool], False), (RootModel[float], 1.5)])
async def test_ordinary_root_values(tmp_path: Path, model: type[RootModel], value: object) -> None:
    import json
    agent = create_execution_agent(tmp_path, 'root-value-request', [json.dumps(value)])
    execution = agent.create_execution().input('Return the declared value.').output(model, format='json')
    assert await execution.async_get_data(max_retries=0) == value
    assert ScriptedExecutionRequester.model_dispatches == 1
