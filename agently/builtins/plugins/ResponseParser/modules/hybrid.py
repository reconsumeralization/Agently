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

Hybrid format combines flat_markdown-style section headers for string text
fields with JSON code blocks for lists, objects, booleans, and numbers.
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Mapping

from agently.types.data.response import StreamingData
from agently.core.model.StructuredOutputParser import _extract_json_block, parse_hybrid_output


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
