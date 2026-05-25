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

from copy import deepcopy
from typing import Any
from urllib.parse import urlparse


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def normalize_mcp_transport(transport: Any, *, headers: dict[str, str] | None = None) -> Any:
    """Normalize URL + headers into FastMCP-compatible MCP config shape."""

    if not headers:
        return transport
    normalized_headers = {str(key): str(value) for key, value in headers.items()}
    if isinstance(transport, str) and _is_url(transport):
        return {"mcpServers": {"default": {"url": transport, "headers": normalized_headers}}}
    if isinstance(transport, dict):
        normalized = deepcopy(transport)
        if isinstance(normalized.get("mcpServers"), dict):
            for config in normalized["mcpServers"].values():
                if isinstance(config, dict) and config.get("url"):
                    merged = dict(config.get("headers") or {})
                    merged.update(normalized_headers)
                    config["headers"] = merged
            return normalized
        if normalized.get("url"):
            merged = dict(normalized.get("headers") or {})
            merged.update(normalized_headers)
            normalized["headers"] = merged
            return normalized
    raise ValueError("headers= is supported only for MCP URL or URL-based MCP config transports.")
