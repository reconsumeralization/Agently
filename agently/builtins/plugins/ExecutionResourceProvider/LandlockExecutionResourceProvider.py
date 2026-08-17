# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Grant-bound Linux Landlock code-execution provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceExecutionManifest,
    resolve_code_execution_workspace_uri,
)
from agently.types.data.code_execution import extract_code_toolchain_version

from .LandlockExecutionHelper import probe_abi_version
from ._bounded_process import run_bounded_process

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceProviderProbe,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


def is_linux() -> bool:
    return platform.system() == "Linux"


def _canonical_landlock_path(path: str) -> str:
    if path.startswith("/proc/"):
        return path
    return str(Path(path).resolve())


def _system_read_roots() -> list[str]:
    candidates = [
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/usr/local/lib",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/alternatives",
        "/etc/ssl",
        "/dev/null",
        "/dev/urandom",
        "/dev/zero",
        "/proc/self/exe",
        sys.executable,
    ]
    roots: list[str] = []
    for candidate in candidates:
        path = Path(candidate)
        if not path.exists():
            continue
        resolved = _canonical_landlock_path(candidate)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _toolchain_root(command: str) -> str | None:
    binary = shutil.which(command)
    if binary is None:
        return None
    path = Path(binary).resolve()
    if str(path).startswith("/usr/"):
        return "/usr"
    return str(path.parent.parent)


def _probe_manifest() -> dict[str, Any]:
    return {
        "version": 1,
        "rules": [{"path": path, "access": "read"} for path in _system_read_roots()],
    }


def inspect_landlock_availability() -> dict[str, Any]:
    """Probe ABI and enforcement through the standalone helper."""

    if not is_linux():
        return {"available": False, "reason": "not_linux"}
    abi = probe_abi_version()
    if abi <= 0:
        return {"available": False, "reason": "kernel_does_not_support_landlock"}
    helper = Path(__file__).with_name("LandlockExecutionHelper.py")
    try:
        with tempfile.TemporaryDirectory(prefix="agently-landlock-probe-") as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(_probe_manifest()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "--manifest",
                    str(manifest),
                    "--",
                    "/usr/bin/true",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                },
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "available": False,
            "reason": "landlock_enforcement_failed",
            "abi_version": abi,
            "error": str(error)[:300],
        }
    if completed.returncode != 0:
        return {
            "available": False,
            "reason": "landlock_enforcement_failed",
            "abi_version": abi,
            "returncode": completed.returncode,
            "stdout": completed.stdout[:300],
            "stderr": completed.stderr[:300],
        }
    return {
        "available": True,
        "reason": "ready",
        "platform": "linux",
        "abi_version": abi,
        "version": f"ABI v{abi}",
    }


