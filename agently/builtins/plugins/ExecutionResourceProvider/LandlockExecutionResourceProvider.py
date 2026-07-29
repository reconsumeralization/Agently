# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
LandlockExecutionResourceProvider — Linux Landlock LSM sandbox backend.

Uses Linux kernel Landlock (5.13+) to provide filesystem access control.
Landlock allows unprivileged processes to restrict their own filesystem
access. Restrictions are **irreversible** once applied.

**Important**: Due to the irreversible nature of landlock_restrict_self,
this provider uses fork() to isolate the restriction to child processes.
The parent process remains unaffected.

This provider conforms to the Agently 4.1.4.2 ExecutionResourceProvider
contract: it registers under ``kind="code_execution"`` and implements
``async_probe`` / ``async_ensure`` / ``async_health_check`` /
``async_release`` / ``async_execute_code``.

Only functional on Linux 5.13+. On other platforms or older kernels
the provider reports itself as unavailable.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import errno
import hashlib
import os
import platform
import shutil
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceExecutionManifest,
    resolve_code_execution_workspace_uri,
)
from agently.types.data.code_execution import extract_code_toolchain_version


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_linux() -> bool:
    return platform.system() == "Linux"


# ---------------------------------------------------------------------------
# Landlock syscall constants and structures
# ---------------------------------------------------------------------------

# Landlock access rights (ABI v1-v3 combined)
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13  # ABI v2+
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14  # ABI v3+

LANDLOCK_RULE_PATH_BENEATH = 1

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

# All read access bits
LANDLOCK_ACCESS_FS_ALL_READ = (
    LANDLOCK_ACCESS_FS_EXECUTE |
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR
)

# All write access bits
LANDLOCK_ACCESS_FS_ALL_WRITE = (
    LANDLOCK_ACCESS_FS_WRITE_FILE |
    LANDLOCK_ACCESS_FS_REMOVE_DIR |
    LANDLOCK_ACCESS_FS_REMOVE_FILE |
    LANDLOCK_ACCESS_FS_MAKE_CHAR |
    LANDLOCK_ACCESS_FS_MAKE_DIR |
    LANDLOCK_ACCESS_FS_MAKE_REG |
    LANDLOCK_ACCESS_FS_MAKE_SOCK |
    LANDLOCK_ACCESS_FS_MAKE_FIFO |
    LANDLOCK_ACCESS_FS_MAKE_BLOCK |
    LANDLOCK_ACCESS_FS_MAKE_SYM |
    LANDLOCK_ACCESS_FS_REFER |
    LANDLOCK_ACCESS_FS_TRUNCATE
)

# Base read-only paths automatically injected so that basic commands
# (python3, shared libraries, dynamic linker, etc.) work out of the box.
_BASE_READ_DIRS: list[str] = [
    "/usr",
    "/lib",
    "/lib64",
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/dev/null",
    "/dev/urandom",
    "/dev/zero",
]

# Exit codes used by _apply_landlock preexec_fn to signal Landlock failures.
# The parent process inspects these to distinguish "Landlock setup failed" from
# "user command failed".
_LANDLOCK_EXIT_LIB_NOT_FOUND = 127  # ctypes.util.find_library("c") failed
_LANDLOCK_EXIT_CREATE_FAILED = 126  # landlock_create_ruleset failed
_LANDLOCK_EXIT_RESTRICT_FAILED = 125  # landlock_restrict_self failed


# ---------------------------------------------------------------------------
# Landlock syscall wrappers via ctypes
# ---------------------------------------------------------------------------

_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        libc_name = ctypes.util.find_library("c")
        if libc_name:
            _libc = ctypes.CDLL(libc_name, use_errno=True)
    return _libc


def _syscall_landlock_create_ruleset(attr_ptr, attr_size, flags):
    """Call landlock_create_ruleset syscall."""
    libc = _get_libc()
    if libc is None:
        return -1
    # syscall number varies by architecture; 444 on x86_64/arm64
    SYS_LANDLOCK_CREATE_RULESET = 444
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        attr_ptr,
        attr_size,
        flags,
    )


