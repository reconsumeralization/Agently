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
SeatbeltExecutionResourceProvider — macOS Seatbelt sandbox backend.

Uses ``sandbox-exec`` with SBPL (Seatbelt Profile Language) to provide
kernel-level syscall filtering on macOS.

This provider conforms to the Agently 4.1.4.2 ExecutionResourceProvider
contract: it registers under ``kind="code_execution"`` and implements
``async_probe`` / ``async_ensure`` / ``async_health_check`` /
``async_release`` / ``async_execute_code``.

Only functional on macOS.  On other platforms the provider reports itself
as unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess
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

from ._bounded_process import run_bounded_process


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_macos() -> bool:
    return platform.system() == "Darwin"


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------

def inspect_seatbelt_availability() -> dict[str, Any]:
    """Check whether macOS Seatbelt (sandbox-exec) is usable."""
    if not is_macos():
        return {"available": False, "reason": "not_macos"}
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return {"available": False, "reason": "sandbox_exec_missing"}
    return {
        "available": True,
        "binary": binary,
        "platform": "macos",
    }


# ---------------------------------------------------------------------------
# SBPL profile generation
# ---------------------------------------------------------------------------

def _build_sbpl_profile(
    *,
    network: bool = False,
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
    extra_rules: str = "",
) -> str:
    """Generate an SBPL (Seatbelt Profile Language) profile string."""
    read_paths = read_paths or []
    write_paths = write_paths or []

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        "# Always allow basic process operations",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "",
        "# Filesystem — deny by default, selectively allow",
        "(allow file-read* (subpath \"/usr/lib\")",
        "                 (subpath \"/usr/share\")",
        "                 (subpath \"/Library/Frameworks\")",
        "                 (subpath \"/System/Library/Frameworks\"))",
    ]

    for p in read_paths:
        lines.append(f'(allow file-read* (subpath "{p}"))')

    for p in write_paths:
        lines.append(f'(allow file-write* (subpath "{p}"))')

    if network:
        lines.extend([
            "",
            "# Network access",
            "(allow network*)",
        ])
    else:
        lines.extend([
            "",
            "# Network denied (default)",
            "(deny network*)",
        ])

    if extra_rules.strip():
        lines.append("")
        lines.append("# Extra rules")
        lines.append(extra_rules.strip())

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SeatbeltCodeExecutionResource
# ---------------------------------------------------------------------------

