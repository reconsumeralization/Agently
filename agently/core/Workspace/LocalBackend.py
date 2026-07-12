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
import errno
import hashlib
import importlib
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, AsyncIterator, Concatenate, Iterator, ParamSpec, TypeVar, cast

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.workspace import (
    WorkspaceBackendCapabilities,
    WorkspaceContentSegment,
    WorkspaceFileRef,
    WorkspaceFilePolicyMetadata,
    WorkspaceLeaseRef,
    WorkspaceLinkRef,
    WorkspaceRecordRef,
    WorkspaceReferenceEnvelope,
    WorkspaceRetainedReference,
    WorkspaceRetentionAnchor,
    WorkspaceRetentionDiagnostic,
    WorkspaceRetentionLifecycle,
    WorkspaceRetentionPolicy,
    WorkspaceRetentionPreview,
    WorkspaceRetentionResult,
    WorkspaceRetentionTerminalStatus,
    WorkspaceRuntimeEventRecord,
    WorkspaceScratchLease,
)
from agently.utils import DataFormatter

from .Errors import WorkspaceConfigurationError, WorkspacePolicyError
from .Stores import (
    ChromaVectorStoreProvider,
    EmbeddingProviderUnavailableError,
    LocalContentStore,
    LocalWorkspacePolicyEngine,
    NoopVectorIndex,
    SQLiteVectorStoreProvider,
    VectorIndexPipeline,
    VectorStoreProviderUnavailableError,
    delete_owned_file_descriptor_relative,
    supports_descriptor_relative_delete,
)
from .Retention import (
    NormalizedRetainedRoot,
    build_retention_selection,
    build_retention_preview,
    calculate_retention_logical_bytes,
    deduplicate_retained_refs,
    normalized_retained_root,
    read_only_retention_components,
    retention_diagnostic,
    retention_lifecycle_diagnostics,
    retention_selection_nonempty,
    resolve_retention_policy,
    strict_retention_json,
    strict_retention_json_value,
    validate_retained_reference_shape,
    validate_retention_preview,
)
from ._defaults import (
    SCOPE_LINEAGE_KINDS,
    WORKSPACE_FILE_AREAS,
    WORKSPACE_GUIDE_FILENAME,
    normalize_lineage,
    scope_filter_path_nodes,
)
from ._utils import json_dumps, json_loads, slug, utc_now


try:
    _fcntl: Any = importlib.import_module("fcntl")
except ImportError:
    _fcntl = None

@dataclass(frozen=True)
class _RetentionSQLiteSnapshot:
    all_record_rows: list[sqlite3.Row]
    scoped_rows: list[sqlite3.Row]
    checkpoint_rows: list[sqlite3.Row]
    runtime_event_rows: list[sqlite3.Row]
    anchor_rows: list[sqlite3.Row]
    scratch_rows: list[sqlite3.Row]
    link_rows: list[sqlite3.Row]
    scope_index_rows: list[sqlite3.Row]
    fts_rows: list[sqlite3.Row]
    manifest_rows: list[sqlite3.Row]
    vector_rows: list[sqlite3.Row]


@dataclass(frozen=True)
class _DecodedRetentionSnapshot:
    all_records: dict[str, WorkspaceRecordRef]
    runtime_events: list[WorkspaceRuntimeEventRecord]
    anchors: list[WorkspaceRetentionAnchor]
    links: list[WorkspaceLinkRef]
    scratch_leases: list[WorkspaceScratchLease]
    checkpoint_facts: list[dict[str, Any]]
    manifest_values: dict[str, Any]
    manifest_raw: dict[str, str]


@dataclass(frozen=True)
class _ResolvedRetentionRoots:
    canonical_records: dict[str, WorkspaceRecordRef]
    canonical_refs: list[WorkspaceRetainedReference]
    record_ids: set[str]
    file_paths: set[str]
    content_paths: set[str]
    event_ids: set[str]
    diagnostics: list[WorkspaceRetentionDiagnostic]


class _TerminalManifestPlanConflict(RuntimeError):
    def __init__(self, current: WorkspaceRecordRef):
        super().__init__("Workspace terminal manifest plan changed during derived cleanup.")
        self.current = current


class _TerminalManifestLedgerError(RuntimeError):
    pass


class _DerivedCleanupOperationalError(RuntimeError):
    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


class _AdvisoryLockAcquisitionError(WorkspaceConfigurationError):
    diagnostic_code = "workspace.retention.advisory_lock_failed"


class _AdvisoryLockCarrierError(_AdvisoryLockAcquisitionError):
    diagnostic_code = "workspace.retention.advisory_lock_invalid"


