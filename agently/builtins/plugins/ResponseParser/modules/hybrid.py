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

"""Hybrid output parsing and streaming support.

Used by AgentlyResponseParser when ``output_format == "hybrid"``.

Hybrid format combines flat_markdown-style section headers for scalar
fields with JSON code blocks for complex fields (lists, nested dicts).
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Mapping

import json5

from agently.types.data.prompt import _classify_field_spec
from agently.types.data.response import StreamingData
from agently.utils import DataLocator

from .code_fence import strip_enclosing_code_fence


def _extract_json_block(content: str) -> str | None:
    """Extract JSON from a markdown code block or raw text.

    Looks for ```` ```json ... ``` ```` first, then falls back to
    :func:`DataLocator.locate_output_json`.
    """
    # Try markdown code block first
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def parse_hybrid_output(text: str, output_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    """Parse a hybrid-format model response into a dict.

    Splits by ``### field_name`` headers, then:

    - **Scalar fields**: content is trimmed and stored as-is (pydantic
      type coercion handles the rest).
    - **Complex fields**: content is searched for a JSON code block;
      on success the JSON is parsed with ``json5``. If no JSON block is
      found the raw content is stored as a string.

    Args:
        text: The raw model response text.
        output_schema: The output dict schema (field_name -> field_spec).

    Returns:
        Parsed dict, or ``None`` if no sections are found.
    """
    if not isinstance(output_schema, Mapping) or not output_schema:
        return None

    field_names = list(output_schema.keys())
    if not field_names:
        return None
    escaped_names = "|".join(re.escape(name) for name in field_names)
    pattern = rf"^###\s+({escaped_names})\s*(?:\[(?:text|JSON)\])?\s*$"

    sections = re.split(pattern, text, flags=re.MULTILINE)
    # sections[0] = preamble, sections[1] = first field_name, sections[2] = content, ...

    result: dict[str, Any] = {}
    for i in range(1, len(sections), 2):
        field_name = sections[i].strip()
        content = sections[i + 1].strip() if i + 1 < len(sections) else ""
        field_spec = output_schema.get(field_name)

        if _classify_field_spec(field_spec) == "complex":
            json_text = _extract_json_block(content)
            if json_text is None:
                json_text = DataLocator.locate_output_json(content, {field_name: field_spec})
            if json_text:
                try:
                    result[field_name] = json5.loads(json_text)
                except Exception:
                    result[field_name] = content
            else:
                result[field_name] = content
        else:
            # Scalar field. If the model JSON-encoded the value as a string,
            # decode it so quotes/escapes resolve; otherwise unwrap any single
            # enclosing code fence (```html, ```svg, ``` ...) and store the raw
            # artifact. Content that is not wholly a fence is left untouched.
            json_text = _extract_json_block(content)
            decoded_string: str | None = None
            if json_text is not None:
                try:
                    parsed = json5.loads(json_text)
                    if isinstance(parsed, str):
                        decoded_string = parsed
                except Exception:
                    decoded_string = None
            result[field_name] = decoded_string if decoded_string is not None else strip_enclosing_code_fence(content)

    return result if result else None


class HybridStreamingParser:
    """Streaming parser for hybrid output format.

    Behaves identically to :class:`FlatMarkdownStreamingParser` during
    streaming (emits text deltas per field).  JSON sub-parsing is deferred
    to :func:`parse_hybrid_output` during finalisation.

    Args:
        output_schema: The output dict schema (field_name -> spec tuple).
    """

    def __init__(self, output_schema: Mapping[str, Any]):
        self._field_names = list(output_schema.keys()) if isinstance(output_schema, Mapping) else []
        self._escaped_names = "|".join(re.escape(name) for name in self._field_names)
        self._header_pattern = re.compile(
            rf"^###\s+({self._escaped_names})\s*(?:\[(?:text|JSON)\])?\s*$",
            flags=re.MULTILINE,
        ) if self._field_names else None

        self._buffer = ""
        self._current_field: str | None = None
        self._field_started: set[str] = set()
        self._field_completed: set[str] = set()

    async def parse_chunk(self, chunk: str) -> AsyncGenerator[StreamingData, None]:
        """Feed a text chunk and yield any new :class:`StreamingData` events."""
        if not self._header_pattern:
            return

        self._buffer += chunk

        while True:
            match = self._header_pattern.search(self._buffer)
            if not match:
                if self._current_field is not None and self._buffer:
                    last_nl = self._buffer.rfind("\n")
                    if last_nl >= 0:
                        safe = self._buffer[: last_nl + 1]
                        self._buffer = self._buffer[last_nl + 1 :]
                        if safe.strip():
                            yield StreamingData(
                                path=self._current_field,
                                value=safe,
                                delta=safe,
                                is_complete=False,
                                event_type="delta",
                            )
                return

            new_field_name = match.group(1)
            pre_content = self._buffer[:match.start()]

            if self._current_field is not None:
                if pre_content:
                    yield StreamingData(
                        path=self._current_field,
                        value=pre_content,
                        delta=pre_content,
                        is_complete=False,
                        event_type="delta",
                    )
                self._field_completed.add(self._current_field)
                yield StreamingData(
                    path=self._current_field,
                    value="",
                    delta="",
                    is_complete=True,
                    event_type="done",
                )

            self._current_field = new_field_name
            self._field_started.add(new_field_name)
            yield StreamingData(
                path=new_field_name,
                value="",
                delta="",
                is_complete=False,
                event_type="delta",
            )

            header_end = match.end()
            self._buffer = self._buffer[header_end:].lstrip("\n")

    async def flush(self) -> AsyncGenerator[StreamingData, None]:
        """Flush remaining buffered content and emit completion events."""
        if self._current_field is not None:
            remaining = self._buffer.strip()
            if remaining:
                yield StreamingData(
                    path=self._current_field,
                    value=remaining,
                    delta=remaining,
                    is_complete=False,
                    event_type="delta",
                )
            if self._current_field not in self._field_completed:
                yield StreamingData(
                    path=self._current_field,
                    value="",
                    delta="",
                    is_complete=True,
                    event_type="done",
                )
            self._buffer = ""

        for name in self._field_names:
            if name not in self._field_started:
                yield StreamingData(
                    path=name,
                    value="",
                    delta="",
                    is_complete=True,
                    event_type="done",
                )
