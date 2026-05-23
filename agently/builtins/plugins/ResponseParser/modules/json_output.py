# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JSON output parsing support.

Used by AgentlyResponseParser when ``output_format == "json"``.
"""

from __future__ import annotations

from typing import Any, Callable

import json5
from pydantic import BaseModel

from agently.utils import DataLocator, StreamingJSONCompleter


def parse_json_output(
    text: str,
    output_schema: Any,
    build_result_object: Callable[[Any], BaseModel | None],
) -> tuple[str | None, Any, BaseModel | None, bool]:
    """Parse a JSON model response string into structured data.

    Attempts to locate, complete, and parse JSON from the raw response text.
    If parsing fails, attempts JSON repair and retries.

    Args:
        text: The raw model response text.
        output_schema: The output schema used to guide JSON location.
        build_result_object: Callback that validates parsed data into a
            Pydantic model (or returns ``None``).

    Returns:
        Tuple of ``(cleaned_json_str, parsed_data, result_object, was_repaired)``.
        All values except ``was_repaired`` may be ``None`` on failure.
    """
    cleaned_json = DataLocator.locate_output_json(text, output_schema)
    if cleaned_json is None:
        return None, None, None, False

    completer = StreamingJSONCompleter()
    completer.reset(cleaned_json)
    completed = completer.complete()
    try:
        parsed = json5.loads(completed)
        return completed, parsed, build_result_object(parsed), False
    except Exception:
        repaired_json = DataLocator.repair_json_fragment(cleaned_json)
        if repaired_json == cleaned_json:
            return completed, None, None, False

        completer.reset(repaired_json)
        repaired_completed = completer.complete()
        try:
            parsed = json5.loads(repaired_completed)
            return repaired_completed, parsed, build_result_object(parsed), True
        except Exception:
            return repaired_completed, None, None, False