def _syscall_landlock_add_rule(ruleset_fd, rule_type, rule_attr, flags):
    """Call landlock_add_rule syscall."""
    libc = _get_libc()
    if libc is None:
        return -1
    SYS_LANDLOCK_ADD_RULE = 445
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(
        SYS_LANDLOCK_ADD_RULE,
        ruleset_fd,
        rule_type,
        rule_attr,
        flags,
    )


def _syscall_landlock_restrict_self(ruleset_fd, flags):
    """Call landlock_restrict_self syscall."""
    libc = _get_libc()
    if libc is None:
        return -1
    SYS_LANDLOCK_RESTRICT_SELF = 446
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(
        SYS_LANDLOCK_RESTRICT_SELF,
        ruleset_fd,
        flags,
    )


def landlock_probe_abi_version() -> int:
    """Probe the maximum supported Landlock ABI version. Returns 0 if unsupported."""
    if not is_linux():
        return 0
    try:
        version = _syscall_landlock_create_ruleset(
            None, 0, LANDLOCK_CREATE_RULESET_VERSION
        )
        if version < 0:
            return 0
        return int(version)
    except Exception:
        return 0


def _landlock_supported_access(abi_version: int) -> int:
    """Return the access bits supported by the given ABI version."""
    mask = (
        LANDLOCK_ACCESS_FS_ALL_READ |
        LANDLOCK_ACCESS_FS_WRITE_FILE |
        LANDLOCK_ACCESS_FS_REMOVE_DIR |
        LANDLOCK_ACCESS_FS_REMOVE_FILE |
        LANDLOCK_ACCESS_FS_MAKE_CHAR |
        LANDLOCK_ACCESS_FS_MAKE_DIR |
        LANDLOCK_ACCESS_FS_MAKE_REG |
        LANDLOCK_ACCESS_FS_MAKE_SOCK |
        LANDLOCK_ACCESS_FS_MAKE_FIFO |
        LANDLOCK_ACCESS_FS_MAKE_BLOCK |
        LANDLOCK_ACCESS_FS_MAKE_SYM
    )
    if abi_version >= 2:
        mask |= LANDLOCK_ACCESS_FS_REFER
    if abi_version >= 3:
        mask |= LANDLOCK_ACCESS_FS_TRUNCATE
    return mask


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------

def inspect_landlock_availability() -> dict[str, Any]:
    """Check whether Landlock is supported by the current kernel."""
    if not is_linux():
        return {"available": False, "reason": "not_linux"}
    abi_version = landlock_probe_abi_version()
    if abi_version <= 0:
        return {"available": False, "reason": "kernel_does_not_support_landlock"}
    return {
        "available": True,
        "platform": "linux",
        "abi_version": abi_version,
        "version": f"ABI v{abi_version}",
    }


# ---------------------------------------------------------------------------
# LandlockCodeExecutionResource
# ---------------------------------------------------------------------------

