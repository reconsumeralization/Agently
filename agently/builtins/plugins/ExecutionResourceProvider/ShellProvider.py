"""Managed native/Docker Shell resources; no Action or approval owner here."""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from agently.types.data import ExecutionResourceHandle, ExecutionResourcePolicy, ExecutionResourceRequirement, ExecutionResourceStatus, ExecutionResourceProviderProbe
from agently.types.data.shell import ShellResult
from agently.types.plugins.ShellResource import ShellResource

from ._base import BuiltinExecutionResourceProvider
from ._bounded_process import run_bounded_process
from .DockerExecutionResourceProvider import DockerExecutionResource
from .Shell import BashExecutor, PowerShellExecutor
from ._windows_sandbox import WindowsSandbox


class _ShellResource:
    def __init__(self, config: dict[str, Any]) -> None:
        self.root = Path(config["root"]).resolve(strict=True)
        self.environment = config["environment"]
        self.language = config["shell"]
        self.timeout = float(config["timeout"])
        self.limit = int(config["max_output_bytes"])
        self.read_only = config["read_only"]
        self.read_paths = {name: Path(path).resolve(strict=True) for name, path in config["read_paths"].items()}
        self.env = dict(config["env"])
        self.image = config["docker_image"]
        self.executor = (
            PowerShellExecutor(config["binary"]) if self.language == "powershell" else BashExecutor(config["binary"])
        )
        self.docker: DockerExecutionResource | None = None
        self.windows: WindowsSandbox | None = None
        self.closed = False
        if self.environment != "host":
            if os.name == "nt":
                self.windows = WindowsSandbox(config)
                return
            self.docker = DockerExecutionResource(
                docker_binary=config["docker_binary"],
                timeout=math.ceil(self.timeout),
                max_output_bytes=self.limit,
                runtime_profile={
                    "network_mode": "disabled" if self.environment == "offline" else "bridge",
                    "provisioning_profile": config["provisioning_profile"],
                    "image_pull_policy": config["image_pull_policy"],
                },
            )

    def _workdir(self, workdir: str) -> tuple[Path, str]:
        if not isinstance(workdir, str) or "\0" in workdir:
            raise ValueError("workdir must be a NUL-free string")
        relative = workdir
        if self.windows is not None:
            from pathlib import PureWindowsPath
            selected = PureWindowsPath(workdir)
            if selected.is_absolute():
                relative = str(selected.relative_to(PureWindowsPath(r"C:\workspace")))
        elif self.environment != "host" and workdir.startswith("/workspace/"):
            relative = workdir[len("/workspace/"):]
        elif self.environment != "host" and workdir == "/workspace":
            relative = "."
        path = Path(relative)
        path = (path if path.is_absolute() else self.root / path).resolve(strict=True)
        # This bounds the selected starting directory, not host shell effects.
        suffix = path.relative_to(self.root)
        if not path.is_dir():
            raise ValueError("workdir must be a directory")
        if self.windows is not None:
            return path, str(Path(r"C:\workspace") / suffix)
        return path, "/workspace" if suffix == Path(".") else "/workspace/" + suffix.as_posix()

    async def async_run(self, command: str, *, workdir: str = ".") -> ShellResult:
        if self.closed:
            raise RuntimeError("Shell resource is closed")
        argv = self.executor.prepare(command)
        host_dir, container_dir = self._workdir(workdir)
        if self.windows is not None:
            return await self.windows.run(argv, container_dir)
        if self.docker is not None:
            mounts = [f"{self.root}:/workspace:{'ro' if self.read_only else 'rw'}"]
            mounts.extend(f"{path}:/skills/{name}:ro" for name, path in self.read_paths.items())
            result = await self.docker._run_container(
                image=self.image, cmd=argv, workdir=container_dir, extra_mounts=mounts,
                timeout=self.timeout, env=self.env,
            )
            if "returncode" not in result and result.get("status") != "timed_out":
                raise RuntimeError(str(result.get("reason") or result.get("error") or "Shell provider failed"))
            return {
                "ok": result.get("ok") is True,
                "returncode": int(result.get("returncode", 124)),
                "stdout": str(result.get("stdout", "")), "stderr": str(result.get("stderr", "")),
                "stdout_truncated": result.get("stdout_truncated") is True,
                "stderr_truncated": result.get("stderr_truncated") is True,
                "timed_out": result.get("status") == "timed_out",
            }
        # Deliberately exclude shell startup injection and unrelated credentials.
        env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL") if key in os.environ}
        env.update(self.env)
        result = await run_bounded_process(
            argv, cwd=str(host_dir), env=env, timeout=self.timeout, max_output_bytes=self.limit,
        )
        return {
            "ok": result.returncode == 0 and not result.timed_out,
            "returncode": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
            "stdout_truncated": result.stdout_truncated, "stderr_truncated": result.stderr_truncated,
            "timed_out": result.timed_out,
        }


