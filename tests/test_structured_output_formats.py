from __future__ import annotations

from typing import Any, cast
import warnings

import pytest

import agently.base
from agently import Agently
from agently.builtins.plugins.ResponseParser.AgentlyResponseParser import AgentlyResponseParser
from agently.builtins.plugins.ResponseParser.modules.hybrid import parse_hybrid_output
from agently.builtins.plugins.ResponseParser.modules.xml_field import parse_xml_field_output
from agently.builtins.plugins.ResponseParser.modules.yaml_literal import parse_yaml_literal_output
from agently.types.data.prompt import PromptModel
from agently.utils import Settings


async def _noop_async_emit_runtime(_event):
    return None


class DummyPrompt:
    def __init__(self, output: dict[str, Any], output_format: str):
        self._prompt_object = PromptModel(output=output, output_format=output_format)

    def to_prompt_object(self):
        return self._prompt_object

    def to_output_model(self):
        return None


def create_parser(events, output_schema: dict[str, Any], output_format: str):
    async def response_generator():
        for event, data in events:
            yield event, data

    return AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-structured",
        prompt=cast(Any, DummyPrompt(output_schema, output_format)),
        response_generator=response_generator(),
        settings=Settings(),
    )


def create_parser_with_prompt(events, prompt):
    async def response_generator():
        for event, data in events:
            yield event, data

    return AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-structured",
        prompt=prompt,
        response_generator=response_generator(),
        settings=Settings(),
    )


def test_xml_field_parser_is_not_strict_xml():
    text = """
<think>provider side reasoning</think>
<agently_output>
<field name="notes" type="text">
Markdown with raw & and XML-like <tag>content</tag>.
</field>
<field name="items" type="json">
[{"name": "one", "active": true}]
</field>
</agently_output>
"""
    result = parse_xml_field_output(text, {"notes": (str,), "items": [{"name": (str,), "active": (bool,)}]})
    assert result == {
        "notes": "Markdown with raw & and XML-like <tag>content</tag>.",
        "items": [{"name": "one", "active": True}],
    }


def test_xml_field_text_can_contain_field_boundary_literal():
    text = """
<agently_output>
<field name="long_code" type="text">
```python
token = "</field>"
yaml_end = "<<<END AGENTLY_YAML>>>"
think = "<think>literal</think>"
```
</field>
<field name="test_cases" type="json">
[{"case_id": "literal-boundary", "must_pass": true}]
</field>
</agently_output>
"""
    result = parse_xml_field_output(
        text,
        {
            "long_code": (str,),
            "test_cases": [{"case_id": (str,), "must_pass": (bool,)}],
        },
    )
    assert result is not None
    assert 'token = "</field>"' in result["long_code"]
    assert result["test_cases"] == [{"case_id": "literal-boundary", "must_pass": True}]


def test_hybrid_requires_json_for_bool_number_and_records():
    text = """### summary
Done.

### customer_visible
```json
true
```

### risk_score
```json
3
```

### actions
```json
[{"owner": "sre", "action": "check"}]
```
"""
    result = parse_hybrid_output(
        text,
        {
            "summary": (str,),
            "customer_visible": (bool,),
            "risk_score": (int,),
            "actions": [{"owner": (str,), "action": (str,)}],
        },
    )
    assert result == {
        "summary": "Done.",
        "customer_visible": True,
        "risk_score": 3,
        "actions": [{"owner": "sre", "action": "check"}],
    }


def test_yaml_literal_uses_target_boundary():
    text = """outside prose
<<<BEGIN AGENTLY_YAML>>>
lesson_script: |
  Line 1
  Line 2
environment_checklist:
  - item: Python
    why: Required
    command: python --version
final_confirmation: Done
<<<END AGENTLY_YAML>>>
outside suffix
"""
    result = parse_yaml_literal_output(
        text,
        {
            "lesson_script": (str,),
            "environment_checklist": [{"item": (str,), "why": (str,), "command": (str,)}],
            "final_confirmation": (str,),
        },
    )
    assert result is not None
    assert result["lesson_script"] == "Line 1\nLine 2\n"
    assert result["environment_checklist"][0]["command"] == "python --version"