class SeatbeltCodeExecutionResource:
    """Execute code inside a macOS Seatbelt sandbox.

    Follows the same bundle/manifest/grant validation pattern as
    TrustedLocalCodeExecutionResource, but wraps each execution step
    with ``sandbox-exec -f - <sbpl_profile>``.
    """

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
        network: bool = False,
        read_paths: list[str] | None = None,
        write_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
        python_binary: str = "python3",
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self.network = network
        self.read_paths = [str(p) for p in (read_paths or [])]
        self.write_paths = [str(p) for p in (write_paths or [])]
        self.extra_sbpl_rules = extra_sbpl_rules
        self.python_binary = python_binary
        self._active_executions: set[asyncio.Task[Any]] = set()
        self._closed = False

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
            raise PermissionError("Seatbelt resource is bound to another Workspace grant.")
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

    def _build_profile(self) -> str:
        # Always include grant workspace roots in the SBPL read/write paths.
        all_read = list(self.read_paths)
        all_write = list(self.write_paths)
        for root in self.grant.roots:
            if root.access_mode == "read_write":
                all_write.append(root.host_path)
            else:
                all_read.append(root.host_path)
        return _build_sbpl_profile(
            network=self.network,
            read_paths=all_read,
            write_paths=all_write,
            extra_rules=self.extra_sbpl_rules,
        )

    def _step_argv(self, *, step: Any, area: Path) -> list[str]:
        """Wrap the step's argv with sandbox-exec."""
        profile_text = self._build_profile()
        # sandbox-exec reads the profile from stdin when using -f -
        # We write the profile to a temp file for reliability.
        sbpl_path = area / "logs" / ".sbpl_profile"
        sbpl_path.parent.mkdir(parents=True, exist_ok=True)
        sbpl_path.write_text(profile_text, encoding="utf-8")
        return ["sandbox-exec", "-f", str(sbpl_path), *list(step.argv)]

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
        final_stdout = b""
        final_stderr = b""
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
            argv = self._step_argv(step=step, area=area)
            completed = await run_bounded_process(
                argv,
                cwd=str(cwd),
                env=environment,
                timeout=max(1, timeout),
                max_output_bytes=self.max_output_bytes,
            )
            returncode = completed.returncode
            final_stdout = completed.stdout
            final_stderr = completed.stderr
            if completed.timed_out:
                timeout_message = (
                    f"execution timed out after {timeout} seconds\n".encode()
                )
                remaining = max(0, self.max_output_bytes - len(final_stderr))
                final_stderr += timeout_message[:remaining]
            stdout_path.write_bytes(final_stdout)
            stderr_path.write_bytes(final_stderr)
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
        stdout_truncated = completed.stdout_truncated
        stderr_truncated = completed.stderr_truncated or completed.timed_out
        return {
            "ok": returncode == 0,
            "status": "success" if returncode == 0 else "error",
            "returncode": returncode,
            "stdout": final_stdout.decode("utf-8", errors="replace"),
            "stderr": final_stderr.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
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
            raise RuntimeError("Seatbelt execution resource is closed.")
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


# ---------------------------------------------------------------------------
# SeatbeltExecutionResourceProvider
# ---------------------------------------------------------------------------

class SeatbeltExecutionResourceProvider:
    """Provider that creates Seatbelt-sandboxed execution resources.

    Conforms to the 4.1.4.2 ExecutionResourceProvider contract:
    - ``provider_id = "seatbelt"``
    - ``supported_kinds = ("code_execution",)``
    - Implements ``async_probe`` / ``async_ensure`` / ``async_health_check``
      / ``async_release``
    """

    name = "SeatbeltExecutionResourceProvider"
    provider_id = "seatbelt"
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
        availability = await asyncio.to_thread(inspect_seatbelt_availability)
        available = bool(availability.get("available"))
        reason = str(availability.get("reason", "available")) if not available else "seatbelt available"
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
                    "process_contained": True,
                    "host_filesystem_restricted": True,
                    "privilege_escalation_blocked": True,
                    "syscalls_restricted": True,
                    "mechanism": "seatbelt",
                    "network_mode": "configurable",
                },
                "workspace_access_modes": ["snapshot", "read_only", "read_write"],
                "network": "configurable",
                "safety_class": "isolated",
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
                    "Seatbelt code execution requires a TaskWorkspace access grant.",
                    code="execution_resource.workspace_grant_required",
                    payload={"provider_id": self.provider_id},
                )

        availability = await asyncio.to_thread(inspect_seatbelt_availability)
        if not availability.get("available"):
            raise ExecutionResourceError(
                f"Seatbelt is not available: {availability.get('reason', 'unknown')}",
                code="execution_resource.seatbelt_unavailable",
                payload={"provider_id": self.provider_id, "availability": availability},
            )

        return {
            "handle_id": f"seatbelt:{uuid.uuid4().hex}",
            "resource": SeatbeltCodeExecutionResource(
                grant=grant,
                max_output_bytes=int(policy.get("max_output_bytes", 20000)),
                network=bool(config.get("network", False)),
                read_paths=[str(p) for p in config.get("read_paths", [])],
                write_paths=[str(p) for p in config.get("write_paths", [])],
                extra_sbpl_rules=str(config.get("extra_sbpl_rules", "")),
                python_binary=str(config.get("python_binary", "python3")),
            ),
            "status": "ready",
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "macos",
                "grant_id": grant.grant_id if isinstance(grant, TaskWorkspaceAccessGrant) else None,
            },
        }

    async def async_health_check(self, handle):
        return "ready" if isinstance(
            handle.get("resource"), SeatbeltCodeExecutionResource
        ) else "unhealthy"

    async def async_release(self, handle) -> None:
        resource = handle.get("resource")
        if isinstance(resource, SeatbeltCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "SeatbeltCodeExecutionResource",
    "SeatbeltExecutionResourceProvider",
    "inspect_seatbelt_availability",
]
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
SeatbeltExecutionResourceProvider — macOS Seatbelt sandbox backend.

Uses `sandbox-exec` with SBPL (Seatbelt Profile Language) to provide
kernel-level syscall filtering on macOS, as an alternative to Docker-based
isolation.

SBPL Profile Design (inspired by OpenHanako):
- Default deny all
- Basic capabilities always allowed (process, mach, ipc)
- File read: globally allowed (system libs needed for command execution)
- File write: whitelist only (writable_paths + temp dirs)
- Protected paths: deny write (after allow, last-match-wins)
- Deny-read paths: deny both read and write
- Device files: /dev/null, /dev/ptmx, pseudo-tty
- Network: optional outbound switch

