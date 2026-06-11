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

from collections.abc import Callable
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agently.types.plugins import WorkspaceBackend

from .Workspace import Workspace

if TYPE_CHECKING:
    from .Manager import WorkspaceManager


class LazyWorkspace:
    """Agent-facing Workspace facade that materializes the backend on first use."""

    def __init__(
        self,
        manager: "WorkspaceManager",
        default_root: str | Path | WorkspaceBackend,
        *,
        create: bool = True,
        mode: str = "read_write",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        on_materialize: Callable[[Workspace], None] | None = None,
    ):
        self.manager = manager
        self._default_root = default_root
        self._create = create
        self._mode = mode
        self._provider = provider
        self._provider_options = dict(provider_options or {})
        self._workspace: Workspace | None = None
        self._on_materialize = on_materialize

    @property
    def is_materialized(self) -> bool:
        return self._workspace is not None

    @property
    def root(self) -> Path:
        workspace = self._workspace
        if workspace is not None:
            return workspace.root
        return Path(str(self._default_root)).expanduser().resolve()

    @property
    def content_root(self) -> Path:
        workspace = self._workspace
        if workspace is not None:
            return workspace.content_root
        return self.root / "content"

    @property
    def files_root(self) -> Path:
        workspace = self._workspace
        if workspace is not None:
            return workspace.files_root
        return self.root / "files"

    @property
    def backend(self) -> WorkspaceBackend:
        return self._materialize().backend

    def materialize(self) -> Workspace:
        return self._materialize()

    def capabilities(self):
        return self._materialize().capabilities()

    async def put_checkpoint(self, *args: Any, **kwargs: Any):
        return await self._materialize().put_checkpoint(*args, **kwargs)

    async def put_snapshot(self, *args: Any, **kwargs: Any):
        return await self._materialize().put_snapshot(*args, **kwargs)

    async def get_snapshot(self, *args: Any, **kwargs: Any):
        return await self._materialize().get_snapshot(*args, **kwargs)

    async def append_runtime_event(self, *args: Any, **kwargs: Any):
        return await self._materialize().append_runtime_event(*args, **kwargs)

    def _materialize(self) -> Workspace:
        if self._workspace is None:
            self._workspace = self.manager.create(
                self._default_root,
                create=self._create,
                mode=self._mode,
                provider=self._provider,
                provider_options=self._provider_options,
            )
            if self._on_materialize is not None:
                self._on_materialize(self._workspace)
        return self._workspace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._materialize(), name)

    def __repr__(self) -> str:
        state = "materialized" if self.is_materialized else "lazy"
        return f"<LazyWorkspace state={state} root={self.root!s}>"
