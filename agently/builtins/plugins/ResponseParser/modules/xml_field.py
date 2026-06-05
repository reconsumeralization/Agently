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

"""XML-like field envelope output parsing and streaming support.

This is intentionally not a strict XML parser. The format uses XML-like
boundaries to separate fields, while field content remains model-owned text or
JSON. Text fields may contain ordinary Markdown, code, ``&`` characters, or
XML-like snippets without entity escaping.
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Mapping

from agently.types.data.response import StreamingData

from .section_value import normalize_json_section_value, normalize_scalar_section_value


_TARGET_START_RE = re.compile(r"<agently_output\b[^>]*>", flags=re.IGNORECASE)
_TARGET_END_RE = re.compile(r"</agently_output>", flags=re.IGNORECASE)
_FIELD_START_RE = re.compile(
    r"<field\s+name=[\"']([^\"']+)[\"']\s+type=[\"'](text|json)[\"']\s*>",
    flags=re.IGNORECASE,
)
_FIELD_CLOSE_RE = re.compile(
    r"</field\s*>(?=\s*(?:<field\b|</agently_output>))",
    flags=re.IGNORECASE,
)


def extract_xml_field_target(text: str) -> str | None:
    start = _TARGET_START_RE.search(text)
    if start is None:
        return None
    end = None
    for match in _TARGET_END_RE.finditer(text, start.end()):
        end = match
    if end is None:
        return None
    return text[start.start(): end.end()]


def _iter_field_sections(target: str):
    search_pos = 0
    while True:
        start = _FIELD_START_RE.search(target, search_pos)
        if start is None:
            return
        close = _FIELD_CLOSE_RE.search(target, start.end())
        if close is None:
            return
        yield start.group(1), start.group(2).lower(), target[start.end(): close.start()]
        search_pos = close.end()


def parse_xml_field_output(text: str, output_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(output_schema, Mapping) or not output_schema:
        return None

    target = extract_xml_field_target(text)
    if target is None:
        return None

    result: dict[str, Any] = {}
    for field_name, field_type, raw_content in _iter_field_sections(target):
        if field_name not in output_schema:
            continue
        content = raw_content.strip()
        if field_type == "json":
            ok, value = normalize_json_section_value(content, field_name=field_name)
        else:
            ok, value = normalize_scalar_section_value(content, field_name=field_name)
        if not ok:
            return None
        result[field_name] = value

    return result if result else None


class XmlFieldStreamingParser:
    """Field-level streaming parser for ``xml_field`` output."""

    def __init__(self, output_schema: Mapping[str, Any]):
        self._field_names = set(output_schema.keys()) if isinstance(output_schema, Mapping) else set()
        self._buffer = ""
        self._inside_target = False
        self._current_field: str | None = None
        self._current_type: str | None = None
        self._field_started: set[str] = set()
        self._field_completed: set[str] = set()

    async def parse_chunk(self, chunk: str) -> AsyncGenerator[StreamingData, None]:
        self._buffer += chunk

        while True:
            if not self._inside_target:
                start = re.search(r"<agently_output\b[^>]*>", self._buffer, flags=re.IGNORECASE)
                if start is None:
                    self._buffer = self._buffer[-64:]
                    return
                self._inside_target = True
                self._buffer = self._buffer[start.end():]

            if self._current_field is None:
                target_end = re.search(r"</agently_output>", self._buffer, flags=re.IGNORECASE)
                start = _FIELD_START_RE.search(self._buffer)
                if start is None:
                    if target_end is not None:
                        self._inside_target = False
                        self._buffer = self._buffer[target_end.end():]
                        continue
                    self._buffer = self._buffer[-128:]
                    return
                if target_end is not None and target_end.start() < start.start():
                    self._inside_target = False
                    self._buffer = self._buffer[target_end.end():]
                    continue
                field_name = start.group(1)
                self._current_field = field_name
                self._current_type = start.group(2).lower()
                self._field_started.add(field_name)
                self._buffer = self._buffer[start.end():]
                if field_name in self._field_names:
                    yield StreamingData(
                        path=field_name,
                        value="",
                        delta="",
                        is_completed=False,
                        event_type="delta",
                    )

            end = _FIELD_CLOSE_RE.search(self._buffer)
            if end is None:
                last_nl = self._buffer.rfind("\n")
                possible_close = self._buffer.lower().rfind("</field")
                if possible_close >= 0:
                    last_nl = min(last_nl, possible_close - 1)
                if last_nl >= 0:
                    safe = self._buffer[: last_nl + 1]
                    self._buffer = self._buffer[last_nl + 1:]
                    field = self._current_field
                    if safe.strip() and field is not None and field in self._field_names:
                        yield StreamingData(
                            path=field,
                            value=safe,
                            delta=safe,
                            is_completed=False,
                            event_type="delta",
                        )
                return

            content = self._buffer[:end.start()]
            self._buffer = self._buffer[end.end():]
            field = self._current_field
            if content.strip() and field is not None and field in self._field_names:
                yield StreamingData(
                    path=field,
                    value=content,
                    delta=content,
                    is_completed=False,
                    event_type="delta",
                )
            if field is not None and field in self._field_names:
                self._field_completed.add(field)
                yield StreamingData(
                    path=field,
                    value="",
                    delta="",
                    is_completed=True,
                    event_type="done",
                )
            self._current_field = None
            self._current_type = None

    async def flush(self) -> AsyncGenerator[StreamingData, None]:
        if self._current_field is not None and self._current_field in self._field_names:
            remaining = self._buffer.strip()
            if remaining:
                yield StreamingData(
                    path=self._current_field,
                    value=remaining,
                    delta=remaining,
                    is_completed=False,
                    event_type="delta",
                )
            yield StreamingData(
                path=self._current_field,
                value="",
                delta="",
                is_completed=True,
                event_type="done",
            )
            self._field_completed.add(self._current_field)
        self._buffer = ""