class LandlockCodeExecutionResource:
    """Execute code with Landlock filesystem restrictions.

    Due to the irreversible nature of landlock_restrict_self, this resource
    uses fork() to apply restrictions only in child processes. The parent
    process remains unaffected.

    Follows the same bundle/manifest/grant validation pattern as
    TrustedLocalCodeExecutionResource.
    """

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
        allowed_read_dirs: list[str] | None = None,
        allowed_write_dirs: list[str] | None = None,
        abi_version: int = 0,  # 0 = auto-detect
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        # P1-3: resolve paths to prevent escape via ../ or symlinks
        self.allowed_read_dirs = [
            str(Path(p).resolve()) for p in (allowed_read_dirs or [])
        ]
        self.allowed_write_dirs = [
            str(Path(p).resolve()) for p in (allowed_write_dirs or [])
        ]
        self.abi_version = abi_version
        self._active_executions: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._temp_write_dir: str | None = None  # P1-4: auto-created temp dir

    @staticmethod
    def _sha256(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    def _validate_materialization(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
    ) -> Path:
        if grant != self.grant:
            raise PermissionError("Landlock resource is bound to another Workspace grant.")
        if (
            manifest.grant_id != grant.grant_id
            or manifest.bundle_id != bundle.bundle_id
            or manifest.bundle_digest != bundle.bundle_digest
        ):
            raise PermissionError("Code execution manifest does not match the bound bundle and grant.")
        area = Path(grant.execution_area).resolve()
        manifest_files = {Path(item.host_path).resolve(): item for item in manifest.files}
        for item in bundle.files:
            target = (area / "source" / Path(item.path)).resolve()
            if area not in target.parents or target.is_symlink() or not target.is_file():
                raise PermissionError("Materialized bundle file escaped or is unavailable.")
            recorded = manifest_files.get(target)
            if recorded is None or recorded.sha256 != item.sha256:
                raise PermissionError("Materialized bundle file is absent from the Workspace manifest.")
            if self._sha256(target) != item.sha256:
                raise PermissionError("Materialized bundle file digest changed before execution.")
        return area

    def _build_landlock_rules(
        self, *, cwd: str | None = None,
    ) -> tuple[int, list[tuple[str, int]]]:
        """Build Landlock ruleset and return (handled_access, rules_list).

        rules_list is a list of (path, allowed_access) tuples.
        Automatically injects _BASE_READ_DIRS and cwd so that basic
        commands work out of the box.
        """
        # Determine ABI version
        kernel_ab = landlock_probe_abi_version()
        if self.abi_version > 0:
            abi = min(self.abi_version, kernel_ab)
        else:
            abi = kernel_ab

        handled = _landlock_supported_access(abi)
        read_access = LANDLOCK_ACCESS_FS_ALL_READ & handled
        write_access = (LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE) & handled

        rules: list[tuple[str, int]] = []
        seen: set[str] = set()

        def _add(path: str, access: int) -> None:
            resolved = str(Path(path).resolve())
            if resolved not in seen and Path(resolved).exists():
                seen.add(resolved)
                rules.append((resolved, access))

        # P0-2: auto-inject base read-only paths
        for path in _BASE_READ_DIRS:
            _add(path, read_access)

        # P0-2: auto-inject cwd as read-only (must be traversable)
        if cwd:
            _add(cwd, read_access)

        # Add user-specified read dirs
        for path in self.allowed_read_dirs:
            _add(path, read_access)

        # Add user-specified write dirs
        for path in self.allowed_write_dirs:
            _add(path, write_access)

        # Add temp write dir (P1-4)
        if self._temp_write_dir:
            _add(self._temp_write_dir, write_access)

        # Add grant roots
        for root in self.grant.roots:
            path = root.host_path
            if root.access_mode == "read_write":
                _add(path, write_access)
            else:
                _add(path, read_access)

        return handled, rules

    async def _run_with_landlock(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        """Run command in a subprocess with Landlock restrictions.

        Uses preexec_fn to apply Landlock in the child process before exec.
        The parent process remains unaffected.

        Key requirement: PR_SET_NO_NEW_PRIVS must be set before
        landlock_restrict_self, otherwise the syscall returns EPERM.
        """
        # Build Landlock rules for this execution (pass cwd for auto-inject)
        handled_access, rules = self._build_landlock_rules(cwd=cwd)

        def _apply_landlock():
            """preexec_fn: apply Landlock in child before exec.

            On any failure the child calls os._exit() with a diagnostic
            exit code so the parent can detect that Landlock was NOT
            applied (instead of silently running unrestricted).
            """
            import ctypes as _ct
            import ctypes.util as _ct_util
            import struct as _struct

            _libc_name = _ct_util.find_library("c")
            if not _libc_name:
                os._exit(_LANDLOCK_EXIT_LIB_NOT_FOUND)
            _libc = _ct.CDLL(_libc_name, use_errno=True)
            _libc.syscall.restype = _ct.c_long

            # 1. Set PR_SET_NO_NEW_PRIVS (required before restrict_self)
            _libc.prctl(38, 1, 0, 0, 0)

            # 2. Create ruleset
            _attr = _struct.pack("Q", handled_access)
            _buf = _ct.create_string_buffer(_attr)
            _ruleset_fd = int(_libc.syscall(444, _buf, len(_attr), 0))
            if _ruleset_fd < 0:
                os._exit(_LANDLOCK_EXIT_CREATE_FAILED)

            # 3. Add path rules
            for path, access in rules:
                try:
                    _path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                    _rule_attr = _struct.pack("Qi", access, _path_fd)
                    _rule_buf = _ct.create_string_buffer(_rule_attr)
                    _libc.syscall(445, _ruleset_fd, 1, _rule_buf, 0)
                    os.close(_path_fd)
                except OSError:
                    pass

            # 4. Apply restrictions
            _rc = int(_libc.syscall(446, _ruleset_fd, 0))
            os.close(_ruleset_fd)
            if _rc != 0:
                os._exit(_LANDLOCK_EXIT_RESTRICT_FAILED)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            preexec_fn=_apply_landlock,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "ok": False,
                "status": "error",
                "returncode": -1,
                "stdout": "",
                "stderr": f"execution timed out after {timeout} seconds",
                "stdout_truncated": False,
                "stderr_truncated": True,
            }

        stdout_bytes = stdout or b""
        stderr_bytes = stderr or b""

        # P0-1: detect Landlock setup failures by exit code
        _landlock_errors = {
            _LANDLOCK_EXIT_LIB_NOT_FOUND: (
                "Landlock setup failed: libc not found in child process"
            ),
            _LANDLOCK_EXIT_CREATE_FAILED: (
                "Landlock setup failed: landlock_create_ruleset returned error"
            ),
            _LANDLOCK_EXIT_RESTRICT_FAILED: (
                "Landlock setup failed: landlock_restrict_self returned error"
            ),
        }
        if proc.returncode in _landlock_errors:
            diag = _landlock_errors[proc.returncode]
            stderr_bytes = (stderr_bytes + diag.encode()).strip()
            return {
                "ok": False,
                "status": "error",
                "returncode": proc.returncode,
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

        # Truncate output
        stdout_truncated = len(stdout_bytes) > self.max_output_bytes
        stderr_truncated = len(stderr_bytes) > self.max_output_bytes
        if stdout_truncated:
            stdout_bytes = stdout_bytes[:self.max_output_bytes]
        if stderr_truncated:
            stderr_bytes = stderr_bytes[:self.max_output_bytes]

        return {
            "ok": proc.returncode == 0,
            "status": "success" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }

    async def _run(
        self,
        *,
        bundle: CodeExecutionBundle,
        area: Path,
        timeout: int,
    ) -> dict[str, Any]:
        logs_root = area / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        steps = (*bundle.build_steps, bundle.run_step)
        final_stdout = ""
        final_stderr = ""
        returncode = 0
        log_refs: list[str] = []
        for index, step in enumerate(steps):
            cwd = (area / Path(step.cwd)).resolve()
            if area not in cwd.parents or not cwd.is_dir() or cwd.is_symlink():
                raise PermissionError("Execution step cwd escaped its Workspace grant.")
            stdout_path = logs_root / f"{index:02d}-{step.role}.stdout.log"
            stderr_path = logs_root / f"{index:02d}-{step.role}.stderr.log"
            environment = dict(os.environ)
            workspace_roots = {
                root.role: root.host_path
                for root in self.grant.roots
                if root.role in {"source", "build", "output", "logs"}
            }
            environment.update(
                {
                    key: resolve_code_execution_workspace_uri(
                        value,
                        roots=workspace_roots,
                    )
                    for key, value in step.env.items()
                }
            )
            result = await self._run_with_landlock(
                list(step.argv),
                cwd=str(cwd),
                env=environment,
                timeout=timeout,
            )
            returncode = result["returncode"]
            final_stdout = result["stdout"]
            final_stderr = result["stderr"]
            stdout_path.write_text(final_stdout, encoding="utf-8")
            stderr_path.write_text(final_stderr, encoding="utf-8")
            log_refs.extend(
                [
                    f"logs/{stdout_path.name}",
                    f"logs/{stderr_path.name}",
                ]
            )
            if returncode != 0:
                break
        outputs = [
            path
            for path in bundle.expected_outputs
            if (area / Path(path)).is_file() and not (area / Path(path)).is_symlink()
        ]
        return {
            "ok": returncode == 0,
            "status": "success" if returncode == 0 else "error",
            "returncode": returncode,
            "stdout": final_stdout,
            "stderr": final_stderr,
            "stdout_truncated": result.get("stdout_truncated", False),
            "stderr_truncated": result.get("stderr_truncated", False),
            "outputs": outputs,
            "log_refs": log_refs,
        }

    async def async_execute_code(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
        timeout: int,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Landlock execution resource is closed.")
        area = self._validate_materialization(
            bundle=bundle,
            manifest=manifest,
            grant=grant,
        )
        task = asyncio.current_task()
        if task is not None:
            self._active_executions.add(task)
        try:
            return await self._run(
                bundle=bundle,
                area=area,
                timeout=timeout,
            )
        finally:
            if task is not None:
                self._active_executions.discard(task)

    async def async_close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        active = [task for task in self._active_executions if task is not current]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        # P1-4: clean up auto-created temp write dir
        if self._temp_write_dir:
            try:
                shutil.rmtree(self._temp_write_dir, ignore_errors=True)
            except Exception:
                pass
            self._temp_write_dir = None


# ---------------------------------------------------------------------------
# LandlockExecutionResourceProvider
# ---------------------------------------------------------------------------

class LandlockExecutionResourceProvider:
    """Provider that creates Landlock-restricted execution resources.

    Conforms to the 4.1.4.2 ExecutionResourceProvider contract:
    - ``provider_id = "landlock"``
    - ``supported_kinds = ("code_execution",)``
    - Implements ``async_probe`` / ``async_ensure`` / ``async_health_check``
      / ``async_release``

    Uses ``preexec_fn`` in subprocess to apply Landlock restrictions in the
    child process before exec. The parent process remains unaffected.

    Requirement: ``PR_SET_NO_NEW_PRIVS`` must be set before
    ``landlock_restrict_self``, otherwise the syscall returns EPERM.
    """

    name = "LandlockExecutionResourceProvider"
    provider_id = "landlock"
    supported_kinds = ("code_execution",)

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    @staticmethod
    def _tool_facts() -> dict[str, dict[str, Any]]:
        commands = {
            "python": ("python3", ("--version",)),
        }
        facts: dict[str, dict[str, Any]] = {}
        for language, (tool, command_args) in commands.items():
            binary = shutil.which(tool)
            fact: dict[str, Any] = {
                "tool": tool,
                "available": binary is not None,
                "binary": binary or "",
                "version": "",
                "raw_version": "",
            }
            if binary is not None:
                try:
                    completed = subprocess.run(
                        [binary, *command_args],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    raw_version = str(completed.stdout or completed.stderr).strip()[:300]
                    fact["raw_version"] = raw_version
                    fact["version"] = extract_code_toolchain_version(raw_version)
                    fact["available"] = completed.returncode == 0
                except Exception as error:
                    fact.update(available=False, error=str(error)[:300])
            facts[language] = fact
        return facts

    async def async_probe(self, *, requirement, policy):
        _ = requirement, policy
        availability = await asyncio.to_thread(inspect_landlock_availability)
        available = bool(availability.get("available"))
        reason = str(availability.get("reason", "available")) if not available else "landlock available"
        facts = await asyncio.to_thread(self._tool_facts) if available else {}
        languages = [language for language, fact in facts.items() if fact["available"]]
        toolchains = {
            str(fact["tool"]): {
                "available": bool(fact["available"]),
                "version": str(fact.get("version", "")),
                "raw_version": str(fact.get("raw_version", "")),
                "binary": str(fact.get("binary", "")),
            }
            for fact in facts.values()
        }
        return {
            "provider_id": self.provider_id,
            "available": available and bool(languages),
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": languages,
                "toolchains": toolchains,
                "isolation": {
                    "process_contained": False,  # Landlock only restricts filesystem
                    "host_filesystem_restricted": True,
                    "privilege_escalation_blocked": True,  # PR_SET_NO_NEW_PRIVS
                    "syscalls_restricted": False,
                    "mechanism": "landlock",
                    "abi_version": availability.get("abi_version", 0),
                    "landlock_version": f"ABI v{availability.get('abi_version', 0)}",
                    "filesystem_only": True,
                    "auto_base_paths": True,
                },
                "workspace_access_modes": ["snapshot", "read_only", "read_write"],
                "network": "inherited",  # Landlock doesn't control network
                "safety_class": "filesystem_only",
            },
            "reason": reason,
            "meta": {"availability": availability, "toolchains": facts},
        }

    async def async_ensure(self, *, requirement, policy):
        from agently.core import ExecutionResourceError

        config = requirement.get("config", {})
        config = config if isinstance(config, dict) else {}
        grant = requirement.get("task_workspace_access_grant")
        if str(requirement.get("kind", "")) == "code_execution":
            if not isinstance(grant, TaskWorkspaceAccessGrant):
                raise ExecutionResourceError(
                    "Landlock code execution requires a TaskWorkspace access grant.",
                    code="execution_resource.workspace_grant_required",
                    payload={"provider_id": self.provider_id},
                )

        availability = await asyncio.to_thread(inspect_landlock_availability)
        if not availability.get("available"):
            raise ExecutionResourceError(
                f"Landlock is not available: {availability.get('reason', 'unknown')}",
                code="execution_resource.landlock_unavailable",
                payload={"provider_id": self.provider_id, "availability": availability},
            )

        # P1-4: auto-create temp write dir if none specified
        write_dirs = [str(p) for p in config.get("allowed_write_dirs", [])]
        temp_write_dir: str | None = None
        if not write_dirs:
            temp_write_dir = tempfile.mkdtemp(prefix="landlock_")
            write_dirs = [temp_write_dir]

        resource = LandlockCodeExecutionResource(
            grant=grant,
            max_output_bytes=int(policy.get("max_output_bytes", 20000)),
            allowed_read_dirs=[str(p) for p in config.get("allowed_read_dirs", [])],
            allowed_write_dirs=write_dirs,
            abi_version=int(config.get("abi_version", 0)),
        )
        resource._temp_write_dir = temp_write_dir

        return {
            "handle_id": f"landlock:{uuid.uuid4().hex}",
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "linux",
                "abi_version": availability.get("abi_version", 0),
                "grant_id": grant.grant_id if isinstance(grant, TaskWorkspaceAccessGrant) else None,
                "temp_write_dir": temp_write_dir,
            },
        }

    async def async_health_check(self, handle):
        resource = handle.get("resource")
        if not isinstance(resource, LandlockCodeExecutionResource):
            return "unhealthy"
        # P2-5: also verify Landlock is still available on this kernel
        abi = await asyncio.to_thread(landlock_probe_abi_version)
        return "ready" if abi > 0 else "unhealthy"

    async def async_release(self, handle) -> None:
        resource = handle.get("resource")
        if isinstance(resource, LandlockCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "LandlockCodeExecutionResource",
    "LandlockExecutionResourceProvider",
    "inspect_landlock_availability",
]