def test_prompt_model_accepts_new_explicit_formats_and_rejects_non_dict():
    assert PromptModel(output={"notes": (str,)}, output_format="xml_field").output_format == "xml_field"
    assert PromptModel(output={"notes": (str,)}, output_format="yaml_literal").output_format == "yaml_literal"

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        model = PromptModel(output=(str,), output_format="xml_field")
    assert model.output_format == "json"
    assert "xml_field" in str(captured[0].message)


def test_prompt_generator_renders_xml_field_and_yaml_literal():
    agent = Agently.create_agent("structured-format-prompt")
    schema = {"notes": (str, "notes"), "items": [{"name": (str,), "active": (bool,)}]}

    agent.request.output(schema, format="xml_field")
    xml_prompt = agent.request.prompt.to_text()
    assert "<agently_output>" in xml_prompt
    assert '<field name="notes" type="text">' in xml_prompt
    assert '<field name="items" type="json">' in xml_prompt
    assert "<str>" not in xml_prompt

    agent = Agently.create_agent("structured-format-yaml")
    agent.request.output(schema, format="yaml_literal")
    yaml_prompt = agent.request.prompt.to_text()
    assert "<<<BEGIN AGENTLY_YAML>>>" in yaml_prompt
    assert "items:\n  - name:" in yaml_prompt

    agent = Agently.create_agent("structured-format-hybrid")
    agent.request.output(schema, format="hybrid")
    hybrid_prompt = agent.request.prompt.to_text()
    assert "```json" in hybrid_prompt
    assert "<str>" not in hybrid_prompt


