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

import posixpath
from pathlib import PurePosixPath
from urllib.parse import SplitResult, urlsplit, urlunsplit


def normalize_locator(locator_kind: str, value: str) -> str:
    kind = str(locator_kind or "").strip().lower()
    text = str(value or "").strip()
    if not kind or not text:
        raise ValueError("Workspace locators require a kind and value.")
    if kind == "path":
        normalized = PurePosixPath(text.replace("\\", "/")).as_posix()
        if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
            raise ValueError("Workspace identity paths must be relative and contained.")
        return normalized
    if kind == "url":
        parsed = urlsplit(text)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("Workspace URL locators must be absolute URLs.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Workspace URL locators cannot contain credentials.")
        scheme = parsed.scheme.lower()
        host = parsed.hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("Workspace URL locator port is invalid.") from error
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        netloc = host if port is None or default_port else f"{host}:{port}"
        raw_path = parsed.path or "/"
        path = posixpath.normpath(raw_path)
        if raw_path.startswith("/") and not path.startswith("/"):
            path = f"/{path}"
        if raw_path.endswith("/") and not path.endswith("/"):
            path = f"{path}/"
        return urlunsplit(SplitResult(scheme, netloc, path, parsed.query, ""))
    return text
