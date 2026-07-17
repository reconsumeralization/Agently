# Copyright 2023-2026 AgentEra(Agently.Tech)

from __future__ import annotations

from pathlib import Path
from typing import Any

from agently.types.data import (
    TaskWorkspaceExportResult,
    TaskWorkspaceFileInfo,
    TaskWorkspaceFileOperation,
    TaskWorkspaceReadResult,
    TaskWorkspaceWriteResult,
)
from agently.types.plugins import TaskWorkspaceFileIOHandler

from .FileIO import (
    DefaultTextTaskWorkspaceFileIOHandler,
    HtmlExportTaskWorkspaceFileIOHandler,
    ImageVLMTaskWorkspaceFileIOHandler,
    OfficeTaskWorkspaceFileIOHandler,
    PdfTaskWorkspaceFileIOHandler,
    inspect_task_workspace_file,
    unsupported_export_result,
    unsupported_read_result,
    unsupported_write_result,
)
from .TaskWorkspace import TaskWorkspace


class TaskWorkspaceManager:
    """Factory and format-handler registry for the explicit task file boundary."""

    def __init__(self) -> None:
        self._file_io_handlers: dict[str, TaskWorkspaceFileIOHandler] = {}
        self.register_file_io_handler(DefaultTextTaskWorkspaceFileIOHandler())
        self.register_file_io_handler(PdfTaskWorkspaceFileIOHandler())
        self.register_file_io_handler(OfficeTaskWorkspaceFileIOHandler())
        self.register_file_io_handler(ImageVLMTaskWorkspaceFileIOHandler())
        self.register_file_io_handler(HtmlExportTaskWorkspaceFileIOHandler())

    def create(
        self,
        root: str | Path,
        *,
        create: bool = True,
        mode: str = "read_only",
        execution_id: str | None = None,
    ) -> TaskWorkspace:
        return TaskWorkspace(
            root,
            create=create,
            mode=mode,
            execution_id=execution_id,
        )

    def register_file_io_handler(
        self,
        handler: TaskWorkspaceFileIOHandler,
        *,
        replace: bool = False,
    ) -> "TaskWorkspaceManager":
        name = str(getattr(handler, "name", "")).strip()
        if not name:
            raise ValueError("TaskWorkspace file IO handler name must be non-empty.")
        for method_name in ("supports", "read", "write", "export"):
            if not callable(getattr(handler, method_name, None)):
                raise TypeError(
                    f"TaskWorkspace file IO handler must provide {method_name}(...)."
                )
        if name in self._file_io_handlers and not replace:
            raise ValueError(f"TaskWorkspace file IO handler is already registered: {name}")
        self._file_io_handlers[name] = handler
        return self

    def unregister_file_io_handler(self, handler_id: str) -> "TaskWorkspaceManager":
        self._file_io_handlers.pop(str(handler_id).strip(), None)
        return self

    def list_file_io_handlers(self) -> list[str]:
        return sorted(self._file_io_handlers)

    def inspect_file_path(self, path: Path, *, relative_path: str) -> TaskWorkspaceFileInfo:
        return inspect_task_workspace_file(path, relative_path=relative_path)

    def _select_file_io_handler(
        self,
        *,
        operation: TaskWorkspaceFileOperation,
        file_info: TaskWorkspaceFileInfo,
        handler: str | None = None,
        export_kind: str | None = None,
    ) -> TaskWorkspaceFileIOHandler | None:
        if handler is not None:
            selected = self._file_io_handlers.get(str(handler).strip())
            if selected is None:
                raise KeyError(f"TaskWorkspace file IO handler is not registered: {handler}")
            return (
                selected
                if selected.supports(
                    operation=operation,
                    file_info=file_info,
                    export_kind=export_kind,
                )
                else None
            )
        for candidate in sorted(
            self._file_io_handlers.values(),
            key=lambda item: (
                int(getattr(item, "priority", 1000)),
                str(getattr(item, "name", "")),
            ),
        ):
            if candidate.supports(
                operation=operation,
                file_info=file_info,
                export_kind=export_kind,
            ):
                return candidate
        return None

    async def read_file_path(
        self,
        path: Path,
        *,
        relative_path: str,
        max_bytes: int,
        offset: int = 0,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult:
        file_info = self.inspect_file_path(path, relative_path=relative_path)
        selected = self._select_file_io_handler(
            operation="read",
            file_info=file_info,
            handler=handler,
        )
        if selected is None:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=handler or "none",
                code="task_workspace.file.no_read_handler",
                message="No registered TaskWorkspace file IO handler can read this file type.",
            )
        return await selected.read(
            path=path,
            file_info=file_info,
            max_bytes=max_bytes,
            offset=offset,
            options=options,
        )

    async def write_file_path(
        self,
        path: Path,
        *,
        relative_path: str,
        content: str,
        append: bool = False,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult:
        file_info = self.inspect_file_path(path, relative_path=relative_path)
        selected = self._select_file_io_handler(
            operation="write",
            file_info=file_info,
            handler=handler,
        )
        if selected is None:
            return unsupported_write_result(
                file_info=file_info,
                handler_id=handler or "none",
                code="task_workspace.file.no_write_handler",
                message="No registered TaskWorkspace file IO handler can write this file type.",
            )
        return await selected.write(
            path=path,
            file_info=file_info,
            content=content,
            append=append,
            options=options,
        )

    async def export_file_path(
        self,
        source_path: Path,
        output_path: Path,
        *,
        source_relative_path: str,
        output_relative_path: str,
        export_kind: str,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult:
        source_info = self.inspect_file_path(
            source_path,
            relative_path=source_relative_path,
        )
        output_info = self.inspect_file_path(
            output_path,
            relative_path=output_relative_path,
        )
        selected = self._select_file_io_handler(
            operation="export",
            file_info=source_info,
            handler=handler,
            export_kind=export_kind,
        )
        if selected is None:
            return unsupported_export_result(
                source_info=source_info,
                output_info=output_info,
                export_kind=export_kind,
                handler_id=handler or "none",
                code="task_workspace.file.no_export_handler",
                message=(
                    "No registered TaskWorkspace file IO handler can export this "
                    "source file to the requested kind."
                ),
            )
        return await selected.export(
            source_path=source_path,
            output_path=output_path,
            source_info=source_info,
            output_info=output_info,
            export_kind=export_kind,
            options=options,
        )


__all__ = ["TaskWorkspaceManager"]
