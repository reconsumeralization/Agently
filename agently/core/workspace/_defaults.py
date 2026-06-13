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

import sys
from pathlib import Path
from typing import Any

from ._utils import slug


def default_workspace_base_root() -> Path:
    return Path(".agently") / "workspaces"


def script_scope(settings: Any = None) -> str:
    configured = _settings_get(settings, "workspace.script_scope")
    if configured is not None:
        return slug(str(configured), "script")
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        name = Path(argv0).stem
        if name:
            return slug(name, "script")
    return slug(Path.cwd().name, "script")


def default_physical_root(settings: Any = None, *, session_id: str | None = None) -> Path:
    configured = _settings_get(settings, "workspace.default_root")
    if configured is not None:
        return Path(str(configured))
    configured_project = _settings_get(settings, "workspace.project_id")
    if configured_project is not None:
        return default_workspace_base_root() / "projects" / slug(str(configured_project), "project")
    resolved_session_id = session_id or _settings_get(settings, "runtime.session_id")
    if resolved_session_id:
        return default_workspace_base_root() / "sessions" / slug(str(resolved_session_id), "session")
    return default_workspace_base_root() / "scripts" / script_scope(settings)


def scoped_files_root(physical_root: str | Path, scope_kind: str, scope_id: str | None) -> Path:
    normalized_kind = slug(scope_kind, "scope")
    normalized_id = slug(scope_id or "default", "default")
    return Path(physical_root) / "files" / normalized_kind / normalized_id


def merge_scope(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    merged = {key: value for key, value in dict(base or {}).items() if value is not None}
    merged.update({key: value for key, value in dict(override or {}).items() if value is not None})
    return merged


def _settings_get(settings: Any, key: str) -> Any:
    if settings is None:
        return None
    getter = getattr(settings, "get", None)
    if callable(getter):
        return getter(key, None)
    if isinstance(settings, dict):
        return settings.get(key)
    return None

