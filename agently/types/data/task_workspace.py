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

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from typing_extensions import NotRequired, TypedDict


TaskWorkspaceFileOperation = Literal["read", "write", "export"]
TaskWorkspaceAccessMode = Literal["snapshot", "read_only", "read_write"]
TaskWorkspaceAccessRootRole = Literal[
    "workspace",
    "source",
    "build",
    "output",
    "logs",
]


def _execution_relative_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} path must be a string")
    normalized = value.strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\\" in normalized
        or "\x00" in normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{field_name} path must be normalized and relative")
    if path.parts[0].casefold() == ".agently":
        raise ValueError(f"{field_name} path must not address private TaskWorkspace data")
    return path.as_posix()


@dataclass(frozen=True)
class TaskWorkspaceAccessRequirement:
    mode: TaskWorkspaceAccessMode = "snapshot"
    include_workspace_root: bool = False
    input_paths: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()
    retain_source: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"snapshot", "read_only", "read_write"}:
            raise ValueError(f"unsupported TaskWorkspace access mode: {self.mode!r}")
        input_paths = tuple(
            _execution_relative_path(path, field_name="input") for path in self.input_paths
        )
        output_paths = tuple(
            _execution_relative_path(path, field_name="output") for path in self.output_paths
        )
        if len({path.casefold() for path in input_paths}) != len(input_paths):
            raise ValueError("input path contains a duplicate or case collision")
        if len({path.casefold() for path in output_paths}) != len(output_paths):
            raise ValueError("output path contains a duplicate or case collision")
        object.__setattr__(self, "input_paths", input_paths)
        object.__setattr__(self, "output_paths", output_paths)


@dataclass(frozen=True)
class TaskWorkspaceAccessRoot:
    role: TaskWorkspaceAccessRootRole
    host_path: str
    access_mode: Literal["read_only", "read_write"]


@dataclass(frozen=True)
class TaskWorkspaceAccessGrant:
    grant_id: str
    task_workspace_id: str
    execution_id: str
    action_call_id: str
    mode: TaskWorkspaceAccessMode
    execution_area: str
    roots: tuple[TaskWorkspaceAccessRoot, ...]
    issued_at: str


@dataclass(frozen=True)
class TaskWorkspaceExecutionManifestFile:
    path: str
    host_path: str
    sha256: str
    bytes: int
    role: str


@dataclass(frozen=True)
class TaskWorkspaceExecutionManifest:
    grant_id: str
    task_workspace_id: str
    execution_id: str
    action_call_id: str
    bundle_id: str
    bundle_digest: str
    files: tuple[TaskWorkspaceExecutionManifestFile, ...]
    entrypoint: str
    expected_outputs: tuple[str, ...]


class TaskWorkspaceDiagnostic(TypedDict):
    code: str
    message: str
    handler_id: NotRequired[str | None]
    dependency: NotRequired[str | None]
    detail: NotRequired[dict[str, Any]]


class TaskWorkspaceFileRef(TypedDict):
    path: str
    sha256: str
    type: NotRequired[Literal["file"]]
    task_workspace_id: NotRequired[str]
    execution_id: NotRequired[str | None]
    size: NotRequired[int]
    available: NotRequired[bool]
    bytes: NotRequired[int]
    media_type: NotRequired[str | None]
    content_kind: NotRequired[str]
    role: NotRequired[str]
    locator_id: NotRequired[str]
    content_version_id: NotRequired[str]
    segment_id: NotRequired[str]
    link_id: NotRequired[str]


class TaskWorkspaceFileInfo(TypedDict):
    path: str
    extension: str
    media_type: str | None
    content_kind: str
    bytes: int
    sha256: str
    signatures: list[str]
    readable: bool
    writable: bool
    exists: bool


class TaskWorkspaceReadResult(TypedDict):
    ok: bool
    readable: bool
    path: str
    content: str
    truncated: bool
    bytes: int
    offset: int
    read_bytes: int
    sha256: str
    media_type: str | None
    content_kind: str
    encoding: str | None
    handler_id: str
    extraction_method: str
    diagnostics: list[TaskWorkspaceDiagnostic]
    file_refs: list[TaskWorkspaceFileRef]
    attachments: NotRequired[list[dict[str, Any]]]


class TaskWorkspaceWriteResult(TypedDict):
    ok: bool
    writable: bool
    path: str
    bytes: int
    sha256: str
    media_type: str | None
    content_kind: str
    encoding: str | None
    mode: str
    handler_id: str
    replacements: NotRequired[int]
    diagnostics: list[TaskWorkspaceDiagnostic]
    file_refs: list[TaskWorkspaceFileRef]


