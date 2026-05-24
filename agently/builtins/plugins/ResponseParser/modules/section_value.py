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

"""Shared section value normalization for markdown-style output formats."""

from __future__ import annotations

import re
from typing import Any

import json5

from .code_fence import strip_enclosing_code_fence


_COMMENT_RE = re.compile(r"^\s*<!--(?P<body>.*?)-->\s*$", flags=re.DOTALL)
_ANGLE_PLACEHOLDER_RE = re.compile(
    r"^\s*\(?\s*(?:json|text)?\s*\)?\s*<[^>\n]{1,160}>\s*$",
    flags=re.IGNORECASE,
)
_PLACEHOLDER_HINT_RE = re.compile(
    r"(?:placeholder|description|your\s+\w+|todo|字段|描述|说明|内容)",
    flags=re.IGNORECASE,
)


def normalize_scalar_section_value(
    content: str,
    *,
    field_name: str,
) -> tuple[bool, Any]:
    """Normalize a scalar markdown section.

    Returns ``(True, value)`` when the section is usable. Returns
    ``(False, None)`` for copied placeholders or JSON containers that can not
    represent a scalar field.
    """
    raw = _clean_section_text(content)
    if _looks_like_placeholder(raw):
        return False, None

    if _should_parse_scalar_json(raw):
        parsed_ok, parsed = _try_parse_json(raw)
        if parsed_ok:
            return _normalize_json_value(parsed, field_name=field_name, scalar=True)

    return True, raw


def normalize_complex_section_value(
    content: str,
    *,
    field_name: str,
) -> tuple[bool, Any]:
    """Normalize a complex markdown section into a dict/list value."""
    raw = _clean_section_text(content)
    if _looks_like_placeholder(raw):
        return False, None

    parsed_ok, parsed = _try_parse_json(raw)
    if not parsed_ok:
        return False, None
    return _normalize_json_value(parsed, field_name=field_name, scalar=False)


def _clean_section_text(content: str) -> str:
    return strip_enclosing_code_fence(content).strip()


def _try_parse_json(text: str) -> tuple[bool, Any]:
    try:
        return True, json5.loads(text)
    except Exception:
        return False, None


def _should_parse_scalar_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("{", "[", '"'))


def _normalize_json_value(
    value: Any,
    *,
    field_name: str,
    scalar: bool,
) -> tuple[bool, Any]:
    if isinstance(value, dict):
        if set(value.keys()) != {field_name}:
            return False, None
        value = value[field_name]

    if isinstance(value, str) and _looks_like_placeholder(value):
        return False, None

    if scalar:
        if isinstance(value, (dict, list)):
            return False, None
        return True, value

    if isinstance(value, (dict, list)):
        return True, value
    return False, None


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False

    comment = _COMMENT_RE.match(stripped)
    if comment:
        body = comment.group("body").strip()
        return bool(_ANGLE_PLACEHOLDER_RE.match(body) or _PLACEHOLDER_HINT_RE.search(body))

    if _ANGLE_PLACEHOLDER_RE.match(stripped) and _PLACEHOLDER_HINT_RE.search(stripped):
        return True

    return False