class LandlockCodeExecutionResource:
    """Execute immutable bundles through a helper that self-applies Landlock."""

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self._active_executions: set[asyncio.Task[Any]] = set()
        self._manifest_paths: set[Path] = set()
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
            raise PermissionError("Landlock resource is bound to another Workspace grant.")
        if (
            manifest.grant_id != grant.grant_id
            or manifest.bundle_id != bundle.bundle_id
            or manifest.bundle_digest != bundle.bundle_digest
        ):
            raise PermissionError("Code execution manifest does not match the bound bundle and grant.")
        area = Path(grant.execution_area).resolve()
        recorded = {Path(item.host_path).resolve(): item for item in manifest.files}
        for item in bundle.files:
            target = (area / "source" / Path(item.path)).resolve()
            manifest_item = recorded.get(target)
            if (
                area not in target.parents
                or target.is_symlink()
                or not target.is_file()
                or manifest_item is None
                or manifest_item.sha256 != item.sha256
                or self._sha256(target) != item.sha256
            ):
                raise PermissionError("Materialized bundle file escaped or changed.")
        return area

    def _root_map(self) -> dict[str, str]:
        return {
            root.role: root.host_path
            for root in self.grant.roots
            if root.role in {"source", "build", "output", "logs"}
        }

    def _rule_manifest(self, *, argv: list[str], cwd: str) -> dict[str, Any]:
        rules: dict[str, str] = {}

        def add(path: str, access: str) -> None:
            candidate = Path(path)
            if not candidate.exists():
                return
            resolved = _canonical_landlock_path(path)
            if rules.get(resolved) == "write":
                return
            rules[resolved] = access

        for path in _system_read_roots():
            add(path, "read")
        if argv:
            toolchain_root = _toolchain_root(argv[0])
            if toolchain_root:
                add(toolchain_root, "read")
        add(cwd, "read")
        for root in self.grant.roots:
            add(root.host_path, "write" if root.access_mode == "read_write" else "read")
        return {
            "version": 1,
            "rules": [
                {"path": path, "access": access}
                for path, access in sorted(rules.items())
            ],
        }

    async def _run_with_landlock(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        logs_root = Path(self._root_map()["logs"])
        manifest = logs_root / f".landlock-{uuid.uuid4().hex}.json"
        manifest.write_text(
            json.dumps(self._rule_manifest(argv=argv, cwd=cwd), sort_keys=True),
            encoding="utf-8",
        )
        self._manifest_paths.add(manifest)
        helper = Path(__file__).with_name("LandlockExecutionHelper.py")
        try:
            completed = await run_bounded_process(
                [
                    sys.executable,
                    str(helper),
                    "--manifest",
                    str(manifest),
                    "--",
                    *argv,
                ],
                cwd=cwd,
                env=env,
                timeout=max(1, timeout),
                max_output_bytes=self.max_output_bytes,
            )
        finally:
            manifest.unlink(missing_ok=True)
            self._manifest_paths.discard(manifest)
        return {
            "ok": completed.returncode == 0,
            "status": "success" if completed.returncode == 0 else "error",
            "returncode": completed.returncode,
            "stdout": completed.stdout.decode("utf-8", errors="replace"),
            "stderr": completed.stderr.decode("utf-8", errors="replace"),
            "stdout_truncated": completed.stdout_truncated,
            "stderr_truncated": completed.stderr_truncated or completed.timed_out,
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
        roots = self._root_map()
        temp_root = roots.get("build") or roots.get("logs")
        final: dict[str, Any] = {
            "ok": False,
            "status": "error",
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        log_refs: list[str] = []
        for index, step in enumerate((*bundle.build_steps, bundle.run_step)):
            cwd = (area / Path(step.cwd)).resolve()
            if area not in cwd.parents or not cwd.is_dir() or cwd.is_symlink():
                raise PermissionError("Execution step cwd escaped its Workspace grant.")
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            if "LD_LIBRARY_PATH" in os.environ:
                environment["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
            if temp_root:
                environment.update(TMPDIR=temp_root, TMP=temp_root, TEMP=temp_root)
            environment.update(
                {
                    key: resolve_code_execution_workspace_uri(value, roots=roots)
                    for key, value in step.env.items()
                }
            )
            final = await self._run_with_landlock(
                list(step.argv),
                cwd=str(cwd),
                env=environment,
                timeout=timeout,
            )
            stdout_path = logs_root / f"{index:02d}-{step.role}.stdout.log"
            stderr_path = logs_root / f"{index:02d}-{step.role}.stderr.log"
            stdout_path.write_text(str(final["stdout"]), encoding="utf-8")
            stderr_path.write_text(str(final["stderr"]), encoding="utf-8")
            log_refs.extend([f"logs/{stdout_path.name}", f"logs/{stderr_path.name}"])
            if not final["ok"]:
                break
        final["outputs"] = [
            path
            for path in bundle.expected_outputs
            if (area / Path(path)).is_file() and not (area / Path(path)).is_symlink()
        ]
        final["log_refs"] = log_refs
        final["meta"] = {"mechanism": "landlock", "filesystem_only": True}
        return final

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
        area = self._validate_materialization(bundle=bundle, manifest=manifest, grant=grant)
        task = asyncio.current_task()
        if task is not None:
            self._active_executions.add(task)
        try:
            return await self._run(bundle=bundle, area=area, timeout=timeout)
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
        for manifest in tuple(self._manifest_paths):
            manifest.unlink(missing_ok=True)
            self._manifest_paths.discard(manifest)


class LandlockExecutionResourceProvider:
    name = "LandlockExecutionResourceProvider"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    provider_id = "landlock"
    supported_kinds = ("code_execution",)
    _allowed_config = {"dependency_policy"}

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    def _tool_facts(self) -> dict[str, dict[str, Any]]:
        commands = {
            "python": ("python3", ("--version",)),
            "nodejs": ("node", ("--version",)),
            "go": ("go", ("version",)),
            "cpp": ("c++", ("--version",)),
        }
        facts: dict[str, dict[str, Any]] = {}
        for language, (tool, command_args) in commands.items():
            binary = shutil.which(tool)
            fact: dict[str, Any] = {
                "tool": {"nodejs": "node", "cpp": "c++"}.get(language, language),
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
                    raw = str(completed.stdout or completed.stderr).strip()[:300]
                    fact.update(
                        available=completed.returncode == 0,
                        raw_version=raw,
                        version=extract_code_toolchain_version(raw),
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    fact.update(available=False, error=str(error)[:300])
            facts[language] = fact
        return facts

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> None:
        unknown = sorted(set(config).difference(LandlockExecutionResourceProvider._allowed_config))
        if unknown:
            from agently.core import ExecutionResourceError

            raise ExecutionResourceError(
                "Landlock provider configuration contains unsupported rule fields.",
                code="execution_resource.landlock_config_invalid",
                payload={"unsupported_fields": unknown},
            )

    def create_resource(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int,
    ) -> LandlockCodeExecutionResource:
        return LandlockCodeExecutionResource(grant=grant, max_output_bytes=max_output_bytes)

    async def async_probe(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
    ) -> "ExecutionResourceProviderProbe":
        _ = requirement, policy
        availability = await asyncio.to_thread(inspect_landlock_availability)
        available = bool(availability.get("available"))
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
        abi = int(availability.get("abi_version", 0))
        return {
            "provider_id": self.provider_id,
            "available": available and bool(languages),
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": languages,
                "toolchains": toolchains,
                "isolation": {
                    "process_contained": False,
                    "host_filesystem_restricted": True,
                    "privilege_escalation_blocked": True,
                    "syscalls_restricted": False,
                    "mechanism": "landlock",
                    "abi_version": abi,
                    "filesystem_only": True,
                },
                "workspace_access_modes": ["snapshot", "read_only", "read_write"],
                "network": "inherited",
                "safety_class": "filesystem_only",
            },
            "reason": "ready" if available and languages else str(availability.get("reason", "toolchain_unavailable")),
            "meta": {"availability": availability, "toolchains": facts},
        }

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        _ = existing_handle
        from agently.core import ExecutionResourceError

        config = requirement.get("config", {})
        config = dict(config) if isinstance(config, dict) else {}
        self._validate_config(config)
        grant = requirement.get("task_workspace_access_grant")
        if not isinstance(grant, TaskWorkspaceAccessGrant):
            raise ExecutionResourceError(
                "Landlock code execution requires a TaskWorkspace access grant.",
                code="execution_resource.workspace_grant_required",
                payload={"provider_id": self.provider_id},
            )
        availability = await asyncio.to_thread(inspect_landlock_availability)
        if not availability.get("available"):
            raise ExecutionResourceError(
                f"Landlock is unavailable: {availability.get('reason', 'unknown')}",
                code="execution_resource.landlock_unavailable",
                payload={"provider_id": self.provider_id, "availability": availability},
            )
        resource = self.create_resource(
            grant=grant,
            max_output_bytes=int(policy.get("max_output_bytes", 20000)),
        )
        try:
            verified = await asyncio.to_thread(inspect_landlock_availability)
            if not verified.get("available"):
                raise ExecutionResourceError(
                    "Landlock mechanism verification failed before handle readiness.",
                    code="execution_resource.landlock_unavailable",
                    payload={"provider_id": self.provider_id, "availability": verified},
                )
        except BaseException:
            await resource.async_close()
            raise
        return {
            "handle_id": f"landlock:{uuid.uuid4().hex}",
            "provider_id": self.provider_id,
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "mechanism_verified": True,
                "availability": verified,
                "abi_version": verified.get("abi_version", 0),
                "grant_id": grant.grant_id,
            },
        }

    async def async_health_check(
        self,
        handle: "ExecutionResourceHandle",
    ) -> "ExecutionResourceStatus":
        resource = handle.get("resource")
        meta = handle.get("meta")
        if (
            not isinstance(resource, LandlockCodeExecutionResource)
            or not isinstance(meta, dict)
            or not meta.get("mechanism_verified")
            or resource._closed
        ):
            return "unhealthy"
        availability = await asyncio.to_thread(inspect_landlock_availability)
        return "ready" if availability.get("available") else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        resource = handle.get("resource")
        if isinstance(resource, LandlockCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "LandlockCodeExecutionResource",
    "LandlockExecutionResourceProvider",
    "inspect_landlock_availability",
]
