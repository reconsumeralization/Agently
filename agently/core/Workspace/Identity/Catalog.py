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
import json
import os
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from ..Errors import WorkspaceError, WorkspacePolicyError
from .Encoding import decode_base62, encode_base62
from .Locators import normalize_locator
from .Retention import identity_reference_graph, retained_identity_closure
from .Types import (
    ContentObservation,
    IDENTITY_PREFIXES,
    IdentityKind,
    IdentityRetentionReport,
    ScopedIdentity,
)


WORKSPACE_IDENTITY_STATE_SCHEMA_VERSION = "workspace_identity_state/v1"
WORKSPACE_IDENTITY_OBJECT_SCHEMA_VERSION = "workspace_identity_object/v1"


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(_json_bytes(value))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
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


class WorkspaceIdentityCatalog:
    """Private filesystem owner for one Workspace identity high-water mark."""

    def __init__(
        self,
        system_root: str | Path,
        *,
        workspace_id: str,
        create: bool = True,
        private_write: bool = True,
    ) -> None:
        if not str(workspace_id or "").strip():
            raise ValueError("Workspace identity catalog requires a workspace_id.")
        self.system_root = Path(system_root).expanduser().resolve()
        self.root = self.system_root / "identity"
        self.workspace_id = str(workspace_id)
        self.create = bool(create)
        self.private_write = bool(private_write)
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"
        self.pins_path = self.root / "pins.json"

    async def allocate(self, kind: IdentityKind) -> ScopedIdentity:
        return await asyncio.to_thread(self._allocate_sync, kind)

    async def lease_task_range(self, task_id: str, *, size: int = 1024) -> tuple[int, int]:
        return await asyncio.to_thread(self._lease_task_range_sync, task_id, size)

    async def observe_content(
        self,
        *,
        locator_kind: str,
        normalized_locator: str,
        digest: str,
        size: int,
        payload_pointer: Mapping[str, Any],
    ) -> ContentObservation:
        return await asyncio.to_thread(
            self._observe_content_sync,
            locator_kind,
            normalized_locator,
            digest,
            size,
            dict(payload_pointer),
        )

    async def resolve(self, entity_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._resolve_sync, entity_id)

    async def add_segment(
        self,
        *,
        content_version_id: str,
        ordinal: int,
        offset: int,
        length: int,
        digest: str,
        payload_pointer: Mapping[str, Any],
    ) -> ScopedIdentity:
        return await asyncio.to_thread(
            self._add_segment_sync,
            content_version_id,
            ordinal,
            offset,
            length,
            digest,
            dict(payload_pointer),
        )

    async def add_link(
        self,
        *,
        source_id: str,
        target_id: str,
        relation: str,
        role: str,
        meta: Mapping[str, Any] | None = None,
    ) -> ScopedIdentity:
        return await asyncio.to_thread(
            self._add_link_sync,
            source_id,
            target_id,
            relation,
            role,
            dict(meta or {}),
        )

    async def retain_task_manifest(
        self,
        task_id: str,
        *,
        root_ids: Sequence[str],
        state: str,
        task_reference_catalog: Mapping[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._retain_task_manifest_sync,
            task_id,
            tuple(root_ids),
            state,
            dict(task_reference_catalog) if task_reference_catalog is not None else None,
        )

    async def pin(self, entity_id: str, *, reason: str) -> None:
        await asyncio.to_thread(self._pin_sync, entity_id, reason)

    async def unpin(self, entity_id: str) -> None:
        await asyncio.to_thread(self._unpin_sync, entity_id)

    async def collect_unreachable(
        self,
        *,
        strong_roots: Sequence[str] = (),
        audit_retained_ids: Sequence[str] = (),
    ) -> IdentityRetentionReport:
        return await asyncio.to_thread(
            self._collect_unreachable_sync,
            tuple(strong_roots),
            tuple(audit_retained_ids),
        )

    async def discard(self, entity_ids: Sequence[str]) -> tuple[str, ...]:
        return await asyncio.to_thread(self._discard_sync, tuple(entity_ids))

    def _allocate_sync(self, kind: IdentityKind) -> ScopedIdentity:
        self._require_persistence()
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            return self._allocate_locked(kind)

    def _allocate_locked(
        self,
        kind: IdentityKind,
        *,
        manifest: Mapping[str, Any] | None = None,
    ) -> ScopedIdentity:
        try:
            prefix = IDENTITY_PREFIXES[kind]
        except KeyError as error:
            raise ValueError(f"Unknown Workspace identity kind: {kind!r}.") from error
        state = self._read_state()
        sequence = int(state["high_water"]) + 1
        revision = int(state["revision"]) + 1
        identity = ScopedIdentity(
            scope_kind="workspace",
            scope_id=self.workspace_id,
            entity_id=f"{prefix}_{encode_base62(sequence)}",
            sequence=sequence,
        )
        manifest_path = self._manifest_path(kind, sequence)
        if manifest_path.exists():
            raise WorkspaceError("Workspace identity state regressed to an already allocated sequence.")
        _write_json_atomic(
            self.state_path,
            {
                "schema_version": WORKSPACE_IDENTITY_STATE_SCHEMA_VERSION,
                "workspace_id": self.workspace_id,
                "high_water": str(sequence),
                "revision": revision,
            },
        )
        _write_json_atomic(
            manifest_path,
            {
                "schema_version": WORKSPACE_IDENTITY_OBJECT_SCHEMA_VERSION,
                "scope_kind": identity.scope_kind,
                "scope_id": identity.scope_id,
                "entity_id": identity.entity_id,
                "sequence": str(identity.sequence),
                "kind": kind,
                **dict(manifest or {}),
            },
        )
        return identity

    def _observe_content_sync(
        self,
        locator_kind: str,
        normalized_locator: str,
        digest: str,
        size: int,
        payload_pointer: Mapping[str, Any],
    ) -> ContentObservation:
        locator_kind = str(locator_kind or "").strip().lower()
        normalized_locator = normalize_locator(locator_kind, normalized_locator)
        digest = str(digest or "").strip().lower()
        if not locator_kind or not normalized_locator:
            raise ValueError("Workspace content observations require a locator kind and value.")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Workspace content observations require a SHA-256 digest.")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Workspace content observation size must be a non-negative integer.")
        self._require_persistence()
        self.root.mkdir(parents=True, exist_ok=True)
        index_path = self._locator_index_path(locator_kind, normalized_locator)
        with _exclusive_file_lock(self.lock_path):
            index: dict[str, Any]
            if index_path.exists():
                index = self._read_json_object(index_path, label="Workspace locator index")
                if (
                    index.get("workspace_id") != self.workspace_id
                    or index.get("locator_kind") != locator_kind
                    or index.get("normalized_locator") != normalized_locator
                ):
                    raise WorkspaceError("Workspace locator index collision or owner mismatch.")
                locator_id = str(index.get("locator_id") or "")
            else:
                locator = self._allocate_locked(
                    "locator",
                    manifest={
                        "locator_kind": locator_kind,
                        "normalized_locator": normalized_locator,
                    },
                )
                locator_id = locator.entity_id
                index = {
                    "schema_version": "workspace_locator_index/v1",
                    "workspace_id": self.workspace_id,
                    "locator_kind": locator_kind,
                    "normalized_locator": normalized_locator,
                    "locator_id": locator_id,
                    "current_content_version_id": None,
                    "versions": [],
                }
            versions = index.get("versions")
            if not isinstance(versions, list):
                raise WorkspaceError("Workspace locator version index is invalid.")
            for raw_version in versions:
                if not isinstance(raw_version, Mapping) or raw_version.get("digest") != digest:
                    continue
                content_version_id = str(raw_version.get("content_version_id") or "")
                if not content_version_id:
                    raise WorkspaceError("Workspace locator version index has an empty target.")
                version_manifest = self._resolve_sync(content_version_id)
                if (
                    version_manifest.get("kind") != "content_version"
                    or version_manifest.get("locator_id") != locator_id
                    or version_manifest.get("digest") != digest
                ):
                    raise WorkspaceError("Workspace locator version target does not match its immutable manifest.")
                stored_size = raw_version.get("size")
                if isinstance(stored_size, bool) or not isinstance(stored_size, int) or stored_size != size:
                    raise WorkspaceError("Workspace locator version digest matches but its size differs.")
                if index.get("current_content_version_id") != content_version_id:
                    index["current_content_version_id"] = content_version_id
                    _write_json_atomic(index_path, index)
                return ContentObservation(
                    locator_id=locator_id,
                    content_version_id=content_version_id,
                    digest=digest,
                    size=stored_size,
                    created=False,
                )
            version = self._allocate_locked(
                "content_version",
                manifest={
                    "locator_id": locator_id,
                    "digest": digest,
                    "size": size,
                    "payload_pointer": dict(payload_pointer),
                },
            )
            version_entry = {
                "content_version_id": version.entity_id,
                "digest": digest,
                "size": size,
            }
            index["versions"] = [*versions, version_entry]
            index["current_content_version_id"] = version.entity_id
            _write_json_atomic(index_path, index)
            return ContentObservation(
                locator_id=locator_id,
                content_version_id=version.entity_id,
                digest=digest,
                size=size,
                created=True,
            )

    def _resolve_sync(self, entity_id: str) -> dict[str, Any]:
        kind, sequence = self._parse_entity_id(entity_id)
        path = self._manifest_path(kind, sequence)
        if not path.exists():
            raise KeyError(f"Workspace identity does not exist: {entity_id}")
        manifest = self._read_json_object(path, label="Workspace identity object")
        if manifest.get("entity_id") != entity_id or manifest.get("scope_id") != self.workspace_id:
            raise WorkspaceError("Workspace identity object does not match its scoped key.")
        return manifest

    def _add_segment_sync(
        self,
        content_version_id: str,
        ordinal: int,
        offset: int,
        length: int,
        digest: str,
        payload_pointer: Mapping[str, Any],
    ) -> ScopedIdentity:
        for label, value in {"ordinal": ordinal, "offset": offset, "length": length}.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Workspace segment {label} must be a non-negative integer.")
        digest = str(digest or "").strip().lower()
        self._validate_digest(digest)
        self._require_persistence()
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            parent = self._resolve_sync(content_version_id)
            if parent.get("kind") != "content_version":
                raise ValueError("Workspace segments must belong to a content version.")
            return self._allocate_locked(
                "segment",
                manifest={
                    "content_version_id": content_version_id,
                    "ordinal": ordinal,
                    "offset": offset,
                    "length": length,
                    "digest": digest,
                    "payload_pointer": dict(payload_pointer),
                },
            )

    def _add_link_sync(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        role: str,
        meta: Mapping[str, Any],
    ) -> ScopedIdentity:
        relation = str(relation or "").strip()
        role = str(role or "").strip()
        if not relation or not role:
            raise ValueError("Workspace identity links require relation and role values.")
        self._require_persistence()
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            self._resolve_sync(source_id)
            self._resolve_sync(target_id)
            return self._allocate_locked(
                "link",
                manifest={
                    "source_id": source_id,
                    "target_id": target_id,
                    "relation": relation,
                    "role": role,
                    "meta": dict(meta),
                },
            )

    def _retain_task_manifest_sync(
        self,
        task_id: str,
        root_ids: Sequence[str],
        state: str,
        task_reference_catalog: Mapping[str, Any] | None,
    ) -> None:
        normalized_task_id = str(task_id or "").strip()
        normalized_state = str(state or "").strip().lower()
        if not normalized_task_id:
            raise ValueError("Workspace retained task manifests require a task_id.")
        if normalized_state not in {"active", "recovery", "accepted", "released"}:
            raise ValueError("Workspace retained task manifest state is invalid.")
        normalized_roots = tuple(dict.fromkeys(str(entity_id).strip() for entity_id in root_ids))
        if any(not entity_id for entity_id in normalized_roots):
            raise ValueError("Workspace retained task roots cannot be empty.")
        self._require_persistence()
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            for entity_id in normalized_roots:
                self._resolve_sync(entity_id)
            task_path = self._task_manifest_path(normalized_task_id)
            if task_path.exists():
                task_manifest = self._read_json_object(
                    task_path,
                    label="Workspace task identity manifest",
                )
                if task_manifest.get("task_id") != normalized_task_id:
                    raise WorkspaceError("Workspace task identity manifest has an invalid owner.")
            else:
                task_manifest = {
                    "schema_version": "workspace_task_identity_manifest/v1",
                    "workspace_id": self.workspace_id,
                    "task_id": normalized_task_id,
                    "leases": [],
                }
            task_manifest["state"] = normalized_state
            task_manifest["root_ids"] = list(normalized_roots)
            if task_reference_catalog is not None:
                task_manifest["task_reference_catalog"] = json.loads(
                    json.dumps(task_reference_catalog, ensure_ascii=False, sort_keys=True, default=str)
                )
            _write_json_atomic(task_path, task_manifest)

    def _pin_sync(self, entity_id: str, reason: str) -> None:
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("Workspace identity pins require a reason.")
        self._require_persistence()
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            self._resolve_sync(entity_id)
            pins = self._read_pins()
            pins[entity_id] = normalized_reason
            _write_json_atomic(
                self.pins_path,
                {
                    "schema_version": "workspace_identity_pins/v1",
                    "workspace_id": self.workspace_id,
                    "pins": pins,
                },
            )

    def _unpin_sync(self, entity_id: str) -> None:
        if not self.pins_path.exists():
            return
        self._require_persistence()
        with _exclusive_file_lock(self.lock_path):
            pins = self._read_pins()
            pins.pop(str(entity_id), None)
            if pins:
                _write_json_atomic(
                    self.pins_path,
                    {
                        "schema_version": "workspace_identity_pins/v1",
                        "workspace_id": self.workspace_id,
                        "pins": pins,
                    },
                )
            else:
                self.pins_path.unlink(missing_ok=True)

    def _collect_unreachable_sync(
        self,
        strong_roots: Sequence[str],
        audit_retained_ids: Sequence[str],
    ) -> IdentityRetentionReport:
        if not self.state_path.exists():
            return IdentityRetentionReport((), (), (), (), "0")
        self._require_persistence()
        with _exclusive_file_lock(self.lock_path):
            state = self._read_state()
            manifests, manifest_paths = self._load_identity_manifests()
            locator_indexes, locator_index_paths = self._load_locator_indexes()
            roots = {str(entity_id).strip() for entity_id in strong_roots if str(entity_id).strip()}
            roots.update(
                entity_id
                for entity_id, manifest in manifests.items()
                if manifest.get("kind") == "record"
                or (manifest.get("kind") == "link" and not manifest.get("source_id"))
            )
            pins = self._read_pins()
            roots.update(pins)
            released_task_paths: list[Path] = []
            for task_path in self.root.glob("tasks/*/manifest.json"):
                task_manifest = self._read_json_object(
                    task_path,
                    label="Workspace task identity manifest",
                )
                if task_manifest.get("workspace_id") != self.workspace_id:
                    raise WorkspaceError("Workspace task identity manifest owner mismatch.")
                task_state = str(task_manifest.get("state") or "")
                if task_state == "released":
                    released_task_paths.append(task_path)
                    continue
                if task_state not in {"active", "recovery", "accepted"}:
                    continue
                raw_roots = task_manifest.get("root_ids", [])
                if not isinstance(raw_roots, list):
                    raise WorkspaceError("Workspace task identity roots are invalid.")
                roots.update(str(entity_id) for entity_id in raw_roots if str(entity_id))
            unknown_roots = roots - manifests.keys()
            if unknown_roots:
                raise WorkspaceError(
                    "Workspace identity retention roots are missing: " + ", ".join(sorted(unknown_roots))
                )
            graph = identity_reference_graph(manifests, locator_indexes)
            retained = retained_identity_closure(graph, roots)
            deleted = set(manifests) - retained

            all_managed_payloads = self._managed_payloads(manifests.values())
            retained_managed_payloads = self._managed_payloads(manifests[entity_id] for entity_id in retained)
            audit_ids = {str(entity_id) for entity_id in audit_retained_ids}
            for locator_id, index_path in locator_index_paths.items():
                if locator_id not in retained:
                    index_path.unlink(missing_ok=True)
                    continue
                index = locator_indexes[locator_id]
                raw_versions = index.get("versions")
                if not isinstance(raw_versions, list):
                    raise WorkspaceError("Workspace locator version index is invalid.")
                retained_versions = [
                    dict(version)
                    for version in raw_versions
                    if isinstance(version, Mapping) and str(version.get("content_version_id") or "") in retained
                ]
                current_version_id = str(index.get("current_content_version_id") or "")
                if current_version_id not in retained:
                    raise WorkspaceError("Retained Workspace locator lost its current content version.")
                if retained_versions != raw_versions:
                    index["versions"] = retained_versions
                    _write_json_atomic(index_path, index)
            for entity_id in sorted(deleted):
                manifest = manifests[entity_id]
                if entity_id in audit_ids:
                    kind, sequence = self._parse_entity_id(entity_id)
                    _write_json_atomic(
                        self.root / "tombstones" / IDENTITY_PREFIXES[kind] / f"{sequence}.json",
                        {
                            "schema_version": "workspace_identity_tombstone/v1",
                            "workspace_id": self.workspace_id,
                            "entity_id": entity_id,
                            "kind": manifest.get("kind"),
                            "reason": "audit_retained_deletion",
                        },
                    )
                manifest_paths[entity_id].unlink(missing_ok=True)
            for task_path in released_task_paths:
                task_path.unlink(missing_ok=True)

            deleted_payloads: list[str] = []
            for relative_path in sorted(all_managed_payloads - retained_managed_payloads):
                payload_path = self._managed_payload_path(relative_path)
                if payload_path is None:
                    continue
                if payload_path.is_file() or payload_path.is_symlink():
                    payload_path.unlink()
                    deleted_payloads.append(relative_path)

            self._remove_empty_identity_directories()
            return IdentityRetentionReport(
                roots=tuple(sorted(roots)),
                retained_entity_ids=tuple(sorted(retained)),
                deleted_entity_ids=tuple(sorted(deleted)),
                deleted_payloads=tuple(deleted_payloads),
                high_water=str(state["high_water"]),
            )

    def _discard_sync(self, entity_ids: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(entity_id).strip() for entity_id in entity_ids))
        if not normalized or not self.state_path.exists():
            return ()
        self._require_persistence()
        discarded: list[str] = []
        with _exclusive_file_lock(self.lock_path):
            for entity_id in normalized:
                kind, sequence = self._parse_entity_id(entity_id)
                path = self._manifest_path(kind, sequence)
                if not path.exists():
                    continue
                manifest = self._read_json_object(path, label="Workspace identity object")
                if manifest.get("entity_id") != entity_id or manifest.get("scope_id") != self.workspace_id:
                    raise WorkspaceError("Workspace identity discard target is invalid.")
                path.unlink()
                discarded.append(entity_id)
            self._remove_empty_identity_directories()
        return tuple(discarded)

    def _read_pins(self) -> dict[str, str]:
        if not self.pins_path.exists():
            return {}
        raw = self._read_json_object(self.pins_path, label="Workspace identity pins")
        if (
            raw.get("schema_version") != "workspace_identity_pins/v1"
            or raw.get("workspace_id") != self.workspace_id
            or not isinstance(raw.get("pins"), dict)
        ):
            raise WorkspaceError("Workspace identity pins are invalid.")
        return {str(key): str(value) for key, value in raw["pins"].items()}

    def _load_identity_manifests(self) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
        manifests: dict[str, dict[str, Any]] = {}
        paths: dict[str, Path] = {}
        for path in self.root.glob("objects/*/*/*.json"):
            manifest = self._read_json_object(path, label="Workspace identity object")
            entity_id = str(manifest.get("entity_id") or "")
            if not entity_id or entity_id in manifests or manifest.get("scope_id") != self.workspace_id:
                raise WorkspaceError("Workspace identity object key or owner is invalid.")
            manifests[entity_id] = manifest
            paths[entity_id] = path
        return manifests, paths

    def _load_locator_indexes(self) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
        indexes: dict[str, dict[str, Any]] = {}
        paths: dict[str, Path] = {}
        for path in self.root.glob("indexes/locators/*/*.json"):
            index = self._read_json_object(path, label="Workspace locator index")
            locator_id = str(index.get("locator_id") or "")
            if not locator_id or locator_id in indexes or index.get("workspace_id") != self.workspace_id:
                raise WorkspaceError("Workspace locator index key or owner is invalid.")
            indexes[locator_id] = index
            paths[locator_id] = path
        return indexes, paths

    @staticmethod
    def _managed_payloads(manifests: Iterable[Mapping[str, Any]]) -> set[str]:
        output: set[str] = set()
        for manifest in manifests:
            pointer = manifest.get("payload_pointer")
            if not isinstance(pointer, Mapping) or pointer.get("managed") is not True:
                continue
            path = str(pointer.get("path") or "").strip()
            if path:
                output.add(path)
        return output

    def _managed_payload_path(self, relative_path: str) -> Path | None:
        candidate = (self.system_root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def _remove_empty_identity_directories(self) -> None:
        for directory in sorted(
            (path for path in self.root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _validate_digest(digest: str) -> None:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Workspace content identities require a SHA-256 digest.")

    def _lease_task_range_sync(self, task_id: str, size: int) -> tuple[int, int]:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise ValueError("Workspace identity range leases require a task_id.")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError("Workspace identity range lease size must be a positive integer.")
        self._require_persistence()
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive_file_lock(self.lock_path):
            state = self._read_state()
            start = int(state["high_water"]) + 1
            end = start + size - 1
            revision = int(state["revision"]) + 1
            _write_json_atomic(
                self.state_path,
                {
                    "schema_version": WORKSPACE_IDENTITY_STATE_SCHEMA_VERSION,
                    "workspace_id": self.workspace_id,
                    "high_water": str(end),
                    "revision": revision,
                },
            )
            task_path = self._task_manifest_path(normalized_task_id)
            task_manifest: dict[str, Any]
            if task_path.exists():
                try:
                    raw = json.loads(task_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise WorkspaceError("Workspace task identity manifest is corrupt.") from error
                if not isinstance(raw, dict) or raw.get("task_id") != normalized_task_id:
                    raise WorkspaceError("Workspace task identity manifest has an invalid owner.")
                task_manifest = dict(raw)
            else:
                task_manifest = {
                    "schema_version": "workspace_task_identity_manifest/v1",
                    "workspace_id": self.workspace_id,
                    "task_id": normalized_task_id,
                    "leases": [],
                }
            leases = task_manifest.get("leases")
            if not isinstance(leases, list):
                raise WorkspaceError("Workspace task identity leases are invalid.")
            task_manifest["leases"] = [*leases, {"start": str(start), "end": str(end)}]
            _write_json_atomic(task_path, task_manifest)
            return (start, end)

    def _require_persistence(self) -> None:
        if not self.private_write:
            raise WorkspacePolicyError("Agently-private Workspace identity persistence is disabled.")
        if not self.create and not self.state_path.exists():
            raise WorkspacePolicyError("Workspace identity persistence is not initialized and create=False.")

    def _read_state(self) -> dict[str, str | int]:
        if not self.state_path.exists():
            return {
                "schema_version": WORKSPACE_IDENTITY_STATE_SCHEMA_VERSION,
                "workspace_id": self.workspace_id,
                "high_water": "0",
                "revision": 0,
            }
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceError("Workspace identity state is unreadable or corrupt.") from error
        if not isinstance(raw, Mapping):
            raise WorkspaceError("Workspace identity state must be a JSON object.")
        if raw.get("schema_version") != WORKSPACE_IDENTITY_STATE_SCHEMA_VERSION:
            raise WorkspaceError("Workspace identity state schema is unsupported.")
        if raw.get("workspace_id") != self.workspace_id:
            raise WorkspaceError("Workspace identity state belongs to a different Workspace.")
        high_water = raw.get("high_water")
        revision = raw.get("revision")
        if (
            isinstance(high_water, bool)
            or not isinstance(high_water, str)
            or not high_water.isdecimal()
            or (len(high_water) > 1 and high_water.startswith("0"))
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise WorkspaceError("Workspace identity state counters are invalid.")
        if int(high_water) < 0 or revision > int(high_water):
            raise WorkspaceError("Workspace identity state counters regressed or diverged.")
        return cast(dict[str, str | int], dict(raw))

    def _manifest_path(self, kind: IdentityKind, sequence: int) -> Path:
        prefix = IDENTITY_PREFIXES[kind]
        shard = f"{sequence // 1000:08d}"
        return self.root / "objects" / prefix / shard / f"{sequence}.json"

    def _locator_index_path(self, locator_kind: str, normalized_locator: str) -> Path:
        key = hashlib.sha256(f"{locator_kind}\0{normalized_locator}".encode("utf-8")).hexdigest()
        return self.root / "indexes" / "locators" / key[:2] / f"{key}.json"

    @staticmethod
    def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceError(f"{label} is unreadable or corrupt.") from error
        if not isinstance(raw, dict):
            raise WorkspaceError(f"{label} must be a JSON object.")
        return dict(raw)

    @staticmethod
    def _parse_entity_id(entity_id: str) -> tuple[IdentityKind, int]:
        value = str(entity_id or "").strip()
        prefix, separator, encoded = value.partition("_")
        if not separator or not encoded:
            raise ValueError(f"Invalid Workspace identity: {entity_id!r}.")
        kind_by_prefix: dict[str, IdentityKind] = {
            prefix_value: kind for kind, prefix_value in IDENTITY_PREFIXES.items()
        }
        try:
            kind = kind_by_prefix[prefix]
        except KeyError as error:
            raise ValueError(f"Unknown Workspace identity prefix: {prefix!r}.") from error
        sequence = decode_base62(encoded)
        if sequence <= 0:
            raise ValueError("Workspace entity ids cannot use the unallocated zero sentinel.")
        return kind, sequence

    def _task_manifest_path(self, task_id: str) -> Path:
        task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        return self.root / "tasks" / task_key / "manifest.json"
