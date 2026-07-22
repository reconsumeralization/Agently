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
from typing import Any, Protocol, runtime_checkable

from .base import AgentlyPlugin

from agently.types.data.task_workspace import (
    TaskWorkspaceExportResult,
    TaskWorkspaceFileInfo,
    TaskWorkspaceFileOperation,
    TaskWorkspaceReadResult,
    TaskWorkspaceWriteResult,
)


@runtime_checkable
class TaskWorkspaceFileIOHandler(AgentlyPlugin, Protocol):
    """Plugin protocol for TaskWorkspace file read/write/export behavior.

    Implementations must keep parsing, rendering, MCP, and model-request
    dependencies behind their own handler boundary. TaskWorkspace owns path
    containment and dispatch; handlers own only the format-specific operation.
    """

    name: str
    priority: int

    def supports(
        self,
        *,
        operation: TaskWorkspaceFileOperation,
        file_info: TaskWorkspaceFileInfo,
        export_kind: str | None = None,
    ) -> bool: ...

    async def read(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        max_bytes: int = 20000,
        offset: int = 0,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult: ...

    async def write(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        content: str,
        append: bool = False,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult: ...

    async def export(
        self,
        *,
        source_path: Path,
        output_path: Path,
        source_info: TaskWorkspaceFileInfo,
        output_info: TaskWorkspaceFileInfo,
        export_kind: str,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult: ...