class TaskWorkspaceExportResult(TypedDict):
    ok: bool
    exported: bool
    source_path: str
    output_path: str
    export_kind: str
    bytes: int
    sha256: str
    media_type: str | None
    content_kind: str
    handler_id: str
    diagnostics: list[TaskWorkspaceDiagnostic]
    file_refs: list[TaskWorkspaceFileRef]


TaskWorkspaceTerminalStatus = Literal["completed", "failed", "cancelled"]


class TaskWorkspaceRetentionDiagnostic(TypedDict, total=False):
    code: str
    message: str
    retryable: bool
    entity: str
    detail: dict[str, Any]


class TaskWorkspaceRetentionResult(TypedDict):
    status: Literal["applied", "deferred", "noop"]
    execution_id: str
    retained_refs: list[TaskWorkspaceFileRef]
    retained_bytes: int
    deleted_bytes: int
    diagnostics: list[TaskWorkspaceRetentionDiagnostic]


class _ResultMapping(Mapping[str, Any]):
    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True)
class TaskWorkspaceFileRead(_ResultMapping):
    path: str
    content: str
    data: bytes
    total_bytes: int
    offset: int
    truncated: bool
    sha256: str
    media_type: str | None
    task_workspace_id: str
    execution_id: str
    readable: bool = True
    content_kind: str = "text"
    encoding: str | None = "utf-8"
    handler_id: str = "task_workspace.text"
    extraction_method: str = "plain_text"
    diagnostics: tuple[TaskWorkspaceDiagnostic, ...] = ()
    attachments: tuple[dict[str, Any], ...] = ()

    @property
    def exists(self) -> bool:
        return True

    @property
    def bytes(self) -> int:
        return self.total_bytes

    @property
    def read_bytes(self) -> int:
        return len(self.data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.readable,
            "exists": True,
            "readable": self.readable,
            "path": self.path,
            "content": self.content,
            "data": self.data,
            "bytes": self.total_bytes,
            "total_bytes": self.total_bytes,
            "read_bytes": len(self.data),
            "offset": self.offset,
            "truncated": self.truncated,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "content_kind": self.content_kind,
            "encoding": self.encoding,
            "handler_id": self.handler_id,
            "extraction_method": self.extraction_method,
            "diagnostics": [dict(item) for item in self.diagnostics],
            "attachments": [dict(item) for item in self.attachments],
            "file_refs": [
                {
                    "type": "file",
                    "task_workspace_id": self.task_workspace_id,
                    "execution_id": self.execution_id,
                    "path": self.path,
                    "size": self.total_bytes,
                    "bytes": self.total_bytes,
                    "sha256": self.sha256,
                    "media_type": self.media_type,
                    "content_kind": self.content_kind,
                    "role": "source",
                }
            ],
        }


@dataclass(frozen=True)
class TaskWorkspaceFileWrite(_ResultMapping):
    path: str
    requested_path: str
    bytes: int
    sha256: str
    fallback: bool
    task_workspace_id: str
    execution_id: str
    replacements: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "path": self.path,
            "requested_path": self.requested_path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "fallback": self.fallback,
            "replacements": self.replacements,
            "file_refs": [
                {
                    "type": "file",
                    "task_workspace_id": self.task_workspace_id,
                    "execution_id": self.execution_id,
                    "path": self.path,
                    "size": self.bytes,
                    "bytes": self.bytes,
                    "sha256": self.sha256,
                    "role": "artifact",
                }
            ],
        }


__all__ = [
    "TaskWorkspaceAccessGrant",
    "TaskWorkspaceAccessMode",
    "TaskWorkspaceAccessRequirement",
    "TaskWorkspaceAccessRoot",
    "TaskWorkspaceAccessRootRole",
    "TaskWorkspaceDiagnostic",
    "TaskWorkspaceExportResult",
    "TaskWorkspaceFileInfo",
    "TaskWorkspaceFileOperation",
    "TaskWorkspaceFileRead",
    "TaskWorkspaceFileRef",
    "TaskWorkspaceFileWrite",
    "TaskWorkspaceExecutionManifest",
    "TaskWorkspaceExecutionManifestFile",
    "TaskWorkspaceReadResult",
    "TaskWorkspaceRetentionDiagnostic",
    "TaskWorkspaceRetentionResult",
    "TaskWorkspaceTerminalStatus",
    "TaskWorkspaceWriteResult",
]
