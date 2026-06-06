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

"""YAML literal output parsing and streaming support."""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Mapping

import yaml

from agently.types.data.response import StreamingData


_YAML_TARGET_RE = re.compile(
    r"<<<BEGIN\s+AGENTLY_YAML>>>\s*(.*?)\s*<<<END\s+AGENTLY_YAML>>>",
    flags=re.DOTALL | re.IGNORECASE,
)


def extract_yaml_literal_target(text: str) -> str | None:
    match = _YAML_TARGET_RE.search(text)
    if match:
        return match.group(1).strip()
    fence = re.search(r"```(?:yaml|yml)?[ \t]*\n(.*?)\n?```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return None


def parse_yaml_literal_output(text: str, output_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(output_schema, Mapping) or not output_schema:
        return None
    target = extract_yaml_literal_target(text)
    if target is None:
        return None
    data = yaml.safe_load(target)
    if not isinstance(data, dict):
        return None
    return data


class YamlLiteralStreamingParser:
    """Top-level field streaming parser for ``yaml_literal`` output."""

    def __init__(self, output_schema: Mapping[str, Any]):
        self._field_names = list(output_schema.keys()) if isinstance(output_schema, Mapping) else []
        names = "|".join(re.escape(name) for name in self._field_names)
        self._field_header_pattern = re.compile(rf"^({names}):(?:\s*.*)?$", flags=re.MULTILINE) if names else None
        self._buffer = ""
        self._inside_target = False
        self._current_field: str | None = None
        self._field_started: set[str] = set()
        self._field_completed: set[str] = set()

    async def parse_chunk(self, chunk: str) -> AsyncGenerator[StreamingData, None]:
        if self._field_header_pattern is None:
            return

        self._buffer += chunk
        if not self._inside_target:
            start = re.search(r"<<<BEGIN\s+AGENTLY_YAML>>>", self._buffer, flags=re.IGNORECASE)
            if start is None:
                self._buffer = self._buffer[-128:]
                return
            self._inside_target = True
            self._buffer = self._buffer[start.end():]

        while True:
            match = self._field_header_pattern.search(self._buffer)
            if match is None:
                if self._current_field is not None:
                    last_nl = self._buffer.rfind("\n")
                    if last_nl >= 0:
                        safe = self._buffer[: last_nl + 1]
                        self._buffer = self._buffer[last_nl + 1:]
                        if safe.strip():
                            yield StreamingData(
                                path=self._current_field,
                                value=safe,
                                delta=safe,
                                is_complete=False,
                                event_type="delta",
                            )
                return

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

            field_name = match.group(1)
            self._current_field = field_name
            self._field_started.add(field_name)
            yield StreamingData(
                path=field_name,
                value="",
                delta="",
                is_complete=False,
                event_type="delta",
            )
            self._buffer = self._buffer[match.end():].lstrip("\n")

    async def flush(self) -> AsyncGenerator[StreamingData, None]:
        if self._current_field is not None:
            remaining = re.sub(
                r"<<<END\s+AGENTLY_YAML>>>.*$",
                "",
                self._buffer,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
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
