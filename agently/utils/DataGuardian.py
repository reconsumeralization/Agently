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

"""Defensive data guards used at framework boundaries.

Pure functions with no framework dependencies. They protect internal code from
malformed or unexpected external data and are not coupled to any subsystem.
"""

from __future__ import annotations

import copy
import re
from typing import Any


def _ensure_dict(value: Any) -> dict[str, Any]:
    """Return *value* if it is a dict, otherwise an empty dict."""
    return dict(value) if isinstance(value, dict) else {}


def _ensure_list(value: Any) -> list[Any]:
    """Normalize None / list / tuple / set / scalar into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    """Return a list containing only the dict items from *value*."""
    return [dict(item) for item in _ensure_list(value) if isinstance(item, dict)]


def _ensure_string_list(value: Any) -> list[str]:
    """Return a list of non-empty strings coerced from *value*."""
    return [str(item) for item in _ensure_list(value) if str(item).strip()]


def _copy_public(value: Any) -> Any:
    """Deep-copy a value to prevent accidental mutation of shared state."""
    return copy.deepcopy(value)


def _sanitize_id(value: str) -> str:
    """Normalize a string into a safe identifier slug (alphanumeric, underscore, dot, hyphen)."""
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip()).strip("_.-")
    return normalized or "unnamed"
