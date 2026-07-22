# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

import asyncio
import hashlib
import os
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


class TrustedLocalCodeExecutionResource:
    """Explicitly unsafe host-process runner bound to one Workspace grant."""

    unsafe = True

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
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
            raise PermissionError("Trusted local resource is bound to another Workspace grant.")
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
            completed = await run_bounded_process(
                self._step_argv(step=step, area=area),
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
            "unsafe": self.unsafe,
        }

    def _step_argv(self, *, step: Any, area: Path) -> list[str]:
        _ = area
        return list(step.argv)

    async def async_execute_code(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
        timeout: int,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Trusted local execution resource is closed.")
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


class TrustedLocalExecutionResourceProvider:
    name = "TrustedLocalExecutionResourceProvider"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    provider_id = "trusted_local"
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
            "python": ("python", ("--version",)),
            "nodejs": ("node", ("--version",)),
            "go": ("go", ("version",)),
            "cpp": ("c++", ("--version",)),
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
        facts = await asyncio.to_thread(self._tool_facts)
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
            "available": bool(languages),
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": languages,
                "toolchains": toolchains,
                "isolation": {
                    "process_contained": False,
                    "host_filesystem_restricted": False,
                    "privilege_escalation_blocked": False,
                    "syscalls_restricted": False,
                    "mechanism": "trusted_local",
                },
                "workspace_access_modes": ["snapshot"],
                "network": "inherited",
                "unsafe": True,
                "safety_class": "trusted_local",
            },
            "reason": "unsafe host toolchains available" if languages else "no host toolchains found",
            "meta": {"toolchains": facts},
        }

    async def async_ensure(self, *, requirement, policy, existing_handle=None):
        _ = existing_handle
        from agently.core import ExecutionResourceError

        config = requirement.get("config", {})
        if not isinstance(config, dict) or config.get("allow_unsafe_local") is not True:
            raise ExecutionResourceError(
                "Trusted local code execution is unsafe and requires host allow_unsafe_local=True.",
                code="execution_resource.trusted_local_not_allowed",
                payload={"provider_id": self.provider_id, "unsafe": True},
            )
        grant = requirement.get("task_workspace_access_grant")
        if not isinstance(grant, TaskWorkspaceAccessGrant):
            raise ExecutionResourceError(
                "Trusted local code execution requires a TaskWorkspace access grant.",
                code="execution_resource.workspace_grant_required",
                payload={"provider_id": self.provider_id},
            )
        if grant.mode != "snapshot":
            raise ExecutionResourceError(
                "Trusted local fallback only accepts snapshot Workspace grants.",
                code="execution_resource.trusted_local_snapshot_required",
                payload={"provider_id": self.provider_id, "grant_mode": grant.mode},
            )
        return {
            "handle_id": f"trusted-local:{uuid.uuid4().hex}",
            "resource": TrustedLocalCodeExecutionResource(
                grant=grant,
                max_output_bytes=int(policy.get("max_output_bytes", 20000)),
            ),
            "status": "ready",
            "meta": {"unsafe": True, "grant_id": grant.grant_id},
        }

    async def async_health_check(self, handle):
        return "ready" if isinstance(
            handle.get("resource"), TrustedLocalCodeExecutionResource
        ) else "unhealthy"

    async def async_release(self, handle) -> None:
        resource = handle.get("resource")
        if isinstance(resource, TrustedLocalCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "TrustedLocalCodeExecutionResource",
    "TrustedLocalExecutionResourceProvider",
]