This provider is **only** available on macOS. On other platforms it reports
itself as unavailable.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from agently.core.operation.ExecutionResource import (
    ExecutionResource,
    ExecutionResourceProvider,
)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_macos() -> bool:
    """Return True when running on macOS (Darwin)."""
    return platform.system() == "Darwin"


def inspect_seatbelt_availability() -> dict[str, Any]:
    """Check whether macOS Seatbelt (sandbox-exec) is usable.

    Returns a dict with at least ``{"available": bool}``. When unavailable
    the dict also contains a ``"reason"`` key.
    """
    if not is_macos():
        return {"available": False, "reason": "not_macos"}
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return {"available": False, "reason": "sandbox_exec_missing"}
    return {
        "available": True,
        "binary": binary,
        "platform": "macos",
    }


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def _realpath(path: str) -> str:
    """Resolve symbolic links to prevent subpath bypass attacks.

    SBPL's (subpath "...") can be bypassed via symlinks if the target
    is outside the allowed subtree. Resolving to realpath prevents this.
    """
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return path


def _get_tmpdir() -> str:
    """Get the actual temporary directory on macOS."""
    return os.environ.get("TMPDIR", "/tmp")


# ---------------------------------------------------------------------------
# SBPL profile generation
# ---------------------------------------------------------------------------

def _build_sbpl_profile(
    *,
    network: bool = False,
    writable_paths: list[str] | None = None,
    protected_paths: list[str] | None = None,
    deny_read_paths: list[str] | None = None,
    extra_rules: str = "",
) -> str:
    """Generate an SBPL (Seatbelt Profile Language) profile string.

    Design follows the "last-match-wins" rule: deny rules are placed AFTER
    allow rules to override them for specific paths.

    Parameters
    ----------
    network:
        Allow network outbound access when True.
    writable_paths:
        Filesystem paths where writing is allowed (whitelist).
    protected_paths:
        Paths where writing is explicitly denied (e.g., .git directories).
        These override writable_paths due to last-match-wins.
    deny_read_paths:
        Paths where both reading and writing are denied (e.g., secrets).
    extra_rules:
        Raw SBPL rule text appended verbatim at the end.
    """
    writable_paths = [_realpath(p) for p in (writable_paths or [])]
    protected_paths = [_realpath(p) for p in (protected_paths or [])]
    deny_read_paths = [_realpath(p) for p in (deny_read_paths or [])]
    tmpdir = _get_tmpdir()

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        ";; ═══ Basic capabilities (always allowed) ═══",
        "(allow process-exec* process-fork signal)",
        "(allow sysctl-read)",
        "(allow mach(*))",
        "(allow ipc-posix*)",
        "",
        ";; ═══ File read: globally allowed ═══",
        ";; AI agents need to read system libs to execute commands",
        "(allow file-read*)",
        "",
        ";; ═══ File write: whitelist only ═══",
    ]

    # Writable paths (user-specified workspace)
    for p in writable_paths:
        lines.append(f'(allow file-write* (subpath "{p}"))')

    # Temporary directories (always writable)
    lines.append("")
    lines.append(";; Temporary directories (always writable)")
    lines.append('(allow file-write* (subpath "/private/tmp"))')
    lines.append(f'(allow file-write* (subpath "{tmpdir}"))')

    # Device files
    lines.append("")
    lines.append(";; Device files + PTY")
    lines.append('(allow file-write* (literal "/dev/null"))')
    lines.append('(allow file-write* (regex #"^/dev/ttys[0-9]+$"))')
    lines.append('(allow file-write* (literal "/dev/ptmx"))')
    lines.append("(allow pseudo-tty)")

    # Protected paths: deny write (AFTER allow, last-match-wins)
    if protected_paths:
        lines.append("")
        lines.append(";; Protected paths: deny write (overrides allow)")
        for p in protected_paths:
            lines.append(f'(deny file-write* (subpath "{p}"))')

    # Deny-read paths: deny both read and write
    if deny_read_paths:
        lines.append("")
        lines.append(";; Deny-read paths: deny both read and write")
        for p in deny_read_paths:
            lines.append(f'(deny file-read* (subpath "{p}"))')
            lines.append(f'(deny file-write* (subpath "{p}"))')

    # Network switch
    lines.append("")
    if network:
        lines.append(";; Network: allow outbound")
        lines.append("(allow network-outbound)")
    else:
        lines.append(";; Network: completely denied")
        lines.append("(deny network)")

    # Extra rules (verbatim)
    if extra_rules.strip():
        lines.append("")
        lines.append(";; Extra rules")
        lines.append(extra_rules.strip())

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SeatbeltExecutionResource
# ---------------------------------------------------------------------------