class _AdvisoryLockReleaseError(WorkspaceConfigurationError):
    def __init__(
        self,
        message: str,
        *,
        native_release_uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.native_release_uncertain = native_release_uncertain


def _close_advisory_descriptors(descriptors: Sequence[int | None]) -> list[OSError]:
    errors: list[OSError] = []
    for descriptor in descriptors:
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            errors.append(error)
    return errors


@dataclass(frozen=True)
class _AdvisoryLockHandle:
    carrier_fd: int
    root_fd: int
    path: Path

    def release(self) -> None:
        unlock_error: OSError | None = None
        try:
            _fcntl.flock(self.carrier_fd, _fcntl.LOCK_UN)
        except OSError as error:
            unlock_error = error
        carrier_close_error: OSError | None = None
        root_close_error: OSError | None = None
        try:
            os.close(self.carrier_fd)
        except OSError as error:
            carrier_close_error = error
        try:
            os.close(self.root_fd)
        except OSError as error:
            root_close_error = error
        errors = [
            error
            for error in (unlock_error, carrier_close_error, root_close_error)
            if error is not None
        ]
        if errors:
            failures = "; ".join(str(error) for error in errors)
            raise _AdvisoryLockReleaseError(
                f"Workspace advisory lock release failed for {self.path}: {failures}",
                native_release_uncertain=(
                    unlock_error is not None
                    and carrier_close_error is not None
                    and carrier_close_error.errno != errno.EBADF
                ),
            )


class _PosixAdvisoryLockWaiter:
    """One safely opened carrier descriptor reused by non-blocking retries."""

    def __init__(
        self,
        *,
        path: Path,
        root_fd: int,
        carrier_fd: int,
    ) -> None:
        self.path = path
        self._root_fd: int | None = root_fd
        self._carrier_fd: int | None = carrier_fd

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        create: bool,
    ) -> "_PosixAdvisoryLockWaiter | None":
        root_fd: int | None = None
        carrier_fd: int | None = None
        waiter: _PosixAdvisoryLockWaiter | None = None
        primary_error: _AdvisoryLockAcquisitionError | None = None
        try:
            root_fd = os.open(
                path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                named_stat = os.stat(
                    path.name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                named_stat = None
            if named_stat is not None and not stat.S_ISREG(named_stat.st_mode):
                raise _AdvisoryLockCarrierError(
                    f"Workspace advisory lock carrier is not a regular file: {path}"
                )
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if create:
                flags |= os.O_CREAT
            try:
                carrier_fd = os.open(
                    path.name,
                    flags,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                if not create:
                    carrier_fd = None
                else:
                    raise
            if carrier_fd is not None:
                waiter = cls(path=path, root_fd=root_fd, carrier_fd=carrier_fd)
                waiter._verify_named_identity()
                return waiter
        except _AdvisoryLockAcquisitionError as error:
            primary_error = error
        except OSError as error:
            error_type = (
                _AdvisoryLockCarrierError
                if error.errno in {errno.EISDIR, errno.ELOOP, errno.ENOTDIR}
                else _AdvisoryLockAcquisitionError
            )
            primary_error = error_type(
                f"Workspace advisory lock carrier open failed for {path}: {error}"
            )
        close_errors = _close_advisory_descriptors((carrier_fd, root_fd))
        if primary_error is not None:
            raise primary_error
        if close_errors:
            raise _AdvisoryLockAcquisitionError(
                f"Workspace advisory lock carrier close failed for {path}: "
                + "; ".join(str(error) for error in close_errors)
            )
        return None

    def _verify_named_identity(self) -> None:
        if self._root_fd is None or self._carrier_fd is None:
            raise RuntimeError("Workspace advisory lock waiter is closed.")
        descriptor_stat = os.fstat(self._carrier_fd)
        try:
            named_stat = os.stat(
                self.path.name,
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise _AdvisoryLockCarrierError(
                f"Workspace advisory lock carrier identity is unavailable for {self.path}: {error}"
            ) from error
        if not stat.S_ISREG(descriptor_stat.st_mode) or not stat.S_ISREG(
            named_stat.st_mode
        ):
            raise _AdvisoryLockCarrierError(
                f"Workspace advisory lock carrier is not a regular file: {self.path}"
            )
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            named_stat.st_dev,
            named_stat.st_ino,
        ):
            raise _AdvisoryLockCarrierError(
                f"Workspace advisory lock carrier changed during acquisition: {self.path}"
            )

    def try_acquire(self) -> _AdvisoryLockHandle | None:
        if self._carrier_fd is None:
            raise RuntimeError("Workspace advisory lock waiter is closed.")
        try:
            _fcntl.flock(self._carrier_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return None
            raise _AdvisoryLockAcquisitionError(
                f"Workspace advisory lock acquisition failed for {self.path}: {error}"
            ) from error
        try:
            self._verify_named_identity()
        except _AdvisoryLockAcquisitionError:
            try:
                _fcntl.flock(self._carrier_fd, _fcntl.LOCK_UN)
            except OSError:
                pass
            raise
        except OSError as error:
            try:
                _fcntl.flock(self._carrier_fd, _fcntl.LOCK_UN)
            except OSError:
                pass
            raise _AdvisoryLockAcquisitionError(
                f"Workspace advisory lock identity verification failed for {self.path}: {error}"
            ) from error
        carrier_fd = self._carrier_fd
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("Workspace advisory lock root descriptor is closed.")
        handle = _AdvisoryLockHandle(
            carrier_fd=carrier_fd,
            root_fd=root_fd,
            path=self.path,
        )
        self._carrier_fd = None
        self._root_fd = None
        return handle

    def close(self) -> None:
        descriptors = (self._carrier_fd, self._root_fd)
        self._carrier_fd = None
        self._root_fd = None
        errors = _close_advisory_descriptors(descriptors)
        if errors:
            failures = "; ".join(str(error) for error in errors)
            raise _AdvisoryLockAcquisitionError(
                f"Workspace advisory lock waiter close failed for {self.path}: {failures}"
            )


class _NativeAdvisoryLock:
    """Proven host-lock seam; unsupported hosts stay process-local."""

    @staticmethod
    def supported() -> bool:
        return bool(
            _fcntl is not None
            and getattr(os, "O_DIRECTORY", 0)
            and getattr(os, "O_NOFOLLOW", 0)
            and getattr(os, "O_CLOEXEC", 0)
            and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd
            and os.stat in os.supports_follow_symlinks
        )

    @staticmethod
    def open_waiter(
        path: Path,
        *,
        create: bool,
    ) -> _PosixAdvisoryLockWaiter | None:
        if not _NativeAdvisoryLock.supported():
            raise _AdvisoryLockAcquisitionError(
                "Workspace native advisory locking is unavailable."
            )
        return _PosixAdvisoryLockWaiter.open(path, create=create)

    @staticmethod
    def preflight(path: Path) -> _AdvisoryLockAcquisitionError | None:
        try:
            waiter = _NativeAdvisoryLock.open_waiter(path, create=False)
        except _AdvisoryLockAcquisitionError as error:
            return error
        if waiter is not None:
            try:
                waiter.close()
            except _AdvisoryLockAcquisitionError as error:
                return error
        return None


class _RootMutationGuard:
    """Task-reentrant process and OS mutation ownership for one canonical root."""

    def __init__(self, lock_path: Path) -> None:
        self._state_lock = threading.Lock()
        self._owner: object | None = None
        self._depth = 0
        self._lock_path = lock_path
        self._advisory_handle: _AdvisoryLockHandle | None = None
        self._poison_message: str | None = None

    @staticmethod
    def _owner_token() -> object:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is not None:
            return task
        return ("thread", threading.get_ident())

    def _try_reserve(self, owner: object) -> bool | None:
        with self._state_lock:
            if self._poison_message is not None:
                raise _AdvisoryLockReleaseError(
                    self._poison_message,
                    native_release_uncertain=True,
                )
            if self._owner is None:
                self._owner = owner
                self._depth = 1
                return True
            if self._owner == owner:
                self._depth += 1
                return False
            return None

    def _release(self, owner: object) -> None:
        with self._state_lock:
            if self._owner != owner or self._depth <= 0:
                raise RuntimeError("Workspace mutation guard release ownership mismatch.")
            self._depth -= 1
            if self._depth > 0:
                return
            advisory_handle = self._advisory_handle
            if advisory_handle is not None:
                try:
                    advisory_handle.release()
                except _AdvisoryLockReleaseError as error:
                    if error.native_release_uncertain:
                        poison_message = (
                            "Workspace root mutation guard is permanently poisoned "
                            f"after uncertain native lock release: {error}"
                        )
                        self._poison_message = poison_message
                        _retain_poisoned_root_mutation_guard(self)
                        self._owner = None
                        raise _AdvisoryLockReleaseError(
                            poison_message,
                            native_release_uncertain=True,
                        )
                    self._owner = None
                    self._advisory_handle = None
                    raise
            self._owner = None
            self._advisory_handle = None

    def _set_advisory_handle(
        self,
        owner: object,
        handle: _AdvisoryLockHandle,
    ) -> None:
        with self._state_lock:
            if self._owner != owner or self._depth <= 0:
                handle.release()
                raise RuntimeError(
                    "Workspace mutation guard lost ownership during advisory lock acquisition."
                )
            self._advisory_handle = handle

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        owner = self._owner_token()
        reservation: bool | None = None
        while reservation is None:
            reservation = self._try_reserve(owner)
            if reservation is not None:
                break
            await asyncio.sleep(0.001)
        waiter: _PosixAdvisoryLockWaiter | None = None
        body_failed = False
        try:
            if reservation and _NativeAdvisoryLock.supported():
                waiter = _NativeAdvisoryLock.open_waiter(
                    self._lock_path,
                    create=True,
                )
                if waiter is None:
                    raise _AdvisoryLockAcquisitionError(
                        f"Workspace advisory lock carrier disappeared: {self._lock_path}"
                    )
                delay = 0.005
                handle = waiter.try_acquire()
                while handle is None:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 0.05)
                    handle = waiter.try_acquire()
                self._set_advisory_handle(owner, handle)
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            teardown_errors: list[WorkspaceConfigurationError] = []
            if waiter is not None:
                try:
                    waiter.close()
                except _AdvisoryLockAcquisitionError as error:
                    teardown_errors.append(error)
            try:
                self._release(owner)
            except _AdvisoryLockReleaseError as error:
                teardown_errors.append(error)
            if teardown_errors and not body_failed:
                release_errors = [
                    error
                    for error in teardown_errors
                    if isinstance(error, _AdvisoryLockReleaseError)
                ]
                if release_errors:
                    raise _AdvisoryLockReleaseError(
                        "; ".join(str(error) for error in teardown_errors)
                    )
                raise _AdvisoryLockAcquisitionError(
                    "; ".join(str(error) for error in teardown_errors)
                )

    @contextmanager
    def try_acquire_sync(self) -> Iterator[bool]:
        owner = self._owner_token()
        reservation = self._try_reserve(owner)
        acquired = reservation is not None
        if reservation and _NativeAdvisoryLock.supported():
            waiter: _PosixAdvisoryLockWaiter | None = None
            setup_error: Exception | None = None
            cleanup_error: _AdvisoryLockAcquisitionError | None = None
            handle: _AdvisoryLockHandle | None = None
            try:
                waiter = _NativeAdvisoryLock.open_waiter(
                    self._lock_path,
                    create=True,
                )
                handle = waiter.try_acquire() if waiter is not None else None
            except Exception as error:
                setup_error = error
            finally:
                if waiter is not None:
                    try:
                        waiter.close()
                    except _AdvisoryLockAcquisitionError as error:
                        cleanup_error = error
            if setup_error is not None or cleanup_error is not None:
                self._release(owner)
                if setup_error is not None:
                    raise setup_error
                if cleanup_error is not None:
                    raise cleanup_error
            if handle is None:
                self._release(owner)
                acquired = False
            else:
                self._set_advisory_handle(owner, handle)
        body_failed = False
        try:
            yield acquired
        except BaseException:
            body_failed = True
            raise
        finally:
            if acquired:
                teardown_errors: list[WorkspaceConfigurationError] = []
                try:
                    self._release(owner)
                except _AdvisoryLockReleaseError as error:
                    teardown_errors.append(error)
                if teardown_errors and not body_failed:
                    raise teardown_errors[0]


_ROOT_MUTATION_GUARDS: weakref.WeakValueDictionary[str, _RootMutationGuard] = (
    weakref.WeakValueDictionary()
)
_POISONED_ROOT_MUTATION_GUARDS: dict[str, _RootMutationGuard] = {}
_ROOT_MUTATION_GUARDS_LOCK = threading.Lock()


def _retain_poisoned_root_mutation_guard(guard: _RootMutationGuard) -> None:
    key = str(guard._lock_path.parent.expanduser().resolve())
    with _ROOT_MUTATION_GUARDS_LOCK:
        _POISONED_ROOT_MUTATION_GUARDS[key] = guard


def _root_mutation_guard(root: Path) -> _RootMutationGuard:
    key = str(root.expanduser().resolve())
    with _ROOT_MUTATION_GUARDS_LOCK:
        guard = _POISONED_ROOT_MUTATION_GUARDS.get(key)
        if guard is None:
            guard = _ROOT_MUTATION_GUARDS.get(key)
        if guard is None:
            guard = _RootMutationGuard(Path(key) / ".workspace.mutation.lock")
            _ROOT_MUTATION_GUARDS[key] = guard
        return guard


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _guard_local_mutation(
    method: Callable[Concatenate["LocalWorkspaceBackend", _P], Coroutine[Any, Any, _R]],
) -> Callable[Concatenate["LocalWorkspaceBackend", _P], Coroutine[Any, Any, _R]]:
    @wraps(method)
    async def guarded(
        self: "LocalWorkspaceBackend",
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        async with self._mutation_guard():
            return await method(self, *args, **kwargs)

    return cast(
        Callable[Concatenate["LocalWorkspaceBackend", _P], Coroutine[Any, Any, _R]],
        guarded,
    )


class LocalWorkspaceBackend:
    """Local filesystem content plus SQLite metadata and FTS index."""

    DEFAULT_COLLECTIONS = ("dialogue", "observations", "decisions", "artifacts", "checkpoints", "runtime_events")
    DB_STORE_PROVIDER_METHODS = frozenset(
        {
            "put_record",
            "get_record",
            "index_record",
            "search",
            "link",
            "link_evidence",
            "links",
            "checkpoint",
            "put_checkpoint",
            "get_checkpoint",
            "put_artifact_ref",
            "claim_lease",
            "heartbeat_lease",
            "release_lease",
            "put_snapshot",
            "get_snapshot",
            "latest_snapshot",
            "latest_checkpoint",
            "checkpoint_history",
            "append_runtime_event",
            "query_runtime_events",
            "record_file_policy",
            "get_file_policy",
            "add_retention_anchor",
            "retention_anchors",
            "get_retention_lifecycle",
            "inspect_retention",
            "apply_retention",
            "prune_scope",
            "register_scratch_lease",
            "get_scratch_lease",
            "list_scratch_leases",
            "close_scratch_lease",
        }
    )

    def __getattribute__(self, name: str) -> Any:
        if name in object.__getattribute__(self, "DB_STORE_PROVIDER_METHODS"):
            try:
                provider = object.__getattribute__(self, "db_store_provider")
            except AttributeError:
                provider = None
            if provider is not None and provider is not self:
                return getattr(provider, name)
        return object.__getattribute__(self, name)

    def __init__(
        self,
        root: str | Path,
        *,
        create: bool = True,
        mode: str = "read_write",
        initialize_default_vector_store_provider: bool = True,
    ):
        self.root = Path(root).expanduser().resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self.content_root = self.root / "content"
        self.files_root = self.root / "files"
        self.db_path = self.root / "workspace.db"
        self._retention_full_vacuum_min_bytes = 64 * 1024 * 1024
        self._retention_full_vacuum_ratio = 0.25
        self._retention_incremental_vacuum_pages = 1024
        self._root_mutation_guard = _root_mutation_guard(self.root)
        self.mode = mode
        self.read_only = mode in {"read", "read_only", "readonly"}
        self.workspace_id = self._default_workspace_id()
        self.policy = LocalWorkspacePolicyEngine(self.content_root, read_only=self.read_only)
        self.content = LocalContentStore(self.content_root, self.policy)
        self.db_store_provider = self
        self.db_store_provider_name = "sqlite"
        self.embedding_provider = None
        self.vector_store_fallback_reason: str | None = None
        self.vector_store_provider = None
        self.vector_store_provider_name = None
        self.metadata = self
        self.checkpoint_store = self
        self.runtime_event_store = self
        self.ref_resolver = self
        self.retention_policy = self
        self.evidence_linker = self
        self.text_index = self
        self.vector_index = self._default_vector_index()
        if create:
            with self._try_sync_mutation_guard() as acquired:
                if not acquired:
                    raise WorkspaceConfigurationError(
                        "Workspace root mutation is busy during initialization."
                    )
                self._initialize()
        elif not self.root.exists():
            raise WorkspaceConfigurationError(f"Workspace root does not exist: { self.root }")
        else:
            self.workspace_id = self._load_workspace_meta().get("workspace_id", self.workspace_id)
        if initialize_default_vector_store_provider:
            self.vector_store_provider = self._default_vector_store_provider(create=create)
        self.vector_store_provider_name = getattr(self.vector_store_provider, "name", None)
        self.vector_index = self._default_vector_index()

    @asynccontextmanager
    async def _mutation_guard(self) -> AsyncIterator[None]:
        """Serialize root-shared mutations with asyncio-task re-entrancy."""

        async with self._root_mutation_guard.acquire():
            yield

    @contextmanager
    def _try_sync_mutation_guard(self) -> Iterator[bool]:
        with self._root_mutation_guard.try_acquire_sync() as acquired:
            yield acquired

    @staticmethod
    def _supports_descriptor_relative_delete() -> bool:
        return supports_descriptor_relative_delete()

    @staticmethod
    def _supports_advisory_lock() -> bool:
        return _NativeAdvisoryLock.supported()

    def _initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.content_root.mkdir(parents=True, exist_ok=True)
        self.files_root.mkdir(parents=True, exist_ok=True)
        for collection in self.DEFAULT_COLLECTIONS:
            self._ensure_collection(collection)
        meta_path = self.root / "workspace.meta.json"
        meta = self._load_workspace_meta()
        if "workspace_id" not in meta:
            meta["workspace_id"] = self.workspace_id
        self.workspace_id = str(meta["workspace_id"])
        meta.update(
            {
                "schema_version": "agently.workspace.local.v1",
                "backend": "local",
                "content_root": str(self.content_root),
                "files_root": str(self.files_root),
            }
        )
        meta.setdefault("created_at", utc_now())
        meta_path.write_text(json_dumps(meta), encoding="utf-8")
        self._ensure_root_guide()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema(conn)

    def _ensure_root_guide(self) -> None:
        guide_path = self.root / WORKSPACE_GUIDE_FILENAME
        if guide_path.exists():
            return
        area_lines = [
            f"- { name }/: { description }"
            for name, description in sorted(WORKSPACE_FILE_AREAS.items())
        ]
        guide_path.write_text(
            "\n".join(
                [
                    "# Agently Workspace",
                    "",
                    "This directory is managed by Agently.",
                    "",
                    "Directory roles:",
                    "",
                    "- workspace.db: local metadata, search index, links, checkpoints, and runtime events.",
                    "- workspace.meta.json: machine-readable Workspace metadata.",
                    "- content/: managed record payloads owned by Workspace.",
                    "- files/: editable file working trees scoped by lineage.",
                    "",
                    "Standard file areas inside each scoped files root:",
                    *area_lines,
                    "",
                    "Use files/lineage/.../files for task artifacts, downloads, and files shared with Actions or external coding agents.",
                    "Use scratch/lineage/.../scratch only through Workspace scratch APIs; do not mix scratch files into files/.",
                    "Do not edit workspace.db or content/ directly unless you are debugging Workspace internals.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _default_workspace_id(self):
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:24]
        return f"ws_{ digest }"

    def _default_vector_store_provider(self, *, create: bool):
        try:
            store = ChromaVectorStoreProvider(
                self.root / "vectors" / "chroma",
                create=create,
                mode=self.mode,
            )
            self.vector_store_fallback_reason = None
            return store
        except Exception as error:
            self.vector_store_fallback_reason = f"chroma_unavailable:{type(error).__name__}"
            return SQLiteVectorStoreProvider(
                self.db_path,
                read_only=self.read_only,
                create=create,
            )

    def _default_vector_index(self):
        if self.vector_store_provider is None:
            return NoopVectorIndex()
        return VectorIndexPipeline(
            embedding_provider=self.embedding_provider,
            vector_store_provider=self.vector_store_provider,
        )

    def configure_components(
        self,
        *,
        db_store_provider: Any | None = None,
        db_store_provider_name: str | None = None,
        embedding_provider: Any | None = None,
        vector_store_provider: Any | None = None,
        vector_store_provider_name: str | None = None,
        vector_store_fallback_reason: str | None = None,
    ) -> None:
        if db_store_provider is not None:
            self.db_store_provider = db_store_provider
        if db_store_provider_name is not None:
            self.db_store_provider_name = db_store_provider_name
        self.embedding_provider = embedding_provider
        if vector_store_provider is not None:
            self.vector_store_provider = vector_store_provider
        self.vector_store_provider_name = vector_store_provider_name or getattr(self.vector_store_provider, "name", None)
        self.vector_store_fallback_reason = vector_store_fallback_reason
        self.vector_index = self._default_vector_index()

    def _load_workspace_meta(self):
        meta_path = self.root / "workspace.meta.json"
        if not meta_path.exists():
            return {}
        return json_loads(meta_path.read_text(encoding="utf-8"), {})

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def _create_schema(self, conn: sqlite3.Connection):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                kind TEXT,
                path TEXT,
                sha256 TEXT,
                size INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '',
                scope_json TEXT NOT NULL DEFAULT '{}',
                source_json TEXT NOT NULL DEFAULT '{}',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                is_checkpoint INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL,
                step_id TEXT,
                record_id TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manifests (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS records_fts
            USING fts5(record_id UNINDEXED, summary, content)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS record_scope_index (
                record_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                scope_value TEXT NOT NULL,
                PRIMARY KEY(record_id, scope_key)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS record_scope_index_lookup_idx
            ON record_scope_index(scope_key, scope_value, record_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                state_version INTEGER,
                idempotency_key TEXT,
                parent_id TEXT,
                causation_id TEXT,
                parent_signal_id TEXT,
                node_id TEXT,
                operator_id TEXT,
                interrupt_id TEXT,
                resume_request_id TEXT,
                actor_id TEXT,
                lease_owner_id TEXT,
                aggregation_scope TEXT,
                snapshot_ref_json TEXT,
                exchange_id TEXT,
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                persisted_at TEXT
            )
            """
        )
        self._ensure_runtime_event_schema(conn)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS runtime_events_execution_sequence_idx
            ON runtime_events(execution_id, sequence)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS runtime_events_idempotency_idx
            ON runtime_events(execution_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS retention_anchors (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                anchor_type TEXT NOT NULL,
                sequence INTEGER,
                record_ref_json TEXT,
                summary_ref_json TEXT,
                preserved_event_ids_json TEXT NOT NULL DEFAULT '[]',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch_leases (
                lease_id TEXT PRIMARY KEY,
                scope_json TEXT NOT NULL DEFAULT '{}',
                local_path TEXT,
                mount_json TEXT,
                purpose TEXT,
                cleanup_policy TEXT NOT NULL DEFAULT 'on_close',
                expires_at TEXT,
                read_only INTEGER NOT NULL DEFAULT 0,
                policy_labels_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS scratch_leases_open_idx
            ON scratch_leases(closed_at, expires_at)
            """
        )
        self._backfill_scope_index(conn)
        conn.commit()

    def _ensure_runtime_event_schema(self, conn: sqlite3.Connection):
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(runtime_events)").fetchall()
        }
        for column, column_type in {
            "state_version": "INTEGER",
            "parent_signal_id": "TEXT",
            "operator_id": "TEXT",
            "interrupt_id": "TEXT",
            "resume_request_id": "TEXT",
            "actor_id": "TEXT",
            "lease_owner_id": "TEXT",
            "snapshot_ref_json": "TEXT",
            "persisted_at": "TEXT",
        }.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE runtime_events ADD COLUMN { column } { column_type }")

    def _ensure_writable(self):
        self.policy.ensure_writable()

    def _ensure_collection(self, collection: str):
        collection_path = self.content.ensure_collection(collection)
        descriptor = collection_path / "_collection.meta.json"
        if not descriptor.exists():
            descriptor.write_text(
                json_dumps(
                    {
                        "schema_version": "agently.workspace.collection.v1",
                        "collection": collection,
                        "created_at": utc_now(),
                    }
                ),
                encoding="utf-8",
            )

    def _resolve_content_path(self, path: str | Path):
        try:
            return self.policy.resolve_content_path(path)
        except WorkspacePolicyError:
            raise

    @staticmethod
    def _content_to_bytes(content: Any) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        return json_dumps(content).encode("utf-8")

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            return content
        return json_dumps(content)

    def _row_to_ref(self, row: sqlite3.Row) -> WorkspaceRecordRef:
        return {
            "id": str(row["id"]),
            "collection": str(row["collection"]),
            "kind": row["kind"],
            "path": row["path"],
            "sha256": row["sha256"],
            "size": int(row["size"] or 0),
            "summary": str(row["summary"] or ""),
            "scope": json_loads(row["scope_json"], {}),
            "source": json_loads(row["source_json"], {}),
            "created_at": str(row["created_at"]),
            "meta": json_loads(row["meta_json"], {}),
        }

    def _row_to_link(self, row: sqlite3.Row) -> WorkspaceLinkRef:
        return {
            "id": str(row["id"]),
            "source_id": str(row["source_id"]),
            "target_id": str(row["target_id"]),
            "relation": str(row["relation"]),
            "created_at": str(row["created_at"]),
            "meta": json_loads(row["meta_json"], {}),
        }

    def _features(self) -> dict[str, bool]:
        vector_index = self.vector_index
        vector_search = vector_index is not None and getattr(vector_index, "name", None) != "noop"
        if isinstance(vector_index, VectorIndexPipeline):
            vector_search = self.embedding_provider is not None and self.vector_store_provider is not None
        retention_provider = self.db_store_provider
        supports_retention = (
            callable(getattr(retention_provider, "inspect_retention", None))
            and callable(getattr(retention_provider, "apply_retention", None))
            and self._supports_advisory_lock()
        )
        return {
            "structured_get_data": True,
            "links_query": True,
            "checkpoint_lookup": True,
            "metadata_filters": True,
            "text_search": True,
            "vector_search": vector_search,
            "workspace_reference_envelopes": True,
            "bounded_read": True,
            "stream_read": True,
            "runtime_event_store": True,
            "runtime_event_idempotency": True,
            "snapshot_store": True,
            "evidence_links": True,
            "file_policy_metadata": True,
            "retention_anchors": True,
            "supports_cas": True,
            "supports_lease": True,
            "supports_artifact_refs": True,
            "supports_event_sequence": True,
            "supports_range_read": True,
            "supports_stream_read": True,
            "supports_retention": supports_retention,
            "supports_physical_reclamation": bool(
                retention_provider is self
                and isinstance(self.db_path, Path)
                and hasattr(os.stat_result, "st_blocks")
                and self._supports_advisory_lock()
                and supports_retention
            ),
            "supports_compaction_anchor": True,
            "supports_remote_backend": False,
        }

    @staticmethod
    def _policy_labels(ref: WorkspaceRecordRef) -> list[str]:
        labels = ref.get("meta", {}).get("policy_labels", [])
        if isinstance(labels, list):
            return [str(label) for label in labels]
        if isinstance(labels, str):
            return [labels]
        return []

    def _record_ref_envelope(self, ref: WorkspaceRecordRef) -> WorkspaceReferenceEnvelope:
        return {
            "workspace_id": self.workspace_id,
            "kind": str(ref.get("kind") or ref.get("collection") or "record"),
            "collection": str(ref.get("collection") or ""),
            "record_id": str(ref.get("id") or ""),
            "version": ref.get("meta", {}).get("version"),
            "content_ref": ref.get("path"),
            "digest": ref.get("sha256"),
            "size": int(ref.get("size") or 0),
            "created_at": str(ref.get("created_at") or ""),
            "policy_labels": self._policy_labels(ref),
            "backend_capabilities": self._features(),
        }

    @staticmethod
    def _is_reference_envelope(value: Any) -> bool:
        return isinstance(value, dict) and "workspace_id" in value and (
            "record_id" in value or "content_ref" in value
        )

    async def _coerce_ref_envelope(
        self,
        value: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None,
    ) -> WorkspaceReferenceEnvelope | None:
        if value is None:
            return None
        if self._is_reference_envelope(value):
            return value  # type: ignore[return-value]
        return await self.ref_envelope(value)  # type: ignore[arg-type]

    async def ref_envelope(self, ref_or_id: WorkspaceRecordRef | str) -> WorkspaceReferenceEnvelope:
        if isinstance(ref_or_id, dict):
            return self._record_ref_envelope(ref_or_id)
        if str(ref_or_id).startswith("rec_"):
            ref = await self.get_record(str(ref_or_id))
            if ref is None:
                raise FileNotFoundError(f"Workspace record not found: { ref_or_id }")
            return self._record_ref_envelope(ref)
        path = str(ref_or_id)
        target = self.policy.resolve_content_path(path)
        size = target.stat().st_size if target.exists() else 0
        digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        return {
            "workspace_id": self.workspace_id,
            "kind": "content",
            "collection": "",
            "record_id": "",
            "version": None,
            "content_ref": path,
            "digest": digest,
            "size": size,
            "created_at": "",
            "policy_labels": [],
            "backend_capabilities": self._features(),
        }

    def _content_type_for_path(self, path: str | None):
        if path and path.endswith(".json"):
            return "application/json"
        if path and path.endswith(".md"):
            return "text/markdown"
        return "text/plain"

    async def _resolve_read_target(
        self,
        ref_or_path: WorkspaceRecordRef | str,
    ) -> tuple[str, WorkspaceReferenceEnvelope, str | None, str | None]:
        path: str | None = None
        ref: WorkspaceRecordRef | None = None
        if isinstance(ref_or_path, dict):
            ref = ref_or_path
            path = ref_or_path.get("path")
        elif isinstance(ref_or_path, str) and ref_or_path.startswith("rec_"):
            ref = await self.get_record(ref_or_path)
            if ref is not None:
                path = ref.get("path")
        else:
            path = str(ref_or_path)
        if not path:
            raise FileNotFoundError(f"Workspace record content not found: { ref_or_path }")
        envelope = self._record_ref_envelope(ref) if ref is not None else await self.ref_envelope(path)
        digest = ref.get("sha256") if ref is not None else envelope.get("digest")
        return path, envelope, digest, self._content_type_for_path(path)

    @staticmethod
    def _normalize_runtime_event(event: RuntimeEvent | RuntimeEventDict | dict[str, Any]) -> dict[str, Any]:
        if hasattr(event, "model_dump"):
            try:
                return event.model_dump(mode="json")  # type: ignore[union-attr]
            except Exception:
                sanitized = DataFormatter.sanitize(event.model_dump(mode="python"))  # type: ignore[union-attr]
                return sanitized if isinstance(sanitized, dict) else {"value": sanitized}
        sanitized = DataFormatter.sanitize(dict(event))
        return sanitized if isinstance(sanitized, dict) else {"value": sanitized}

    def _row_to_runtime_event_record(self, row: sqlite3.Row) -> WorkspaceRuntimeEventRecord:
        snapshot_ref = json_loads(row["snapshot_ref_json"], None)
        return {
            "id": str(row["id"]),
            "execution_id": str(row["execution_id"]),
            "sequence": int(row["sequence"]),
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "state_version": row["state_version"],
            "idempotency_key": row["idempotency_key"],
            "parent_id": row["parent_id"],
            "causation_id": row["causation_id"],
            "parent_signal_id": row["parent_signal_id"],
            "node_id": row["node_id"],
            "operator_id": row["operator_id"],
            "interrupt_id": row["interrupt_id"],
            "resume_request_id": row["resume_request_id"],
            "actor_id": row["actor_id"],
            "lease_owner_id": row["lease_owner_id"],
            "aggregation_scope": row["aggregation_scope"],
            "snapshot_ref": snapshot_ref,
            "exchange_id": row["exchange_id"],
            "artifact_refs": json_loads(row["artifact_refs_json"], []),
            "event": json_loads(row["event_json"], {}),
            "created_at": str(row["created_at"]),
            "persisted_at": row["persisted_at"],
        }

    def _row_to_retention_anchor(self, row: sqlite3.Row) -> WorkspaceRetentionAnchor:
        return {
            "id": str(row["id"]),
            "execution_id": str(row["execution_id"]),
            "anchor_type": str(row["anchor_type"]),
            "sequence": row["sequence"],
            "record_ref": json_loads(row["record_ref_json"], None),
            "summary_ref": json_loads(row["summary_ref_json"], None),
            "preserved_event_ids": json_loads(row["preserved_event_ids_json"], []),
            "created_at": str(row["created_at"]),
            "meta": json_loads(row["meta_json"], {}),
        }

    @staticmethod
    def _strict_retention_record_row(row: sqlite3.Row) -> WorkspaceRecordRef:
        scope = strict_retention_json(
            row["scope_json"], dict, field=f"records.{row['id']}.scope_json"
        )
        source = strict_retention_json(
            row["source_json"], dict, field=f"records.{row['id']}.source_json"
        )
        meta = strict_retention_json(
            row["meta_json"], dict, field=f"records.{row['id']}.meta_json"
        )
        return cast(
            WorkspaceRecordRef,
            {
                "id": str(row["id"]),
                "collection": str(row["collection"]),
                "kind": row["kind"],
                "path": row["path"],
                "sha256": row["sha256"],
                "size": int(row["size"] or 0),
                "summary": str(row["summary"] or ""),
                "scope": scope,
                "source": source,
                "created_at": str(row["created_at"]),
                "meta": meta,
            },
        )

    def _strict_retention_runtime_event_row(
        self,
        row: sqlite3.Row,
    ) -> WorkspaceRuntimeEventRecord:
        field_root = f"runtime_events.{row['id']}"
        snapshot_value = strict_retention_json(
            row["snapshot_ref_json"],
            dict,
            field=f"{field_root}.snapshot_ref_json",
            nullable=True,
        )
        snapshot_ref = (
            validate_retained_reference_shape(
                snapshot_value,
                field=f"{field_root}.snapshot_ref_json",
            )
            if snapshot_value is not None
            else None
        )
        if snapshot_ref is not None and "workspace_id" not in snapshot_ref:
            raise ValueError(f"Persisted Workspace retention field '{field_root}.snapshot_ref_json' must be an envelope.")
        artifact_values = strict_retention_json(
            row["artifact_refs_json"], list, field=f"{field_root}.artifact_refs_json"
        )
        artifact_refs: list[WorkspaceReferenceEnvelope] = []
        for index, value in enumerate(artifact_values or []):
            ref = validate_retained_reference_shape(
                value,
                field=f"{field_root}.artifact_refs_json[{index}]",
            )
            if "workspace_id" not in ref:
                raise ValueError(
                    f"Persisted Workspace retention field '{field_root}.artifact_refs_json[{index}]' "
                    "must be an envelope."
                )
            artifact_refs.append(cast(WorkspaceReferenceEnvelope, ref))
        event = strict_retention_json(
            row["event_json"], dict, field=f"{field_root}.event_json"
        )
        record = self._row_to_runtime_event_record(row)
        record["snapshot_ref"] = cast(WorkspaceReferenceEnvelope | None, snapshot_ref)
        record["artifact_refs"] = artifact_refs
        record["event"] = event or {}
        return record

    def _strict_retention_anchor_row(
        self,
        row: sqlite3.Row,
    ) -> WorkspaceRetentionAnchor:
        field_root = f"retention_anchors.{row['id']}"

        def optional_envelope(column: str) -> WorkspaceReferenceEnvelope | None:
            value = strict_retention_json(
                row[column], dict, field=f"{field_root}.{column}", nullable=True
            )
            if value is None:
                return None
            ref = validate_retained_reference_shape(value, field=f"{field_root}.{column}")
            if "workspace_id" not in ref:
                raise ValueError(
                    f"Persisted Workspace retention field '{field_root}.{column}' must be an envelope."
                )
            return cast(WorkspaceReferenceEnvelope, ref)

        preserved_event_ids = strict_retention_json(
            row["preserved_event_ids_json"],
            list,
            field=f"{field_root}.preserved_event_ids_json",
        )
        if not all(isinstance(value, str) and value for value in preserved_event_ids or []):
            raise ValueError(
                f"Persisted Workspace retention field '{field_root}.preserved_event_ids_json' "
                "must contain non-empty strings."
            )
        meta = strict_retention_json(
            row["meta_json"], dict, field=f"{field_root}.meta_json"
        )
        return {
            "id": str(row["id"]),
            "execution_id": str(row["execution_id"]),
            "anchor_type": str(row["anchor_type"]),
            "sequence": row["sequence"],
            "record_ref": optional_envelope("record_ref_json"),
            "summary_ref": optional_envelope("summary_ref_json"),
            "preserved_event_ids": cast(list[str], preserved_event_ids),
            "created_at": str(row["created_at"]),
            "meta": meta or {},
        }

    @staticmethod
    def _strict_retention_link_row(row: sqlite3.Row) -> WorkspaceLinkRef:
        meta = strict_retention_json(
            row["meta_json"], dict, field=f"links.{row['id']}.meta_json"
        )
        return {
            "id": str(row["id"]),
            "source_id": str(row["source_id"]),
            "target_id": str(row["target_id"]),
            "relation": str(row["relation"]),
            "created_at": str(row["created_at"]),
            "meta": meta or {},
        }

    @staticmethod
    def _strict_retention_scratch_row(row: sqlite3.Row) -> WorkspaceScratchLease:
        field_root = f"scratch_leases.{row['lease_id']}"
        scope = strict_retention_json(
            row["scope_json"], dict, field=f"{field_root}.scope_json"
        )
        mount = strict_retention_json(
            row["mount_json"], dict, field=f"{field_root}.mount_json", nullable=True
        )
        policy_labels = strict_retention_json(
            row["policy_labels_json"], list, field=f"{field_root}.policy_labels_json"
        )
        if not all(isinstance(value, str) for value in policy_labels or []):
            raise ValueError(
                f"Persisted Workspace retention field '{field_root}.policy_labels_json' must contain strings."
            )
        return cast(
            WorkspaceScratchLease,
            {
                "lease_id": row["lease_id"],
                "scope": scope,
                "local_path": row["local_path"],
                "mount": mount,
                "purpose": row["purpose"],
                "cleanup_policy": row["cleanup_policy"],
                "expires_at": row["expires_at"],
                "read_only": bool(row["read_only"]),
                "policy_labels": policy_labels,
                "created_at": row["created_at"],
                "closed_at": row["closed_at"],
            },
        )

    @staticmethod
    def _scope_index_value(value: Any) -> str:
        return json_dumps(value)

    @staticmethod
    def _replace_scope_index_on_conn(conn: sqlite3.Connection, record_id: str, scope: dict[str, Any]) -> None:
        conn.execute("DELETE FROM record_scope_index WHERE record_id = ?", (record_id,))
        for key, value in scope.items():
            if value is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO record_scope_index(record_id, scope_key, scope_value)
                VALUES (?, ?, ?)
                """,
                (record_id, str(key), LocalWorkspaceBackend._scope_index_value(value)),
            )

    def _backfill_scope_index(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) AS count FROM record_scope_index").fetchone()
        if row is not None and int(row["count"] or 0) > 0:
            return
        rows = conn.execute("SELECT id, scope_json FROM records").fetchall()
        for record in rows:
            scope = json_loads(record["scope_json"], {})
            if isinstance(scope, dict):
                self._replace_scope_index_on_conn(conn, str(record["id"]), scope)

    def _get_manifest(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM manifests WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json_loads(row["value_json"], default)

    @staticmethod
    def _manifest_from_conn(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = conn.execute("SELECT value_json FROM manifests WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json_loads(row["value_json"], default)

    @staticmethod
    def _set_manifest_on_conn(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO manifests(key, value_json) VALUES (?, ?)",
            (key, json_dumps(value)),
        )

    def _set_manifest(self, key: str, value: Any) -> None:
        self._ensure_writable()
        with self._connect() as conn:
            self._set_manifest_on_conn(conn, key, value)
            conn.commit()

    @staticmethod
    def _checkpoint_state_version(state: Any) -> int | None:
        if not isinstance(state, dict):
            return None
        value = state.get("state_version")
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    def _latest_checkpoint_state_version(self, conn: sqlite3.Connection, run_id: str) -> int | None:
        row = conn.execute(
            """
            SELECT state_json FROM checkpoints
            WHERE run_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return 0
        return self._checkpoint_state_version(json_loads(row["state_json"], {}))

    def _ensure_expected_checkpoint_state_version(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        expected_state_version: int | None,
    ) -> None:
        if expected_state_version is None:
            return
        current_state_version = self._latest_checkpoint_state_version(conn, run_id)
        if current_state_version != expected_state_version:
            raise RuntimeError(
                f"Workspace checkpoint state version conflict for run '{ run_id }': "
                f"expected { expected_state_version }, current state version is { current_state_version }."
            )

    @staticmethod
    def _lease_manifest_key(run_id: str) -> str:
        return f"lease.{ run_id }"

    def _terminal_manifest_record_id(self, execution_id: str) -> str:
        digest = hashlib.sha256(
            f"{self.workspace_id}:{execution_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"rec_workspace_terminal_{digest}"

    def _require_active_lease(
        self,
        lease: Any,
        *,
        run_id: str,
        owner_id: str,
        lease_token: str,
        now: float,
    ) -> WorkspaceLeaseRef:
        if not isinstance(lease, dict) or lease.get("released_at") is not None:
            raise RuntimeError(f"Workspace lease for run '{ run_id }' is not active.")
        if float(lease.get("lease_until") or 0) <= now:
            raise RuntimeError(f"Workspace lease for run '{ run_id }' has expired.")
        if lease.get("owner_id") != owner_id or lease.get("lease_token") != lease_token:
            raise RuntimeError(f"Workspace lease conflict for run '{ run_id }'.")
        return cast(WorkspaceLeaseRef, lease)

    @staticmethod
    def _snapshot_recovery_active(state: Any) -> bool:
        if not isinstance(state, dict):
            return False
        interrupts = state.get("interrupts")
        if isinstance(interrupts, dict) and any(
            isinstance(item, dict) and item.get("status") == "waiting"
            for item in interrupts.values()
        ):
            return True
        intervention = state.get("intervention")
        intervention_ledger = (
            intervention.get("ledger") if isinstance(intervention, dict) else None
        )
        if isinstance(intervention_ledger, dict) and any(
            isinstance(item, dict) and item.get("status") == "pending"
            for item in intervention_ledger.values()
        ):
            return True
        pending_task_count = state.get("pending_task_count")
        if isinstance(pending_task_count, int) and not isinstance(pending_task_count, bool):
            if pending_task_count > 0:
                return True
        return False

    async def put(
        self,
        content: Any,
        *,
        collection: str,
        kind: str | None = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef:
        async with self._mutation_guard():
            return await self._put_unlocked(
                content,
                collection=collection,
                kind=kind,
                summary=summary,
                scope=scope,
                source=source,
                meta=meta,
            )

    async def _put_unlocked(
        self,
        content: Any,
        *,
        collection: str,
        kind: str | None = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef:
        self._ensure_writable()
        collection = slug(collection, "artifacts")
        self._ensure_collection(collection)
        record_id = f"rec_{ uuid.uuid4().hex }"
        content_bytes = self._content_to_bytes(content)
        content_text = self._content_to_text(content)
        digest = hashlib.sha256(content_bytes).hexdigest()
        suffix = ".json" if not isinstance(content, (str, bytes)) else ".txt"
        file_name = f"{ record_id }-{ slug(kind or collection, 'record') }{ suffix }"
        relative_path = f"{ collection }/{ file_name }"
        relative_path = await self.content.write_content(relative_path, content_bytes)
        created_at = utc_now()
        record_summary = summary or content_text[:240].replace("\n", " ").strip()
        ref: WorkspaceRecordRef = {
            "id": record_id,
            "collection": collection,
            "kind": kind,
            "path": relative_path,
            "sha256": digest,
            "size": len(content_bytes),
            "summary": record_summary,
            "scope": scope or {},
            "source": source or {},
            "created_at": created_at,
            "meta": meta or {},
        }
        await self.put_record(ref)
        await self.index_record(ref, content_text)
        try:
            await self.vector_index.index_record(ref, content_text)
        except (EmbeddingProviderUnavailableError, VectorStoreProviderUnavailableError):
            pass
        return ref

    @_guard_local_mutation
    async def put_record(self, ref: WorkspaceRecordRef) -> WorkspaceRecordRef:
        self._ensure_writable()
        self._ensure_collection(ref["collection"])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO records (
                    id, collection, kind, path, sha256, size, summary,
                    scope_json, source_json, meta_json, created_at, is_checkpoint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref["id"],
                    ref["collection"],
                    ref["kind"],
                    ref["path"],
                    ref["sha256"],
                    ref["size"],
                    ref["summary"],
                    json_dumps(ref["scope"]),
                    json_dumps(ref["source"]),
                    json_dumps(ref["meta"]),
                    ref["created_at"],
                    1 if ref["collection"] == "checkpoints" or ref["meta"].get("checkpoint") else 0,
                ),
            )
            self._replace_scope_index_on_conn(conn, ref["id"], ref["scope"])
            conn.commit()
        return ref

    async def get_record(self, record_id: str) -> WorkspaceRecordRef | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_ref(row)

    @_guard_local_mutation
    async def index_record(self, ref: WorkspaceRecordRef, content: str) -> None:
        self._ensure_writable()
        with self._connect() as conn:
            conn.execute("DELETE FROM records_fts WHERE record_id = ?", (ref["id"],))
            conn.execute(
                "INSERT INTO records_fts(record_id, summary, content) VALUES (?, ?, ?)",
                (ref["id"], ref["summary"], content),
            )
            conn.commit()

    async def get(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
        path: str | None = None
        if isinstance(ref_or_path, dict):
            path = ref_or_path.get("path")
        elif isinstance(ref_or_path, str) and ref_or_path.startswith("rec_"):
            with self._connect() as conn:
                row = conn.execute("SELECT path FROM records WHERE id = ?", (ref_or_path,)).fetchone()
            if row is not None:
                path = row["path"]
        else:
            path = str(ref_or_path)
        if not path:
            raise FileNotFoundError(f"Workspace record content not found: { ref_or_path }")
        return await self.content.read_content(path)

    async def get_data(self, ref_or_path: WorkspaceRecordRef | str) -> Any:
        content = await self.get(ref_or_path)
        path: str | None = None
        if isinstance(ref_or_path, dict):
            path = ref_or_path.get("path")
        elif isinstance(ref_or_path, str) and ref_or_path.startswith("rec_"):
            record = await self.get_record(ref_or_path)
            path = record.get("path") if record is not None else None
        else:
            path = str(ref_or_path)
        if path and path.endswith(".json") and isinstance(content, str):
            return json_loads(content, content)
        return content

    async def read_bounded(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> WorkspaceContentSegment:
        path, envelope, digest, content_type = await self._resolve_read_target(ref_or_path)
        segment = await self.content.read_content_segment(path, offset=offset, limit=limit)
        segment["ref"] = envelope
        segment["digest"] = digest
        segment["content_type"] = content_type
        return segment

    def stream_read(
        self,
        ref_or_path: WorkspaceRecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[WorkspaceContentSegment]:
        async def _stream():
            path, envelope, digest, content_type = await self._resolve_read_target(ref_or_path)
            async for segment in self.content.stream_content(
                path,
                offset=offset,
                limit=limit,
                chunk_size=chunk_size,
            ):
                segment["ref"] = envelope
                segment["digest"] = digest
                segment["content_type"] = content_type
                yield segment

        return _stream()

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[WorkspaceRecordRef]:
        filters = filters or {}
        params: list[Any] = []
        clauses: list[str] = []
        if filters.get("id") is not None:
            clauses.append("r.id = ?")
            params.append(str(filters["id"]))
        if filters.get("path") is not None:
            clauses.append("r.path = ?")
            params.append(str(filters["path"]))
        if filters.get("collection") is not None:
            clauses.append("r.collection = ?")
            params.append(str(filters["collection"]))
        if filters.get("kind") is not None:
            clauses.append("r.kind = ?")
            params.append(str(filters["kind"]))
        scope_filter_keys: set[str] = set()
        scope_index = 0
        for key, value in filters.items():
            if not key.startswith("scope."):
                continue
            scope_key = key.split(".", 1)[1]
            scope_filter_keys.add(key)
            alias = f"s{scope_index}"
            scope_index += 1
            clauses.append(
                f"""
                EXISTS (
                    SELECT 1 FROM record_scope_index {alias}
                    WHERE {alias}.record_id = r.id
                    AND {alias}.scope_key = ?
                    AND {alias}.scope_value = ?
                )
                """
            )
            params.extend([scope_key, self._scope_index_value(value)])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            if query:
                fts_query = self._safe_fts_query(query)
                rows = []
                if fts_query:
                    sql = (
                        "SELECT r.* FROM records r JOIN records_fts f ON r.id = f.record_id "
                        f"{ where + ' AND' if where else 'WHERE' } records_fts MATCH ? "
                        "ORDER BY bm25(records_fts)"
                    )
                    try:
                        rows = conn.execute(sql, [*params, fts_query]).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                if not rows:
                    rows = self._like_search_rows(conn, where=where, params=params, query=query)
            else:
                rows = conn.execute(f"SELECT r.* FROM records r { where } ORDER BY created_at DESC", params).fetchall()
        refs = [self._row_to_ref(row) for row in rows]
        for key, value in filters.items():
            if key in {"id", "path", "collection", "kind"} or key in scope_filter_keys:
                continue
            if key.startswith("scope."):
                path = key.split(".", 1)[1]
                refs = [ref for ref in refs if ref.get("scope", {}).get(path) == value]
            elif key.startswith("meta."):
                path = key.split(".", 1)[1]
                refs = [ref for ref in refs if ref.get("meta", {}).get(path) == value]
        return refs

    @staticmethod
    def _safe_fts_query(query: str) -> str:
        tokens = re.findall(r"[\w][\w.\-:/]*", str(query), flags=re.UNICODE)
        phrases = []
        for token in tokens[:16]:
            normalized = token.strip().strip(".:-/")
            if not normalized:
                continue
            escaped = normalized.replace('"', '""')
            phrases.append(f'"{ escaped }"')
        return " OR ".join(phrases)

    @staticmethod
    def _like_search_rows(
        conn: sqlite3.Connection,
        *,
        where: str,
        params: list[Any],
        query: str,
    ) -> list[sqlite3.Row]:
        like = f"%{ query }%"
        like_clauses = "(r.summary LIKE ? OR f.summary LIKE ? OR f.content LIKE ?)"
        sql = (
            "SELECT DISTINCT r.* FROM records r LEFT JOIN records_fts f ON r.id = f.record_id "
            f"{ where + ' AND ' if where else 'WHERE ' }{ like_clauses } "
            "ORDER BY r.created_at DESC"
        )
        return conn.execute(sql, [*params, like, like, like]).fetchall()

    @staticmethod
    def _record_id(value: WorkspaceRecordRef | str) -> str:
        if isinstance(value, dict):
            return str(value.get("id", ""))
        return str(value)

    @_guard_local_mutation
    async def link(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef:
        self._ensure_writable()
        link_id = f"link_{ uuid.uuid4().hex }"
        created_at = utc_now()
        source_id = self._record_id(source)
        target_id = self._record_id(target)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO links(id, source_id, target_id, relation, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, source_id, target_id, relation, json_dumps(meta or {}), created_at),
            )
            conn.commit()
        return {
            "id": link_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "created_at": created_at,
            "meta": meta or {},
        }

    @_guard_local_mutation
    async def link_evidence(
        self,
        source: WorkspaceRecordRef | str,
        target: WorkspaceRecordRef | str,
        relation: str,
        *,
        execution_id: str | None = None,
        operation_id: str | None = None,
        runtime_event_id: str | None = None,
        checkpoint_id: str | None = None,
        exchange_id: str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceLinkRef:
        evidence_meta = dict(meta or {})
        evidence_meta["evidence"] = {
            key: value
            for key, value in {
                "execution_id": execution_id,
                "operation_id": operation_id,
                "runtime_event_id": runtime_event_id,
                "checkpoint_id": checkpoint_id,
                "exchange_id": exchange_id,
                "artifact_refs": [
                    await self._coerce_ref_envelope(ref)
                    for ref in (artifact_refs or [])
                ],
            }.items()
            if value is not None
        }
        return await self.link(source, target, relation, evidence_meta)

    async def links(
        self,
        ref_or_id: WorkspaceRecordRef | str | None = None,
        *,
        source: WorkspaceRecordRef | str | None = None,
        target: WorkspaceRecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[WorkspaceLinkRef]:
        params: list[Any] = []
        clauses: list[str] = []
        if ref_or_id is not None:
            record_id = self._record_id(ref_or_id)
            clauses.append("(source_id = ? OR target_id = ?)")
            params.extend([record_id, record_id])
        if source is not None:
            clauses.append("source_id = ?")
            params.append(self._record_id(source))
        if target is not None:
            clauses.append("target_id = ?")
            params.append(self._record_id(target))
        if relation is not None:
            clauses.append("relation = ?")
            params.append(relation)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM links { where } ORDER BY created_at ASC", params).fetchall()
        return [self._row_to_link(row) for row in rows]

    @_guard_local_mutation
    async def checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        self._ensure_writable()
        with self._connect() as conn:
            self._ensure_expected_checkpoint_state_version(
                conn,
                run_id=run_id,
                expected_state_version=expected_state_version,
            )
        ref = await self.put(
            state,
            collection="checkpoints",
            kind="checkpoint",
            summary=f"Checkpoint for { run_id }" + (f" step { step_id }" if step_id else ""),
            scope={"run_id": run_id, **({"step_id": step_id} if step_id else {})},
            source={"type": "workspace", "name": "checkpoint"},
            meta={"checkpoint": True},
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(run_id, step_id, record_id, state_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, step_id, ref["id"], json_dumps(state), ref["created_at"]),
            )
            conn.execute(
                "INSERT OR REPLACE INTO manifests(key, value_json) VALUES (?, ?)",
                (f"checkpoint.latest.{ run_id }", json_dumps(ref)),
            )
            conn.commit()
        return ref

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        return await self.checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )

    async def get_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> WorkspaceRecordRef:
        return await self.put_checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        ref = await self.latest_snapshot(run_id)
        if ref is None:
            return None
        state = await self.get_data(ref)
        return state if isinstance(state, dict) else None

    async def latest_snapshot(self, run_id: str) -> WorkspaceRecordRef | None:
        return await self.latest_checkpoint(run_id)

    async def latest_checkpoint(self, run_id: str) -> WorkspaceRecordRef | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.* FROM checkpoints c
                JOIN records r ON r.id = c.record_id
                WHERE c.run_id = ?
                ORDER BY c.created_at DESC, c.rowid DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_ref(row)

    async def get_retention_lifecycle(
        self,
        execution_id: str,
        *,
        status: WorkspaceRetentionTerminalStatus,
        terminal_at: str,
    ) -> WorkspaceRetentionLifecycle:
        if not execution_id:
            raise ValueError("Workspace retention lifecycle requires a non-empty execution_id.")
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"Unsupported Workspace retention terminal status: {status}.")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT state_json FROM checkpoints
                WHERE run_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (execution_id,),
            ).fetchone()
            state = json_loads(row["state_json"], {}) if row is not None else {}
            lease = self._manifest_from_conn(conn, self._lease_manifest_key(execution_id), None)
        return {
            "execution_id": execution_id,
            "status": status,
            "terminal_at": terminal_at,
            "state_version": self._checkpoint_state_version(state) if row is not None else 0,
            "recovery_active": self._snapshot_recovery_active(state),
            "lease_active": bool(
                isinstance(lease, dict)
                and lease.get("released_at") is None
                and float(lease.get("lease_until") or 0) > time.time()
            ),
        }

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceRecordRef:
        metadata = dict(metadata or {})
        kind = str(metadata.pop("kind", "runtime_artifact"))
        summary = metadata.pop("summary", f"Artifact for { run_id }")
        scope = metadata.pop("scope", {})
        if not isinstance(scope, dict):
            scope = {}
        source = metadata.pop("source", {})
        if not isinstance(source, dict):
            source = {}
        source = {"type": "workspace", "name": "artifact_ref", **source}
        return await self.put(
            artifact,
            collection="artifacts",
            kind=kind,
            summary=str(summary),
            scope={"run_id": run_id, **scope},
            source=source,
            meta={"artifact_ref": True, **metadata},
        )

    @_guard_local_mutation
    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> WorkspaceLeaseRef:
        self._ensure_writable()
        if not owner_id:
            raise ValueError("owner_id must be non-empty.")
        if ttl <= 0:
            raise ValueError("ttl must be greater than 0.")
        now = time.time()
        lease_key = self._lease_manifest_key(run_id)
        with self._connect() as conn:
            self._ensure_expected_checkpoint_state_version(
                conn,
                run_id=run_id,
                expected_state_version=expected_state_version,
            )
            current = self._manifest_from_conn(conn, lease_key, None)
            if (
                isinstance(current, dict)
                and current.get("released_at") is None
                and float(current.get("lease_until") or 0) > now
                and current.get("owner_id") != owner_id
            ):
                raise RuntimeError(f"Workspace lease conflict for run '{ run_id }'.")
            timestamp = utc_now()
            lease: WorkspaceLeaseRef = {
                "run_id": run_id,
                "owner_id": owner_id,
                "lease_token": uuid.uuid4().hex,
                "lease_ttl": float(ttl),
                "lease_until": now + float(ttl),
                "claimed_at": timestamp,
                "heartbeat_at": timestamp,
                "released_at": None,
                "state_version": self._latest_checkpoint_state_version(conn, run_id),
            }
            self._set_manifest_on_conn(conn, lease_key, lease)
            conn.commit()
        return lease

    @_guard_local_mutation
    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        self._ensure_writable()
        now = time.time()
        lease_key = self._lease_manifest_key(run_id)
        with self._connect() as conn:
            active_lease = self._require_active_lease(
                self._manifest_from_conn(conn, lease_key, None),
                run_id=run_id,
                owner_id=owner_id,
                lease_token=lease_token,
                now=now,
            )
            lease: dict[str, Any] = dict(active_lease)
            lease["heartbeat_at"] = utc_now()
            lease_ttl = lease.get("lease_ttl")
            lease["lease_until"] = now + float(lease_ttl if isinstance(lease_ttl, (int, float, str)) else 0)
            self._set_manifest_on_conn(conn, lease_key, lease)
            conn.commit()
        return cast(WorkspaceLeaseRef, lease)

    @_guard_local_mutation
    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> WorkspaceLeaseRef:
        self._ensure_writable()
        now = time.time()
        lease_key = self._lease_manifest_key(run_id)
        with self._connect() as conn:
            active_lease = self._require_active_lease(
                self._manifest_from_conn(conn, lease_key, None),
                run_id=run_id,
                owner_id=owner_id,
                lease_token=lease_token,
                now=now,
            )
            lease: dict[str, Any] = dict(active_lease)
            lease["released_at"] = utc_now()
            lease["lease_until"] = now
            self._set_manifest_on_conn(conn, lease_key, lease)
            conn.commit()
        return cast(WorkspaceLeaseRef, lease)

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRecordRef]:
        params: list[Any] = [run_id]
        step_clause = ""
        if step_id is not None:
            step_clause = "AND c.step_id = ?"
            params.append(step_id)
        limit_clause = ""
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be greater than or equal to 0.")
            limit_clause = "LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.* FROM checkpoints c
                JOIN records r ON r.id = c.record_id
                WHERE c.run_id = ? { step_clause }
                ORDER BY c.created_at DESC, c.rowid DESC
                { limit_clause }
                """,
                params,
            ).fetchall()
        return [self._row_to_ref(row) for row in rows]

    @_guard_local_mutation
    async def append_runtime_event(
        self,
        execution_id: str,
        event: RuntimeEvent | RuntimeEventDict | dict[str, Any],
        *,
        sequence: int | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        snapshot_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        artifact_refs: list[WorkspaceRecordRef | WorkspaceReferenceEnvelope | str] | None = None,
        exchange_id: str | None = None,
        state_version: int | None = None,
        parent_id: str | None = None,
        causation_id: str | None = None,
        parent_signal_id: str | None = None,
        node_id: str | None = None,
        operator_id: str | None = None,
        interrupt_id: str | None = None,
        resume_request_id: str | None = None,
        actor_id: str | None = None,
        lease_owner_id: str | None = None,
        aggregation_scope: str | None = None,
    ) -> WorkspaceRuntimeEventRecord:
        self._ensure_writable()
        if not execution_id:
            raise ValueError("execution_id must be non-empty.")
        event_dict = self._normalize_runtime_event(event)
        event_id = str(event_dict.get("event_id") or f"evt_{ uuid.uuid4().hex }")
        event_dict["event_id"] = event_id
        event_type = str(event_dict.get("event_type") or "runtime.event")
        raw_meta = event_dict.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        resolved_parent_id = parent_id or meta.get("parent_event_id") or meta.get("parent_id")
        resolved_causation_id = causation_id or meta.get("causation_id")
        resolved_snapshot_ref = await self._coerce_ref_envelope(snapshot_ref)
        resolved_artifact_refs = [
            envelope
            for envelope in [
                await self._coerce_ref_envelope(ref)
                for ref in (artifact_refs or [])
            ]
            if envelope is not None
        ]
        created_at = utc_now()
        persisted_at = created_at
        with self._connect() as conn:
            if idempotency_key is not None:
                existing = conn.execute(
                    """
                    SELECT * FROM runtime_events
                    WHERE execution_id = ? AND idempotency_key = ?
                    """,
                    (execution_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._row_to_runtime_event_record(existing)
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM runtime_events WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            next_sequence = int(row["max_sequence"] or 0) + 1
            if expected_sequence is not None and int(expected_sequence) != next_sequence:
                raise RuntimeError(
                    f"Workspace runtime event sequence conflict for execution '{ execution_id }': "
                    f"expected { expected_sequence }, next sequence is { next_sequence }."
                )
            if sequence is None:
                sequence = next_sequence
            record_id = f"rtevt_{ uuid.uuid4().hex }"
            conn.execute(
                """
                INSERT INTO runtime_events (
                    id, execution_id, sequence, event_id, event_type, idempotency_key,
                    parent_id, causation_id, parent_signal_id, node_id, operator_id,
                    interrupt_id, resume_request_id, actor_id, lease_owner_id, state_version,
                    aggregation_scope, snapshot_ref_json,
                    exchange_id, artifact_refs_json, event_json, created_at, persisted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    execution_id,
                    sequence,
                    event_id,
                    event_type,
                    idempotency_key,
                    resolved_parent_id,
                    resolved_causation_id,
                    parent_signal_id or meta.get("parent_signal_id"),
                    node_id or meta.get("node_id"),
                    operator_id or meta.get("operator_id"),
                    interrupt_id or meta.get("interrupt_id"),
                    resume_request_id or meta.get("resume_request_id"),
                    actor_id or meta.get("actor_id"),
                    lease_owner_id or meta.get("lease_owner_id"),
                    state_version,
                    aggregation_scope or meta.get("aggregation_scope"),
                    json_dumps(resolved_snapshot_ref) if resolved_snapshot_ref is not None else None,
                    exchange_id or meta.get("exchange_id"),
                    json_dumps(resolved_artifact_refs),
                    json_dumps(event_dict),
                    created_at,
                    persisted_at,
                ),
            )
            row = conn.execute("SELECT * FROM runtime_events WHERE id = ?", (record_id,)).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError(f"Workspace runtime event insert failed: { record_id }")
        return self._row_to_runtime_event_record(row)

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRuntimeEventRecord]:
        params: list[Any] = [execution_id]
        clauses = ["execution_id = ?"]
        if sequence_from is not None:
            clauses.append("sequence >= ?")
            params.append(sequence_from)
        if sequence_to is not None:
            clauses.append("sequence <= ?")
            params.append(sequence_to)
        if event_id is not None:
            clauses.append("event_id = ?")
            params.append(event_id)
        limit_clause = ""
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be greater than or equal to 0.")
            limit_clause = "LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM runtime_events
                WHERE {' AND '.join(clauses)}
                ORDER BY sequence ASC
                { limit_clause }
                """,
                params,
            ).fetchall()
        return [self._row_to_runtime_event_record(row) for row in rows]

    @_guard_local_mutation
    async def record_file_policy(
        self,
        *,
        action_file_root: str | None = None,
        allowed_roots: list[str] | None = None,
        root_source: str = "workspace",
        path_normalization: str = "resolve",
        symlink_policy: str = "resolved_within_root",
        case_policy: str = "platform_default",
        policy_labels: list[str] | None = None,
        links: dict[str, str] | None = None,
    ) -> WorkspaceFilePolicyMetadata:
        metadata: WorkspaceFilePolicyMetadata = {
            "content_root": str(self.content_root),
            "files_root": str(self.files_root),
            "action_file_root": action_file_root,
            "allowed_roots": allowed_roots or [str(self.files_root)],
            "root_source": root_source,
            "path_normalization": path_normalization,
            "symlink_policy": symlink_policy,
            "case_policy": case_policy,
            "policy_labels": policy_labels or [],
            "links": links or {},
        }
        self._set_manifest("file_policy", metadata)
        return metadata

    async def get_file_policy(self) -> WorkspaceFilePolicyMetadata:
        existing = self._get_manifest("file_policy", None)
        if existing is not None:
            return existing
        return {
            "content_root": str(self.content_root),
            "files_root": str(self.files_root),
            "action_file_root": None,
            "allowed_roots": [str(self.files_root)],
            "root_source": "workspace",
            "path_normalization": "resolve",
            "symlink_policy": "resolved_within_root",
            "case_policy": "platform_default",
            "policy_labels": [],
            "links": {},
        }

    @_guard_local_mutation
    async def add_retention_anchor(
        self,
        execution_id: str,
        *,
        anchor_type: str,
        sequence: int | None = None,
        record_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        summary_ref: WorkspaceRecordRef | WorkspaceReferenceEnvelope | str | None = None,
        preserved_event_ids: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WorkspaceRetentionAnchor:
        self._ensure_writable()
        if not execution_id:
            raise ValueError("execution_id must be non-empty.")
        if not anchor_type:
            raise ValueError("anchor_type must be non-empty.")
        anchor_id = f"ret_{ uuid.uuid4().hex }"
        created_at = utc_now()
        resolved_record_ref = await self._coerce_ref_envelope(record_ref)
        resolved_summary_ref = await self._coerce_ref_envelope(summary_ref)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO retention_anchors (
                    id, execution_id, anchor_type, sequence, record_ref_json,
                    summary_ref_json, preserved_event_ids_json, meta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor_id,
                    execution_id,
                    anchor_type,
                    sequence,
                    json_dumps(resolved_record_ref) if resolved_record_ref is not None else None,
                    json_dumps(resolved_summary_ref) if resolved_summary_ref is not None else None,
                    json_dumps(preserved_event_ids or []),
                    json_dumps(meta or {}),
                    created_at,
                ),
            )
            row = conn.execute("SELECT * FROM retention_anchors WHERE id = ?", (anchor_id,)).fetchone()
            conn.commit()
        return self._row_to_retention_anchor(row)

    async def retention_anchors(
        self,
        execution_id: str,
        *,
        anchor_type: str | None = None,
        limit: int | None = None,
    ) -> list[WorkspaceRetentionAnchor]:
        params: list[Any] = [execution_id]
        clauses = ["execution_id = ?"]
        if anchor_type is not None:
            clauses.append("anchor_type = ?")
            params.append(anchor_type)
        limit_clause = ""
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be greater than or equal to 0.")
            limit_clause = "LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM retention_anchors
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC
                { limit_clause }
                """,
                params,
            ).fetchall()
        return [self._row_to_retention_anchor(row) for row in rows]

    @staticmethod
    def _retention_diagnostic(
        code: str,
        message: str,
        *,
        entity: str,
        detail: dict[str, Any] | None = None,
    ) -> WorkspaceRetentionDiagnostic:
        return retention_diagnostic(code, message, entity=entity, detail=detail)

    @staticmethod
    def _advisory_lock_diagnostic(
        error: _AdvisoryLockAcquisitionError,
        *,
        entity: str,
    ) -> WorkspaceRetentionDiagnostic:
        diagnostic = retention_diagnostic(
            error.diagnostic_code,
            str(error),
            entity=entity,
            detail={"error_type": type(error).__name__},
        )
        diagnostic["retryable"] = False
        return diagnostic

    def _safe_resolve_path(
        self,
        path: Path,
        *,
        entity: str,
        operation: str,
        code: str = "workspace.retention.ref_readback_failed",
    ) -> tuple[Path | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return path.expanduser().resolve(), None
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    def _safe_walk_paths(
        self,
        root: Path,
        pattern: str,
        *,
        entity: str,
        operation: str,
        code: str = "workspace.retention.ref_readback_failed",
    ) -> tuple[list[Path] | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return list(root.rglob(pattern)), None
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    def _safe_stat_path(
        self,
        path: Path,
        *,
        entity: str,
        operation: str,
        follow_symlinks: bool = True,
        code: str = "workspace.retention.ref_readback_failed",
        missing_message: str | None = None,
        missing_ok: bool = False,
    ) -> tuple[Any | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return (path.stat() if follow_symlinks else path.lstat()), None
        except FileNotFoundError as error:
            if missing_ok:
                return None, None
            if missing_message is not None:
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    missing_message,
                    entity=entity,
                )
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                code,
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    def _safe_read_path(
        self,
        path: Path,
        *,
        entity: str,
        operation: str,
    ) -> tuple[bytes | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return path.read_bytes(), None
        except (OSError, RuntimeError) as error:
            return None, self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                f"Workspace { operation } failed: { error }",
                entity=entity,
            )

    @staticmethod
    def _deduplicate_retained_refs(
        retained_refs: Sequence[WorkspaceRetainedReference],
    ) -> list[WorkspaceRetainedReference]:
        return deduplicate_retained_refs(retained_refs)

    def _retention_preview(
        self,
        *,
        status: str,
        scope: dict[str, Any],
        lifecycle: WorkspaceRetentionLifecycle,
        policy: WorkspaceRetentionPolicy,
        retained_refs: list[WorkspaceRetainedReference],
        inline_result: Any,
        diagnostics: list[WorkspaceRetentionDiagnostic] | None = None,
        selected: dict[str, list[str]] | None = None,
        logical_bytes: int = 0,
    ) -> WorkspaceRetentionPreview:
        return build_retention_preview(
            status=status,
            scope=scope,
            lifecycle=lifecycle,
            policy=policy,
            retained_refs=retained_refs,
            inline_result=inline_result,
            diagnostics=diagnostics,
            selected=selected,
            logical_bytes=logical_bytes,
        )

    async def _verified_record_envelope_unchecked(
        self,
        ref: WorkspaceRecordRef,
    ) -> tuple[WorkspaceReferenceEnvelope, WorkspaceRetentionDiagnostic | None]:
        envelope = await self.ref_envelope(ref)
        path = ref.get("path")
        if not path:
            if int(ref.get("size") or 0) != 0 or ref.get("sha256") is not None:
                return envelope, self._retention_diagnostic(
                    "workspace.retention.ref_readback_failed",
                    "Workspace record has content facts but no contained content path.",
                    entity=ref["id"],
                )
            return envelope, None
        target, diagnostic = self._safe_resolve_path(
            self.content_root / str(path),
            entity=ref["id"],
            operation="record content resolution",
        )
        if diagnostic is not None or target is None:
            return envelope, diagnostic
        try:
            target.relative_to(self.content_root)
        except ValueError:
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                "Workspace record content path is outside the Workspace content root.",
                entity=ref["id"],
            )
        target_stat, diagnostic = self._safe_stat_path(
            target,
            entity=ref["id"],
            operation="record content stat",
            missing_message="Workspace record content is missing.",
        )
        if diagnostic is not None:
            return envelope, diagnostic
        if target_stat is None or not stat.S_ISREG(target_stat.st_mode):
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_missing",
                "Workspace record content is missing.",
                entity=ref["id"],
            )
        raw, diagnostic = self._safe_read_path(
            target,
            entity=ref["id"],
            operation="record content readback",
        )
        if diagnostic is not None or raw is None:
            return envelope, diagnostic
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != int(ref.get("size") or 0):
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_size_mismatch",
                "Workspace record content size does not match its persisted ref.",
                entity=ref["id"],
            )
        if ref.get("sha256") != digest:
            return envelope, self._retention_diagnostic(
                "workspace.retention.ref_digest_mismatch",
                "Workspace record content digest does not match its persisted ref.",
                entity=ref["id"],
            )
        return envelope, None

    async def _verified_record_envelope(
        self,
        ref: WorkspaceRecordRef,
    ) -> tuple[WorkspaceReferenceEnvelope, WorkspaceRetentionDiagnostic | None]:
        try:
            return await self._verified_record_envelope_unchecked(ref)
        except (OSError, RuntimeError, WorkspaceConfigurationError, WorkspacePolicyError) as error:
            return self._record_ref_envelope(ref), self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                f"Workspace record readback failed: { error }",
                entity=str(ref.get("id") or ""),
            )

    async def _verify_retained_ref_unchecked(
        self,
        ref: WorkspaceRetainedReference,
        *,
        records_by_id: Mapping[str, WorkspaceRecordRef] | None = None,
    ) -> tuple[NormalizedRetainedRoot | None, WorkspaceRetentionDiagnostic | None]:
        if "workspace_id" in ref:
            envelope_ref = cast(WorkspaceReferenceEnvelope, ref)
            if str(envelope_ref.get("workspace_id") or "") != self.workspace_id:
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_workspace_mismatch",
                    "Retained ref belongs to another Workspace.",
                    entity=str(envelope_ref.get("record_id") or envelope_ref.get("content_ref") or ""),
                )
            record_id = str(envelope_ref.get("record_id") or "")
            if record_id:
                actual = (
                    records_by_id.get(record_id)
                    if records_by_id is not None
                    else await self.get_record(record_id)
                )
                if actual is None:
                    return None, self._retention_diagnostic(
                        "workspace.retention.ref_missing",
                        "Retained Workspace record does not exist.",
                        entity=record_id,
                    )
                actual_envelope, diagnostic = await self._verified_record_envelope(actual)
                if diagnostic is not None:
                    return None, diagnostic
                if envelope_ref.get("digest") != actual_envelope.get("digest"):
                    return None, self._retention_diagnostic(
                        "workspace.retention.ref_digest_mismatch",
                        "Retained Workspace envelope digest does not match readback.",
                        entity=record_id,
                    )
                if int(envelope_ref.get("size") or 0) != int(actual_envelope.get("size") or 0):
                    return None, self._retention_diagnostic(
                        "workspace.retention.ref_size_mismatch",
                        "Retained Workspace envelope size does not match readback.",
                        entity=record_id,
                    )
                if envelope_ref.get("content_ref") != actual_envelope.get("content_ref"):
                    return None, self._retention_diagnostic(
                        "workspace.retention.ref_path_mismatch",
                        "Retained Workspace envelope content path does not match readback.",
                        entity=record_id,
                    )
                return normalized_retained_root(
                    [actual_envelope],
                    record_ids=[record_id],
                    content_paths=[str(actual.get("path") or "")],
                ), None
            content_ref = str(envelope_ref.get("content_ref") or "")
            if not content_ref:
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "Retained Workspace envelope has no record or content identity.",
                    entity="retained_ref",
                )
            target, diagnostic = self._safe_resolve_path(
                self.content_root / content_ref,
                entity=content_ref,
                operation="retained content resolution",
            )
            if diagnostic is not None or target is None:
                return None, diagnostic
            try:
                target.relative_to(self.content_root)
            except ValueError:
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_readback_failed",
                    "Retained content ref is outside the Workspace content root.",
                    entity=content_ref,
                )
            target_stat, diagnostic = self._safe_stat_path(
                target,
                entity=content_ref,
                operation="retained content stat",
                missing_message="Retained Workspace content does not exist.",
            )
            if diagnostic is not None:
                return None, diagnostic
            if target_stat is None or not stat.S_ISREG(target_stat.st_mode):
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "Retained Workspace content does not exist.",
                    entity=content_ref,
                )
            actual_envelope = await self.ref_envelope(content_ref)
            if envelope_ref.get("digest") != actual_envelope.get("digest"):
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_digest_mismatch",
                    "Retained content envelope digest does not match readback.",
                    entity=content_ref,
                )
            if int(envelope_ref.get("size") or 0) != int(actual_envelope.get("size") or 0):
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_size_mismatch",
                    "Retained content envelope size does not match readback.",
                    entity=content_ref,
                )
            owners = sorted(
                (
                    owner
                    for owner in (records_by_id or {}).values()
                    if str(owner.get("path") or "") == content_ref
                ),
                key=lambda owner: str(owner.get("id") or ""),
            )
            canonical_refs: list[WorkspaceRetainedReference] = [actual_envelope]
            owner_ids: list[str] = []
            for owner in owners:
                _, owner_diagnostic = await self._verified_record_envelope(owner)
                if owner_diagnostic is not None:
                    return None, owner_diagnostic
                canonical_refs.append(owner)
                owner_ids.append(str(owner["id"]))
            return normalized_retained_root(
                canonical_refs,
                record_ids=owner_ids,
                content_paths=[content_ref],
            ), None

        if "id" in ref:
            record_ref = cast(WorkspaceRecordRef, ref)
            record_id = str(record_ref.get("id") or "")
            actual = (
                records_by_id.get(record_id)
                if records_by_id is not None
                else await self.get_record(record_id)
            )
            if actual is None:
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "Retained Workspace record does not exist.",
                    entity=record_id,
                )
            _, diagnostic = await self._verified_record_envelope(actual)
            if diagnostic is not None:
                return None, diagnostic
            if record_ref.get("sha256") != actual.get("sha256"):
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_digest_mismatch",
                    "Retained Workspace record digest does not match readback.",
                    entity=record_id,
                )
            if int(record_ref.get("size") or 0) != int(actual.get("size") or 0):
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_size_mismatch",
                    "Retained Workspace record size does not match readback.",
                    entity=record_id,
                )
            if record_ref.get("path") != actual.get("path"):
                return None, self._retention_diagnostic(
                    "workspace.retention.ref_path_mismatch",
                    "Retained Workspace record path does not match readback.",
                    entity=record_id,
                )
            return normalized_retained_root(
                [actual],
                record_ids=[record_id],
                content_paths=[str(actual.get("path") or "")],
            ), None

        file_ref = cast(WorkspaceFileRef, ref)
        relative_path = str(file_ref.get("path") or "")
        candidate = Path(relative_path)
        target = candidate if candidate.is_absolute() else self.files_root / candidate
        target, diagnostic = self._safe_resolve_path(
            target,
            entity=relative_path,
            operation="retained file resolution",
        )
        if diagnostic is not None or target is None:
            return None, diagnostic
        try:
            normalized_path = target.relative_to(self.files_root).as_posix()
        except ValueError:
            return None, self._retention_diagnostic(
                "workspace.retention.file_ref_invalid",
                "Retained file ref is outside the Workspace file root.",
                entity=relative_path,
            )
        target_stat, diagnostic = self._safe_stat_path(
            target,
            entity=normalized_path,
            operation="retained file stat",
            missing_message="Retained Workspace file does not exist.",
        )
        if diagnostic is not None:
            return None, diagnostic
        if target_stat is None or not stat.S_ISREG(target_stat.st_mode):
            return None, self._retention_diagnostic(
                "workspace.retention.ref_missing",
                "Retained Workspace file does not exist.",
                entity=normalized_path,
            )
        raw, diagnostic = self._safe_read_path(
            target,
            entity=normalized_path,
            operation="retained file readback",
        )
        if diagnostic is not None or raw is None:
            return None, diagnostic
        if int(file_ref.get("bytes") or 0) != len(raw):
            return None, self._retention_diagnostic(
                "workspace.retention.ref_size_mismatch",
                "Retained Workspace file size does not match readback.",
                entity=normalized_path,
            )
        digest = hashlib.sha256(raw).hexdigest()
        if str(file_ref.get("sha256") or "") != digest:
            return None, self._retention_diagnostic(
                "workspace.retention.ref_digest_mismatch",
                "Retained Workspace file digest does not match readback.",
                entity=normalized_path,
            )
        canonical_file_ref = dict(file_ref)
        canonical_file_ref["path"] = normalized_path
        return normalized_retained_root(
            [cast(WorkspaceFileRef, canonical_file_ref)],
            file_paths=[normalized_path],
        ), None

    async def _verify_retained_ref(
        self,
        ref: WorkspaceRetainedReference,
        *,
        records_by_id: Mapping[str, WorkspaceRecordRef] | None = None,
    ) -> tuple[NormalizedRetainedRoot | None, WorkspaceRetentionDiagnostic | None]:
        try:
            return await self._verify_retained_ref_unchecked(
                ref,
                records_by_id=records_by_id,
            )
        except (OSError, RuntimeError, WorkspaceConfigurationError, WorkspacePolicyError) as error:
            entity = str(
                ref.get("id")
                or ref.get("record_id")
                or ref.get("content_ref")
                or ref.get("path")
                or "retained_ref"
            )
            return None, self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                f"Workspace retained-ref readback failed: { error }",
                entity=entity,
            )

    def _retention_area_root(
        self,
        area: str,
    ) -> tuple[Path | None, Any | None, WorkspaceRetentionDiagnostic | None]:
        lexical_area_root = self.root / area
        lexical_area_stat, diagnostic = self._safe_stat_path(
            lexical_area_root,
            entity=str(lexical_area_root),
            operation="lineage area-root stat",
            follow_symlinks=False,
            code="workspace.retention.lineage_ambiguous",
            missing_ok=True,
        )
        if diagnostic is not None:
            return None, None, diagnostic
        if lexical_area_stat is None:
            if area == "scratch":
                return None, None, None
            return None, None, self._retention_diagnostic(
                "workspace.retention.lineage_ambiguous",
                "Workspace files area root is required for retention inspection.",
                entity=str(lexical_area_root),
            )
        if (
            stat.S_ISLNK(lexical_area_stat.st_mode)
            or not stat.S_ISDIR(lexical_area_stat.st_mode)
        ):
            return None, None, self._retention_diagnostic(
                "workspace.retention.lineage_ambiguous",
                "Workspace retention area root must be a directly owned non-symlink directory.",
                entity=str(lexical_area_root),
            )
        area_root, diagnostic = self._safe_resolve_path(
            lexical_area_root,
            entity=area,
            operation="lineage area resolution",
        )
        if diagnostic is not None or area_root is None:
            return None, None, diagnostic
        if area_root != lexical_area_root or area_root.parent != self.root:
            return None, None, self._retention_diagnostic(
                "workspace.retention.lineage_ambiguous",
                "Workspace retention area root is not directly contained by the Workspace root.",
                entity=str(lexical_area_root),
            )
        confirmed_area_stat, diagnostic = self._safe_stat_path(
            lexical_area_root,
            entity=str(lexical_area_root),
            operation="lineage area-root confirmation stat",
            follow_symlinks=False,
        )
        if diagnostic is not None or confirmed_area_stat is None:
            return None, None, diagnostic
        if (
            stat.S_ISLNK(confirmed_area_stat.st_mode)
            or not stat.S_ISDIR(confirmed_area_stat.st_mode)
            or (confirmed_area_stat.st_dev, confirmed_area_stat.st_ino)
            != (lexical_area_stat.st_dev, lexical_area_stat.st_ino)
        ):
            return None, None, self._retention_diagnostic(
                "workspace.retention.ref_readback_failed",
                "Workspace retention area root changed during inspection.",
                entity=str(lexical_area_root),
            )
        return area_root, lexical_area_stat, None

    def _scoped_area_files(
        self,
        scope: dict[str, Any],
        area: str,
    ) -> tuple[list[str], WorkspaceRetentionDiagnostic | None]:
        lexical_area_root = self.root / area
        area_root, lexical_area_stat, diagnostic = self._retention_area_root(area)
        if diagnostic is not None:
            return [], diagnostic
        if area_root is None or lexical_area_stat is None:
            return [], None
        lineage_root = area_root / "lineage"
        lineage_stat, diagnostic = self._safe_stat_path(
            lineage_root,
            entity=str(lineage_root),
            operation="lineage root stat",
            follow_symlinks=False,
            code="workspace.retention.lineage_ambiguous",
            missing_ok=True,
        )
        if diagnostic is not None:
            return [], diagnostic
        if lineage_stat is None:
            current_area_stat, area_diagnostic = self._safe_stat_path(
                lexical_area_root,
                entity=str(lexical_area_root),
                operation="lineage empty-area confirmation stat",
                follow_symlinks=False,
            )
            if area_diagnostic is not None or current_area_stat is None:
                return [], area_diagnostic
            if (current_area_stat.st_dev, current_area_stat.st_ino) != (
                lexical_area_stat.st_dev,
                lexical_area_stat.st_ino,
            ):
                return [], self._retention_diagnostic(
                    "workspace.retention.ref_readback_failed",
                    "Workspace retention area root changed during inspection.",
                    entity=str(lexical_area_root),
                )
            return [], None
        if stat.S_ISLNK(lineage_stat.st_mode) or not stat.S_ISDIR(lineage_stat.st_mode):
            return [], self._retention_diagnostic(
                "workspace.retention.lineage_ambiguous",
                "Workspace lineage root must be a real directory, not a symlink.",
                entity=str(lineage_root),
            )
        candidate_roots: list[Path] = []
        raw_lineage = scope.get("scope_lineage")
        if raw_lineage is not None:
            if not isinstance(raw_lineage, list) or not raw_lineage:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope_lineage must be a non-empty ordered list.",
                    entity=area,
                )
            lineage = normalize_lineage(raw_lineage)
            if len(lineage) != len(raw_lineage) or any(
                not isinstance(item, Mapping) or item.get("id") is None for item in raw_lineage
            ):
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope_lineage contains an incomplete node.",
                    entity=area,
                )
            candidate = lineage_root
            for node in lineage:
                scope_key = SCOPE_LINEAGE_KINDS.get(node["kind"])
                if scope_key is not None and scope_key in scope and str(scope[scope_key]) != node["id"]:
                    return [], self._retention_diagnostic(
                        "workspace.retention.lineage_ambiguous",
                        "Workspace cleanup scope conflicts with its ordered scope_lineage.",
                        entity=scope_key,
                    )
                candidate = candidate / slug(node["kind"], "scope") / slug(node["id"], "default")
            candidate_roots = [candidate]
        else:
            nodes = scope_filter_path_nodes(scope)
            if not nodes:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope cannot identify a contained lineage subtree.",
                    entity=area,
                )
            matched_candidates: set[Path] = set()
            required_pairs = {
                (slug(node["kind"], "scope"), slug(node["id"], "default"))
                for node in nodes
            }
            for leaf in nodes:
                leaf_kind = slug(leaf["kind"], "scope")
                leaf_id = slug(leaf["id"], "default")
                candidates, diagnostic = self._safe_walk_paths(
                    lineage_root,
                    leaf_id,
                    entity=str(lineage_root),
                    operation="lineage discovery",
                )
                if diagnostic is not None or candidates is None:
                    return [], diagnostic
                for candidate in candidates:
                    candidate_stat, diagnostic = self._safe_stat_path(
                        candidate,
                        entity=str(candidate),
                        operation="lineage candidate stat",
                        follow_symlinks=False,
                        code="workspace.retention.lineage_ambiguous",
                        missing_ok=True,
                    )
                    if diagnostic is not None:
                        return [], diagnostic
                    if (
                        candidate_stat is None
                        or stat.S_ISLNK(candidate_stat.st_mode)
                        or not stat.S_ISDIR(candidate_stat.st_mode)
                        or candidate.parent.name != leaf_kind
                    ):
                        continue
                    parts = candidate.relative_to(lineage_root).parts
                    lineage_pairs = set(zip(parts[0::2], parts[1::2]))
                    if required_pairs.issubset(lineage_pairs):
                        matched_candidates.add(candidate)
            candidate_roots = sorted(matched_candidates)
            if not candidate_roots:
                return [], None
            if len(candidate_roots) != 1:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    "Workspace cleanup scope matches multiple lineage subtrees.",
                    entity=area,
                )

        paths: set[str] = set()
        for candidate in candidate_roots:
            lexical_parts = (lineage_root,) + tuple(
                lineage_root.joinpath(*candidate.relative_to(lineage_root).parts[:index])
                for index in range(1, len(candidate.relative_to(lineage_root).parts) + 1)
            )
            candidate_missing = False
            for lexical_path in lexical_parts:
                lexical_stat, diagnostic = self._safe_stat_path(
                    lexical_path,
                    entity=str(lexical_path),
                    operation="lineage lexical-component stat",
                    follow_symlinks=False,
                    code="workspace.retention.lineage_ambiguous",
                    missing_ok=True,
                )
                if diagnostic is not None:
                    return [], diagnostic
                if lexical_stat is None:
                    candidate_missing = True
                    break
                if stat.S_ISLNK(lexical_stat.st_mode):
                    return [], self._retention_diagnostic(
                        "workspace.retention.lineage_ambiguous",
                        "Workspace cleanup does not traverse symlinks in a lineage path.",
                        entity=str(lexical_path),
                    )
            if candidate_missing:
                continue
            resolved_candidate, diagnostic = self._safe_resolve_path(
                candidate,
                entity=str(candidate),
                operation="lineage subtree resolution",
                code="workspace.retention.lineage_ambiguous",
            )
            if diagnostic is not None or resolved_candidate is None:
                return [], diagnostic
            confirmed_candidate_stat, diagnostic = self._safe_stat_path(
                candidate,
                entity=str(candidate),
                operation="lineage subtree confirmation stat",
                follow_symlinks=False,
            )
            if diagnostic is not None or confirmed_candidate_stat is None:
                return [], diagnostic
            if (
                lexical_stat is None
                or (confirmed_candidate_stat.st_dev, confirmed_candidate_stat.st_ino)
                != (lexical_stat.st_dev, lexical_stat.st_ino)
            ):
                return [], self._retention_diagnostic(
                    "workspace.retention.ref_readback_failed",
                    "Workspace lineage subtree changed during inspection.",
                    entity=str(candidate),
                )
            try:
                resolved_candidate.relative_to(lineage_root)
            except ValueError as error:
                return [], self._retention_diagnostic(
                    "workspace.retention.lineage_ambiguous",
                    f"Workspace lineage subtree is not contained: { error }",
                    entity=str(candidate),
                )
            descendants, diagnostic = self._safe_walk_paths(
                candidate,
                "*",
                entity=str(candidate),
                operation="lineage readback",
            )
            if diagnostic is not None or descendants is None:
                return [], diagnostic
            for path in descendants:
                path_stat, diagnostic = self._safe_stat_path(
                    path,
                    entity=str(path),
                    operation="lineage descendant stat",
                    follow_symlinks=False,
                )
                if diagnostic is not None:
                    return [], diagnostic
                if path_stat is None:
                    return [], self._retention_diagnostic(
                        "workspace.retention.ref_readback_failed",
                        "Workspace lineage entry disappeared during inspection.",
                        entity=str(path),
                    )
                if stat.S_ISLNK(path_stat.st_mode):
                    return [], self._retention_diagnostic(
                        "workspace.retention.lineage_ambiguous",
                        "Workspace cleanup does not traverse symlinks in a lineage subtree.",
                        entity=str(path),
                    )
                if not stat.S_ISREG(path_stat.st_mode):
                    continue
                resolved, diagnostic = self._safe_resolve_path(
                    path,
                    entity=str(path),
                    operation="lineage file resolution",
                )
                if diagnostic is not None or resolved is None:
                    return [], diagnostic
                try:
                    paths.add(resolved.relative_to(area_root).as_posix())
                except ValueError as error:
                    return [], self._retention_diagnostic(
                        "workspace.retention.ref_readback_failed",
                        f"Workspace lineage readback failed: { error }",
                        entity=str(path),
                    )
        return sorted(paths), None

    def _read_retention_snapshot(
        self,
        scope: Mapping[str, Any],
        *,
        execution_id: str,
        checkpoint_manifest_key: str,
        lease_manifest_key: str,
        connection: sqlite3.Connection | None = None,
    ) -> _RetentionSQLiteSnapshot:
        if connection is not None:
            return self._read_retention_snapshot_on_conn(
                connection,
                scope,
                execution_id=execution_id,
                checkpoint_manifest_key=checkpoint_manifest_key,
                lease_manifest_key=lease_manifest_key,
            )
        with self._connect() as conn:
            conn.execute("BEGIN")
            snapshot = self._read_retention_snapshot_on_conn(
                conn,
                scope,
                execution_id=execution_id,
                checkpoint_manifest_key=checkpoint_manifest_key,
                lease_manifest_key=lease_manifest_key,
            )
            conn.rollback()
            return snapshot

    def _read_retention_snapshot_on_conn(
        self,
        conn: sqlite3.Connection,
        scope: Mapping[str, Any],
        *,
        execution_id: str,
        checkpoint_manifest_key: str,
        lease_manifest_key: str,
    ) -> _RetentionSQLiteSnapshot:
        all_record_rows = conn.execute("SELECT * FROM records ORDER BY id").fetchall()
        clauses: list[str] = []
        scope_params: list[Any] = []
        for index, (key, value) in enumerate(scope.items()):
            alias = f"ret_scope_{index}"
            clauses.append(
                f"""
                EXISTS (
                    SELECT 1 FROM record_scope_index {alias}
                    WHERE {alias}.record_id = r.id
                    AND {alias}.scope_key = ?
                    AND {alias}.scope_value = ?
                )
                """
            )
            scope_params.extend([key, self._scope_index_value(value)])
        scoped_rows = conn.execute(
            f"SELECT r.id FROM records r WHERE {' AND '.join(clauses)} ORDER BY r.id",
            scope_params,
        ).fetchall()
        checkpoint_scope = (
            f"""
            OR EXISTS (
                SELECT 1 FROM records r
                WHERE r.id = checkpoints.record_id
                AND {' AND '.join(clauses)}
            )
            """
            if clauses
            else ""
        )
        checkpoint_rows = conn.execute(
            f"""
            SELECT * FROM checkpoints
            WHERE run_id = ? {checkpoint_scope}
            ORDER BY created_at ASC, rowid ASC
            """,
            [execution_id, *scope_params],
        ).fetchall()
        runtime_event_rows = conn.execute(
            "SELECT * FROM runtime_events WHERE execution_id = ? ORDER BY sequence ASC, id ASC",
            (execution_id,),
        ).fetchall()
        anchor_rows = conn.execute(
            "SELECT * FROM retention_anchors WHERE execution_id = ? ORDER BY id",
            (execution_id,),
        ).fetchall()
        scratch_rows = conn.execute("SELECT * FROM scratch_leases ORDER BY lease_id").fetchall()
        link_rows = conn.execute("SELECT * FROM links ORDER BY id").fetchall()
        scope_index_rows = conn.execute(
            "SELECT record_id, scope_key, scope_value FROM record_scope_index ORDER BY record_id, scope_key"
        ).fetchall()
        fts_rows = conn.execute(
            "SELECT record_id, summary, content FROM records_fts ORDER BY record_id"
        ).fetchall()
        manifest_keys = {
            checkpoint_manifest_key,
            lease_manifest_key,
            *(
                f"checkpoint.latest.{str(row['run_id'])}"
                for row in checkpoint_rows
            ),
        }
        manifest_placeholders = ", ".join("?" for _ in manifest_keys)
        manifest_rows = conn.execute(
            f"SELECT key, value_json FROM manifests WHERE key IN ({manifest_placeholders}) ORDER BY key",
            sorted(manifest_keys),
        ).fetchall()
        vector_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspace_vectors'"
        ).fetchone()
        vector_rows = (
            conn.execute(
                "SELECT record_id, ref_json, embedding_json FROM workspace_vectors ORDER BY record_id"
            ).fetchall()
            if vector_table is not None
            else []
        )
        return _RetentionSQLiteSnapshot(
            all_record_rows=all_record_rows,
            scoped_rows=scoped_rows,
            checkpoint_rows=checkpoint_rows,
            runtime_event_rows=runtime_event_rows,
            anchor_rows=anchor_rows,
            scratch_rows=scratch_rows,
            link_rows=link_rows,
            scope_index_rows=scope_index_rows,
            fts_rows=fts_rows,
            manifest_rows=manifest_rows,
            vector_rows=vector_rows,
        )

    def _decode_retention_snapshot(
        self,
        snapshot: _RetentionSQLiteSnapshot,
        *,
        checkpoint_manifest_key: str,
        lease_manifest_key: str,
    ) -> _DecodedRetentionSnapshot:
        all_records = {
            str(row["id"]): self._strict_retention_record_row(row)
            for row in snapshot.all_record_rows
        }
        runtime_events = [
            self._strict_retention_runtime_event_row(row)
            for row in snapshot.runtime_event_rows
        ]
        anchors = [self._strict_retention_anchor_row(row) for row in snapshot.anchor_rows]
        links = [self._strict_retention_link_row(row) for row in snapshot.link_rows]
        scratch_leases = [
            self._strict_retention_scratch_row(row) for row in snapshot.scratch_rows
        ]
        checkpoint_facts = [
            {
                "run_id": str(row["run_id"]),
                "step_id": row["step_id"],
                "record_id": str(row["record_id"]),
                "state": strict_retention_json(
                    row["state_json"],
                    dict,
                    field=f"checkpoints.{row['run_id']}.{row['record_id']}.state_json",
                ),
                "created_at": str(row["created_at"]),
            }
            for row in snapshot.checkpoint_rows
        ]
        manifest_values: dict[str, Any] = {}
        for row in snapshot.manifest_rows:
            key = str(row["key"])
            value = strict_retention_json(
                row["value_json"], dict, field=f"manifests.{key}.value_json"
            )
            if key.startswith("checkpoint.latest."):
                value = validate_retained_reference_shape(
                    value,
                    field=f"manifests.{key}.value_json",
                )
            elif key == lease_manifest_key:
                required_strings = ("run_id", "owner_id", "lease_token")
                if value is None or any(
                    not isinstance(value.get(field), str) or not value[field]
                    for field in required_strings
                ):
                    raise ValueError(
                        f"Persisted Workspace retention field 'manifests.{key}.value_json' "
                        "has an invalid lease identity."
                    )
                if isinstance(value.get("lease_until"), bool) or not isinstance(
                    value.get("lease_until"), (int, float)
                ):
                    raise ValueError(
                        f"Persisted Workspace retention field 'manifests.{key}.value_json' "
                        "has an invalid lease deadline."
                    )
                if isinstance(value.get("lease_ttl"), bool) or not isinstance(
                    value.get("lease_ttl"), (int, float)
                ):
                    raise ValueError(
                        f"Persisted Workspace retention field 'manifests.{key}.value_json' "
                        "has an invalid lease TTL."
                    )
                for timestamp_field in ("claimed_at", "heartbeat_at"):
                    if not isinstance(value.get(timestamp_field), str):
                        raise ValueError(
                            f"Persisted Workspace retention field 'manifests.{key}.value_json' "
                            f"has an invalid {timestamp_field}."
                        )
                if not isinstance(value.get("released_at"), (str, type(None))):
                    raise ValueError(
                        f"Persisted Workspace retention field 'manifests.{key}.value_json' "
                        "has an invalid released_at."
                    )
                if isinstance(value.get("state_version"), bool) or not isinstance(
                    value.get("state_version"), (int, type(None))
                ):
                    raise ValueError(
                        f"Persisted Workspace retention field 'manifests.{key}.value_json' "
                        "has an invalid state_version."
                    )
            manifest_values[key] = value
        actual_scope_index: dict[tuple[str, str], str] = {}
        for row in snapshot.scope_index_rows:
            scope_value = strict_retention_json_value(
                row["scope_value"],
                field=f"record_scope_index.{row['record_id']}.{row['scope_key']}.scope_value",
            )
            actual_scope_index[(str(row["record_id"]), str(row["scope_key"]))] = json_dumps(
                scope_value
            )
        expected_scope_index = {
            (record_id, str(scope_key)): json_dumps(scope_value)
            for record_id, record in all_records.items()
            for scope_key, scope_value in record["scope"].items()
            if scope_value is not None
        }
        if actual_scope_index != expected_scope_index:
            raise ValueError(
                "Persisted Workspace record_scope_index does not match authoritative record scope data."
            )
        for row in snapshot.vector_rows:
            record_id = str(row["record_id"])
            vector_ref = strict_retention_json(
                row["ref_json"],
                dict,
                field=f"workspace_vectors.{record_id}.ref_json",
            )
            validate_retained_reference_shape(
                vector_ref,
                field=f"workspace_vectors.{record_id}.ref_json",
            )
            embedding = strict_retention_json(
                row["embedding_json"],
                list,
                field=f"workspace_vectors.{record_id}.embedding_json",
            )
            if not all(
                not isinstance(value, bool) and isinstance(value, (int, float))
                for value in embedding or []
            ):
                raise ValueError(
                    f"Persisted Workspace retention field 'workspace_vectors.{record_id}.embedding_json' "
                    "must contain numbers."
                )
        return _DecodedRetentionSnapshot(
            all_records=all_records,
            runtime_events=runtime_events,
            anchors=anchors,
            links=links,
            scratch_leases=scratch_leases,
            checkpoint_facts=checkpoint_facts,
            manifest_values=manifest_values,
            manifest_raw={
                str(row["key"]): str(row["value_json"]) for row in snapshot.manifest_rows
            },
        )

    async def _resolve_retention_roots(
        self,
        *,
        owned_record_ids: set[str],
        all_records: Mapping[str, WorkspaceRecordRef],
        retained_refs: Sequence[WorkspaceRetainedReference],
        checkpoint_manifest: WorkspaceRetainedReference | None,
        retain_checkpoint_manifest: bool,
        anchors: Sequence[WorkspaceRetentionAnchor],
        runtime_events: Sequence[WorkspaceRuntimeEventRecord],
        retain_all_runtime_events: bool,
        links: Sequence[WorkspaceLinkRef],
    ) -> _ResolvedRetentionRoots:
        diagnostics: list[WorkspaceRetentionDiagnostic] = []
        canonical_records: dict[str, WorkspaceRecordRef] = {}
        for record_id in sorted(owned_record_ids):
            ref = all_records.get(record_id)
            if ref is None:
                diagnostics.append(
                    retention_diagnostic(
                        "workspace.retention.ref_missing",
                        "A Workspace checkpoint row references a missing record.",
                        entity=record_id,
                    )
                )
                continue
            _, diagnostic = await self._verified_record_envelope(ref)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                continue
            canonical_records[record_id] = ref

        canonical_refs: list[WorkspaceRetainedReference] = []
        record_ids: set[str] = set()
        file_paths: set[str] = set()
        content_paths: set[str] = set()

        async def retain(ref: WorkspaceRetainedReference) -> None:
            root, diagnostic = await self._verify_retained_ref(ref, records_by_id=all_records)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                canonical_refs.append(ref)
                return
            if root is not None:
                canonical_refs.extend(root.canonical_refs)
                record_ids.update(root.record_ids)
                file_paths.update(root.file_paths)
                content_paths.update(root.content_paths)

        for ref in retained_refs:
            await retain(ref)
        if retain_checkpoint_manifest and checkpoint_manifest is not None:
            await retain(checkpoint_manifest)

        preserved_event_ids: set[str] = set()
        for anchor in anchors:
            for anchor_ref in (anchor.get("record_ref"), anchor.get("summary_ref")):
                if anchor_ref is not None:
                    await retain(anchor_ref)
            preserved_event_ids.update(str(value) for value in anchor["preserved_event_ids"])

        known_event_ids = {event["event_id"] for event in runtime_events}
        for preserved_event_id in sorted(preserved_event_ids - known_event_ids):
            diagnostics.append(
                retention_diagnostic(
                    "workspace.retention.ref_missing",
                    "A retention anchor references a RuntimeEvent that does not exist.",
                    entity=preserved_event_id,
                )
            )
        event_ids = known_event_ids if retain_all_runtime_events else preserved_event_ids
        for event in runtime_events:
            if event["event_id"] not in event_ids:
                continue
            event_refs: list[WorkspaceReferenceEnvelope] = list(event["artifact_refs"])
            if event["snapshot_ref"] is not None:
                event_refs.append(event["snapshot_ref"])
            for event_ref in event_refs:
                await retain(event_ref)

        adjacency: dict[str, list[tuple[str, str]]] = {}
        for link in links:
            adjacency.setdefault(link["source_id"], []).append((link["target_id"], link["id"]))
            adjacency.setdefault(link["target_id"], []).append((link["source_id"], link["id"]))
            if link["target_id"] in owned_record_ids and link["source_id"] not in owned_record_ids:
                diagnostics.append(
                    retention_diagnostic(
                        "workspace.retention.incoming_reference",
                        "A record in the cleanup scope has an incoming link from outside the scope.",
                        entity=link["id"],
                        detail={"source_id": link["source_id"], "target_id": link["target_id"]},
                    )
                )
        closure_queue = sorted(record_ids)
        closure_seen = set(record_ids)
        while closure_queue:
            current = closure_queue.pop(0)
            for neighbor, _ in sorted(adjacency.get(current, [])):
                if neighbor in closure_seen:
                    continue
                closure_seen.add(neighbor)
                neighbor_ref = all_records.get(neighbor)
                if neighbor_ref is None:
                    diagnostics.append(
                        retention_diagnostic(
                            "workspace.retention.ref_missing",
                            "A retained Workspace link reaches a missing record.",
                            entity=neighbor,
                        )
                    )
                    continue
                _, diagnostic = await self._verified_record_envelope(neighbor_ref)
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                    continue
                record_ids.add(neighbor)
                canonical_refs.append(neighbor_ref)
                closure_queue.append(neighbor)
        return _ResolvedRetentionRoots(
            canonical_records=canonical_records,
            canonical_refs=canonical_refs,
            record_ids=record_ids,
            file_paths=file_paths,
            content_paths=content_paths,
            event_ids=event_ids,
            diagnostics=diagnostics,
        )

    def _retention_provider_selection(
        self,
        selected: Mapping[str, Sequence[str]],
        *,
        selected_record_ids: set[str],
        vector_rows: Sequence[sqlite3.Row],
    ) -> tuple[list[str], Any | None, WorkspaceRetentionDiagnostic | None]:
        if retention_selection_nonempty(selected):
            read_only_components = read_only_retention_components(
                {
                    "backend": bool(self.read_only),
                    "db_store": bool(getattr(self.db_store_provider, "read_only", False)),
                    "vector_store": bool(getattr(self.vector_store_provider, "read_only", False)),
                }
            )
            if read_only_components:
                return [], None, retention_diagnostic(
                    "workspace.retention.provider_capability_missing",
                    "Workspace retention cannot apply a non-empty plan through read-only components.",
                    entity=type(self).__name__,
                    detail={"read_only_components": read_only_components},
                )

        vector_provider = self.vector_store_provider
        if vector_provider is None or not selected_record_ids:
            return [], vector_provider, None
        if not callable(getattr(vector_provider, "delete_records", None)):
            return [], vector_provider, retention_diagnostic(
                "workspace.retention.provider_capability_missing",
                "Configured vector provider cannot delete derived record entries.",
                entity=type(vector_provider).__name__,
                detail={"missing_method": "delete_records"},
            )
        if isinstance(vector_provider, SQLiteVectorStoreProvider):
            vector_ids = sorted(
                str(row["record_id"])
                for row in vector_rows
                if str(row["record_id"]) in selected_record_ids
            )
        else:
            vector_ids = sorted(selected_record_ids)
        return vector_ids, vector_provider, None

    def _selected_retention_path_sizes(
        self,
        selected: Mapping[str, Sequence[str]],
    ) -> tuple[list[int], WorkspaceRetentionDiagnostic | None]:
        sizes: list[int] = []
        for area, key in (("files", "file_paths"), ("scratch", "scratch_paths")):
            area_root = self.root / area
            for relative_path in selected[key]:
                target, diagnostic = self._safe_resolve_path(
                    area_root / relative_path,
                    entity=relative_path,
                    operation="selected-path resolution",
                )
                if diagnostic is not None or target is None:
                    return [], diagnostic
                target_stat, diagnostic = self._safe_stat_path(
                    target,
                    entity=str(target),
                    operation="selected-path stat",
                )
                if diagnostic is not None or target_stat is None:
                    return [], diagnostic
                sizes.append(int(target_stat.st_size))
        return sizes, None

    async def _inspect_retention(
        self,
        scope: dict[str, Any],
        *,
        lifecycle: WorkspaceRetentionLifecycle,
        retained_refs: Sequence[WorkspaceRetainedReference] = (),
        inline_result: Any = None,
        policy: WorkspaceRetentionPolicy | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> WorkspaceRetentionPreview:
        normalized_scope = {str(key): value for key, value in dict(scope or {}).items() if value is not None}
        if not normalized_scope:
            raise ValueError("Workspace inspect_retention requires at least one scope value.")

        requested_policy = resolve_retention_policy(policy, supports_cold=True)
        supports_cold = bool(self._features().get("supports_cold_retention", False))
        try:
            resolved_policy = resolve_retention_policy(requested_policy, supports_cold=supports_cold)
        except ValueError as error:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=requested_policy,
                retained_refs=list(retained_refs),
                inline_result=inline_result,
                diagnostics=[
                    self._retention_diagnostic(
                        "workspace.retention.policy_unsupported",
                        str(error),
                        entity="policy",
                    )
                ],
            )
        if not self._supports_advisory_lock():
            diagnostic = self._retention_diagnostic(
                "workspace.retention.advisory_lock_unsupported",
                "Workspace retention requires a native OS advisory lock mechanism.",
                entity=str(normalized_scope.get("execution_id") or "workspace"),
            )
            diagnostic["retryable"] = False
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=list(retained_refs),
                inline_result=inline_result,
                diagnostics=[diagnostic],
            )
        advisory_lock_error = _NativeAdvisoryLock.preflight(
            self.root / ".workspace.mutation.lock"
        )
        if advisory_lock_error is not None:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=list(retained_refs),
                inline_result=inline_result,
                diagnostics=[
                    self._advisory_lock_diagnostic(
                        advisory_lock_error,
                        entity=str(
                            normalized_scope.get("execution_id") or "workspace"
                        ),
                    )
                ],
            )
        if not self._supports_descriptor_relative_delete():
            diagnostic = self._retention_diagnostic(
                "workspace.retention.derived_delete_unsupported",
                "Workspace retention requires descriptor-relative no-follow deletion support.",
                entity=str(normalized_scope.get("execution_id") or "workspace"),
            )
            diagnostic["retryable"] = False
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=list(retained_refs),
                inline_result=inline_result,
                diagnostics=[diagnostic],
            )

        representation_by_category = {
            rule["category"]: rule["representation"] for rule in resolved_policy.get("rules", [])
        }
        execution_id = str(lifecycle.get("execution_id") or "")
        checkpoint_manifest_key = f"checkpoint.latest.{ execution_id }"
        lease_manifest_key = self._lease_manifest_key(execution_id)

        snapshot = self._read_retention_snapshot(
            normalized_scope,
            execution_id=execution_id,
            checkpoint_manifest_key=checkpoint_manifest_key,
            lease_manifest_key=lease_manifest_key,
            connection=connection,
        )
        diagnostics: list[WorkspaceRetentionDiagnostic] = []
        try:
            decoded = self._decode_retention_snapshot(
                snapshot,
                checkpoint_manifest_key=checkpoint_manifest_key,
                lease_manifest_key=lease_manifest_key,
            )
        except (TypeError, ValueError) as error:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=list(retained_refs),
                inline_result=inline_result,
                diagnostics=[
                    self._retention_diagnostic(
                        "workspace.retention.ref_readback_failed",
                        str(error),
                        entity="persisted_json",
                    )
                ],
            )

        all_records = decoded.all_records
        runtime_events = decoded.runtime_events
        anchors = decoded.anchors
        links = decoded.links
        scratch_leases = decoded.scratch_leases
        checkpoint_facts = decoded.checkpoint_facts
        manifest_values = decoded.manifest_values
        manifest_raw = decoded.manifest_raw
        scoped_record_ids = {str(row["id"]) for row in snapshot.scoped_rows}
        checkpoint_record_ids = {
            str(row["record_id"]) for row in snapshot.checkpoint_rows
        }
        owned_record_ids = scoped_record_ids | checkpoint_record_ids
        persisted_lease = manifest_values.get(lease_manifest_key)
        checkpoint_version = None
        for row in reversed(checkpoint_facts):
            if str(row.get("run_id") or "") != execution_id:
                continue
            checkpoint_version = self._checkpoint_state_version(row["state"])
            if checkpoint_version is not None:
                break
        diagnostics.extend(
            retention_lifecycle_diagnostics(
                scope=normalized_scope,
                lifecycle=lifecycle,
                execution_id=execution_id,
                lease_manifest_key=lease_manifest_key,
                persisted_lease=(
                    cast(Mapping[str, Any], persisted_lease)
                    if isinstance(persisted_lease, dict)
                    else None
                ),
                checkpoint_version=checkpoint_version,
                runtime_events=runtime_events,
                now=time.time(),
            )
        )

        scoped_scratch_leases: list[WorkspaceScratchLease] = []
        for lease in scratch_leases:
            lease_scope = lease.get("scope")
            if not isinstance(lease_scope, dict) or not all(
                lease_scope.get(key) == value for key, value in normalized_scope.items()
            ):
                continue
            scoped_scratch_leases.append(lease)
            if lease.get("closed_at") is None:
                diagnostics.append(
                    self._retention_diagnostic(
                        "workspace.retention.lease_active",
                        "A scratch lease remains active for the cleanup scope.",
                        entity=str(lease.get("lease_id") or ""),
                    )
                )

        checkpoint_manifest = manifest_values.get(checkpoint_manifest_key)
        terminal_manifest = all_records.get(
            self._terminal_manifest_record_id(execution_id)
        )
        roots = await self._resolve_retention_roots(
            owned_record_ids=owned_record_ids,
            all_records=all_records,
            retained_refs=retained_refs,
            checkpoint_manifest=(
                cast(WorkspaceRetainedReference, checkpoint_manifest)
                if isinstance(checkpoint_manifest, dict)
                else None
            ),
            retain_checkpoint_manifest=representation_by_category["checkpoints"] == "hot",
            anchors=anchors,
            runtime_events=runtime_events,
            retain_all_runtime_events=representation_by_category["runtime_events"] == "hot",
            links=links,
        )
        diagnostics.extend(roots.diagnostics)
        canonical_records = roots.canonical_records
        canonical_retained_refs = roots.canonical_refs
        retained_record_ids = roots.record_ids
        if (
            terminal_manifest is not None
            and terminal_manifest.get("collection") == "artifacts"
            and terminal_manifest.get("kind") == "workspace_terminal_manifest"
        ):
            retained_record_ids.add(str(terminal_manifest["id"]))
        retained_file_paths = roots.file_paths
        retained_content_paths = roots.content_paths
        retained_event_ids = roots.event_ids

        selected_file_paths: list[str] = []
        selected_scratch_paths: list[str] = []
        if representation_by_category["files"] != "hot":
            selected_file_paths, diagnostic = self._scoped_area_files(normalized_scope, "files")
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        else:
            _, _, diagnostic = self._retention_area_root("files")
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        if representation_by_category["scratch"] != "hot":
            selected_scratch_paths, diagnostic = self._scoped_area_files(normalized_scope, "scratch")
            if diagnostic is not None:
                diagnostics.append(diagnostic)

        if diagnostics:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=self._deduplicate_retained_refs(
                    canonical_retained_refs or list(retained_refs)
                ),
                inline_result=inline_result,
                diagnostics=diagnostics,
            )

        scope_index_facts = [dict(row) for row in snapshot.scope_index_rows]
        fts_facts = [dict(row) for row in snapshot.fts_rows]
        selection_result = build_retention_selection(
            owned_record_ids=owned_record_ids,
            records_by_id=canonical_records,
            retained_record_ids=retained_record_ids,
            retained_content_paths=retained_content_paths,
            checkpoint_record_ids=checkpoint_record_ids,
            checkpoint_rows=checkpoint_facts,
            runtime_events=runtime_events,
            retained_event_ids=retained_event_ids,
            links=links,
            anchors=anchors,
            scratch_leases=cast(Sequence[Mapping[str, Any]], scoped_scratch_leases),
            selected_scratch_paths=selected_scratch_paths,
            selected_file_paths=selected_file_paths,
            retained_file_paths=retained_file_paths,
            scope_index_rows=scope_index_facts,
            fts_rows=fts_facts,
            manifest_keys=set(manifest_values),
            checkpoint_manifest_key=checkpoint_manifest_key,
            lease_manifest_key=lease_manifest_key,
            representation_by_category=representation_by_category,
        )
        selected = selection_result.selected
        selected_record_sizes = selection_result.record_content_sizes
        selected_checkpoint_rows = selection_result.checkpoint_rows
        selected_record_id_set = set(selected["record_ids"])
        if representation_by_category["checkpoints"] != "hot":
            for manifest_key, manifest_value in manifest_values.items():
                if not manifest_key.startswith("checkpoint.latest."):
                    continue
                if not isinstance(manifest_value, Mapping):
                    selected["manifest_keys"].append(manifest_key)
                    continue
                manifest_record_id = str(
                    manifest_value.get("record_id")
                    or manifest_value.get("id")
                    or ""
                )
                if manifest_record_id not in retained_record_ids:
                    selected["manifest_keys"].append(manifest_key)

        vector_ids, vector_provider, capability_diagnostic = self._retention_provider_selection(
            selected,
            selected_record_ids=selected_record_id_set,
            vector_rows=snapshot.vector_rows,
        )
        if capability_diagnostic is not None:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
                inline_result=inline_result,
                diagnostics=[capability_diagnostic],
            )
        selected["vector_record_ids"] = vector_ids

        for key in selected:
            selected[key] = sorted(set(selected[key]))

        selected_path_sizes, path_diagnostic = self._selected_retention_path_sizes(selected)
        if path_diagnostic is not None:
            return self._retention_preview(
                status="deferred",
                scope=normalized_scope,
                lifecycle=lifecycle,
                policy=resolved_policy,
                retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
                inline_result=inline_result,
                diagnostics=[path_diagnostic],
            )

        logical_bytes = calculate_retention_logical_bytes(
            selected=selected,
            record_content_sizes=selected_record_sizes,
            record_rows=[dict(row) for row in snapshot.all_record_rows],
            runtime_events=runtime_events,
            links=links,
            anchors=anchors,
            checkpoint_rows=selected_checkpoint_rows,
            scope_index_rows=scope_index_facts,
            scratch_rows=[dict(row) for row in snapshot.scratch_rows],
            manifest_raw=manifest_raw,
            fts_rows=fts_facts,
            vector_rows=[dict(row) for row in snapshot.vector_rows],
            sqlite_vector_store=isinstance(vector_provider, SQLiteVectorStoreProvider),
            selected_path_sizes=selected_path_sizes,
        )

        return self._retention_preview(
            status="ready",
            scope=normalized_scope,
            lifecycle=lifecycle,
            policy=resolved_policy,
            retained_refs=self._deduplicate_retained_refs(canonical_retained_refs),
            inline_result=inline_result,
            selected=selected,
            logical_bytes=logical_bytes,
        )

    async def inspect_retention(
        self,
        scope: dict[str, Any],
        *,
        lifecycle: WorkspaceRetentionLifecycle,
        retained_refs: Sequence[WorkspaceRetainedReference] = (),
        inline_result: Any = None,
        policy: WorkspaceRetentionPolicy | None = None,
    ) -> WorkspaceRetentionPreview:
        return await self._inspect_retention(
            scope,
            lifecycle=lifecycle,
            retained_refs=retained_refs,
            inline_result=inline_result,
            policy=policy,
        )

    @staticmethod
    def _retention_result(
        *,
        status: str,
        plan_fingerprint: str,
        manifest_ref: WorkspaceRecordRef | None,
        retained_refs: Sequence[WorkspaceRetainedReference],
        accounting: Mapping[str, Any],
        diagnostics: Sequence[WorkspaceRetentionDiagnostic] = (),
    ) -> WorkspaceRetentionResult:
        return cast(
            WorkspaceRetentionResult,
            {
                "status": status,
                "plan_fingerprint": plan_fingerprint,
                "manifest_ref": manifest_ref,
                "retained_refs": list(retained_refs),
                "accounting": dict(accounting),
                "diagnostics": list(diagnostics),
            },
        )

    @staticmethod
    def _zero_retention_accounting(
        accounting: Mapping[str, Any],
    ) -> dict[str, Any]:
        entities = cast(Mapping[str, Any], accounting.get("entities") or {})
        return {
            "entities": {str(key): 0 for key in entities},
            "logical_bytes_deleted": 0,
            "physical_bytes_reclaimed": 0,
            "physical_bytes_pending": 0,
        }

    def _terminal_manifest_ref_from_row(
        self,
        row: sqlite3.Row,
        *,
        execution_id: str,
    ) -> WorkspaceRecordRef:
        ref = self._strict_retention_record_row(row)
        expected_id = self._terminal_manifest_record_id(execution_id)
        if (
            ref["id"] != expected_id
            or ref["collection"] != "artifacts"
            or ref.get("kind") != "workspace_terminal_manifest"
            or ref.get("path") is not None
            or ref.get("sha256") is not None
            or ref.get("size") != 0
            or ref.get("source") != {"type": "workspace", "name": "terminal_retention"}
        ):
            raise ValueError("Workspace terminal manifest record identity is invalid.")
        meta = ref.get("meta")
        required_meta = {
            "schema_version",
            "plan_fingerprint",
            "state",
            "lifecycle",
            "retained_refs",
            "inline_result",
            "accounting",
            "derived_cleanup",
        }
        if not isinstance(meta, dict) or set(meta) != required_meta:
            raise ValueError("Workspace terminal manifest ledger has invalid top-level fields.")
        if meta.get("schema_version") != "agently.workspace.terminal_manifest.v1":
            raise ValueError("Workspace terminal manifest schema version is invalid.")
        fingerprint = meta.get("plan_fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("Workspace terminal manifest fingerprint is invalid.")
        if meta.get("state") not in {"db_committed", "derived_pending", "applied"}:
            raise ValueError("Workspace terminal manifest state is invalid.")
        if not isinstance(meta.get("lifecycle"), dict):
            raise ValueError("Workspace terminal manifest lifecycle is invalid.")
        retained_refs = meta.get("retained_refs")
        if not isinstance(retained_refs, list):
            raise ValueError("Workspace terminal manifest retained refs are invalid.")
        for index, retained_ref in enumerate(retained_refs):
            validate_retained_reference_shape(
                retained_ref,
                field=f"terminal_manifest.retained_refs[{index}]",
            )
        accounting = meta.get("accounting")
        if not isinstance(accounting, dict):
            raise ValueError("Workspace terminal manifest accounting is invalid.")
        derived = meta.get("derived_cleanup")
        if not isinstance(derived, dict) or set(derived) != {
            "pending",
            "attempts",
            "last_error",
        }:
            raise ValueError("Workspace terminal manifest derived cleanup ledger is invalid.")
        pending = derived.get("pending")
        pending_keys = {
            "vector_record_ids",
            "content_paths",
            "file_paths",
            "scratch_paths",
        }
        if not isinstance(pending, dict) or set(pending) != pending_keys:
            raise ValueError("Workspace terminal manifest pending cleanup is invalid.")
        for key in sorted(pending_keys):
            values = pending[key]
            if (
                not isinstance(values, list)
                or not all(isinstance(value, str) and value for value in values)
                or values != sorted(set(values))
            ):
                raise ValueError(
                    f"Workspace terminal manifest pending cleanup '{key}' is invalid."
                )
        attempts = derived.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValueError("Workspace terminal manifest cleanup attempts are invalid.")
        last_error = derived.get("last_error")
        if last_error is not None and (
            not isinstance(last_error, dict)
            or not isinstance(last_error.get("code"), str)
            or not isinstance(last_error.get("message"), str)
            or not isinstance(last_error.get("retryable"), bool)
            or not isinstance(last_error.get("entity"), str)
            or ("detail" in last_error and not isinstance(last_error["detail"], dict))
        ):
            raise ValueError("Workspace terminal manifest last error is invalid.")
        return ref

    def _terminal_manifest_from_conn(
        self,
        conn: sqlite3.Connection,
        *,
        execution_id: str,
    ) -> WorkspaceRecordRef | None:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ?",
            (self._terminal_manifest_record_id(execution_id),),
        ).fetchone()
        if row is None:
            return None
        return self._terminal_manifest_ref_from_row(
            row,
            execution_id=execution_id,
        )

    def _write_terminal_manifest_on_conn(
        self,
        conn: sqlite3.Connection,
        *,
        execution_id: str,
        scope: Mapping[str, Any],
        meta: Mapping[str, Any],
        created_at: str | None = None,
    ) -> WorkspaceRecordRef:
        record_id = self._terminal_manifest_record_id(execution_id)
        ref: WorkspaceRecordRef = {
            "id": record_id,
            "collection": "artifacts",
            "kind": "workspace_terminal_manifest",
            "path": None,
            "sha256": None,
            "size": 0,
            "summary": f"Terminal retention manifest for {execution_id}"[:240],
            "scope": dict(scope),
            "source": {"type": "workspace", "name": "terminal_retention"},
            "created_at": created_at or utc_now(),
            "meta": dict(meta),
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO records (
                id, collection, kind, path, sha256, size, summary,
                scope_json, source_json, meta_json, created_at, is_checkpoint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                ref["id"],
                ref["collection"],
                ref["kind"],
                ref["path"],
                ref["sha256"],
                ref["size"],
                ref["summary"],
                json_dumps(ref["scope"]),
                json_dumps(ref["source"]),
                json_dumps(ref["meta"]),
                ref["created_at"],
            ),
        )
        self._replace_scope_index_on_conn(conn, record_id, ref["scope"])
        conn.execute("DELETE FROM records_fts WHERE record_id = ?", (record_id,))
        return ref

    def _update_terminal_manifest_meta(
        self,
        manifest_ref: WorkspaceRecordRef,
        meta: Mapping[str, Any],
    ) -> WorkspaceRecordRef:
        execution_id = str(meta.get("lifecycle", {}).get("execution_id") or "")
        expected_meta = cast(Mapping[str, Any], manifest_ref.get("meta") or {})
        expected_fingerprint = str(expected_meta.get("plan_fingerprint") or "")
        if (
            not execution_id
            or str(meta.get("plan_fingerprint") or "") != expected_fingerprint
        ):
            raise ValueError("Workspace terminal manifest update contract is invalid.")
        expected_raw = json_dumps(expected_meta)
        for _ in range(4):
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM records WHERE id = ?",
                        (manifest_ref["id"],),
                    ).fetchone()
                    if row is None:
                        raise _TerminalManifestLedgerError(
                            "Workspace terminal manifest disappeared during derived cleanup."
                        )
                    try:
                        current = self._terminal_manifest_ref_from_row(
                            row,
                            execution_id=execution_id,
                        )
                    except (TypeError, ValueError) as error:
                        raise _TerminalManifestLedgerError(str(error)) from error
                    current_meta = cast(Mapping[str, Any], current["meta"])
                    current_fingerprint = str(current_meta["plan_fingerprint"])
                    if current_fingerprint != expected_fingerprint:
                        raise _TerminalManifestPlanConflict(current)
                    current_raw = str(row["meta_json"])
                    next_meta = self._merge_terminal_manifest_meta(
                        current_meta,
                        meta,
                    )
                    if current_raw == expected_raw:
                        next_meta["accounting"] = dict(
                            cast(Mapping[str, Any], meta["accounting"])
                        )
                    updated_raw = json_dumps(next_meta)
                    compare_raw = (
                        expected_raw if current_raw == expected_raw else current_raw
                    )
                    cursor = conn.execute(
                        "UPDATE records SET meta_json = ? WHERE id = ? AND meta_json = ?",
                        (updated_raw, manifest_ref["id"], compare_raw),
                    )
                    if cursor.rowcount != 1:
                        conn.rollback()
                        continue
                    conn.commit()
                    updated = cast(WorkspaceRecordRef, dict(current))
                    updated["meta"] = next_meta
                    return updated
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
        raise sqlite3.OperationalError(
            "Workspace terminal manifest compare-and-swap retry limit was exceeded."
        )

    @staticmethod
    def _merge_terminal_manifest_meta(
        current: Mapping[str, Any],
        proposed: Mapping[str, Any],
    ) -> dict[str, Any]:
        if str(current.get("plan_fingerprint") or "") != str(
            proposed.get("plan_fingerprint") or ""
        ):
            raise ValueError("Cannot merge different Workspace retention plans.")
        current_derived = cast(Mapping[str, Any], current["derived_cleanup"])
        proposed_derived = cast(Mapping[str, Any], proposed["derived_cleanup"])
        current_pending = cast(
            Mapping[str, Sequence[str]], current_derived["pending"]
        )
        proposed_pending = cast(
            Mapping[str, Sequence[str]], proposed_derived["pending"]
        )
        merged_pending = {
            key: sorted(set(current_pending[key]) & set(proposed_pending[key]))
            for key in (
                "vector_record_ids",
                "content_paths",
                "file_paths",
                "scratch_paths",
            )
        }
        current_attempts = int(current_derived["attempts"])
        proposed_attempts = int(proposed_derived["attempts"])
        current_state = str(current["state"])
        proposed_state = str(proposed["state"])
        if "applied" in {current_state, proposed_state}:
            state = "applied"
            merged_pending = {key: [] for key in merged_pending}
            last_error = None
        else:
            state = (
                "derived_pending"
                if "derived_pending" in {current_state, proposed_state}
                else "db_committed"
            )
            preferred = (
                proposed_derived
                if proposed_attempts >= current_attempts
                else current_derived
            )
            last_error = preferred.get("last_error")
        merged = dict(current)
        merged["state"] = state
        merged["derived_cleanup"] = {
            "pending": merged_pending,
            "attempts": max(current_attempts, proposed_attempts),
            "last_error": last_error,
        }
        merged["accounting"] = dict(
            cast(Mapping[str, Any], current["accounting"])
        )
        return merged

    @staticmethod
    def _delete_ids_on_conn(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        values: Sequence[str],
    ) -> None:
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
            list(values),
        )

    def _delete_retention_logical_selection_on_conn(
        self,
        conn: sqlite3.Connection,
        selected: Mapping[str, Sequence[str]],
    ) -> None:
        self._delete_ids_on_conn(conn, "links", "id", selected["link_ids"])
        self._delete_ids_on_conn(
            conn,
            "checkpoints",
            "record_id",
            selected["checkpoint_ids"],
        )
        self._delete_ids_on_conn(
            conn,
            "records_fts",
            "record_id",
            selected["fts_record_ids"],
        )
        for identity in selected["record_scope_index_ids"]:
            record_id, separator, scope_key = identity.partition(":")
            if not separator or not record_id or not scope_key:
                raise ValueError(
                    f"Invalid Workspace retention scope-index identity: {identity}"
                )
            conn.execute(
                "DELETE FROM record_scope_index WHERE record_id = ? AND scope_key = ?",
                (record_id, scope_key),
            )
        self._delete_ids_on_conn(conn, "records", "id", selected["record_ids"])
        self._delete_ids_on_conn(
            conn,
            "runtime_events",
            "id",
            selected["runtime_event_ids"],
        )
        self._delete_ids_on_conn(
            conn,
            "retention_anchors",
            "id",
            selected["retention_anchor_ids"],
        )
        self._delete_ids_on_conn(
            conn,
            "manifests",
            "key",
            selected["manifest_keys"],
        )
        self._delete_ids_on_conn(
            conn,
            "scratch_leases",
            "lease_id",
            selected["scratch_lease_ids"],
        )

    def _delete_retention_lineage_file(self, area: str, relative_path: str) -> bool:
        relative = Path(relative_path)
        if (
            area not in {"files", "scratch"}
            or relative.is_absolute()
            or not relative.parts
            or relative.parts[0] != "lineage"
            or ".." in relative.parts
        ):
            raise WorkspacePolicyError(
                f"Workspace retention selected an invalid {area} path: {relative_path}"
            )
        area_root, _, diagnostic = self._retention_area_root(area)
        if diagnostic is not None:
            raise WorkspacePolicyError(
                diagnostic.get("message", "Workspace retention area validation failed.")
            )
        if area_root is None:
            return False
        return delete_owned_file_descriptor_relative(
            area_root,
            relative,
            protected_parent_depth=1,
        )

    async def _resume_retention_derived_cleanup(
        self,
        manifest_ref: WorkspaceRecordRef,
    ) -> WorkspaceRetentionResult:
        try:
            return await self._resume_retention_derived_cleanup_unhandled(
                manifest_ref
            )
        except _TerminalManifestPlanConflict as error:
            current_meta = cast(Mapping[str, Any], error.current["meta"])
            diagnostic = retention_diagnostic(
                "workspace.retention.plan_conflict",
                "A newer Workspace retention plan replaced this cleanup ledger.",
                entity=str(error.current["id"]),
                detail={
                    "existing_plan_fingerprint": str(
                        current_meta["plan_fingerprint"]
                    ),
                    "requested_plan_fingerprint": str(
                        manifest_ref["meta"]["plan_fingerprint"]
                    ),
                },
            )
            return self._retention_result(
                status="deferred",
                plan_fingerprint=str(manifest_ref["meta"]["plan_fingerprint"]),
                manifest_ref=error.current,
                retained_refs=cast(
                    Sequence[WorkspaceRetainedReference],
                    current_meta["retained_refs"],
                ),
                accounting=self._zero_retention_accounting(
                    cast(Mapping[str, Any], manifest_ref["meta"]["accounting"])
                ),
                diagnostics=[diagnostic],
            )
        except _TerminalManifestLedgerError as error:
            diagnostic = retention_diagnostic(
                "workspace.retention.ledger_invalid",
                f"Workspace terminal retention ledger is invalid: {error}",
                entity=str(manifest_ref["id"]),
                detail={"error_type": type(error).__name__},
            )
            diagnostic["retryable"] = False
            return self._retention_result(
                status="deferred",
                plan_fingerprint=str(manifest_ref["meta"]["plan_fingerprint"]),
                manifest_ref=manifest_ref,
                retained_refs=cast(
                    Sequence[WorkspaceRetainedReference],
                    manifest_ref["meta"]["retained_refs"],
                ),
                accounting=cast(
                    Mapping[str, Any], manifest_ref["meta"]["accounting"]
                ),
                diagnostics=[diagnostic],
            )
        except (sqlite3.Error, OSError) as error:
            diagnostic = retention_diagnostic(
                "workspace.retention.ledger_update_failed",
                f"Workspace terminal retention ledger update failed: {error}",
                entity=str(manifest_ref["id"]),
                detail={"error_type": type(error).__name__},
            )
            return self._retention_result(
                status="deferred",
                plan_fingerprint=str(manifest_ref["meta"]["plan_fingerprint"]),
                manifest_ref=manifest_ref,
                retained_refs=cast(
                    Sequence[WorkspaceRetainedReference],
                    manifest_ref["meta"]["retained_refs"],
                ),
                accounting=cast(
                    Mapping[str, Any], manifest_ref["meta"]["accounting"]
                ),
                diagnostics=[diagnostic],
            )

    async def _resume_retention_derived_cleanup_unhandled(
        self,
        manifest_ref: WorkspaceRecordRef,
    ) -> WorkspaceRetentionResult:
        meta = cast(dict[str, Any], dict(manifest_ref["meta"]))
        derived = cast(dict[str, Any], dict(meta["derived_cleanup"]))
        pending = {
            key: list(values)
            for key, values in cast(Mapping[str, Sequence[str]], derived["pending"]).items()
        }
        derived["pending"] = pending
        derived["attempts"] = int(derived["attempts"]) + 1
        derived["last_error"] = None
        meta["state"] = "derived_pending"
        meta["derived_cleanup"] = derived
        manifest_ref = self._update_terminal_manifest_meta(manifest_ref, meta)
        if manifest_ref["meta"]["state"] == "applied":
            return self._retention_result(
                status="noop",
                plan_fingerprint=str(meta["plan_fingerprint"]),
                manifest_ref=manifest_ref,
                retained_refs=cast(
                    Sequence[WorkspaceRetainedReference],
                    manifest_ref["meta"]["retained_refs"],
                ),
                accounting=self._zero_retention_accounting(
                    cast(Mapping[str, Any], manifest_ref["meta"]["accounting"])
                ),
            )

        try:
            vector_ids = list(pending["vector_record_ids"])
            if vector_ids:
                delete_vectors = getattr(self.vector_store_provider, "delete_records", None)
                if not callable(delete_vectors):
                    raise _DerivedCleanupOperationalError(
                        WorkspaceConfigurationError(
                            "Configured vector provider cannot delete derived record entries."
                        )
                    )
                try:
                    await cast(
                        Callable[[Sequence[str]], Awaitable[None]], delete_vectors
                    )(vector_ids)
                except (
                    OSError,
                    sqlite3.Error,
                    WorkspaceConfigurationError,
                    WorkspacePolicyError,
                ) as error:
                    raise _DerivedCleanupOperationalError(error) from error
                pending["vector_record_ids"] = []
                manifest_ref = self._update_terminal_manifest_meta(manifest_ref, meta)

            for key in ("content_paths", "file_paths", "scratch_paths"):
                for relative_path in list(pending[key]):
                    try:
                        if key == "content_paths":
                            await self.content.delete_content(relative_path)
                        elif key == "file_paths":
                            self._delete_retention_lineage_file("files", relative_path)
                        else:
                            self._delete_retention_lineage_file("scratch", relative_path)
                    except (
                        OSError,
                        sqlite3.Error,
                        WorkspaceConfigurationError,
                        WorkspacePolicyError,
                    ) as error:
                        raise _DerivedCleanupOperationalError(error) from error
                    pending[key].remove(relative_path)
                    manifest_ref = self._update_terminal_manifest_meta(manifest_ref, meta)
        except (
            _TerminalManifestPlanConflict,
            _TerminalManifestLedgerError,
            sqlite3.Error,
        ):
            raise
        except _DerivedCleanupOperationalError as failure:
            error = failure.error
            diagnostic = retention_diagnostic(
                "workspace.retention.derived_cleanup_failed",
                f"Workspace derived retention cleanup failed: {error}",
                entity=str(manifest_ref["id"]),
                detail={"error_type": type(error).__name__},
            )
            if isinstance(error, (WorkspaceConfigurationError, WorkspacePolicyError)):
                diagnostic["retryable"] = False
            meta["state"] = "derived_pending"
            derived["last_error"] = diagnostic
            manifest_ref = self._update_terminal_manifest_meta(manifest_ref, meta)
            return self._retention_result(
                status="deferred",
                plan_fingerprint=str(meta["plan_fingerprint"]),
                manifest_ref=manifest_ref,
                retained_refs=cast(
                    Sequence[WorkspaceRetainedReference],
                    meta["retained_refs"],
                ),
                accounting=cast(Mapping[str, Any], meta["accounting"]),
                diagnostics=[diagnostic],
            )

        meta["state"] = "applied"
        derived["last_error"] = None
        manifest_ref = self._update_terminal_manifest_meta(manifest_ref, meta)
        return self._retention_result(
            status="applied",
            plan_fingerprint=str(meta["plan_fingerprint"]),
            manifest_ref=manifest_ref,
            retained_refs=cast(
                Sequence[WorkspaceRetainedReference],
                meta["retained_refs"],
            ),
            accounting=cast(Mapping[str, Any], meta["accounting"]),
        )

    def _sqlite_allocated_bytes(self) -> int:
        total = 0
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            try:
                allocated = path.stat().st_blocks * 512
            except FileNotFoundError:
                continue
            total += int(allocated)
        return total

    def _sqlite_pending_bytes_sync(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, page_size * freelist)

    def _checkpoint_sqlite_wal_sync(self, mode: str) -> None:
        with sqlite3.connect(self.db_path, timeout=0.1) as conn:
            row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            if row is not None and int(row[0]) != 0:
                raise sqlite3.OperationalError(
                    f"SQLite WAL {mode} checkpoint is busy."
                )

    def _run_sqlite_physical_maintenance_sync(self) -> None:
        attempted_vacuum = False
        with sqlite3.connect(self.db_path, timeout=0.1) as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise sqlite3.OperationalError(
                    "SQLite WAL PASSIVE checkpoint is busy."
                )
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
            free_bytes = page_size * freelist
            db_allocated = (
                int(self.db_path.stat().st_blocks * 512)
                if self.db_path.exists()
                else 0
            )
            if auto_vacuum == 2 and freelist > 0:
                pages = min(freelist, self._retention_incremental_vacuum_pages)
                conn.execute(f"PRAGMA incremental_vacuum({pages})")
                attempted_vacuum = True
            elif free_bytes > 0 and (
                free_bytes >= self._retention_full_vacuum_min_bytes
                or free_bytes >= db_allocated * self._retention_full_vacuum_ratio
            ):
                conn.execute("VACUUM")
                attempted_vacuum = True
        if attempted_vacuum:
            self._checkpoint_sqlite_wal_sync("TRUNCATE")

    async def _await_completion_before_cancellation(
        self,
        awaitable: Awaitable[_R],
    ) -> _R:
        worker = asyncio.ensure_future(awaitable)
        first_cancellation: asyncio.CancelledError | None = None
        worker_error: BaseException | None = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as error:
                if first_cancellation is None:
                    first_cancellation = error
            except BaseException as error:
                worker_error = error
                break
        if worker.done() and worker_error is None:
            try:
                worker.result()
            except BaseException as error:
                worker_error = error
        if first_cancellation is not None:
            raise first_cancellation
        if worker_error is not None:
            raise worker_error
        return worker.result()

    async def _run_sqlite_physical_maintenance(self) -> None:
        await self._await_completion_before_cancellation(
            asyncio.to_thread(self._run_sqlite_physical_maintenance_sync)
        )

    async def _finalize_sqlite_physical_reclamation(
        self,
        result: WorkspaceRetentionResult,
        *,
        allocated_before: int,
    ) -> WorkspaceRetentionResult:
        manifest_ref = result["manifest_ref"]
        if manifest_ref is None:
            return result
        maintenance_error: Exception | None = None
        try:
            await self._run_sqlite_physical_maintenance()
        except (sqlite3.Error, OSError) as error:
            maintenance_error = error

        async def measured_accounting() -> dict[str, Any]:
            pending_total = await asyncio.to_thread(self._sqlite_pending_bytes_sync)
            allocated_after = self._sqlite_allocated_bytes()
            reclaimed = max(0, allocated_before - allocated_after)
            accounting = dict(result["accounting"])
            accounting["physical_bytes_reclaimed"] = reclaimed
            accounting["physical_bytes_pending"] = max(0, pending_total - reclaimed)
            return accounting

        try:
            accounting = await measured_accounting()
        except (sqlite3.Error, OSError) as error:
            diagnostic = retention_diagnostic(
                "workspace.retention.physical_measurement_failed",
                f"Workspace logical cleanup applied but physical allocation measurement failed: {error}",
                entity=str(manifest_ref["id"]),
                detail={"error_type": type(error).__name__},
            )
            diagnostic["retryable"] = True
            return self._retention_result(
                status="deferred",
                plan_fingerprint=result["plan_fingerprint"],
                manifest_ref=manifest_ref,
                retained_refs=result["retained_refs"],
                accounting=result["accounting"],
                diagnostics=[diagnostic],
            )
        updated_ref = manifest_ref
        try:
            meta = cast(dict[str, Any], dict(updated_ref["meta"]))
            meta["accounting"] = accounting
            updated_ref = self._update_terminal_manifest_meta(updated_ref, meta)
        except (
            sqlite3.Error,
            OSError,
            _TerminalManifestLedgerError,
            _TerminalManifestPlanConflict,
        ) as error:
            diagnostic = retention_diagnostic(
                "workspace.retention.physical_accounting_persist_failed",
                f"Workspace physical accounting could not be persisted after logical cleanup: {error}",
                entity=str(manifest_ref["id"]),
                detail={"error_type": type(error).__name__},
            )
            return self._retention_result(
                status="deferred",
                plan_fingerprint=result["plan_fingerprint"],
                manifest_ref=updated_ref,
                retained_refs=result["retained_refs"],
                accounting=accounting,
                diagnostics=[diagnostic],
            )
        persisted_accounting = cast(
            Mapping[str, Any], updated_ref["meta"]["accounting"]
        )
        if dict(persisted_accounting) != accounting:
            diagnostic = retention_diagnostic(
                "workspace.retention.physical_accounting_conflict",
                "Workspace physical accounting changed before the measured snapshot could be persisted.",
                entity=str(updated_ref["id"]),
                detail={"measured_accounting": accounting},
            )
            return self._retention_result(
                status="deferred",
                plan_fingerprint=result["plan_fingerprint"],
                manifest_ref=updated_ref,
                retained_refs=result["retained_refs"],
                accounting=persisted_accounting,
                diagnostics=[diagnostic],
            )
        accounting = dict(persisted_accounting)
        if maintenance_error is not None:
            diagnostic = retention_diagnostic(
                "workspace.retention.physical_maintenance_failed",
                f"Workspace logical cleanup applied but SQLite physical maintenance failed: {maintenance_error}",
                entity=str(updated_ref["id"]),
                detail={"error_type": type(maintenance_error).__name__},
            )
            diagnostic["retryable"] = True
            return self._retention_result(
                status="applied",
                plan_fingerprint=result["plan_fingerprint"],
                manifest_ref=updated_ref,
                retained_refs=result["retained_refs"],
                accounting=accounting,
                diagnostics=[diagnostic],
            )
        return self._retention_result(
            status="applied",
            plan_fingerprint=result["plan_fingerprint"],
            manifest_ref=updated_ref,
            retained_refs=result["retained_refs"],
            accounting=accounting,
        )

    async def apply_retention(
        self,
        preview: WorkspaceRetentionPreview,
    ) -> WorkspaceRetentionResult:
        entity = str(preview["scope"].get("execution_id") or "workspace")
        result: WorkspaceRetentionResult | None = None
        try:
            async with self._mutation_guard():
                if not self._supports_advisory_lock():
                    diagnostic = retention_diagnostic(
                        "workspace.retention.advisory_lock_unsupported",
                        "Workspace retention requires a native OS advisory lock mechanism.",
                        entity=entity,
                    )
                    diagnostic["retryable"] = False
                    result = self._retention_result(
                        status="deferred",
                        plan_fingerprint=preview["plan_fingerprint"],
                        manifest_ref=None,
                        retained_refs=preview["retained_refs"],
                        accounting=self._zero_retention_accounting(
                            preview["accounting"]
                        ),
                        diagnostics=[diagnostic],
                    )
                else:
                    try:
                        allocated_before = self._sqlite_allocated_bytes()
                        result = await self._apply_retention_unlocked(preview)
                        if result["status"] == "applied":
                            result = await self._await_completion_before_cancellation(
                                self._finalize_sqlite_physical_reclamation(
                                    result,
                                    allocated_before=allocated_before,
                                )
                            )
                    except (sqlite3.Error, OSError) as error:
                        diagnostic = retention_diagnostic(
                            "workspace.retention.apply_failed",
                            f"Workspace retention transaction failed and was rolled back: {error}",
                            entity=entity,
                            detail={"error_type": type(error).__name__},
                        )
                        result = self._retention_result(
                            status="deferred",
                            plan_fingerprint=preview["plan_fingerprint"],
                            manifest_ref=None,
                            retained_refs=preview["retained_refs"],
                            accounting=self._zero_retention_accounting(
                                preview["accounting"]
                            ),
                            diagnostics=[diagnostic],
                        )
        except _AdvisoryLockAcquisitionError as error:
            return self._retention_result(
                status="deferred",
                plan_fingerprint=preview["plan_fingerprint"],
                manifest_ref=None,
                retained_refs=preview["retained_refs"],
                accounting=self._zero_retention_accounting(preview["accounting"]),
                diagnostics=[
                    self._advisory_lock_diagnostic(error, entity=entity)
                ],
            )
        except _AdvisoryLockReleaseError as error:
            if result is None:
                message = (
                    "Workspace retention could not start because the root mutation "
                    f"guard has an uncertain native lock release: {error}"
                )
            else:
                message = (
                    "Workspace retention completed its operation but advisory lock "
                    f"release reported a failure: {error}"
                )
            diagnostic = retention_diagnostic(
                "workspace.retention.advisory_lock_release_failed",
                message,
                entity=entity,
                detail={
                    "error_type": type(error).__name__,
                    "operation_status": result["status"] if result is not None else None,
                },
            )
            diagnostic["retryable"] = False
            return self._retention_result(
                status="deferred",
                plan_fingerprint=(
                    result["plan_fingerprint"]
                    if result is not None
                    else preview["plan_fingerprint"]
                ),
                manifest_ref=result["manifest_ref"] if result is not None else None,
                retained_refs=(
                    result["retained_refs"]
                    if result is not None
                    else preview["retained_refs"]
                ),
                accounting=(
                    result["accounting"]
                    if result is not None
                    else self._zero_retention_accounting(preview["accounting"])
                ),
                diagnostics=[diagnostic],
            )
        if result is None:
            raise RuntimeError("Workspace retention apply produced no result.")
        return result

    async def _apply_retention_unlocked(
        self,
        preview: WorkspaceRetentionPreview,
    ) -> WorkspaceRetentionResult:
        self._ensure_writable()
        validated = validate_retention_preview(
            preview,
            scope=preview["scope"],
            lifecycle=preview["lifecycle"],
            policy=preview["policy"],
            declared_retained_refs=preview["retained_refs"],
            inline_result=preview["inline_result"],
        )
        if validated["status"] != "ready":
            diagnostic = retention_diagnostic(
                "workspace.retention.plan_stale",
                "Workspace retention requires a ready inspected plan.",
                entity=str(validated["scope"].get("execution_id") or "execution_id"),
            )
            return self._retention_result(
                status="deferred",
                plan_fingerprint=validated["plan_fingerprint"],
                manifest_ref=None,
                retained_refs=validated["retained_refs"],
                accounting=self._zero_retention_accounting(validated["accounting"]),
                diagnostics=[diagnostic],
            )

        execution_id = str(validated["lifecycle"].get("execution_id") or "")
        if not execution_id:
            raise ValueError("Workspace retention preview has no lifecycle execution_id.")
        manifest_ref: WorkspaceRecordRef | None = None
        resume_pending = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                try:
                    existing = self._terminal_manifest_from_conn(
                        conn,
                        execution_id=execution_id,
                    )
                except (TypeError, ValueError) as error:
                    conn.rollback()
                    diagnostic = retention_diagnostic(
                        "workspace.retention.ledger_invalid",
                        f"Workspace terminal retention ledger is invalid: {error}",
                        entity=self._terminal_manifest_record_id(execution_id),
                        detail={"error_type": type(error).__name__},
                    )
                    diagnostic["retryable"] = False
                    return self._retention_result(
                        status="deferred",
                        plan_fingerprint=validated["plan_fingerprint"],
                        manifest_ref=None,
                        retained_refs=validated["retained_refs"],
                        accounting=self._zero_retention_accounting(
                            validated["accounting"]
                        ),
                        diagnostics=[diagnostic],
                    )
                if existing is not None:
                    existing_meta = cast(Mapping[str, Any], existing["meta"])
                    existing_fingerprint = str(existing_meta["plan_fingerprint"])
                    existing_state = str(existing_meta["state"])
                    if existing_fingerprint == validated["plan_fingerprint"]:
                        conn.commit()
                        if existing_state == "applied":
                            return self._retention_result(
                                status="noop",
                                plan_fingerprint=existing_fingerprint,
                                manifest_ref=existing,
                                retained_refs=cast(
                                    Sequence[WorkspaceRetainedReference],
                                    existing_meta["retained_refs"],
                                ),
                                accounting=self._zero_retention_accounting(
                                    cast(
                                        Mapping[str, Any],
                                        existing_meta["accounting"],
                                    )
                                ),
                            )
                        manifest_ref = existing
                        resume_pending = True
                    elif existing_state != "applied":
                        conn.rollback()
                        diagnostic = retention_diagnostic(
                            "workspace.retention.plan_conflict",
                            "A different Workspace retention plan still has pending derived cleanup.",
                            entity=existing["id"],
                            detail={
                                "existing_plan_fingerprint": existing_fingerprint,
                                "requested_plan_fingerprint": validated["plan_fingerprint"],
                            },
                        )
                        return self._retention_result(
                            status="deferred",
                            plan_fingerprint=validated["plan_fingerprint"],
                            manifest_ref=existing,
                            retained_refs=validated["retained_refs"],
                            accounting=self._zero_retention_accounting(
                                validated["accounting"]
                            ),
                            diagnostics=[diagnostic],
                        )

                if not resume_pending:
                    current = await self._inspect_retention(
                        validated["scope"],
                        lifecycle=validated["lifecycle"],
                        retained_refs=validated["retained_refs"],
                        inline_result=validated["inline_result"],
                        policy=validated["policy"],
                        connection=conn,
                    )
                    if (
                        current["status"] != "ready"
                        or current["plan_fingerprint"] != validated["plan_fingerprint"]
                    ):
                        conn.rollback()
                        diagnostic = retention_diagnostic(
                            "workspace.retention.plan_stale",
                            "Workspace retention scope changed after inspection.",
                            entity=execution_id,
                            detail={
                                "inspected_plan_fingerprint": validated["plan_fingerprint"],
                                "current_plan_fingerprint": current["plan_fingerprint"],
                                "current_status": current["status"],
                            },
                        )
                        return self._retention_result(
                            status="deferred",
                            plan_fingerprint=validated["plan_fingerprint"],
                            manifest_ref=None,
                            retained_refs=validated["retained_refs"],
                            accounting=self._zero_retention_accounting(
                                validated["accounting"]
                            ),
                            diagnostics=[diagnostic],
                        )
                    pending = {
                        key: list(validated["selected"][key])
                        for key in (
                            "vector_record_ids",
                            "content_paths",
                            "file_paths",
                            "scratch_paths",
                        )
                    }
                    has_pending = any(pending.values())
                    meta = {
                        "schema_version": "agently.workspace.terminal_manifest.v1",
                        "plan_fingerprint": validated["plan_fingerprint"],
                        "state": "db_committed" if has_pending else "applied",
                        "lifecycle": dict(validated["lifecycle"]),
                        "retained_refs": list(validated["retained_refs"]),
                        "inline_result": validated["inline_result"],
                        "accounting": dict(validated["accounting"]),
                        "derived_cleanup": {
                            "pending": pending,
                            "attempts": 0,
                            "last_error": None,
                        },
                    }
                    manifest_ref = self._write_terminal_manifest_on_conn(
                        conn,
                        execution_id=execution_id,
                        scope=validated["scope"],
                        meta=meta,
                        created_at=(existing["created_at"] if existing is not None else None),
                    )
                    self._delete_retention_logical_selection_on_conn(
                        conn,
                        validated["selected"],
                    )
                    conn.commit()
                    if not has_pending:
                        return self._retention_result(
                            status="applied",
                            plan_fingerprint=validated["plan_fingerprint"],
                            manifest_ref=manifest_ref,
                            retained_refs=validated["retained_refs"],
                            accounting=validated["accounting"],
                        )
            except (sqlite3.Error, OSError) as error:
                if conn.in_transaction:
                    conn.rollback()
                diagnostic = retention_diagnostic(
                    "workspace.retention.apply_failed",
                    f"Workspace retention transaction failed and was rolled back: {error}",
                    entity=execution_id,
                    detail={"error_type": type(error).__name__},
                )
                return self._retention_result(
                    status="deferred",
                    plan_fingerprint=validated["plan_fingerprint"],
                    manifest_ref=None,
                    retained_refs=validated["retained_refs"],
                    accounting=self._zero_retention_accounting(
                        validated["accounting"]
                    ),
                    diagnostics=[diagnostic],
                )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise

        if manifest_ref is None:
            raise RuntimeError("Workspace retention did not produce a terminal manifest.")
        return await self._resume_retention_derived_cleanup(manifest_ref)

    @_guard_local_mutation
    async def prune_scope(
        self,
        scope: dict[str, Any],
        *,
        remove_files: bool = True,
    ) -> dict[str, Any]:
        self._ensure_writable()
        normalized_scope = {str(key): value for key, value in dict(scope or {}).items() if value is not None}
        if not normalized_scope:
            raise ValueError("Workspace prune_scope requires at least one scope value.")
        with self._connect() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            for index, (key, value) in enumerate(normalized_scope.items()):
                alias = f"s{index}"
                clauses.append(
                    f"""
                    EXISTS (
                        SELECT 1 FROM record_scope_index {alias}
                        WHERE {alias}.record_id = r.id
                        AND {alias}.scope_key = ?
                        AND {alias}.scope_value = ?
                    )
                    """
                )
                params.extend([key, self._scope_index_value(value)])
            rows = conn.execute(
                f"SELECT r.id, r.path FROM records r WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
            record_ids = [str(row["id"]) for row in rows]
            content_paths = [str(row["path"]) for row in rows if row["path"]]
            placeholders = ",".join("?" for _ in record_ids)
            if record_ids:
                conn.execute(
                    f"DELETE FROM links WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                    [*record_ids, *record_ids],
                )
                conn.execute(f"DELETE FROM checkpoints WHERE record_id IN ({placeholders})", record_ids)
                conn.execute(f"DELETE FROM records_fts WHERE record_id IN ({placeholders})", record_ids)
                conn.execute(f"DELETE FROM record_scope_index WHERE record_id IN ({placeholders})", record_ids)
                conn.execute(f"DELETE FROM records WHERE id IN ({placeholders})", record_ids)
            runtime_events_deleted = 0
            retention_anchors_deleted = 0
            execution_id = normalized_scope.get("execution_id")
            if isinstance(execution_id, str) and execution_id:
                runtime_events_deleted = conn.execute(
                    "DELETE FROM runtime_events WHERE execution_id = ?",
                    (execution_id,),
                ).rowcount
                retention_anchors_deleted = conn.execute(
                    "DELETE FROM retention_anchors WHERE execution_id = ?",
                    (execution_id,),
                ).rowcount
            conn.commit()
        delete_vectors = getattr(self.vector_store_provider, "delete_records", None)
        if callable(delete_vectors):
            await cast(Callable[[list[str]], Awaitable[None]], delete_vectors)(record_ids)
        content_files_deleted = 0
        for path in content_paths:
            target = self.content_root / path
            if target.exists() and target.is_file():
                target.unlink()
                content_files_deleted += 1
        removed_paths: list[str] = []
        if remove_files:
            removed_paths = self._prune_scope_subtrees(normalized_scope)
        return {
            "scope": normalized_scope,
            "records_deleted": len(record_ids),
            "content_files_deleted": content_files_deleted,
            "runtime_events_deleted": runtime_events_deleted,
            "retention_anchors_deleted": retention_anchors_deleted,
            "removed_paths": removed_paths,
            "removed_files": bool(removed_paths),
        }

    def _prune_scope_subtrees(self, scope: dict[str, Any]) -> list[str]:
        """Remove only the lineage subtree(s) matching the prune scope.

        Each prunable scope value maps to a ``<kind>/<id>`` lineage node; the
        matching directories under ``files/lineage`` and ``scratch/lineage`` are
        removed as contained subtrees, leaving unrelated siblings intact. This
        replaces the previous whole-``files_root`` deletion (spec sections 8.2 / 9).
        """

        from ._defaults import scope_filter_path_nodes

        nodes = scope_filter_path_nodes(scope)
        if not nodes:
            return []
        removed: list[str] = []
        for area in ("files", "scratch"):
            lineage_root = self.root / area / "lineage"
            if not lineage_root.exists():
                continue
            for node in nodes:
                kind = slug(node["kind"], "scope")
                node_id = slug(node["id"], "default")
                for candidate in list(lineage_root.rglob(node_id)):
                    if not candidate.is_dir() or candidate.parent.name != kind:
                        continue
                    if candidate.exists():
                        shutil.rmtree(candidate)
                        removed.append(str(candidate))
        if removed:
            self._delete_scratch_leases_under(removed)
        return removed

    @staticmethod
    def _row_to_scratch_lease(row: sqlite3.Row) -> WorkspaceScratchLease:
        return cast(
            WorkspaceScratchLease,
            {
                "lease_id": row["lease_id"],
                "scope": json_loads(row["scope_json"], {}),
                "local_path": row["local_path"],
                "mount": json_loads(row["mount_json"], None),
                "purpose": row["purpose"],
                "cleanup_policy": row["cleanup_policy"],
                "expires_at": row["expires_at"],
                "read_only": bool(row["read_only"]),
                "policy_labels": json_loads(row["policy_labels_json"], []),
                "created_at": row["created_at"],
                "closed_at": row["closed_at"],
            },
        )

    @_guard_local_mutation
    async def register_scratch_lease(self, lease: WorkspaceScratchLease) -> WorkspaceScratchLease:
        self._ensure_writable()
        lease_id = str(lease.get("lease_id") or uuid.uuid4().hex)
        record: WorkspaceScratchLease = {
            "lease_id": lease_id,
            "scope": dict(lease.get("scope") or {}),
            "local_path": lease.get("local_path"),
            "mount": lease.get("mount"),
            "purpose": lease.get("purpose"),
            "cleanup_policy": lease.get("cleanup_policy") or "on_close",
            "expires_at": lease.get("expires_at"),
            "read_only": bool(lease.get("read_only", False)),
            "policy_labels": list(lease.get("policy_labels") or []),
            "created_at": lease.get("created_at") or utc_now(),
            "closed_at": lease.get("closed_at"),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scratch_leases(
                    lease_id, scope_json, local_path, mount_json, purpose,
                    cleanup_policy, expires_at, read_only, policy_labels_json,
                    created_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    json_dumps(record["scope"]),
                    record["local_path"],
                    json_dumps(record["mount"]) if record["mount"] is not None else None,
                    record["purpose"],
                    record["cleanup_policy"],
                    record["expires_at"],
                    1 if record["read_only"] else 0,
                    json_dumps(record["policy_labels"]),
                    record["created_at"],
                    record["closed_at"],
                ),
            )
        return record

    async def get_scratch_lease(self, lease_id: str) -> WorkspaceScratchLease | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scratch_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._row_to_scratch_lease(row) if row is not None else None

    async def list_scratch_leases(
        self,
        *,
        include_closed: bool = False,
        expired_before: str | None = None,
    ) -> list[WorkspaceScratchLease]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_closed:
            clauses.append("closed_at IS NULL")
        if expired_before is not None:
            clauses.append("expires_at IS NOT NULL AND expires_at <= ?")
            params.append(expired_before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM scratch_leases {where} ORDER BY created_at",
                params,
            ).fetchall()
        return [self._row_to_scratch_lease(row) for row in rows]

    @_guard_local_mutation
    async def close_scratch_lease(
        self,
        lease_id: str,
        *,
        closed_at: str | None = None,
    ) -> WorkspaceScratchLease | None:
        self._ensure_writable()
        stamp = closed_at or utc_now()
        with self._connect() as conn:
            conn.execute(
                "UPDATE scratch_leases SET closed_at = ? WHERE lease_id = ? AND closed_at IS NULL",
                (stamp, lease_id),
            )
            row = conn.execute(
                "SELECT * FROM scratch_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._row_to_scratch_lease(row) if row is not None else None

    def _delete_scratch_leases_under(self, removed_paths: list[str]) -> None:
        if not removed_paths:
            return
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT lease_id, local_path FROM scratch_leases WHERE local_path IS NOT NULL"
            ).fetchall()
            stale = [
                str(row["lease_id"])
                for row in rows
                if any(str(row["local_path"]).startswith(prefix) for prefix in removed_paths)
            ]
            if stale:
                placeholders = ",".join("?" for _ in stale)
                conn.execute(
                    f"DELETE FROM scratch_leases WHERE lease_id IN ({placeholders})",
                    stale,
                )

    def capabilities(self) -> WorkspaceBackendCapabilities:
        vector_index = self.vector_index
        return {
            "backend": "local",
            "root": str(self.root),
            "content_root": str(self.content_root),
            "files_root": str(self.files_root),
            "read_only": self.read_only,
            "components": {
                "db_store_provider": self.db_store_provider_name,
                "content": type(self.content).__name__,
                "metadata": type(self.metadata).__name__,
                "checkpoint_store": type(self.checkpoint_store).__name__,
                "text_index": type(self.text_index).__name__,
                "policy": type(self.policy).__name__,
                "embedding_provider": (
                    getattr(self.embedding_provider, "name", None) or type(self.embedding_provider).__name__
                    if self.embedding_provider is not None
                    else None
                ),
                "vector_store_provider": (
                    self.vector_store_provider_name
                    or getattr(self.vector_store_provider, "name", None)
                    or type(self.vector_store_provider).__name__
                    if self.vector_store_provider is not None
                    else None
                ),
                "vector_index": type(vector_index).__name__ if vector_index is not None else None,
                "runtime_event_store": type(self.runtime_event_store).__name__,
                "ref_resolver": type(self.ref_resolver).__name__,
                "retention_policy": type(self.retention_policy).__name__,
                "evidence_linker": type(self.evidence_linker).__name__,
            },
            "features": self._features(),
        }
