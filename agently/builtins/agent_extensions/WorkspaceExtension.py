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

from agently.core import BaseAgent, Workspace


class WorkspaceExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace: Workspace | None = None

    def use_workspace(
        self,
        path_or_backend: str | Path | Any,
        *,
        create: bool = True,
        mode: str = "read_write",
    ):
        from agently.base import workspace as global_workspace

        self.workspace = global_workspace.create(path_or_backend, create=create, mode=mode)
        self.settings.set("workspace.root", str(self.workspace.root))
        self.settings.set("workspace.content_root", str(self.workspace.content_root))
        self.settings.set("workspace.files_root", str(self.workspace.files_root))
        self.settings.set("workspace.mode", mode)
        return self
