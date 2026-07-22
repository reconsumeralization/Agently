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

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceAccessRequirement,
    TaskWorkspaceAccessRoot,
    TaskWorkspaceExecutionManifest,
    TaskWorkspaceExecutionManifestFile,
)

from .Errors import TaskWorkspacePolicyError

if TYPE_CHECKING:
    from .TaskWorkspace import TaskWorkspace


@dataclass
class _GrantState:
    grant: TaskWorkspaceAccessGrant
    requirement: TaskWorkspaceAccessRequirement
    snapshot_files: tuple[TaskWorkspaceExecutionManifestFile, ...] = ()
    manifest: TaskWorkspaceExecutionManifest | None = None


class TaskWorkspaceExecutionAccess:
    """Owns short-lived execution grants inside one TaskWorkspace boundary."""

    def __init__(self, task_workspace: "TaskWorkspace") -> None:
        self._task_workspace = task_workspace
        self._active: dict[str, _GrantState] = {}
        self._closed: set[str] = set()

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _safe_call_id(action_call_id: str) -> str:
        if not isinstance(action_call_id, str) or not action_call_id.strip():
            raise ValueError("action_call_id must be a non-empty string")
        compact = re.sub(r"[^A-Za-z0-9._-]+", "-", action_call_id.strip()).strip("-._")
        return (compact or "call")[:48]

    @staticmethod
    def _ensure_directory(base: Path, relative: PurePosixPath) -> Path:
        current = base
        for part in relative.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if current.is_symlink() or not current.is_dir():
                    raise TaskWorkspacePolicyError(
                        "TaskWorkspace execution path contains a symlink or non-directory."
                    )
            else:
                current.mkdir()
        return current

    @classmethod
    def _safe_file_target(cls, base: Path, relative: str) -> Path:
        path = PurePosixPath(relative)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise TaskWorkspacePolicyError("Execution file path is not contained.")
        parent = cls._ensure_directory(base, PurePosixPath(*path.parts[:-1]))
        target = parent / path.name
        if target.is_symlink():
            raise TaskWorkspacePolicyError("Execution file target cannot be a symlink.")
        resolved_base = base.resolve()
        resolved_target = target.resolve(strict=False)
        if resolved_target == resolved_base or resolved_base not in resolved_target.parents:
            raise TaskWorkspacePolicyError("Execution file path escaped its granted root.")
        return target

    def _manifest_file(self, path: Path, *, role: str) -> TaskWorkspaceExecutionManifestFile:
        if path.is_symlink() or not path.is_file():
            raise TaskWorkspacePolicyError("Execution manifest files must be regular files.")
        return TaskWorkspaceExecutionManifestFile(
            path=path.relative_to(self._task_workspace.root).as_posix(),
            host_path=str(path),
            sha256=self._digest(path),
            bytes=path.stat().st_size,
            role=role,
        )

    @staticmethod
    def _write_exact(target: Path, content: bytes, expected_digest: str) -> None:
        try:
            with target.open("xb") as file:
                file.write(content)
                file.flush()
        except FileExistsError as error:
            raise TaskWorkspacePolicyError(
                "Execution bundle has already been materialized or its target exists."
            ) from error
        actual = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
        if actual != expected_digest:
            target.unlink(missing_ok=True)
            raise TaskWorkspacePolicyError("Materialized execution file digest mismatch.")

    def _snapshot_input(self, source: Path, destination: Path) -> list[TaskWorkspaceExecutionManifestFile]:
        if source.is_symlink():
            raise TaskWorkspacePolicyError("Snapshot inputs cannot be symlinks.")
        if source.is_file():
            target = self._safe_file_target(destination.parent, destination.name)
            self._write_exact(target, source.read_bytes(), self._digest(source))
            return [self._manifest_file(target, role="input")]
        if not source.is_dir():
            raise FileNotFoundError(str(source))
        files: list[TaskWorkspaceExecutionManifestFile] = []
        for candidate in sorted(source.rglob("*")):
            if candidate.is_symlink():
                raise TaskWorkspacePolicyError("Snapshot input trees cannot contain symlinks.")
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(source).as_posix()
            target = self._safe_file_target(destination, relative)
            self._write_exact(target, candidate.read_bytes(), self._digest(candidate))
            files.append(self._manifest_file(target, role="input"))
        return files

    def issue(
        self,
        *,
        action_call_id: str,
        requirement: TaskWorkspaceAccessRequirement,
    ) -> TaskWorkspaceAccessGrant:
        if not isinstance(requirement, TaskWorkspaceAccessRequirement):
            raise TypeError("requirement must be TaskWorkspaceAccessRequirement")
        workspace = self._task_workspace
        if workspace.mode == "read_only" and requirement.mode == "read_write":
            raise TaskWorkspacePolicyError(
                "A read_only TaskWorkspace cannot issue read_write execution permission."
            )
        if requirement.mode == "snapshot" and requirement.include_workspace_root:
            raise TaskWorkspacePolicyError(
                "Snapshot execution access cannot expose the TaskWorkspace root."
            )

        grant_id = f"workspace-grant-{uuid.uuid4().hex}"
        safe_call_id = self._safe_call_id(action_call_id)
        execution_area = (
            workspace.fallback_root
            / "code_execution"
            / f"{safe_call_id}-{grant_id.rsplit('-', 1)[-1][:12]}"
        )
        execution_area.mkdir(parents=True, exist_ok=False)
        roots: list[TaskWorkspaceAccessRoot] = []
        for role, access_mode in (
            ("source", "read_only"),
            ("build", "read_write"),
            ("output", "read_write"),
            ("logs", "read_write"),
        ):
            path = execution_area / role
            path.mkdir()
            roots.append(
                TaskWorkspaceAccessRoot(
                    role=role,  # type: ignore[arg-type]
                    host_path=str(path),
                    access_mode=access_mode,  # type: ignore[arg-type]
                )
            )
        if requirement.mode in {"read_only", "read_write"}:
            workspace_access_mode = (
                "read_only" if requirement.mode == "read_only" else "read_write"
            )
            roots.insert(
                0,
                TaskWorkspaceAccessRoot(
                    role="workspace",
                    host_path=str(workspace.root),
                    access_mode=workspace_access_mode,
                ),
            )

        grant = TaskWorkspaceAccessGrant(
            grant_id=grant_id,
            task_workspace_id=workspace.task_workspace_id,
            execution_id=workspace.execution_id,
            action_call_id=action_call_id,
            mode=requirement.mode,
            execution_area=str(execution_area),
            roots=tuple(roots),
            issued_at=datetime.now(timezone.utc).isoformat(),
        )
        snapshot_files: list[TaskWorkspaceExecutionManifestFile] = []
        snapshot_root = execution_area / "source" / "workspace"
        for input_path in requirement.input_paths:
            source = workspace.resolve_file_path(input_path)
            destination = snapshot_root / Path(input_path)
            if source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
            else:
                destination.mkdir(parents=True, exist_ok=True)
            snapshot_files.extend(self._snapshot_input(source, destination))
        self._active[grant_id] = _GrantState(
            grant=grant,
            requirement=requirement,
            snapshot_files=tuple(snapshot_files),
        )
        return grant

    def _require_active(self, grant: TaskWorkspaceAccessGrant) -> _GrantState:
        if grant.grant_id in self._closed:
            raise TaskWorkspacePolicyError("TaskWorkspace execution grant is closed.")
        state = self._active.get(grant.grant_id)
        if state is None or state.grant != grant:
            raise TaskWorkspacePolicyError("TaskWorkspace execution grant is invalid or foreign.")
        workspace = self._task_workspace
        if (
            grant.task_workspace_id != workspace.task_workspace_id
            or grant.execution_id != workspace.execution_id
        ):
            raise TaskWorkspacePolicyError("TaskWorkspace execution grant identity mismatch.")
        area = Path(grant.execution_area)
        if area.is_symlink() or workspace.fallback_root.resolve() not in area.resolve().parents:
            raise TaskWorkspacePolicyError("TaskWorkspace execution grant escaped its boundary.")
        return state

    async def materialize(
        self,
        grant: TaskWorkspaceAccessGrant,
        bundle: CodeExecutionBundle,
    ) -> TaskWorkspaceExecutionManifest:
        state = self._require_active(grant)
        if state.manifest is not None:
            raise TaskWorkspacePolicyError("An execution bundle is already materialized for this grant.")
        if not isinstance(bundle, CodeExecutionBundle):
            raise TypeError("bundle must be CodeExecutionBundle")
        source_root = Path(grant.execution_area) / "source"
        files: list[TaskWorkspaceExecutionManifestFile] = []
        for item in bundle.files:
            target = self._safe_file_target(source_root, item.path)
            self._write_exact(target, item.content, item.sha256)
            files.append(self._manifest_file(target, role=item.role))

        declared_outputs: list[str] = []
        for path in (*bundle.expected_outputs, *state.requirement.output_paths):
            if not path.startswith("output/"):
                raise TaskWorkspacePolicyError(
                    "Declared execution outputs must live under the output root."
                )
            if path.casefold() not in {item.casefold() for item in declared_outputs}:
                declared_outputs.append(path)
        manifest = TaskWorkspaceExecutionManifest(
            grant_id=grant.grant_id,
            task_workspace_id=grant.task_workspace_id,
            execution_id=grant.execution_id,
            action_call_id=grant.action_call_id,
            bundle_id=bundle.bundle_id,
            bundle_digest=bundle.bundle_digest,
            files=tuple(files + list(state.snapshot_files)),
            entrypoint=f"source/{bundle.entrypoint}",
            expected_outputs=tuple(declared_outputs),
        )
        state.manifest = manifest
        return manifest

    async def collect_outputs(
        self,
        grant: TaskWorkspaceAccessGrant,
        paths: list[str] | tuple[str, ...],
    ) -> tuple[TaskWorkspaceExecutionManifestFile, ...]:
        state = self._require_active(grant)
        if state.manifest is None:
            raise TaskWorkspacePolicyError("Execution bundle must be materialized before output collection.")
        declared = {path.casefold(): path for path in state.manifest.expected_outputs}
        collected: list[TaskWorkspaceExecutionManifestFile] = []
        area = Path(grant.execution_area)
        for requested in paths:
            canonical = PurePosixPath(requested).as_posix()
            declared_path = declared.get(canonical.casefold())
            if declared_path is None:
                raise TaskWorkspacePolicyError(
                    f"Execution output was not declared by the bundle or grant: {requested!r}"
                )
            target = self._safe_file_target(area, declared_path)
            if not target.is_file() or target.is_symlink():
                raise FileNotFoundError(str(target))
            collected.append(self._manifest_file(target, role="output"))
        return tuple(collected)

    def close(self, grant_id: str) -> None:
        state = self._active.pop(grant_id, None)
        if state is None:
            if grant_id in self._closed:
                return
            raise TaskWorkspacePolicyError("TaskWorkspace execution grant does not exist.")
        self._closed.add(grant_id)


__all__ = ["TaskWorkspaceExecutionAccess"]
