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

from pathlib import Path
from typing import Any

from agently.core import BaseAgent, LazyWorkspace, Workspace
from agently.core.workspace._defaults import default_physical_root, scoped_files_root, script_scope, slug


class WorkspaceExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._workspace_explicit = False
        self.workspace: Workspace | LazyWorkspace = self._create_lazy_workspace()
        self._sync_workspace_settings(self.workspace, mode="read_write", lazy=True)

    def _default_workspace_root(self) -> str | Path:
        return default_physical_root(self.settings)

    def _default_workspace_files_root(self, root: str | Path) -> Path:
        return scoped_files_root(root, "agents", slug(str(self.name), "agent"))

    def _default_workspace_scope(self) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "agent_id": self.id,
            "agent_name": self.name,
        }
        session_id = self.settings.get("runtime.session_id", None)
        if session_id is not None:
            scope["session_id"] = str(session_id)
        else:
            scope["script_scope"] = script_scope(self.settings)
        project_id = self.settings.get("workspace.project_id", None)
        if project_id is not None:
            scope["project_id"] = str(project_id)
        return scope

    def _default_workspace_search_scope(self) -> dict[str, Any]:
        scope: dict[str, Any] = {}
        session_id = self.settings.get("runtime.session_id", None)
        if session_id is not None:
            scope["session_id"] = str(session_id)
        else:
            scope["script_scope"] = script_scope(self.settings)
        project_id = self.settings.get("workspace.project_id", None)
        if project_id is not None:
            scope["project_id"] = str(project_id)
        return scope

    def _create_lazy_workspace(self) -> LazyWorkspace:
        from agently.base import workspace as global_workspace

        root = self._default_workspace_root()
        return LazyWorkspace(
            global_workspace,
            root,
            files_root=self._default_workspace_files_root(root),
            default_scope=self._default_workspace_scope(),
            default_search_scope=self._default_workspace_search_scope(),
            on_materialize=lambda workspace: self._sync_workspace_settings(workspace, lazy=False),
        )

    def _sync_workspace_settings(
        self,
        workspace: Workspace | LazyWorkspace,
        *,
        mode: str | None = None,
        provider: str | None = None,
        lazy: bool | None = None,
    ) -> None:
        self.settings.set("workspace.root", str(workspace.root))
        self.settings.set("workspace.content_root", str(workspace.content_root))
        self.settings.set("workspace.files_root", str(workspace.files_root))
        if mode is not None:
            self.settings.set("workspace.mode", mode)
        if provider is not None:
            self.settings.set("workspace.provider", provider)
        if lazy is not None:
            self.settings.set("workspace.lazy", lazy)

    def _refresh_default_workspace_binding(self) -> None:
        if self._workspace_explicit:
            return
        workspace = getattr(self, "workspace", None)
        if isinstance(workspace, LazyWorkspace) and not workspace.is_materialized:
            self.workspace = self._create_lazy_workspace()
            self._sync_workspace_settings(self.workspace, mode="read_write", lazy=True)

    def use_workspace(
        self,
        path_or_backend: str | Path | Any = None,
        *,
        create: bool = True,
        mode: str = "read_write",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
    ):
        from agently.base import workspace as global_workspace

        self._workspace_explicit = True
        self.workspace = global_workspace.create(
            path_or_backend,
            create=create,
            mode=mode,
            provider=provider,
            provider_options=provider_options,
        )
        self._sync_workspace_settings(
            self.workspace,
            mode=mode,
            provider=provider,
            lazy=False,
        )
        return self