class ShellProvider(BuiltinExecutionResourceProvider):
    """One-shot Shell transport. Substitute a provider for another isolation backend."""

    name = "ShellProvider"
    kind = "shell"
    DEFAULT_SETTINGS: dict[str, object] = {}

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    @staticmethod
    def _config(requirement: ExecutionResourceRequirement, policy: ExecutionResourcePolicy) -> dict[str, Any]:
        config = dict(requirement.get("config", {}))
        environment = config.get("environment")
        if environment not in {"offline", "online", "host"} or config.get("shell") not in {"bash", "powershell"}:
            raise ValueError("Invalid Shell environment or language")
        for key, policy_key in (("timeout", "timeout_seconds"), ("max_output_bytes", "max_output_bytes")):
            value = config.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Shell {key} must be finite and positive")
            if key == "max_output_bytes" and not isinstance(value, int):
                raise ValueError("Shell max_output_bytes must be an integer")
            bound = policy.get(policy_key)
            if bound is not None:
                if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound) or bound <= 0:
                    raise ValueError(f"Shell policy {policy_key} must be finite and positive")
                if key == "max_output_bytes" and not isinstance(bound, int):
                    raise ValueError("Shell policy max_output_bytes must be an integer")
                config[key] = min(value, bound)
        if policy.get("network_mode") == "disabled" and environment != "offline":
            raise PermissionError("Shell environment conflicts with disabled network policy")
        if any(policy.get(key) is False for key in ("allow_create", "allow_update", "allow_delete")):
            raise PermissionError("General Shell cannot enforce granular file-operation policy; use a restricted file Action")
        if policy.get("allowed_cmd_prefixes") is not None or policy.get("path_denylist"):
            raise PermissionError("General Shell cannot enforce argv prefixes or selective path deny rules")
        config["read_only"] = bool(config.get("read_only") or policy.get("read_only"))
        if environment == "host" and config["read_only"]:
            raise PermissionError("Host Shell cannot enforce read-only access")
        paths = [Path(config["root"]).resolve(strict=True)]
        for alias, raw in config.get("read_paths", {}).items():
            if not isinstance(alias, str) or not alias.isidentifier():
                raise ValueError("Shell resource aliases must be identifiers")
            paths.append(Path(raw).resolve(strict=True))
        for path in paths:
            if not path.is_dir() or path == Path(path.anchor):
                raise ValueError("Shell paths must be bounded directories")
            if environment != "host" and any(char in str(path) for char in (("\n", "\r", "%") if os.name == "nt" else (":", "\n", "\r"))):
                raise ValueError("Invalid Shell mount path")
        for key in ("task_workspace_roots", "path_allowlist"):
            allowed = policy.get(key)
            if allowed is None:
                continue
            if environment == "host":
                raise PermissionError("Host Shell cannot enforce filesystem allowlists")
            roots = [Path(value).resolve(strict=True) for value in allowed]
            if any(not any(path == root or root in path.parents for root in roots) for path in paths):
                raise PermissionError("Shell mount is outside the effective path allowlist")
        for key, value in config.get("env", {}).items():
            if not isinstance(key, str) or not key or "=" in key or "\0" in key or not isinstance(value, str) or "\0" in value:
                raise ValueError("Invalid Shell environment entry")
        return config

    async def async_probe(
        self, *, requirement: ExecutionResourceRequirement, policy: ExecutionResourcePolicy,
    ) -> ExecutionResourceProviderProbe:
        try:
            resource = _ShellResource(self._config(requirement, policy))
            if resource.docker is not None:
                availability = await asyncio.to_thread(resource.docker.inspect_availability)
                available, reason = bool(availability.get("available")), str(availability.get("reason", ""))
            elif resource.windows is not None:
                available, reason = True, "Windows Sandbox CLI present; sandbox startup is checked at execution"
            else:
                available = shutil.which(resource.executor.binary) is not None
                reason = "Native Shell interpreter available" if available else "Shell interpreter unavailable"
            isolated = resource.docker is not None or resource.windows is not None
            return {"provider_id": self.provider_id, "supported_kinds": ["shell"], "available": available,
                    "reason": reason, "capabilities": {"shell": resource.language, "isolation": {
                        "process_contained": isolated, "host_filesystem_restricted": isolated,
                        "network_mode": "disabled" if resource.environment == "offline" else "enabled",
                    }}}
        except (OSError, ValueError, RuntimeError) as error:
            return {"provider_id": self.provider_id, "supported_kinds": ["shell"], "available": False,
                    "capabilities": {}, "reason": str(error)}

    async def async_ensure(
        self, *, requirement: ExecutionResourceRequirement, policy: ExecutionResourcePolicy,
        existing_handle: ExecutionResourceHandle | None = None,
    ) -> ExecutionResourceHandle:
        resource = _ShellResource(self._config(requirement, policy))
        if resource.docker is not None:
            await asyncio.to_thread(resource.docker.ensure_available)
        elif resource.windows is None and shutil.which(resource.executor.binary) is None:
            raise FileNotFoundError(f"Shell interpreter is unavailable: {resource.executor.binary}")
        return {"handle_id": "shell:" + uuid.uuid4().hex, "resource": resource, "status": "ready", "meta": {
            "environment": resource.environment, "shell": resource.language,
            "isolated": resource.docker is not None or resource.windows is not None,
        }}

    async def async_health_check(self, handle: ExecutionResourceHandle) -> ExecutionResourceStatus:
        resource = handle.get("resource")
        return "ready" if isinstance(resource, ShellResource) and not getattr(resource, "closed", False) else "unhealthy"

    async def async_release(self, handle: ExecutionResourceHandle) -> None:
        resource = handle.get("resource")
        if isinstance(resource, _ShellResource):
            resource.closed = True
            if resource.docker is not None:
                await resource.docker.async_close()
