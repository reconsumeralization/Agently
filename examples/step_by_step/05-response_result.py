"""ModelRequest result facade foundation probe.

Run:
    python examples/step_by_step/05-response_result.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

This is a Foundation example effect check for the low-level ModelRequest /
ModelRequestResult substrate. It intentionally avoids AgentExecution so release
reviewers can verify the request foundation directly:

    request = Agently.create_request()
    result = request.input(...).output(...).get_result()

Expected key output from one real DeepSeek run on 2026-06-24:
    provider=deepseek
    result_type=ModelRequestResult
    data_has_definition=True
    data_has_example=True
    text_nonempty=True
    meta_has_id=True
    result_cached=True
    delta_event_count_positive=True
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from agently.core import ModelRequestResult
from examples.dynamic_task._shared import configure_model


def build_result_request() -> ModelRequestResult:
    Agently.set_settings("OpenAICompatible.stream", False)
    request = Agently.create_request()
    result = (
        request.input(
            "Explain recursion for a junior Python developer. Keep the answer short and concrete."
        )
        .output(
            {
                "definition": (str, "One short definition of recursion.", True),
                "example": (str, "One short Python-flavored example.", True),
            },
            format="json",
        )
        .get_result()
    )
    if not isinstance(result, ModelRequestResult):
        raise TypeError(f"Expected ModelRequestResult, got {type(result).__name__}")
    return result


def run_stream_probe() -> int:
    Agently.set_settings("OpenAICompatible.stream", True)
    request = Agently.create_request()
    result = request.input("List three recursion tips in one compact sentence.").get_result()
    delta_count = 0
    for delta in result.get_generator(type="delta"):
        if delta:
            delta_count += 1
    return delta_count


def main() -> None:
    provider = configure_model(temperature=0.0)
    Agently.set_settings("OpenAICompatible.stream_idle_timeout", 45.0)
    Agently.set_settings("OpenAIResponsesCompatible.stream_idle_timeout", 45.0)
    Agently.set_settings("response.materialization_idle_timeout", 45.0)
    result = build_result_request()

    data = result.get_data()
    text = result.get_text()
    meta = result.get_meta()
    cached_data = result.get_data()
    delta_event_count = run_stream_probe()

    data_dict: dict[str, Any] = data if isinstance(data, dict) else {}

    print(f"provider={provider}")
    print("result_type=ModelRequestResult")
    print(f"data_has_definition={bool(data_dict.get('definition'))}")
    print(f"data_has_example={bool(data_dict.get('example'))}")
    print(f"text_nonempty={bool(str(text).strip())}")
    print(f"meta_has_id={bool(meta.get('id'))}")
    print(f"result_cached={cached_data == data}")
    print(f"delta_event_count_positive={delta_event_count > 0}")


if __name__ == "__main__":
    main()


# How it works:
#
# get_result() returns a lazy result facade; the model request does not start
# until data is consumed. Once consumed, all result types (text, data,
# data_object, meta) are cached on the result instance and can be read multiple
# times without re-requesting.
#
# Result accessor pairs:
#   get_text()        / async_get_text()        - raw reply as a string
#   get_data()        / async_get_data()        - parsed structured dict
#   get_data_object() / async_get_data_object() - Pydantic model
#   get_meta()        / async_get_meta()        - request metadata
#   get_generator()   / get_async_generator()   - streaming iterator
