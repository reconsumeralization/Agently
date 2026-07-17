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
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from .Errors import TaskWorkspaceError, TaskWorkspacePolicyError


_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _base62(value: int) -> str:
    if value < 0:
        raise ValueError("TaskWorkspace identity sequence cannot be negative.")
    if value == 0:
        return _ALPHABET[0]
    encoded: list[str] = []
    while value:
        value, remainder = divmod(value, len(_ALPHABET))
        encoded.append(_ALPHABET[remainder])
    return "".join(reversed(encoded))


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(_json_bytes(value))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class TaskWorkspaceContentObservation:
    locator_id: str
    content_version_id: str
    digest: str
    size: int
    created: bool


class TaskWorkspaceIdentityCatalog:
    """Private locator and immutable-content identity owner for one file root."""

    def __init__(self, system_root: Path, *, task_workspace_id: str) -> None:
        self.root = system_root / "identity"
        self.task_workspace_id = task_workspace_id
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"

    def observe_path(
        self,
        *,
        normalized_path: str,
        digest: str,
        size: int,
    ) -> TaskWorkspaceContentObservation:
        path = str(normalized_path or "").strip()
        digest = str(digest or "").strip().lower()
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("TaskWorkspace content identity requires a contained relative path.")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("TaskWorkspace content identity requires a SHA-256 digest.")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("TaskWorkspace content identity size must be a non-negative integer.")
        self.root.mkdir(parents=True, exist_ok=True)
        index_path = self.root / "locators" / f"{hashlib.sha256(path.encode('utf-8')).hexdigest()}.json"
        with _exclusive_file_lock(self.lock_path):
            state = self._read_state()
            if index_path.exists():
                index = self._read_object(index_path, label="TaskWorkspace locator index")
                if (
                    index.get("task_workspace_id") != self.task_workspace_id
                    or index.get("normalized_path") != path
                ):
                    raise TaskWorkspaceError("TaskWorkspace locator index owner or path does not match.")
                locator_id = str(index.get("locator_id") or "")
            else:
                locator_id, state = self._allocate(state, prefix="loc")
                index = {
                    "schema_version": "task_workspace_locator/v1",
                    "task_workspace_id": self.task_workspace_id,
                    "normalized_path": path,
                    "locator_id": locator_id,
                    "current_content_version_id": None,
                    "versions": [],
                }
            versions = index.get("versions")
            if not isinstance(versions, list):
                raise TaskWorkspaceError("TaskWorkspace locator versions are invalid.")
            for raw_version in versions:
                if not isinstance(raw_version, dict) or raw_version.get("sha256") != digest:
                    continue
                if raw_version.get("size") != size:
                    raise TaskWorkspaceError(
                        "TaskWorkspace content digest matches an existing version with a different size."
                    )
                content_version_id = str(raw_version.get("content_version_id") or "")
                if not content_version_id:
                    raise TaskWorkspaceError("TaskWorkspace content version identity is empty.")
                if index.get("current_content_version_id") != content_version_id:
                    index["current_content_version_id"] = content_version_id
                    _write_json_atomic(index_path, index)
                return TaskWorkspaceContentObservation(
                    locator_id=locator_id,
                    content_version_id=content_version_id,
                    digest=digest,
                    size=size,
                    created=False,
                )
            content_version_id, state = self._allocate(state, prefix="cv")
            versions.append(
                {
                    "content_version_id": content_version_id,
                    "sha256": digest,
                    "size": size,
                }
            )
            index["current_content_version_id"] = content_version_id
            _write_json_atomic(index_path, index)
            _write_json_atomic(self.state_path, state)
            return TaskWorkspaceContentObservation(
                locator_id=locator_id,
                content_version_id=content_version_id,
                digest=digest,
                size=size,
                created=True,
            )

    def retain_task_manifest(
        self,
        task_id: str,
        *,
        root_ids: list[str] | tuple[str, ...],
        state: str,
        task_reference_catalog: Mapping[str, object] | None = None,
    ) -> None:
        """Persist the task-owned roots that make retained files auditable."""

        normalized_task_id = str(task_id or "").strip()
        normalized_state = str(state or "").strip().lower()
        if not normalized_task_id:
            raise ValueError("TaskWorkspace retained task manifests require a task_id.")
        if normalized_state not in {"active", "recovery", "accepted", "released"}:
            raise ValueError("TaskWorkspace retained task manifest state is invalid.")
        normalized_roots = tuple(dict.fromkeys(str(item or "").strip() for item in root_ids))
        if any(not item for item in normalized_roots):
            raise ValueError("TaskWorkspace retained task roots cannot be empty.")

        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            known_versions: set[str] = set()
            locator_root = self.root / "locators"
            if locator_root.is_dir():
                for locator_path in locator_root.glob("*.json"):
                    locator = self._read_object(locator_path, label="TaskWorkspace locator index")
                    versions = locator.get("versions")
                    if isinstance(versions, list):
                        known_versions.update(
                            str(version.get("content_version_id") or "")
                            for version in versions
                            if isinstance(version, dict)
                        )
            unknown_roots = [item for item in normalized_roots if item not in known_versions]
            if unknown_roots:
                raise TaskWorkspaceError(
                    "TaskWorkspace retained task manifest references unknown content versions: "
                    + ", ".join(unknown_roots)
                )

            task_path = (
                self.root
                / "tasks"
                / hashlib.sha256(normalized_task_id.encode("utf-8")).hexdigest()
                / "manifest.json"
            )
            manifest: dict[str, object] = {
                "schema_version": "task_workspace_task_identity_manifest/v1",
                "task_workspace_id": self.task_workspace_id,
                "task_id": normalized_task_id,
                "state": normalized_state,
                "root_ids": list(normalized_roots),
            }
            if task_reference_catalog is not None:
                manifest["task_reference_catalog"] = json.loads(
                    json.dumps(
                        task_reference_catalog,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                )
            _write_json_atomic(task_path, manifest)

    def _allocate(self, state: dict[str, object], *, prefix: str) -> tuple[str, dict[str, object]]:
        high_water = int(str(state.get("high_water") or "0")) + 1
        revision = int(str(state.get("revision") or 0)) + 1
        return f"{prefix}_{_base62(high_water)}", {
            "schema_version": "task_workspace_identity_state/v1",
            "task_workspace_id": self.task_workspace_id,
            "high_water": str(high_water),
            "revision": revision,
        }

    def _read_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {
                "schema_version": "task_workspace_identity_state/v1",
                "task_workspace_id": self.task_workspace_id,
                "high_water": "0",
                "revision": 0,
            }
        state = self._read_object(self.state_path, label="TaskWorkspace identity state")
        if state.get("task_workspace_id") != self.task_workspace_id:
            raise TaskWorkspaceError("TaskWorkspace identity state owner does not match.")
        return state

    @staticmethod
    def _read_object(path: Path, *, label: str) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TaskWorkspacePolicyError(f"{label} cannot be read: {error}") from error
        if not isinstance(value, dict):
            raise TaskWorkspaceError(f"{label} must be a JSON object.")
        return value


__all__ = ["TaskWorkspaceContentObservation", "TaskWorkspaceIdentityCatalog"]
