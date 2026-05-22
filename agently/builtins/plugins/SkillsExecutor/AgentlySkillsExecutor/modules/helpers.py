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

from __future__ import annotations

from typing import Any

from agently.utils.DataGuardian import _sanitize_id

# ── Semantic type aliases ───────────────────────────────────────────────────

_SEMANTIC_TYPE_ALIASES: dict[str, list[str]] = {
    "docx": ["docx", "word", "document"],
    "pdf": ["pdf", "printable", "handout"],
    "pptx": ["pptx", "powerpoint", "slides", "slide deck"],
    "xlsx": ["xlsx", "excel", "spreadsheet", "workbook"],
    "json": ["json", "structured"],
    "md": ["markdown", "md"],
    "directory": ["folder", "directory"],
    "zip": ["zip", "archive"],
}


def _semantic_role_and_type(name: str) -> tuple[str, str]:
    cleaned = str(name).strip().strip("/")
    if not cleaned:
        return "output", "artifact"
    leaf = cleaned.split("/")[-1]
    if "." not in leaf:
        return _sanitize_id(leaf), "directory"
    role, suffix = leaf.rsplit(".", 1)
    return _sanitize_id(role), suffix.lower()