@pytest.mark.asyncio
async def test_leading_think_is_reasoning_event_not_parser_content(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    payload = (
        "<think>model reasoning</think>"
        "<agently_output>"
        '<field name="notes" type="text">answer</field>'
        '<field name="ready" type="json">true</field>'
        "</agently_output>"
    )
    parser = create_parser(
        [
            ("original_delta", {"raw": payload}),
            ("delta", payload),
            ("done", payload),
            ("original_done", {"raw": payload}),
        ],
        {"notes": (str,), "ready": (bool,)},
        "xml_field",
    )

    events = []
    async for event, data in parser.get_async_generator(type="specific"):
        events.append((event, data))

    assert ("reasoning_delta", "model reasoning") in events
    assert ("reasoning_done", "model reasoning") in events
    assert all("<think>" not in data for event, data in events if event in {"delta", "done"})
    assert await parser.async_get_data() == {"notes": "answer", "ready": True}
    all_data = await parser.async_get_data(type="all")
    assert all_data["original_delta"] == [{"raw": payload}]
    assert all_data["original_done"] == {"raw": payload}
    assert all_data["text_result"].startswith("<agently_output>")


@pytest.mark.asyncio
async def test_payload_think_is_preserved_when_not_leading_reasoning():
    payload = (
        "<agently_output>"
        '<field name="notes" type="text">Keep <think>literal tag</think> in payload.</field>'
        "</agently_output>"
    )
    parser = create_parser(
        [("delta", payload), ("done", payload)],
        {"notes": (str,)},
        "xml_field",
    )

    events = []
    async for event, data in parser.get_async_generator(type="specific"):
        events.append((event, data))

    assert not any(event.startswith("reasoning") for event, _data in events)
    assert await parser.async_get_data() == {"notes": "Keep <think>literal tag</think> in payload."}


@pytest.mark.asyncio
async def test_structured_final_materialization_paths_and_result_object():
    payload = (
        "<agently_output>"
        '<field name="notes" type="text">answer</field>'
        '<field name="ready" type="json">true</field>'
        "</agently_output>"
    )
    agent = Agently.create_agent("structured-final-materialization")
    agent.request.output({"notes": (str, "notes", True), "ready": (bool, "ready", True)}, format="xml_field")
    parser = create_parser_with_prompt(
        [
            ("original_delta", {"raw": payload}),
            ("delta", payload),
            ("done", payload),
            ("original_done", {"raw": payload}),
        ],
        agent.request.prompt,
    )

    assert await parser.async_get_data(type="parsed") == {"notes": "answer", "ready": True}
    assert await parser.async_get_text() == payload
    assert await parser.async_get_data(type="original") == {"raw": payload}
    all_data = await parser.async_get_data(type="all")
    assert all_data["parsed_result"] == {"notes": "answer", "ready": True}
    assert all_data["text_result"] == payload
    result_object = await parser.async_get_data_object()
    assert result_object is not None
    assert result_object.model_dump() == {"notes": "answer", "ready": True}


@pytest.mark.asyncio
async def test_parse_failed_observation_has_explicit_metadata():
    text = """### count
not json
"""
    parser = create_parser(
        [("delta", text), ("done", text)],
        {"count": (int,)},
        "hybrid",
    )

    assert await parser.async_get_data() is None
    all_data = await parser.async_get_data(type="all")
    assert all_data["extra"]["parse_error"]
    assert all_data["extra"]["payload_extracted"] is True
    assert all_data["extra"]["parse_success"] is False
    observations = parser.drain_runtime_observations()
    failure = next(observation for observation in observations if observation["kind"] == "parse_failed")
    assert failure["payload"]["resolved_format"] == "hybrid"
    assert failure["payload"]["payload_extracted"] is True
    assert failure["payload"]["parse_success"] is False
    assert failure["payload"]["parse_error"]


@pytest.mark.asyncio
async def test_structured_text_parser_falls_back_to_json_dict():
    text = '{"summary": "done", "items": ["a", "b"]}'
    parser = create_parser(
        [("delta", text), ("done", text)],
        {"summary": (str,), "items": ([str],)},
        "hybrid",
    )

    assert await parser.async_get_data() == {"summary": "done", "items": ["a", "b"]}
    all_data = await parser.async_get_data(type="all")
    assert all_data["extra"]["parse_success"] is True
    assert all_data["extra"]["output_format"] == "hybrid"
    assert all_data["extra"]["resolved_output_format"] == "json"
    assert all_data["extra"]["format_fallback"]["from"] == "hybrid"
    observations = parser.drain_runtime_observations()
    completed = next(observation for observation in observations if observation["kind"] == "completed")
    assert completed["payload"]["format"] == "hybrid"
    assert completed["payload"]["resolved_format"] == "json"


def test_prompt_config_aliases_accept_new_formats():
    agent = Agently.create_agent("structured-prompt-config-xml")
    yaml_prompt = """
.execution:
  output:
    $format: xml_field
    notes:
      $type: str
      $ensure: true
"""
    agent.load_yaml_prompt(yaml_prompt)
    assert agent.request_prompt.to_prompt_object().output_format == "xml_field"

    agent = Agently.create_agent("structured-prompt-config-yaml")
    yaml_prompt = """
.execution:
  output:
    .output_format: yaml_literal
    notes:
      $type: str
      $ensure: true
"""
    agent.load_yaml_prompt(yaml_prompt)
    assert agent.request_prompt.to_prompt_object().output_format == "yaml_literal"


def test_sync_instant_generator_supports_hybrid():
    text = """### summary
Done.

### items
```json
["one"]
```
"""
    parser = create_parser(
        [("delta", text), ("done", text)],
        {"summary": (str,), "items": [(str,)]},
        "hybrid",
    )
    events = list(parser.get_generator(type="instant"))
    assert "summary" in {event.path for event in events}
    assert "items" in {event.path for event in events}


@pytest.mark.asyncio
async def test_async_instant_generator_supports_xml_field_and_yaml_literal():
    xml_text = (
        "<agently_output>"
        '<field name="notes" type="text">hello</field>'
        '<field name="items" type="json">["one"]</field>'
        "</agently_output>"
    )
    xml_parser = create_parser(
        [("delta", xml_text), ("done", xml_text)],
        {"notes": (str,), "items": [(str,)]},
        "xml_field",
    )
    xml_events = [event async for event in xml_parser.get_async_generator(type="instant")]
    assert {"notes", "items"} <= {event.path for event in xml_events}

    yaml_text = """<<<BEGIN AGENTLY_YAML>>>
notes: |
  hello
items:
  - one
<<<END AGENTLY_YAML>>>"""
    yaml_parser = create_parser(
        [("delta", yaml_text), ("done", yaml_text)],
        {"notes": (str,), "items": [(str,)]},
        "yaml_literal",
    )
    yaml_events = [event async for event in yaml_parser.get_async_generator(type="instant")]
    assert {"notes", "items"} <= {event.path for event in yaml_events}
