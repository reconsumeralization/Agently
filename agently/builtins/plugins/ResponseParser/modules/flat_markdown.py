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

"""Flat markdown output parsing and streaming support.

Used by AgentlyResponseParser when ``output_format == "flat_markdown"``.
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Mapping

from agently.types.data.response import StreamingData
from agently.core.model.StructuredOutputParser import parse_flat_markdown_output


class FlatMarkdownStreamingParser:
    """Streaming parser for flat_markdown output format.

    Buffers incremental text chunks, detects ``### field_name`` section
    headers, and emits :class:`StreamingData` events compatible with the
    ``instant`` / ``streaming_parse`` generator protocol.

    Interface matches :class:`~agently.utils.StreamingJSONParser` so it
    can be used as a drop-in in :meth:`AgentlyResponseParser.get_async_generator`.

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
        self._field_values: dict[str, list[str]] = {}

    def _append_current_field_delta(self, chunk: str) -> None:
        if self._current_field is None or not chunk:
            return
        self._field_values.setdefault(self._current_field, []).append(chunk)

    def _field_done_value(self, field_name: str) -> str:
        return "".join(self._field_values.get(field_name, [])).strip()

    async def parse_chunk(self, chunk: str) -> AsyncGenerator[StreamingData, None]:
        """Feed a text chunk and yield any new :class:`StreamingData` events.

        Args:
            chunk: The next piece of text from the model stream.

        Yields:
            StreamingData events for field deltas and completions.
        """
        if not self._header_pattern:
            return

        self._buffer += chunk

        while True:
            match = self._header_pattern.search(self._buffer)
            if not match:
                # No complete header in buffer. Emit safe content up to the
                # last newline, keeping the trailing partial line — it could
                # be the start of a section header split across chunks.
                if self._current_field is not None and self._buffer:
                    last_nl = self._buffer.rfind("\n")
                    if last_nl >= 0:
                        safe = self._buffer[: last_nl + 1]
                        self._buffer = self._buffer[last_nl + 1 :]
                        if safe.strip():
                            self._append_current_field_delta(safe)
                            yield StreamingData(
                                path=self._current_field,
                                value=safe,
                                delta=safe,
                                is_complete=False,
                                event_type="delta",
                            )
                    # else: no newline at all — keep everything in buffer
                return

            header_start = match.start()

            # Extract field name from the header
            new_field_name = match.group(1)

            # Content before this header belongs to the current field
            pre_content = self._buffer[:header_start]

            if self._current_field is not None:
                if pre_content:
                    self._append_current_field_delta(pre_content)
                    yield StreamingData(
                        path=self._current_field,
                        value=pre_content,
                        delta=pre_content,
                        is_complete=False,
                        event_type="delta",
                    )
                # Mark previous field as complete
                self._field_completed.add(self._current_field)
                yield StreamingData(
                    path=self._current_field,
                    value=self._field_done_value(self._current_field),
                    delta="",
                    is_complete=True,
                    event_type="done",
                )

            # Start new field
            self._current_field = new_field_name
            self._field_started.add(new_field_name)
            self._field_values.setdefault(new_field_name, [])
            yield StreamingData(
                path=new_field_name,
                value="",
                delta="",
                is_complete=False,
                event_type="delta",
            )

            # Advance buffer past the header
            header_end = match.end()
            self._buffer = self._buffer[header_end:].lstrip("\n")

    async def flush(self) -> AsyncGenerator[StreamingData, None]:
        """Flush remaining buffered content and emit completion events.

        Must be called after the last ``parse_chunk`` to finalize all fields.

        Yields:
            Final StreamingData events for any remaining content and uncompleted fields.
        """
        # Emit remaining buffer content for current field
        if self._current_field is not None:
            remaining = self._buffer.strip()
            if remaining:
                self._append_current_field_delta(remaining)
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
                    value=self._field_done_value(self._current_field),
                    delta="",
                    is_complete=True,
                    event_type="done",
                )
            self._buffer = ""

        # Mark any fields that were never seen as complete with empty value
        for name in self._field_names:
            if name not in self._field_started:
                yield StreamingData(
                    path=name,
                    value="",
                    delta="",
                    is_complete=True,
                    event_type="done",
                )

    async def flush_final_data(self, final_data: Any) -> AsyncGenerator[StreamingData, None]:
        """Flush streaming events from trusted final parsed data when no deltas arrived."""
        if isinstance(final_data, Mapping):
            for name in self._field_names:
                if name in self._field_started or name not in final_data:
                    continue
                value = final_data.get(name)
                value_text = "" if value is None else str(value)
                self._field_started.add(name)
                self._field_values[name] = [value_text] if value_text else []
                if value_text:
                    yield StreamingData(
                        path=name,
                        value=value_text,
                        delta=value_text,
                        is_complete=False,
                        event_type="delta",
                    )
                self._field_completed.add(name)
                yield StreamingData(
                    path=name,
                    value=value_text,
                    delta="",
                    is_complete=True,
                    event_type="done",
                )
        async for event in self.flush():
            yield event
