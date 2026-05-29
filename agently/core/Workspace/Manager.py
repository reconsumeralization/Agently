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
from typing import Any, Callable, cast

from agently.types.plugins import IngestionProfile, WorkspaceBackend

from .Errors import WorkspaceConfigurationError
from .Facade import Workspace
from .LocalBackend import LocalWorkspaceBackend
from .Profiles import CheckpointIngestionProfile, FastIngestionProfile


class WorkspaceManager:
    """Factory and registry for Workspace foundation capabilities."""

    def __init__(self):
        self._profiles: dict[str, IngestionProfile] = {}
        self.register_profile("fast", FastIngestionProfile())
        self.register_profile("checkpoint", CheckpointIngestionProfile())

    def create(
        self,
        path_or_backend: str | Path | WorkspaceBackend,
        *,
        create: bool = True,
        mode: str = "read_write",
    ) -> Workspace:
        if hasattr(path_or_backend, "put") and hasattr(path_or_backend, "search"):
            backend = cast(WorkspaceBackend, path_or_backend)
        else:
            backend = LocalWorkspaceBackend(path_or_backend, create=create, mode=mode)  # type: ignore[arg-type]
        return Workspace(backend, self)

    def register_profile(self, name: str, handler: IngestionProfile | Callable[..., Any]):
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("Workspace profile name must be non-empty.")
        if not hasattr(handler, "ingest"):
            raise TypeError("Workspace profile handler must provide async ingest(...).")
        self._profiles[normalized] = handler  # type: ignore[assignment]
        return self

    def get_profile(self, name: str) -> IngestionProfile:
        normalized = str(name or "fast").strip() or "fast"
        if normalized not in self._profiles:
            raise WorkspaceConfigurationError(f"Workspace ingestion profile is not registered: { normalized }")
        return self._profiles[normalized]

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())
