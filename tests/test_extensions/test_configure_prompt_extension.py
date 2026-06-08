import json
from pathlib import Path

import yaml
import json5
import pytest

from agently import Agently


def test_prompt_to_json_yaml_prompt_returns_string():
    agent = Agently.create_agent()
    execution = agent.input("hello").instruct("say hi")

    json_content = execution.request_prompt.to_json_prompt()
    yaml_content = execution.request_prompt.to_yaml_prompt()

    json_data = json.loads(json_content)
    yaml_data = yaml.safe_load(yaml_content)
    assert json_data["input"] == "hello"
    assert yaml_data["input"] == "hello"


def test_get_json_yaml_prompt_save_to_file(tmp_path: Path):
    agent = Agently.create_agent()
    execution = agent.input("demo input").instruct("demo instruct")

    json_path = tmp_path / "configured_prompt.json"
    yaml_path = tmp_path / "configured_prompt.yaml"

    json_prompt = execution.request_prompt.to_json_prompt()
    yaml_prompt = execution.request_prompt.to_yaml_prompt()
    json_path.write_text(json_prompt, encoding="utf-8")
    yaml_path.write_text(yaml_prompt, encoding="utf-8")

    assert json_path.exists()
    assert yaml_path.exists()
    assert json_path.read_text(encoding="utf-8") == json_prompt
    assert yaml_path.read_text(encoding="utf-8") == yaml_prompt

    json_data = json5.loads(json_prompt)
    yaml_data = yaml.safe_load(yaml_prompt)
    assert isinstance(json_data, dict)
    assert json_data["input"] == "demo input"
    assert yaml_data["input"] == "demo input"


def test_load_yaml_prompt_accepts_long_raw_yaml_string():
    agent = Agently.create_agent()
    input_text = "x" * 400
    yaml_prompt = f"input: { input_text }\n"

    agent.load_yaml_prompt(yaml_prompt)

    assert agent.request_prompt.get("input", inherit=False) == input_text


def test_load_json_prompt_accepts_long_raw_json_string():
    agent = Agently.create_agent()
    input_text = "x" * 400
    json_prompt = json.dumps({"input": input_text})

    agent.load_json_prompt(json_prompt)

    assert agent.request_prompt.get("input", inherit=False) == input_text


def test_load_yaml_prompt_accepts_file_path(tmp_path: Path):
    agent = Agently.create_agent()
    yaml_path = tmp_path / "prompt.yaml"
    yaml_path.write_text("input: from-file\n", encoding="utf-8")

    agent.load_yaml_prompt(yaml_path)

    assert agent.request_prompt.get("input", inherit=False) == "from-file"


def test_load_yaml_prompt_accepts_explicit_mappings_keyword():
    agent = Agently.create_agent()
    yaml_prompt = 'input: "Hello ${name}"\n'

    agent.load_yaml_prompt(yaml_prompt, mappings={"name": "Alice"})

    assert agent.request_prompt.get("input", inherit=False) == "Hello Alice"


def test_agent_set_turn_prompt_matches_set_request_prompt():
    agent = Agently.create_agent()

    assert agent.set_turn_prompt("input", "from-turn") is agent
    assert agent.request_prompt.get("input", inherit=False) == "from-turn"

    assert agent.set_request_prompt("input", "from-request") is agent
    assert agent.request_prompt.get("input", inherit=False) == "from-request"


def test_agent_turn_set_turn_prompt_matches_set_request_prompt():
    agent = Agently.create_agent()
    turn = agent.create_turn()

    assert turn.set_turn_prompt("input", "turn-local") is turn
    assert turn.request_prompt.get("input", inherit=False) == "turn-local"
    assert agent.request_prompt.get("input", inherit=False) is None

    assert turn.set_request_prompt("input", "compat") is turn
    assert turn.request_prompt.get("input", inherit=False) == "compat"


def test_agent_set_turn_prompt_is_transferred_to_create_turn():
    agent = Agently.create_agent()

    agent.set_turn_prompt("input", "pending-turn")
    turn = agent.create_turn()

    assert turn.request_prompt.get("input", inherit=False) == "pending-turn"
    assert agent.request_prompt.get("input", inherit=False) is None


def test_load_yaml_prompt_accepts_turn_scope_and_set_turn_prompt_alias():
    agent = Agently.create_agent()
    yaml_prompt = """
.turn:
  input: "Hello ${name}"
.alias:
  set_turn_prompt:
    .args:
      - instruct
      - Reply briefly.
"""

    agent.load_yaml_prompt(yaml_prompt, mappings={"name": "Alice"})

    assert agent.request_prompt.get("input", inherit=False) == "Hello Alice"
    assert agent.request_prompt.get("instruct", inherit=False) == "Reply briefly."


