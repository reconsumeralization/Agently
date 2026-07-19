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

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from agently.types.data import TaskWorkspaceFileRead, TaskWorkspaceFileWrite
from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceAccessRequirement,
    TaskWorkspaceExecutionManifest,
    TaskWorkspaceExecutionManifestFile,
)

from .Errors import TaskWorkspacePolicyError
from .Identity import TaskWorkspaceIdentityCatalog


class TaskWorkspace:
    """One explicit task file boundary for source files and produced artifacts."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        mode: str = "read_only",
        create: bool = True,
        execution_id: str | None = None,
    ) -> None:
        if mode not in {"read_only", "read_write"}:
            raise ValueError("TaskWorkspace mode must be 'read_only' or 'read_write'.")
        self.root = Path(root).expanduser().resolve()
        if self.root.exists() and not self.root.is_dir():
            raise NotADirectoryError(str(self.root))
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.root.is_dir():
            raise FileNotFoundError(str(self.root))
        self.mode = mode
        self.execution_id = str(execution_id or f"task_{uuid.uuid4().hex}")
        from .Manager import TaskWorkspaceManager

        self.manager = TaskWorkspaceManager()
        self._identity_catalog = TaskWorkspaceIdentityCatalog(
            self.root / ".agently",
            task_workspace_id=self.task_workspace_id,
        )
        from .ExecutionAccess import TaskWorkspaceExecutionAccess

        self._execution_access = TaskWorkspaceExecutionAccess(self)

    @property
    def task_workspace_id(self) -> str:
        return f"task_workspace:{hashlib.sha256(str(self.root).encode('utf-8')).hexdigest()}"

    @property
    def fallback_root(self) -> Path:
        return self.root / ".agently" / "files" / self.execution_id

    def issue_execution_access(
        self,
        *,
        action_call_id: str,
        requirement: TaskWorkspaceAccessRequirement,
    ) -> TaskWorkspaceAccessGrant:
        return self._execution_access.issue(
            action_call_id=action_call_id,
            requirement=requirement,
        )

    async def materialize_execution_bundle(
        self,
        grant: TaskWorkspaceAccessGrant,
        bundle: CodeExecutionBundle,
    ) -> TaskWorkspaceExecutionManifest:
        return await self._execution_access.materialize(grant, bundle)

    async def collect_execution_outputs(
        self,
        grant: TaskWorkspaceAccessGrant,
        paths: list[str] | tuple[str, ...],
    ) -> tuple[TaskWorkspaceExecutionManifestFile, ...]:
        return await self._execution_access.collect_outputs(grant, paths)

    def close_execution_access(self, grant_id: str) -> None:
        self._execution_access.close(grant_id)

    def resolve_path(self, path: str | os.PathLike[str]) -> Path:
        raw = Path(path).expanduser()
        target = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        if target != self.root and self.root not in target.parents:
            raise TaskWorkspacePolicyError(
                f"Path is outside TaskWorkspace root: {path!s}"
            )
        return target

    def resolve_file_path(self, path: str | os.PathLike[str] = ".") -> Path:
        target = self.resolve_path(path)
        if target.exists():
            return target
        raw = Path(path)
        if not raw.is_absolute():
            relative = raw.as_posix()
            if not (relative == ".agently" or relative.startswith(".agently/")):
                fallback = (self.fallback_root / raw).resolve()
                if self.fallback_root.resolve() in fallback.parents and fallback.exists():
                    return fallback
        return target

    def _resolve_external_file_path(self, path: str | os.PathLike[str] = ".") -> Path:
        return self.resolve_path(path)

    def _ordinary_file_relative_path(self, path: str | os.PathLike[str]) -> str:
        target = Path(path).expanduser().resolve()
        return self._relative(target)

    def inspect_file(self, path: str | os.PathLike[str]) -> dict[str, object]:
        target = self.resolve_file_path(path)
        return dict(
            self.manager.inspect_file_path(
                target,
                relative_path=self._relative(target),
            )
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _write_target(self, requested: Path, requested_path: str) -> tuple[Path, bool]:
        fallback_root = self.fallback_root.resolve()
        if requested == fallback_root or fallback_root in requested.parents:
            # A task may continue or replace the private carrier returned by a
            # prior write, but it cannot address another execution's private
            # state through an arbitrary .agently path.
            return requested, True
        if requested_path.startswith(".agently/") or requested_path == ".agently":
            raise TaskWorkspacePolicyError("Caller paths cannot target TaskWorkspace private state.")
        if self.mode == "read_write":
            return requested, False
        if requested.exists():
            raise TaskWorkspacePolicyError(
                "External TaskWorkspace mutation requires explicit write permission."
            )
        fallback = (fallback_root / Path(requested_path)).resolve()
        if fallback_root not in fallback.parents:
            raise TaskWorkspacePolicyError(
                f"Path is outside TaskWorkspace root: {requested_path}"
            )
        return fallback, True

    async def _write_bytes(self, path: str | os.PathLike[str], data: bytes) -> TaskWorkspaceFileWrite:
        requested_path = Path(path).as_posix()
        requested = self.resolve_path(path)
        target, fallback = self._write_target(requested, requested_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return TaskWorkspaceFileWrite(
            path=self._relative(target),
            requested_path=requested_path,
            bytes=len(data),
            sha256=self._sha256(target),
            fallback=fallback,
            task_workspace_id=self.task_workspace_id,
            execution_id=self.execution_id,
        )

    async def write_file(
        self,
        path: str | os.PathLike[str],
        content: str,
        *,
        append: bool = False,
    ) -> TaskWorkspaceFileWrite:
        requested_path = Path(path).as_posix()
        requested = self.resolve_path(path)
        target, fallback = self._write_target(requested, requested_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8") as file:
            file.write(str(content))
        return TaskWorkspaceFileWrite(
            path=self._relative(target),
            requested_path=requested_path,
            bytes=target.stat().st_size,
            sha256=self._sha256(target),
            fallback=fallback,
            task_workspace_id=self.task_workspace_id,
            execution_id=self.execution_id,
        )

    async def read_file(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = 20000,
        offset: int = 0,
    ) -> TaskWorkspaceFileRead:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive.")
        if offset < 0:
            raise ValueError("offset cannot be negative.")
        target = self.resolve_file_path(path)
        if not target.is_file():
            raise FileNotFoundError(f"TaskWorkspace file not found: {path}")
        result = await self.manager.read_file_path(
            target,
            relative_path=self._relative(target),
            max_bytes=max_bytes,
            offset=offset,
        )
        content = str(result.get("content") or "")
        data = content.encode(str(result.get("encoding") or "utf-8"), errors="replace")
        return TaskWorkspaceFileRead(
            path=str(result.get("path") or self._relative(target)),
            content=content,
            data=data,
            total_bytes=int(result.get("bytes") or target.stat().st_size),
            offset=int(result.get("offset") or offset),
            truncated=bool(result.get("truncated")),
            sha256=str(result.get("sha256") or self._sha256(target)),
            media_type=result.get("media_type"),
            task_workspace_id=self.task_workspace_id,
            execution_id=self.execution_id,
            readable=bool(result.get("readable")),
            content_kind=str(result.get("content_kind") or "unknown"),
            encoding=(
                str(result.get("encoding"))
                if result.get("encoding") is not None
                else None
            ),
            handler_id=str(result.get("handler_id") or "none"),
            extraction_method=str(result.get("extraction_method") or "none"),
            diagnostics=tuple(result.get("diagnostics") or ()),
            attachments=tuple(result.get("attachments") or ()),
        )

    async def edit_file(
        self,
        path: str | os.PathLike[str],
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
        expected_sha256: str | None = None,
    ) -> TaskWorkspaceFileWrite:
        target = self.resolve_file_path(path)
        if self.mode != "read_write" and not (
            target == self.fallback_root or self.fallback_root in target.parents
        ):
            raise TaskWorkspacePolicyError(
                "External TaskWorkspace editing requires explicit write permission."
            )
        if not target.is_file():
            raise FileNotFoundError(f"TaskWorkspace file not found: {path}")
        if expected_sha256 and self._sha256(target) != str(expected_sha256):
            raise ValueError("TaskWorkspace file has changed since the expected sha256.")
        text = target.read_text(encoding="utf-8")
        if old_string not in text:
            raise ValueError("old_string was not found in the TaskWorkspace file.")
        if not replace_all and text.count(old_string) != 1:
            raise ValueError("old_string is not unique; pass replace_all=True to replace every match.")
        updated = text.replace(old_string, new_string, -1 if replace_all else 1)
        replacements = text.count(old_string) if replace_all else 1
        target.write_text(updated, encoding="utf-8")
        return TaskWorkspaceFileWrite(
            path=self._relative(target),
            requested_path=Path(path).as_posix(),
            bytes=target.stat().st_size,
            sha256=self._sha256(target),
            fallback=self.fallback_root in target.parents,
            task_workspace_id=self.task_workspace_id,
            execution_id=self.execution_id,
            replacements=replacements,
        )

    async def copy_from(
        self,
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> TaskWorkspaceFileWrite:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(str(source_path))
        return await self._write_bytes(destination, source_path.read_bytes())

    async def materialize_file(
        self,
        path: str | os.PathLike[str],
        content: bytes,
        *,
        source: dict[str, Any] | None = None,
        media_type: str | None = None,
        overwrite: bool = False,
    ) -> TaskWorkspaceFileWrite:
        """Materialize trusted binary bytes inside this task file boundary."""

        _ = source, media_type
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("TaskWorkspace.materialize_file(...) requires bytes content.")
        if self.resolve_file_path(path).exists() and not overwrite:
            raise FileExistsError(str(self.resolve_file_path(path)))
        return await self._write_bytes(path, bytes(content))

    def list_files(self, *, include_private_artifacts: bool = True) -> tuple[str, ...]:
        files: list[str] = []
        for path in self.root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if relative.parts and relative.parts[0] == ".agently":
                allowed = (
                    include_private_artifacts
                    and len(relative.parts) >= 3
                    and relative.parts[:3] == (".agently", "files", self.execution_id)
                )
                if not allowed:
                    continue
            files.append(relative.as_posix())
        return tuple(sorted(files))

    def _scoped_file_search_roots(self, path: str | os.PathLike[str]) -> tuple[Path, ...]:
        direct = self.resolve_path(path)
        roots: list[Path] = [direct] if direct.exists() else []
        raw = Path(path)
        if not raw.is_absolute():
            relative = raw.as_posix()
            if not (relative == ".agently" or relative.startswith(".agently/")):
                fallback = (
                    self.fallback_root
                    if relative in {"", "."}
                    else (self.fallback_root / raw).resolve()
                )
                if fallback.exists() and fallback not in roots:
                    roots.append(fallback)
        return tuple(roots)

    def _logical_file_parts(self, path: Path) -> tuple[str, ...]:
        fallback_root = self.fallback_root.resolve()
        try:
            return path.resolve().relative_to(fallback_root).parts
        except ValueError:
            return path.resolve().relative_to(self.root).parts

    async def search_files(
        self,
        query: str | None = None,
        *,
        path: str | os.PathLike[str] = ".",
        pattern: str = "**/*",
        offset: int = 0,
        max_results: int = 20,
        max_file_bytes: int = 20000,
        include_hidden: bool = False,
        **_: object,
    ) -> list[dict[str, object]]:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        results: list[dict[str, object]] = []
        matched_files = 0
        query_text = str(query or "").casefold()
        pattern = "**/*" if str(pattern or "").strip() in {"", "**"} else str(pattern)
        candidates: set[Path] = set()
        for root in self._scoped_file_search_roots(path):
            candidates.update([root] if root.is_file() else root.rglob(pattern))
        for candidate in sorted(candidates):
            if len(results) >= max_results:
                break
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = self._relative(candidate)
            if not include_hidden and any(part.startswith(".") for part in self._logical_file_parts(candidate)):
                continue
            if candidate.stat().st_size > max_file_bytes:
                continue
            readback = await self.read_file(relative, max_bytes=max_file_bytes)
            for line_no, line in enumerate(readback.content.splitlines(), start=1):
                if query_text and query_text not in line.casefold():
                    continue
                if matched_files < offset:
                    matched_files += 1
                    break
                results.append(
                    {
                        "path": relative,
                        "line": line_no,
                        "text": line,
                        "snippet": line,
                        "bytes": readback.total_bytes,
                        "read_bytes": len(readback.data),
                        "truncated": readback.truncated,
                        "sha256": readback.sha256,
                        "media_type": readback.media_type,
                        "source": "task_workspace.search_files",
                        "content_state": "bounded_readback_available",
                    }
                )
                matched_files += 1
                break
        return results

    async def grep_files(
        self,
        pattern: str,
        *,
        path: str | os.PathLike[str] = ".",
        regex: bool = True,
        glob: str | None = None,
        context_lines: int = 0,
        max_results: int = 50,
        include_hidden: bool = False,
        max_file_bytes: int = 200000,
    ) -> dict[str, Any]:
        matcher = re.compile(pattern) if regex else None
        matches: list[dict[str, Any]] = []
        candidates: set[Path] = set()
        for root in self._scoped_file_search_roots(path):
            candidates.update([root] if root.is_file() else root.rglob(glob or "**/*"))
        for candidate in sorted(candidates):
            if len(matches) >= max_results:
                break
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(self.root)
            if not include_hidden and any(part.startswith(".") for part in self._logical_file_parts(candidate)):
                continue
            if candidate.stat().st_size > max_file_bytes:
                continue
            readback = await self.read_file(relative.as_posix(), max_bytes=max_file_bytes)
            lines = readback.content.splitlines()
            for line_index, line in enumerate(lines):
                matched = bool(matcher.search(line)) if matcher is not None else pattern in line
                if not matched:
                    continue
                before = max(0, line_index - max(0, context_lines))
                after = min(len(lines), line_index + max(0, context_lines) + 1)
                matches.append(
                    {
                        "path": relative.as_posix(),
                        "line": line_index + 1,
                        "text": line,
                        "snippet": "\n".join(lines[before:after]),
                        "line_start": before + 1,
                        "line_end": after,
                        "truncated": readback.truncated,
                    }
                )
                if len(matches) >= max_results:
                    break
        return {
            "pattern": pattern,
            "regex": regex,
            "glob": glob or "**/*",
            "path": Path(path).as_posix(),
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= max_results,
            "max_results": max_results,
        }

    async def glob_files(
        self,
        pattern: str = "*",
        *,
        path: str | os.PathLike[str] = ".",
        max_results: int = 200,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        matches: list[str] = []
        candidates: set[Path] = set()
        for root in self._scoped_file_search_roots(path):
            candidates.update([root] if root.is_file() else root.rglob(pattern))
        for candidate in sorted(candidates):
            if len(matches) >= max_results:
                break
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(self.root)
            if not include_hidden and any(part.startswith(".") for part in self._logical_file_parts(candidate)):
                continue
            matches.append(relative.as_posix())
        return {"ok": True, "matches": matches, "truncated": len(matches) >= max_results}

    async def apply_patch(
        self,
        patch: str,
        *,
        expected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.mode != "read_write":
            raise TaskWorkspacePolicyError("TaskWorkspace patching requires read_write mode.")
        paths: list[str] = []
        for line in str(patch or "").splitlines():
            if not (line.startswith("+++ ") or line.startswith("--- ")):
                continue
            raw_path = line[4:].split("\t", 1)[0].strip()
            if raw_path in {"", "/dev/null"}:
                continue
            if raw_path.startswith(("a/", "b/")):
                raw_path = raw_path[2:]
            normalized = self._relative(self.resolve_path(raw_path))
            if normalized not in paths:
                paths.append(normalized)
        if not paths:
            raise ValueError("Patch did not declare any TaskWorkspace file paths.")
        if expected_files is not None:
            expected = [self._relative(self.resolve_path(item)) for item in expected_files]
            if sorted(paths) != sorted(dict.fromkeys(expected)):
                raise ValueError("Patch file set does not match expected_files.")
        git_path = shutil.which("git")
        if git_path is None:
            raise RuntimeError("git executable is required for apply_patch.")
        completed = await asyncio.to_thread(
            subprocess.run,
            [git_path, "apply", "--whitespace=nowarn"],
            cwd=str(self.root),
            input=str(patch or ""),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(str(completed.stderr or completed.stdout or "git apply failed").strip())
        return {
            "ok": True,
            "status": "success",
            "paths": paths,
            "file_infos": [self.inspect_file(path) for path in paths],
        }

    async def export_file(
        self,
        source_path: str,
        output_path: str,
        *,
        export_kind: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = options
        if export_kind not in {"copy", "raw"}:
            raise ValueError(f"TaskWorkspace export kind is unsupported: {export_kind}")
        result = await self.copy_from(self.resolve_path(source_path), output_path)
        return result.to_dict()

    async def _promote_file_identity(
        self,
        path: str | os.PathLike[str],
        *,
        role: str = "artifact",
    ) -> dict[str, object]:
        readback = await self.read_file(path, max_bytes=1)
        observation = await asyncio.to_thread(
            self._identity_catalog.observe_path,
            normalized_path=readback.path,
            digest=readback.sha256,
            size=readback.total_bytes,
        )
        return {
            "type": "file",
            "id": observation.content_version_id,
            "locator_id": observation.locator_id,
            "content_version_id": observation.content_version_id,
            "task_workspace_id": self.task_workspace_id,
            "execution_id": self.execution_id,
            "path": readback.path,
            "bytes": readback.total_bytes,
            "size": readback.total_bytes,
            "sha256": readback.sha256,
            "media_type": readback.media_type,
            "role": role,
            "content_state": "ref_only",
            "source": "task_workspace",
        }

    async def _close_execution_files(
        self,
        *,
        preserve_paths: object = None,
        retained_refs: object = None,
        status: str = "completed",
        **_: object,
    ) -> dict[str, object]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("TaskWorkspace execution status must be completed, failed, or cancelled.")
        raw_refs = list(retained_refs) if isinstance(retained_refs, (list, tuple)) else []
        if isinstance(preserve_paths, (list, tuple, set)):
            for path in preserve_paths:
                raw_refs.append(
                    {
                        **await self._promote_file_identity(str(path)),
                        "role": "preserved",
                    }
                )

        fallback_root = self.fallback_root.resolve()
        if not fallback_root.exists():
            return {
                "status": "noop",
                "execution_id": self.execution_id,
                "retained_refs": [],
                "retained_bytes": 0,
                "deleted_bytes": 0,
                "diagnostics": [],
            }

        verified_paths: set[Path] = set()
        verified_refs: list[dict[str, object]] = []
        diagnostics: list[dict[str, object]] = []
        for raw_ref in raw_refs:
            ref = dict(raw_ref) if isinstance(raw_ref, dict) else {}
            path = str(ref.get("path") or "")
            diagnostic_code: str | None = None
            diagnostic_message = ""
            target: Path | None = None
            if ref.get("type") != "file":
                diagnostic_code = "task_workspace.file_ref.invalid_type"
                diagnostic_message = "Retained TaskWorkspace reference is not a file ref."
            elif str(ref.get("task_workspace_id") or "") != self.task_workspace_id:
                diagnostic_code = "task_workspace.file_ref.task_workspace_mismatch"
                diagnostic_message = "Retained file ref belongs to another TaskWorkspace."
            elif str(ref.get("execution_id") or "") != self.execution_id:
                diagnostic_code = "task_workspace.file_ref.execution_mismatch"
                diagnostic_message = "Retained file ref belongs to another execution."
            else:
                try:
                    target = self.resolve_path(path)
                except TaskWorkspacePolicyError:
                    diagnostic_code = "task_workspace.file_ref.path_outside_root"
                    diagnostic_message = "Retained file ref is outside the TaskWorkspace root."
            if diagnostic_code is None and target is not None:
                if not target.is_file() or target.is_symlink():
                    diagnostic_code = "task_workspace.file_ref.unavailable"
                    diagnostic_message = "Retained file ref has no readable physical file."
                else:
                    actual_size = target.stat().st_size
                    actual_digest = await asyncio.to_thread(self._sha256, target)
                    claimed_size = ref.get("size")
                    if isinstance(claimed_size, bool) or not isinstance(claimed_size, int):
                        diagnostic_code = "task_workspace.file_ref.size_missing"
                        diagnostic_message = "Retained file ref has no valid size."
                    elif claimed_size != actual_size:
                        diagnostic_code = "task_workspace.file_ref.size_mismatch"
                        diagnostic_message = "Retained file ref size does not match physical readback."
                    elif str(ref.get("sha256") or "") != actual_digest:
                        diagnostic_code = "task_workspace.file_ref.digest_mismatch"
                        diagnostic_message = "Retained file ref digest does not match physical readback."
                    else:
                        verified_refs.append(ref)
                        try:
                            target.relative_to(fallback_root)
                        except ValueError:
                            pass
                        else:
                            verified_paths.add(target)
            if diagnostic_code is not None:
                diagnostics.append(
                    {
                        "code": diagnostic_code,
                        "message": diagnostic_message,
                        "retryable": True,
                        "path": path,
                    }
                )

        if diagnostics:
            return {
                "status": "deferred",
                "execution_id": self.execution_id,
                "retained_refs": [],
                "retained_bytes": 0,
                "deleted_bytes": 0,
                "diagnostics": diagnostics,
            }

        retained_bytes = sum(path.stat().st_size for path in verified_paths)
        deleted_bytes = 0
        candidates = sorted(fallback_root.rglob("*"), reverse=True)
        for target in candidates:
            if not target.is_file() and not target.is_symlink():
                continue
            resolved = target.resolve()
            if resolved in verified_paths:
                continue
            try:
                deleted_bytes += target.lstat().st_size
                target.unlink()
            except OSError as error:
                diagnostics.append(
                    {
                        "code": "task_workspace.execution_file.delete_failed",
                        "message": str(error),
                        "retryable": True,
                        "path": self._relative(target),
                    }
                )
        for directory in sorted(
            (path for path in fallback_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        for directory in (fallback_root, fallback_root.parent, fallback_root.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
        return {
            "status": "deferred" if diagnostics else "applied",
            "execution_id": self.execution_id,
            "retained_refs": verified_refs,
            "retained_bytes": retained_bytes,
            "deleted_bytes": deleted_bytes,
            "diagnostics": diagnostics,
        }


__all__ = ["TaskWorkspace"]