class SeatbeltExecutionResource(ExecutionResource):
    """Execute Python code inside a macOS Seatbelt sandbox.

    The sandbox profile follows these principles:
    - File read: globally allowed (needed for system libs)
    - File write: whitelist only (writable_paths + temp dirs)
    - Protected paths: deny write (overrides writable_paths)
    - Deny-read paths: deny both read and write (for secrets)
    - Network: optional outbound switch
    """

    def __init__(
        self,
        *,
        timeout: int = 60,
        network: bool = False,
        writable_paths: list[str] | None = None,
        protected_paths: list[str] | None = None,
        deny_read_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
        python_binary: str = "python3",
    ):
        self.timeout = timeout
        self.network = network
        self.writable_paths = writable_paths or []
        self.protected_paths = protected_paths or []
        self.deny_read_paths = deny_read_paths or []
        self.extra_sbpl_rules = extra_sbpl_rules
        self.python_binary = python_binary

    # -- availability -------------------------------------------------------

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    # -- SBPL profile -------------------------------------------------------

    def build_profile(self) -> str:
        """Generate the SBPL profile for this sandbox session."""
        return _build_sbpl_profile(
            network=self.network,
            writable_paths=self.writable_paths,
            protected_paths=self.protected_paths,
            deny_read_paths=self.deny_read_paths,
            extra_rules=self.extra_sbpl_rules,
        )

    # -- execution ----------------------------------------------------------

    async def run_python_code(
        self,
        *,
        python_code: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        effective_timeout = timeout or self.timeout
        profile_text = self.build_profile()

        cmd = [
            "sandbox-exec", "-f", "-",
            self.python_binary, "-c", python_code,
        ]

        try:
            result = subprocess.run(
                cmd,
                input=profile_text,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"Seatbelt sandbox timed out after {effective_timeout}s",
                "stdout": "",
                "stderr": "",
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "sandbox-exec binary not found (macOS only)",
                "stdout": "",
                "stderr": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "stdout": "",
                "stderr": "",
            }

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


# ---------------------------------------------------------------------------
# SeatbeltExecutionResourceProvider
# ---------------------------------------------------------------------------

class SeatbeltExecutionResourceProvider(ExecutionResourceProvider):
    """Provider that creates Seatbelt-sandboxed execution resources.

    Registered with ``kind="seatbelt"``. Only functional on macOS.
    """

    @property
    def name(self) -> str:
        return "seatbelt"

    @property
    def kind(self) -> str:
        return "seatbelt"

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    def create_handle(
        self,
        *,
        config: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        availability = inspect_seatbelt_availability()
        if not availability.get("available"):
            return {
                "handle_id": f"seatbelt:{uuid.uuid4().hex}",
                "resource": None,
                "availability": availability,
                "meta": {
                    "provider": self.name,
                    "available": False,
                    "reason": availability.get("reason", "unknown"),
                },
            }

        resource = SeatbeltExecutionResource(
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            network=bool(config.get("network", False)),
            writable_paths=[str(p) for p in config.get("writable_paths", [])],
            protected_paths=[str(p) for p in config.get("protected_paths", [])],
            deny_read_paths=[str(p) for p in config.get("deny_read_paths", [])],
            extra_sbpl_rules=str(config.get("extra_sbpl_rules", "")),
            python_binary=str(config.get("python_binary", "python3")),
        )

        return {
            "handle_id": f"seatbelt:{uuid.uuid4().hex}",
            "resource": resource,
            "availability": availability,
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "macos",
                "network": resource.network,
            },
        }
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
SeatbeltExecutionResourceProvider — macOS Seatbelt sandbox backend.

Uses `sandbox-exec` with SBPL (Seatbelt Profile Language) to provide
kernel-level syscall filtering on macOS, as an alternative to Docker-based
isolation.

This provider is **only** available on macOS.  On other platforms it reports
itself as unavailable and the ExecutionResourceManager will refuse to create
handles for it.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import uuid
from typing import Any

from agently.core.operation.ExecutionResource import (
    ExecutionResource,
    ExecutionResourceProvider,
)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_macos() -> bool:
    """Return True when running on macOS (Darwin)."""
    return platform.system() == "Darwin"


def inspect_seatbelt_availability() -> dict[str, Any]:
    """Check whether macOS Seatbelt (sandbox-exec) is usable.

    Returns a dict with at least ``{"available": bool}``.  When unavailable
    the dict also contains a ``"reason"`` key.
    """
    if not is_macos():
        return {"available": False, "reason": "not_macos"}
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return {"available": False, "reason": "sandbox_exec_missing"}
    return {
        "available": True,
        "binary": binary,
        "platform": "macos",
    }


# ---------------------------------------------------------------------------
# SBPL profile generation
# ---------------------------------------------------------------------------

def _build_sbpl_profile(
    *,
    network: bool = False,
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
    extra_rules: str = "",
) -> str:
    """Generate an SBPL (Seatbelt Profile Language) profile string.

    Parameters
    ----------
    network:
        Allow network access when *True*.
    read_paths:
        Extra filesystem paths allowed for reading.
    write_paths:
        Extra filesystem paths allowed for writing.
    extra_rules:
        Raw SBPL rule text appended verbatim.
    """
    read_paths = read_paths or []
    write_paths = write_paths or []

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        "# Always allow basic process operations",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "",
        "# Filesystem — deny by default, selectively allow",
        "(allow file-read* (subpath \"/usr/lib\")",
        "                 (subpath \"/usr/share\")",
        "                 (subpath \"/Library/Frameworks\")",
        "                 (subpath \"/System/Library/Frameworks\"))",
    ]

    for p in read_paths:
        lines.append(f'(allow file-read* (subpath "{p}"))')

    for p in write_paths:
        lines.append(f'(allow file-write* (subpath "{p}"))')

    if network:
        lines.extend([
            "",
            "# Network access",
            "(allow network*)",
        ])
    else:
        lines.extend([
            "",
            "# Network denied (default)",
            "(deny network*)",
        ])

    if extra_rules.strip():
        lines.append("")
        lines.append("# Extra rules")
        lines.append(extra_rules.strip())

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SeatbeltExecutionResource
# ---------------------------------------------------------------------------

