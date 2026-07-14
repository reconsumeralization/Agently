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
from typing_extensions import Self

from agently.core import BaseAgent, Workspace
from agently.core.Workspace._defaults import default_workspace_root, script_scope


class WorkspaceExtension(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._workspace_explicit = False
        self.workspace: Workspace = self._create_workspace_binding()
        self._sync_workspace_settings(self.workspace, mode="read_only", lazy=True)

    def _default_workspace_root(self) -> str | Path:
        configured = self.settings.get("workspace.default_root", None)
        return Path(str(configured)).expanduser().resolve() if configured is not None else default_workspace_root()

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

    def _create_workspace_binding(self) -> Workspace:
        from agently.base import workspace as global_workspace

        root = self._default_workspace_root()
        return global_workspace.create(
            root,
            mode="read_only",
            default_scope=self._default_workspace_scope(),
            default_search_scope=self._default_workspace_search_scope(),
        )

    def _sync_workspace_settings(
        self,
        workspace: Workspace,
        *,
        mode: str | None = None,
        provider: str | None = None,
        lazy: bool | None = None,
    ) -> None:
        self.settings.set("workspace.root", str(workspace.root))
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
        if isinstance(workspace, Workspace) and workspace._backend is None:
            self.workspace = self._create_workspace_binding()
            self._sync_workspace_settings(self.workspace, mode="read_only", lazy=True)

    def use_workspace(
        self,
        path_or_backend: str | Path | Any = None,
        *,
        create: bool = True,
        mode: str = "read_only",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        db_store_provider: Any | None = None,
        db_store_options: dict[str, Any] | None = None,
        embedding_provider: Any | None = None,
        embedding_options: dict[str, Any] | None = None,
        vector_store_provider: Any | None = None,
        vector_store_options: dict[str, Any] | None = None,
    ) -> Self:
        from agently.base import workspace as global_workspace

        self._workspace_explicit = True
        self.workspace = global_workspace.create(
            path_or_backend,
            create=create,
            mode=mode,
            provider=provider,
            provider_options=provider_options,
            db_store_provider=db_store_provider,
            db_store_options=db_store_options,
            embedding_provider=embedding_provider,
            embedding_options=embedding_options,
            vector_store_provider=vector_store_provider,
            vector_store_options=vector_store_options,
        )
        self._sync_workspace_settings(
            self.workspace,
            mode=mode,
            provider=provider,
            lazy=self.workspace._backend is None,
        )
        bind_session_memory = getattr(self, "_bind_activated_session_memory_workspace", None)
        if callable(bind_session_memory):
            bind_session_memory()
        return self
