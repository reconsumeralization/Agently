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

def _realpath(path: str) -> str:
    """Resolve symbolic links to prevent subpath bypass attacks."""
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return path


def _get_tmpdir() -> str:
    """Get the actual temporary directory on macOS."""
    return os.environ.get("TMPDIR", "/tmp")


def _build_sbpl_profile(
    *,
    network: bool = False,
    writable_paths: list[str] | None = None,
    protected_paths: list[str] | None = None,
    deny_read_paths: list[str] | None = None,
    extra_rules: str = "",
) -> str:
    """Generate an SBPL profile following last-match-wins design.

    - File read: globally allowed (system libs needed for command execution)
    - File write: whitelist only (writable_paths + temp dirs)
    - Protected paths: deny write (after allow, last-match-wins)
    - Deny-read paths: deny both read and write
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
        "(allow mach*)",
        "(allow ipc-posix*)",
        "",
        ";; ═══ File read: globally allowed ═══",
        "(allow file-read*)",
        "",
        ";; ═══ File write: whitelist only ═══",
    ]

    for p in writable_paths:
        lines.append(f'(allow file-write* (subpath "{p}"))')

    lines.extend([
        "",
        ";; Temporary directories (always writable)",
        '(allow file-write* (subpath "/private/tmp"))',
        f'(allow file-write* (subpath "{tmpdir}"))',
        "",
        ";; Device files + PTY",
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-write* (regex #"^/dev/ttys[0-9]+$"))',
        '(allow file-write* (literal "/dev/ptmx"))',
        "(allow pseudo-tty)",
    ])

    if protected_paths:
        lines.append("")
        lines.append(";; Protected paths: deny write (overrides allow)")
        for p in protected_paths:
            lines.append(f'(deny file-write* (subpath "{p}"))')

    if deny_read_paths:
        lines.append("")
        lines.append(";; Deny-read paths: deny both read and write")
        for p in deny_read_paths:
            lines.append(f'(deny file-read* (subpath "{p}"))')
            lines.append(f'(deny file-write* (subpath "{p}"))')

    lines.append("")
    if network:
        lines.append(";; Network: allow outbound")
        lines.append("(allow network-outbound)")
    else:
        lines.append(";; Network: completely denied")
        lines.append("(deny network-outbound)")

    if extra_rules.strip():
        lines.append("")
        lines.append(";; Extra rules")
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
        writable_paths: list[str] | None = None,
        protected_paths: list[str] | None = None,
        deny_read_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
        python_binary: str = "python3",
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self.network = network
        self.writable_paths = [str(p) for p in (writable_paths or [])]
        self.protected_paths = [str(p) for p in (protected_paths or [])]
        self.deny_read_paths = [str(p) for p in (deny_read_paths or [])]
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
        all_writable = list(self.writable_paths)
        all_protected = list(self.protected_paths)
        all_deny_read = list(self.deny_read_paths)
        for root in self.grant.roots:
            if root.access_mode == "read_write":
                all_writable.append(root.host_path)
            else:
                all_deny_read.append(root.host_path)
        return _build_sbpl_profile(
            network=self.network,
            writable_paths=all_writable,
            protected_paths=all_protected,
            deny_read_paths=all_deny_read,
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
                writable_paths=[str(p) for p in config.get("writable_paths", [])],
                protected_paths=[str(p) for p in config.get("protected_paths", [])],
                deny_read_paths=[str(p) for p in config.get("deny_read_paths", [])],
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