def test_load_yaml_prompt_accepts_execution_scope():
    agent = Agently.create_agent()
    yaml_prompt = """
.execution:
  input: "Hello ${name}"
  instruct: "Reply briefly."
"""

    agent.load_yaml_prompt(yaml_prompt, mappings={"name": "Alice"})

    assert agent.request_prompt.get("input", inherit=False) == "Hello Alice"
    assert agent.request_prompt.get("instruct", inherit=False) == "Reply briefly."


def test_agent_prompt_serializes_execution_scope_for_pending_prompt():
    agent = Agently.create_agent()
    agent.load_yaml_prompt("input: hello\n")

    json_content = agent.get_json_prompt()
    yaml_content = agent.get_yaml_prompt()

    json_data = json5.loads(json_content)
    yaml_data = yaml.safe_load(yaml_content)
    assert isinstance(json_data, dict)
    assert isinstance(yaml_data, dict)
    assert ".execution" in json_data
    assert ".request" not in json_data
    assert json_data[".execution"]["input"] == "hello"
    assert ".execution" in yaml_data
    assert ".request" not in yaml_data
    assert yaml_data[".execution"]["input"] == "hello"


def test_agent_execution_prompt_serializes_execution_scope():
    agent = Agently.create_agent()
    execution = agent.input("hello").instruct("say hi")

    json_content = execution.get_json_prompt()
    yaml_content = execution.get_yaml_prompt()

    json_data = json5.loads(json_content)
    yaml_data = yaml.safe_load(yaml_content)
    assert isinstance(json_data, dict)
    assert isinstance(yaml_data, dict)
    assert json_data[".execution"]["input"] == "hello"
    assert json_data[".execution"]["instruct"] == "say hi"
    assert ".request" not in json_data
    assert yaml_data[".execution"]["input"] == "hello"
    assert yaml_data[".execution"]["instruct"] == "say hi"
    assert ".request" not in yaml_data


def test_load_yaml_prompt_rejects_positional_mappings():
    agent = Agently.create_agent()
    yaml_prompt = 'input: "Hello ${name}"\n'

    with pytest.raises(TypeError):
        getattr(agent, "load_yaml_prompt")(yaml_prompt, {"name": "Alice"})


def test_load_yaml_prompt_output_accepts_format_metadata():
    agent = Agently.create_agent()
    yaml_prompt = """
.request:
  output:
    $format: flat_markdown
    reply:
      $type: str
      $desc: final reply
      $ensure: true
"""

    agent.load_yaml_prompt(yaml_prompt)

    assert agent.request_prompt.get("output_format", inherit=False) == "flat_markdown"
    output = agent.request_prompt.get("output", inherit=False)
    assert isinstance(output, dict)
    assert "$format" not in output
    assert agent.request_prompt.to_prompt_object().output_format == "flat_markdown"


def test_load_json_prompt_output_accepts_output_format_metadata():
    agent = Agently.create_agent()
    json_prompt = json.dumps(
        {
            ".request": {
                "output": {
                    "$output_format": "hybrid",
                    "summary": {"$type": "str", "$ensure": True},
                    "items": [{"name": {"$type": "str"}}],
                }
            }
        }
    )

    agent.load_json_prompt(json_prompt)

    assert agent.request_prompt.get("output_format", inherit=False) == "hybrid"
    output = agent.request_prompt.get("output", inherit=False)
    assert isinstance(output, dict)
    assert "$output_format" not in output
    assert agent.request_prompt.to_prompt_object().output_format == "hybrid"


def test_agent_level_output_format_metadata_is_inherited_by_request():
    agent = Agently.create_agent()
    yaml_prompt = """
$output:
  .format: json
  reply:
    $type: str
    $ensure: true
"""

    agent.load_yaml_prompt(yaml_prompt)

    assert agent.agent_prompt.get("output_format", inherit=False) == "json"
    assert agent.request_prompt.to_prompt_object().output_format == "json"


def test_output_format_metadata_supports_mappings():
    agent = Agently.create_agent()
    yaml_prompt = """
.request:
  output:
    $format: ${format_name}
    html:
      $type: str
      $desc: complete HTML
"""

    agent.load_yaml_prompt(yaml_prompt, mappings={"format_name": "flat_markdown"})

    assert agent.request_prompt.get("output_format", inherit=False) == "flat_markdown"