class SeatbeltExecutionResource(ExecutionResource):
    """Execute Python code inside a macOS Seatbelt sandbox."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        network: bool = False,
        read_paths: list[str] | None = None,
        write_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
        python_binary: str = "python3",
    ):
        self.timeout = timeout
        self.network = network
        self.read_paths = read_paths or []
        self.write_paths = write_paths or []
        self.extra_sbpl_rules = extra_sbpl_rules
        self.python_binary = python_binary

    # -- availability -------------------------------------------------------

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    # -- SBPL profile -------------------------------------------------------

    def build_profile(self) -> str:
        return _build_sbpl_profile(
            network=self.network,
            read_paths=self.read_paths,
            write_paths=self.write_paths,
            extra_rules=self.extra_sbpl_rules,
        )

    # -- execution ----------------------------------------------------------

    async def run_python_code(
        self,
        *,
        python_code: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        effective_timeout = timeout or self.timeout
        profile_text = self.build_profile()

        cmd = [
            "sandbox-exec", "-f", "-",
            self.python_binary, "-c", python_code,
        ]

        try:
            result = subprocess.run(
                cmd,
                input=profile_text,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"Seatbelt sandbox timed out after {effective_timeout}s",
                "stdout": "",
                "stderr": "",
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "sandbox-exec binary not found (macOS only)",
                "stdout": "",
                "stderr": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "stdout": "",
                "stderr": "",
            }

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


# ---------------------------------------------------------------------------
# SeatbeltExecutionResourceProvider
# ---------------------------------------------------------------------------

class SeatbeltExecutionResourceProvider(ExecutionResourceProvider):
    """Provider that creates Seatbelt-sandboxed execution resources.

    Registered with ``kind="seatbelt"``.  Only functional on macOS.
    """

    @property
    def name(self) -> str:
        return "seatbelt"

    @property
    def kind(self) -> str:
        return "seatbelt"

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    def create_handle(
        self,
        *,
        config: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        availability = inspect_seatbelt_availability()
        if not availability.get("available"):
            return {
                "handle_id": f"seatbelt:{uuid.uuid4().hex}",
                "resource": None,
                "availability": availability,
                "meta": {
                    "provider": self.name,
                    "available": False,
                    "reason": availability.get("reason", "unknown"),
                },
            }

        resource = SeatbeltExecutionResource(
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            network=bool(config.get("network", False)),
            read_paths=[str(p) for p in config.get("read_paths", [])],
            write_paths=[str(p) for p in config.get("write_paths", [])],
            extra_sbpl_rules=str(config.get("extra_sbpl_rules", "")),
            python_binary=str(config.get("python_binary", "python3")),
        )

        return {
            "handle_id": f"seatbelt:{uuid.uuid4().hex}",
            "resource": resource,
            "availability": availability,
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "macos",
                "network": resource.network,
            },
        }
