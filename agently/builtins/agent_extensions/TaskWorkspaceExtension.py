# Copyright 2023-2026 AgentEra(Agently.Tech)

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing_extensions import Self

from agently.core import BaseAgent, TaskWorkspace
from agently.core.TaskWorkspace._defaults import default_task_workspace_root


class TaskWorkspaceExtension(BaseAgent):
    """Bind the public task file boundary."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.task_workspace = self._create_task_workspace_binding()
        self._sync_task_workspace_settings()

    def _default_task_workspace_root(self) -> Path:
        configured = self.settings.get("task_workspace.default_parent", None)
        parent = (
            Path(str(configured)).expanduser().resolve()
            if configured is not None
            else default_task_workspace_root()
        )
        return parent / ".agently" / "task_workspaces" / self.id

    def _create_task_workspace_binding(
        self,
        root: str | Path | None = None,
        *,
        mode: str = "read_only",
        create: bool = True,
    ) -> TaskWorkspace:
        return TaskWorkspace(
            root or self._default_task_workspace_root(),
            mode=mode,
            create=create,
            execution_id=self.id,
        )

    def _sync_task_workspace_settings(self) -> None:
        self.settings.set("task_workspace.root", str(self.task_workspace.root))
        self.settings.set("task_workspace.mode", self.task_workspace.mode)

    def use_task_workspace(
        self,
        root: str | Path,
        *,
        create: bool = True,
        mode: str = "read_only",
    ) -> Self:
        self.task_workspace = self._create_task_workspace_binding(
            root,
            mode=mode,
            create=create,
        )
        self._sync_task_workspace_settings()
        return self


__all__ = ["TaskWorkspaceExtension"]
