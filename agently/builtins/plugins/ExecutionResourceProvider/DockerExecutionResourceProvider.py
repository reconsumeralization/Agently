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

import asyncio
import hashlib
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from agently.builtins.actions.Cmd import normalize_command_argv
from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceExecutionManifest,
    resolve_code_execution_workspace_uri,
)
from agently.types.data.code_execution import extract_code_toolchain_version

from ._base import BuiltinExecutionResourceProvider
from ._bounded_process import run_bounded_process

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )

DEFAULT_CODE_IMAGES: dict[str, str] = {
    "python": "python:3.12-slim",
    "nodejs": "node:22-slim",
    "go": "golang:1",
    "cpp": "gcc:14",
    # Shell is not a CodeRuntimeAdapter language. It remains a separate broad
    # Action and uses this provider-owned mechanism default only.
    "shell": "python:3.12-slim",
}

CODE_TOOLCHAIN_PROBE_COMMANDS: dict[str, tuple[str, ...]] = {
    "python": ("python", "--version"),
    "node": ("node", "--version"),
    "go": ("go", "version"),
    "c++": ("c++", "--version"),
}


def _canonical_code_language(language: str) -> str:
    from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter

    return str(get_code_runtime_adapter(language).language_id)


class DockerExecutionResource:
    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        timeout: int = 60,
        default_args: list[str] | None = None,
        runtime_profile: dict[str, Any] | None = None,
        workspace_grant: TaskWorkspaceAccessGrant | None = None,
        max_output_bytes: int = 20000,
    ):
        self.docker_binary = docker_binary
        self.timeout = timeout
        self.default_args = default_args or []
        self.runtime_profile = dict(runtime_profile or {})
        self.workspace_grant = workspace_grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self._prepared_images: dict[str, dict[str, Any]] = {}
        self._active_containers: set[str] = set()
        self._closed = False

    async def _remove_container(self, name: str) -> None:
        if not name:
            return
        process = await asyncio.create_subprocess_exec(
            self.docker_binary,
            "rm",
            "-f",
            name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(
                f"docker_runtime.container_cleanup_timeout:{name}"
            )
        if returncode != 0:
            raise RuntimeError(
                f"docker_runtime.container_cleanup_failed:{name}:returncode={returncode}"
            )
        self._active_containers.discard(name)

    async def async_close(self) -> None:
        self._closed = True
        for name in tuple(self._active_containers):
            await self._remove_container(name)

    @staticmethod
    def _sha256(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    def _validate_workspace_bundle(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
    ) -> Path:
        if self.workspace_grant is None or grant != self.workspace_grant:
            raise PermissionError("Docker code resource is bound to another Workspace grant.")
        if (
            manifest.grant_id != grant.grant_id
            or manifest.bundle_id != bundle.bundle_id
            or manifest.bundle_digest != bundle.bundle_digest
        ):
            raise PermissionError("Docker execution manifest does not match the bundle and grant.")
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
                raise PermissionError("Docker source file is not the exact materialized bundle byte set.")
        return area

    async def async_execute_code(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
        timeout: int,
    ) -> dict[str, Any]:
        area = self._validate_workspace_bundle(
            bundle=bundle,
            manifest=manifest,
            grant=grant,
        )
        profile = self._profile(
            {
                "language": bundle.language,
                "image": self.runtime_profile.get("image")
                or self._default_image(bundle.language),
            }
        )
        image = str(profile["image"])
        mounts: list[str] = []
        for root in grant.roots:
            if root.role == "workspace":
                container_path = "/task-workspace"
            else:
                container_path = f"/workspace/{root.role}"
            mount_mode = "ro" if root.access_mode == "read_only" else "rw"
            mounts.append(f"{root.host_path}:{container_path}:{mount_mode}")

        final: dict[str, Any] = {
            "ok": False,
            "status": "error",
            "returncode": 1,
            "stdout": "",
            "stderr": "",
        }
        container_roots = {
            "source": "/workspace/source",
            "build": "/workspace/build",
            "output": "/workspace/output",
            "logs": "/workspace/logs",
        }
        log_refs: list[str] = []
        for index, step in enumerate((*bundle.build_steps, bundle.run_step)):
            result = await self._run_container(
                image=image,
                cmd=list(step.argv),
                profile=profile,
                workdir=f"/workspace/{step.cwd}",
                env={
                    key: resolve_code_execution_workspace_uri(
                        value,
                        roots=container_roots,
                    )
                    for key, value in step.env.items()
                },
                timeout=timeout,
                extra_mounts=mounts,
            )
            stdout = str(result.get("stdout", ""))
            stderr = str(result.get("stderr", ""))
            stdout_path = area / "logs" / f"{index:02d}-{step.role}.stdout.log"
            stderr_path = area / "logs" / f"{index:02d}-{step.role}.stderr.log"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            log_refs.extend(
                [f"logs/{stdout_path.name}", f"logs/{stderr_path.name}"]
            )
            final = dict(result)
            final["stdout"] = stdout.encode("utf-8")[: self.max_output_bytes].decode(
                "utf-8", errors="replace"
            )
            final["stderr"] = stderr.encode("utf-8")[: self.max_output_bytes].decode(
                "utf-8", errors="replace"
            )
            final["stdout_truncated"] = bool(
                result.get("stdout_truncated")
            ) or len(stdout.encode("utf-8")) > self.max_output_bytes
            final["stderr_truncated"] = bool(
                result.get("stderr_truncated")
            ) or len(stderr.encode("utf-8")) > self.max_output_bytes
            if not result.get("ok"):
                break
        final["status"] = "success" if final.get("ok") else str(final.get("status") or "error")
        final["outputs"] = [
            path
            for path in bundle.expected_outputs
            if (area / Path(path)).is_file() and not (area / Path(path)).is_symlink()
        ]
        final["log_refs"] = log_refs
        return final

    async def inspect_toolchain(
        self,
        *,
        image: str,
        tool: str,
        profile: dict[str, Any],
        timeout: int = 10,
    ) -> dict[str, Any]:
        command = CODE_TOOLCHAIN_PROBE_COMMANDS.get(str(tool))
        if command is None:
            return {
                "available": False,
                "tool": str(tool),
                "version": "",
                "raw_version": "",
                "reason": "unsupported toolchain probe",
            }
        result = await self._run_container(
            image=image,
            cmd=list(command),
            profile=profile,
            timeout=max(1, int(timeout)),
        )
        raw_version = str(result.get("stdout") or result.get("stderr") or "").strip()[:300]
        return {
            "available": bool(result.get("ok")),
            "tool": str(tool),
            "version": extract_code_toolchain_version(raw_version),
            "raw_version": raw_version,
            "reason": "observed in runtime image" if result.get("ok") else "toolchain probe failed",
        }

    def is_binary_available(self):
        return shutil.which(self.docker_binary) is not None

    def inspect_availability(self) -> dict[str, Any]:
        if not self.is_binary_available():
            return {
                "available": False,
                "reason": "binary_missing",
                "docker_binary": self.docker_binary,
            }
        try:
            result = subprocess.run(
                [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=min(max(self.timeout, 1), 10),
            )
        except Exception as error:
            return {
                "available": False,
                "reason": "daemon_unavailable",
                "docker_binary": self.docker_binary,
                "error": str(error),
            }
        if result.returncode != 0:
            return {
                "available": False,
                "reason": "daemon_unavailable",
                "docker_binary": self.docker_binary,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        return {
            "available": True,
            "reason": "ready",
            "docker_binary": self.docker_binary,
            "server_version": result.stdout.strip(),
        }

    def is_available(self):
        return bool(self.inspect_availability().get("available", False))

    def ensure_available(self) -> dict[str, Any]:
        availability = self.inspect_availability()
        if availability.get("available"):
            return availability
        from agently.core import ExecutionResourceError

        reason = str(availability.get("reason", "docker_unavailable"))
        message = (
            f"Docker execution resource is unavailable: { reason }. "
            f"Check Docker binary '{ self.docker_binary }' and local Docker daemon status."
        )
        raise ExecutionResourceError(
            message,
            code="execution_resource.docker_unavailable",
            payload=availability,
        )

    @staticmethod
    def _normalize_dependency_policy(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            mode = str(value.get("mode", "deny")).strip().lower() or "deny"
            normalized = dict(value)
            normalized["mode"] = mode
            return normalized
        mode = str(value or "deny").strip().lower() or "deny"
        return {"mode": mode}

    @staticmethod
    def _normalize_provisioning_profile(value: Any) -> str:
        mode = str(value or "strict").strip().lower().replace("-", "_") or "strict"
        if mode not in {"strict", "developer", "ci"}:
            raise ValueError("provisioning_profile must be one of: 'strict', 'developer', 'ci'.")
        return mode

    @staticmethod
    def _normalize_image_pull_policy(value: Any, *, provisioning_profile: str) -> str:
        if value is None or str(value).strip() == "":
            return "if_missing" if provisioning_profile in {"developer", "ci"} else "never"
        mode = str(value).strip().lower().replace("-", "_")
        if mode not in {"never", "request", "if_missing", "always"}:
            raise ValueError("image_pull_policy must be one of: 'never', 'request', 'if_missing', 'always'.")
        return mode

    def _profile(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        profile = dict(self.runtime_profile)
        if overrides:
            profile.update(overrides)
        provisioning_profile = self._normalize_provisioning_profile(profile.get("provisioning_profile", "strict"))
        profile["provisioning_profile"] = provisioning_profile
        if "dependency_policy" in profile:
            dependency_policy = self._normalize_dependency_policy(profile.get("dependency_policy", "deny"))
        else:
            dependency_policy = {"mode": "install"} if provisioning_profile in {"developer", "ci"} else {"mode": "deny"}
        profile["dependency_policy"] = dependency_policy
        profile["image_pull_policy"] = self._normalize_image_pull_policy(
            profile.get("image_pull_policy", None),
            provisioning_profile=provisioning_profile,
        )
        if "network_mode" not in profile:
            profile["network_mode"] = (
                "bridge"
                if provisioning_profile in {"developer", "ci"} and dependency_policy.get("mode") == "install"
                else "disabled"
            )
        profile.setdefault("cpus", "1")
        profile.setdefault("memory", "512m")
        return profile

    @staticmethod
    def _default_image(language: str) -> str:
        canonical = "shell" if language == "shell" else _canonical_code_language(language)
        try:
            return DEFAULT_CODE_IMAGES[canonical]
        except KeyError as error:
            raise ValueError(
                f"Docker provider has no default image for code language {canonical!r}; "
                "configure runtime_profile.image explicitly."
            ) from error

    @staticmethod
    def _safe_relative_path(relative_path: str) -> Path:
        raw = str(relative_path).strip().replace("\\", "/")
        if not raw or raw.startswith("/") or raw.startswith("../") or "/../" in raw or raw == "..":
            raise ValueError(f"Unsafe sandbox file path: { relative_path }")
        path = Path(raw)
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"Unsafe sandbox file path: { relative_path }")
        return path

    def inspect_image(self, image: str) -> dict[str, Any]:
        result = subprocess.run(
            [self.docker_binary, "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=min(max(self.timeout, 1), 30),
        )
        return {
            "exists": result.returncode == 0,
            "image": image,
            "image_id": result.stdout.strip() if result.returncode == 0 else "",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def pull_image(self, image: str, *, timeout: int | None = None) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [self.docker_binary, "pull", image],
                capture_output=True,
                text=True,
                timeout=timeout or max(self.timeout, 300),
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", errors="replace") if isinstance(error.stdout, bytes) else str(error.stdout or "")
            stderr = error.stderr.decode("utf-8", errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
            return {
                "ok": False,
                "status": "timed_out",
                "image": image,
                "timeout_seconds": timeout or max(self.timeout, 300),
                "stdout": stdout,
                "stderr": stderr,
            }
        return {
            "ok": result.returncode == 0,
            "image": image,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def ensure_image_ready(self, image: str, *, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        active_profile = self._profile(profile)
        image_pull_policy = str(active_profile.get("image_pull_policy", "never"))
        cache_key = f"{ image }::{ image_pull_policy }"
        if image_pull_policy != "always" and cache_key in self._prepared_images:
            return self._prepared_images[cache_key]
        from agently.core import ExecutionResourceError

        first_inspect = self.inspect_image(image)
        if first_inspect.get("exists") and image_pull_policy != "always":
            prepared = {
                "status": "local",
                "image": image,
                "image_id": first_inspect.get("image_id", ""),
                "image_pull_policy": image_pull_policy,
            }
            self._prepared_images[cache_key] = prepared
            return prepared
        if image_pull_policy == "never":
            raise ExecutionResourceError(
                f"Docker image '{ image }' is not available locally and image_pull_policy='never'.",
                code="execution_resource.docker_image_missing",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "inspect": first_inspect,
                },
            )
        if image_pull_policy == "request":
            raise ExecutionResourceError(
                f"Docker image '{ image }' requires approval before pulling.",
                code="execution_resource.docker_image_pull_approval_required",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "inspect": first_inspect,
                },
            )
        pull_timeout = int(active_profile.get("image_pull_timeout_seconds", 300) or 300)
        pull = self.pull_image(image, timeout=pull_timeout)
        if not pull.get("ok"):
            raise ExecutionResourceError(
                f"Docker image pull failed for '{ image }'.",
                code="execution_resource.docker_image_pull_failed",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "pull": pull,
                    "inspect": first_inspect,
                },
            )
        second_inspect = self.inspect_image(image)
        if not second_inspect.get("exists"):
            raise ExecutionResourceError(
                f"Docker image '{ image }' was pulled but could not be inspected.",
                code="execution_resource.docker_image_pull_failed",
                payload={
                    "image": image,
                    "image_pull_policy": image_pull_policy,
                    "pull": pull,
                    "inspect": second_inspect,
                },
            )
        prepared = {
            "status": "pulled" if not first_inspect.get("exists") else "refreshed",
            "image": image,
            "image_id": second_inspect.get("image_id", ""),
            "image_pull_policy": image_pull_policy,
            "pull": pull,
        }
        self._prepared_images[cache_key] = prepared
        return prepared

    @staticmethod
    def _normalize_cmd(cmd: str | Sequence[str]) -> list[str]:
        return normalize_command_argv(cmd)

    @staticmethod
    def _cmd_allowed(args: list[str], allowed_cmd_prefixes: Sequence[str] | None) -> bool:
        if not args:
            return False
        allowed = list(allowed_cmd_prefixes or [])
        if not allowed:
            return False
        base = Path(args[0]).name
        for raw_prefix in allowed:
            prefix = shlex.split(str(raw_prefix))
            if not prefix:
                continue
            if len(prefix) == 1:
                if base == prefix[0] or args[0] == prefix[0]:
                    return True
                continue
            if len(args) < len(prefix):
                continue
            first_matches = base == prefix[0] or args[0] == prefix[0]
            if first_matches and args[1 : len(prefix)] == prefix[1:]:
                return True
        return False

    @staticmethod
    def _resolve_workdir(
        workdir: str | Path | None,
        allowed_workdir_roots: Sequence[str | Path] | None,
    ) -> tuple[Path | None, Path | None, str | None]:
        roots = [Path(root).expanduser().resolve() for root in (allowed_workdir_roots or [])]
        if workdir is not None:
            requested_workdir = Path(workdir).expanduser()
            if requested_workdir.is_absolute() or not roots:
                workdir_path = requested_workdir.resolve()
            else:
                workdir_path = (roots[0] / requested_workdir).resolve()
        elif roots:
            workdir_path = roots[0]
        else:
            return None, None, None
        for root in roots:
            try:
                relative = workdir_path.relative_to(root)
            except ValueError:
                continue
            container_workdir = "/workspace" if str(relative) == "." else f"/workspace/{ relative.as_posix() }"
            return workdir_path, root, container_workdir
        return workdir_path, None, None

    def _container_base_args(
        self,
        *,
        profile: dict[str, Any],
        workdir: str = "/sandbox",
        extra_mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        args = [
            self.docker_binary,
            "run",
            "--rm",
            *self.default_args,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
        ]
        network_mode = str(profile.get("network_mode", "disabled"))
        if network_mode == "disabled":
            args.extend(["--network", "none"])
        elif network_mode and network_mode not in {"inherit", "enabled"}:
            args.extend(["--network", network_mode])
        cpus = profile.get("cpus", "1")
        memory = profile.get("memory", "512m")
        if cpus is not None:
            args.extend(["--cpus", str(cpus)])
        if memory is not None:
            args.extend(["--memory", str(memory)])
        for mount in extra_mounts or []:
            args.extend(["-v", mount])
        args.extend(["-w", workdir])
        if isinstance(env, dict):
            for key, value in env.items():
                args.extend(["-e", f"{ key }={ value }"])
        return args

    async def _run_container(
        self,
        *,
        image: str,
        cmd: list[str],
        files: dict[str, str] | None = None,
        profile: dict[str, Any] | None = None,
        workdir: str = "/sandbox",
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        extra_mounts: list[str] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            return {"ok": False, "status": "error", "error": "Docker execution resource is closed."}
        if not image:
            return {"ok": False, "error": "Docker image is required."}
        if not self.is_binary_available():
            return {"ok": False, "error": f"Docker binary not found: { self.docker_binary }"}
        active_profile = self._profile(profile)
        await asyncio.to_thread(
            self.ensure_image_ready,
            image,
            profile=active_profile,
        )
        dependency_policy = self._normalize_dependency_policy(active_profile.get("dependency_policy", "deny"))
        if dependency_policy.get("mode") == "request":
            return {
                "ok": False,
                "status": "approval_required",
                "reason": "dependency_policy_requires_prepare_approval",
                "diagnostics": [{"code": "docker_runtime.dependency_policy_requires_prepare_approval"}],
            }
        with tempfile.TemporaryDirectory(prefix="agently-docker-runtime-") as temp_dir:
            temp_path = Path(temp_dir)
            for relative_path, content in (files or {}).items():
                target = temp_path / self._safe_relative_path(relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            mounts = [f"{ temp_path }:/sandbox:ro", *(extra_mounts or [])]
            args = self._container_base_args(
                profile=active_profile,
                workdir=workdir,
                extra_mounts=mounts,
                env=env,
            )
            container_name = f"agently-code-{uuid.uuid4().hex[:20]}"
            args.extend(["--name", container_name])
            args.append(image)
            args.extend(cmd)
            self._active_containers.add(container_name)
            completed = None
            try:
                completed = await run_bounded_process(
                    args,
                    timeout=float(timeout or self.timeout),
                    max_output_bytes=self.max_output_bytes,
                    on_terminate=lambda: self._remove_container(container_name),
                )
            finally:
                # A normally exited ``docker run --rm`` has already removed
                # its container. Timeout/cancellation cleanup owns removal and
                # must leave a failed target registered so ``async_close`` can
                # retry instead of silently losing lifecycle ownership.
                if completed is not None and not completed.timed_out:
                    self._active_containers.discard(container_name)
            if completed is None:
                # ``run_bounded_process`` raises on every path that does not
                # return a result. Keep the invariant explicit for static
                # consumers and for any future runner implementation that
                # might violate that contract.
                raise RuntimeError(
                    "docker_runtime.container_result_unavailable"
                )
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
            if completed.timed_out:
                return {
                    "ok": False,
                    "status": "timed_out",
                    "reason": "container_timeout",
                    "timeout_seconds": timeout or self.timeout,
                    "stdout": stdout,
                    "stderr": stderr,
                    "diagnostics": [
                        {
                            "code": "docker_runtime.container_timeout",
                            "timeout_seconds": timeout or self.timeout,
                        }
                    ],
                    "stdout_truncated": completed.stdout_truncated,
                    "stderr_truncated": completed.stderr_truncated,
                }
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": completed.stdout_truncated,
            "stderr_truncated": completed.stderr_truncated,
            "diagnostics": [],
        }

    async def run_shell_command(
        self,
        *,
        cmd: str | Sequence[str],
        workdir: str | Path | None = None,
        allow_unsafe: bool = False,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        profile = self._profile({"language": "shell"})
        args = self._normalize_cmd(cmd)
        allowed_cmd_prefixes = profile.get("allowed_cmd_prefixes")
        allowed_roots = profile.get("allowed_workdir_roots")
        workdir_path, root, container_workdir = self._resolve_workdir(workdir, allowed_roots)
        if workdir_path is None:
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "task_workspace_boundary_required",
                "diagnostics": [{"code": "shell.task_workspace_boundary_required"}],
            }
        if root is None or container_workdir is None:
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "workdir_not_allowed",
                "workdir": str(workdir_path),
                "diagnostics": [{"code": "shell.workdir_not_allowed", "workdir": str(workdir_path)}],
            }
        if not allow_unsafe and not self._cmd_allowed(args, allowed_cmd_prefixes):
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "cmd_not_allowed",
                "cmd": args,
                "diagnostics": [{"code": "shell.cmd_not_allowed", "cmd": args}],
            }
        declared_mounts = profile.get("task_workspace_mounts")
        extra_mounts: list[str] = []
        if isinstance(declared_mounts, list) and declared_mounts:
            mounted_workdirs: list[tuple[int, str]] = []
            for item in declared_mounts:
                if not isinstance(item, dict):
                    raise ValueError("Docker TaskWorkspace mount entries must be mappings.")
                host_path = Path(str(item.get("host_path") or "")).expanduser().resolve()
                container_path = str(item.get("container_path") or "").strip()
                mode = str(item.get("mode") or "ro").strip().lower()
                if not container_path.startswith("/") or ":" in container_path:
                    raise ValueError("Docker TaskWorkspace mount container_path must be an absolute container path.")
                if mode not in {"ro", "rw"}:
                    raise ValueError("Docker TaskWorkspace mount mode must be 'ro' or 'rw'.")
                if mode == "rw":
                    host_path.mkdir(parents=True, exist_ok=True)
                elif not host_path.exists():
                    raise FileNotFoundError(f"Read-only Docker TaskWorkspace mount does not exist: {host_path}")
                extra_mounts.append(f"{host_path}:{container_path}:{mode}")
                try:
                    relative_workdir = workdir_path.relative_to(host_path)
                except ValueError:
                    continue
                mounted_workdir = container_path.rstrip("/") or "/"
                if str(relative_workdir) != ".":
                    mounted_workdir = (
                        f"{mounted_workdir.rstrip('/')}/{relative_workdir.as_posix()}"
                    )
                mounted_workdirs.append((len(host_path.parts), mounted_workdir))
            if not mounted_workdirs:
                return {
                    "ok": False,
                    "status": "blocked",
                    "need_approval": True,
                    "reason": "workdir_not_mounted",
                    "workdir": str(workdir_path),
                    "diagnostics": [
                        {
                            "code": "shell.workdir_not_mounted",
                            "workdir": str(workdir_path),
                        }
                    ],
                }
            container_workdir = max(mounted_workdirs, key=lambda item: item[0])[1]
        else:
            extra_mounts = [f"{root}:/workspace:rw"]
        image = str(profile.get("image") or self._default_image("shell"))
        return await self._run_container(
            image=image,
            cmd=args,
            profile=profile,
            workdir=container_workdir,
            timeout=timeout,
            extra_mounts=extra_mounts,
            env=profile.get("env") if isinstance(profile.get("env"), dict) else None,
        )

    async def run(
        self,
        *,
        image: str,
        cmd: str | list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ):
        if not image:
            return {"ok": False, "error": "Docker image is required."}
        if not self.is_binary_available():
            return {"ok": False, "error": f"Docker binary not found: { self.docker_binary }"}
        args = [self.docker_binary, "run", "--rm", *self.default_args]
        if workdir:
            args.extend(["-w", str(workdir)])
        if isinstance(env, dict):
            for key, value in env.items():
                args.extend(["-e", f"{ key }={ value }"])
        args.append(image)
        if isinstance(cmd, str):
            args.extend(shlex.split(cmd))
        else:
            args.extend([str(item) for item in cmd])
        result = await asyncio.to_thread(
            subprocess.run,
            args,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


class DockerExecutionResourceProvider(BuiltinExecutionResourceProvider):
    name = "DockerExecutionResourceProvider"
    DEFAULT_SETTINGS = {}
    kind = "docker"

    @property
    def provider_id(self) -> str:
        return "docker"

    @property
    def supported_kinds(self) -> tuple[str, ...]:
        return ("docker", "code_execution")

    def create_resource(
        self,
        *,
        docker_binary: str,
        timeout: int,
        default_args: Sequence[str] = (),
        runtime_profile: dict[str, Any] | None = None,
        workspace_grant: TaskWorkspaceAccessGrant | None = None,
        max_output_bytes: int = 20000,
    ) -> DockerExecutionResource:
        """Construct the provider-owned resource used by probe and ensure.

        Container-runtime contributors can override this one factory and keep
        Docker's grant, image, lifecycle, health and cleanup implementation.
        Runtime-specific probing and command behavior remain owned by their
        provider/resource subclass; the base does not learn a sandbox enum or
        mechanism-specific command.
        """

        return DockerExecutionResource(
            docker_binary=docker_binary,
            timeout=timeout,
            default_args=list(default_args),
            runtime_profile=runtime_profile,
            workspace_grant=workspace_grant,
            max_output_bytes=max_output_bytes,
        )

    @staticmethod
    def _isolation_capabilities(default_args: Sequence[str]) -> dict[str, Any]:
        normalized = [str(item).strip().lower() for item in default_args]
        unsafe_exact = {
            "--privileged",
            "--pid=host",
            "--userns=host",
            "--network=host",
            "--net=host",
        }
        unsafe_prefixes = (
            "--cap-add",
            "--device",
            "--mount",
            "--volume",
            "--security-opt=seccomp=unconfined",
        )
        unsafe_split_options = {
            ("--pid", "host"),
            ("--userns", "host"),
            ("--network", "host"),
            ("--net", "host"),
            ("--security-opt", "seccomp=unconfined"),
        }
        privileged_override = any(
            item == "--privileged"
            or (
                item.startswith("--privileged=")
                and item.partition("=")[2] not in {"false", "0", "no"}
            )
            for item in normalized
        )
        split_overrides = {
            (item, normalized[index + 1])
            for index, item in enumerate(normalized[:-1])
        }
        has_unsafe_override = any(
            item in unsafe_exact
            or item in {"-v", "--volume", "--mount", "--device", "--cap-add"}
            or item.startswith("-v=")
            or item.startswith(unsafe_prefixes)
            for item in normalized
        ) or privileged_override or bool(
            split_overrides.intersection(unsafe_split_options)
        )
        return {
            "process_contained": not has_unsafe_override,
            "host_filesystem_restricted": not has_unsafe_override,
            "privilege_escalation_blocked": not has_unsafe_override,
            "syscalls_restricted": not has_unsafe_override,
            "mechanism": "container",
            "container_rootfs_read_only": False,
        }

    async def async_probe(self, *, requirement, policy):
        config = requirement.get("config", {})
        config = config if isinstance(config, dict) else {}
        default_args = config.get("default_args", [])
        default_args = default_args if isinstance(default_args, list) else []
        runtime_profile = config.get("runtime_profile", {})
        runtime_profile = runtime_profile if isinstance(runtime_profile, dict) else {}
        resource = self.create_resource(
            docker_binary=str(config.get("docker_binary", "docker")),
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            default_args=[str(item) for item in default_args],
            runtime_profile=runtime_profile,
        )
        availability = await asyncio.to_thread(resource.inspect_availability)
        available = bool(availability.get("available"))
        reason = str(availability.get("reason", "unavailable"))
        image_fact: dict[str, Any] = {}
        toolchain_facts: dict[str, dict[str, Any]] = {}
        if available and str(requirement.get("kind", "")) == "code_execution":
            required = requirement.get("required_capabilities", {})
            required = required if isinstance(required, dict) else {}
            language = _canonical_code_language(
                str(required.get("language") or runtime_profile.get("language") or "python")
            )
            profile = resource._profile(
                {
                    "language": language,
                    "image": resource._default_image(language),
                    **runtime_profile,
                }
            )
            image = str(profile["image"])
            inspected = await asyncio.to_thread(resource.inspect_image, image)
            pull_policy = str(profile.get("image_pull_policy", "never"))
            image_fact = {
                "image": image,
                "exists": bool(inspected.get("exists")),
                "image_pull_policy": pull_policy,
            }
            if not inspected.get("exists") and pull_policy in {"never", "request"}:
                available = False
                reason = "required runtime image is not locally available"
            required_toolchains = required.get("toolchains", {})
            if available and isinstance(required_toolchains, dict):
                for tool in required_toolchains:
                    fact = await resource.inspect_toolchain(
                        image=image,
                        tool=str(tool),
                        profile=profile,
                        timeout=min(int(policy.get("timeout_seconds", 10)), 10),
                    )
                    toolchain_facts[str(tool)] = fact
                    if not fact.get("available"):
                        available = False
                        reason = f"required toolchain is unavailable in runtime image: {tool}"
        return {
            "provider_id": self.provider_id,
            "available": available,
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": ["python", "nodejs", "go", "cpp"],
                "toolchains": toolchain_facts,
                "isolation": {
                    **self._isolation_capabilities(default_args),
                    "network_mode": str(
                        runtime_profile.get("network_mode", "disabled")
                    ),
                },
                "workspace_access_modes": ["snapshot", "read_only", "read_write"],
                "network": "configurable",
                "safety_class": "isolated",
                "container_runtime": "runc",
            },
            "reason": reason,
            "meta": {"availability": availability, "runtime_image": image_fact},
        }

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        _ = existing_handle
        config = requirement.get("config", {})
        default_args = config.get("default_args", [])
        if not isinstance(default_args, list):
            default_args = []
        runtime_profile = config.get("runtime_profile", {})
        if not isinstance(runtime_profile, dict):
            runtime_profile = {}
        grant = requirement.get("task_workspace_access_grant")
        if str(requirement.get("kind", "")) == "code_execution":
            from agently.core import ExecutionResourceError

            if not isinstance(grant, TaskWorkspaceAccessGrant):
                raise ExecutionResourceError(
                    "Docker code execution requires a TaskWorkspace access grant.",
                    code="execution_resource.workspace_grant_required",
                    payload={"provider_id": self.provider_id},
                )
            required = requirement.get("required_capabilities", {})
            required = required if isinstance(required, dict) else {}
            language = _canonical_code_language(
                str(required.get("language") or runtime_profile.get("language") or "python")
            )
            runtime_profile = {
                "language": language,
                "image": DockerExecutionResource._default_image(language),
                **runtime_profile,
            }
        resource = self.create_resource(
            docker_binary=str(config.get("docker_binary", "docker")),
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            default_args=[str(item) for item in default_args],
            runtime_profile=runtime_profile,
            workspace_grant=grant if isinstance(grant, TaskWorkspaceAccessGrant) else None,
            max_output_bytes=int(policy.get("max_output_bytes", 20000)),
        )
        availability = await asyncio.to_thread(resource.ensure_available)
        active_profile = resource._profile()
        image_preparation: dict[str, Any] | None = None
        image = str(active_profile.get("image", ""))
        if image:
            image_preparation = await asyncio.to_thread(
                resource.ensure_image_ready,
                image,
                profile=active_profile,
            )
        return {
            "handle_id": f"docker:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "docker_binary": resource.docker_binary,
                "available": True,
                "availability": availability,
                "runtime_profile": active_profile,
                "image_preparation": image_preparation,
            },
        }

    async def async_health_check(self, handle: "ExecutionResourceHandle") -> "ExecutionResourceStatus":
        resource = handle.get("resource")
        if not isinstance(resource, DockerExecutionResource):
            return "unhealthy"
        availability = await asyncio.to_thread(resource.inspect_availability)
        return "ready" if availability.get("available") else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        resource = handle.get("resource")
        if isinstance(resource, DockerExecutionResource):
            await resource.async_close()
