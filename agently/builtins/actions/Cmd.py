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


import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_SAFE_CMD_PREFIXES = [
    "pwd",
    "ls",
    "rg",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "date",
    "whoami",
    "git status",
    "git diff",
    "git log",
    "git show",
    "git rev-parse",
    "python -m pytest",
    "python -m pyright",
    "pytest",
]


class Cmd:
    def __init__(
        self,
        *,
        allowed_cmd_prefixes: Sequence[str] | None = None,
        allowed_workdir_roots: Iterable[str | Path] | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        max_output_chars: int = 20000,
        output_artifact_dir: str | Path | None = None,
    ):
        self.allowed_cmd_prefixes = set(
            allowed_cmd_prefixes
            if allowed_cmd_prefixes is not None
            else DEFAULT_SAFE_CMD_PREFIXES
        )
        self._allowed_cmd_prefix_tokens = [
            self._normalize_cmd(prefix)
            for prefix in self.allowed_cmd_prefixes
            if isinstance(prefix, str) and prefix.strip()
        ]
        # No implicit process-cwd boundary: a Workspace-bound shell must inject
        # the working directory through the direct Workspace root. Executors
        # must not invent a fallback cwd.
        roots = allowed_workdir_roots if allowed_workdir_roots is not None else []
        self.allowed_workdir_roots = [Path(root).resolve() for root in roots]
        self.timeout = timeout
        self.env = env
        self.max_output_chars = max(1, int(max_output_chars))
        self.output_artifact_dir = Path(output_artifact_dir).resolve() if output_artifact_dir is not None else None

    def register_actions(
        self,
        action,
        *,
        tags: str | list[str] | None = None,
        action_prefix: str = "",
        expose_to_model: bool = True,
        default_policy: dict | None = None,
    ) -> list[str]:
        prefix = action_prefix.strip()
        action_id = f"{ prefix }cmd" if prefix else "cmd"
        action.register_action(
            action_id=action_id,
            desc=(
                "Run a low-level allowlisted shell command with bounded stdout/stderr previews. "
                "Prefer `agent.enable_shell(...)` for user-facing shell access, and prefer "
                "Workspace file actions for reading, searching, editing, and writing files."
            ),
            kwargs={
                "cmd": ("str | list[str]", "Command to run."),
                "workdir": ("str | None", "Working directory."),
            },
            func=self.run,
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            approval_required=False,
            sandbox_required=False,
            expose_to_model=expose_to_model,
            meta={
                "component": "builtins.actions.Cmd",
                "legacy_tool_facade": "agently.builtins.tools.Cmd",
                "recommended_public_helper": "agent.enable_shell",
                "host_only_input_keys": ["allow_unsafe"],
            },
        )
        return [action_id]

    def _normalize_cmd(self, cmd: str | Sequence[str]) -> list[str]:
        if isinstance(cmd, str):
            return shlex.split(cmd)
        return list(cmd)

    def _is_cmd_allowed(self, args: list[str]) -> bool:
        if not args:
            return False
        base = Path(args[0]).name
        for prefix in self._allowed_cmd_prefix_tokens:
            if len(prefix) == 0:
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

    def _is_workdir_allowed(self, workdir: str | Path | None) -> bool:
        workdir_path = self._resolve_workdir(workdir)
        if workdir_path is None or not self.allowed_workdir_roots:
            return False
        for root in self.allowed_workdir_roots:
            try:
                workdir_path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _resolve_workdir(self, workdir: str | Path | None) -> Path | None:
        if workdir is not None:
            return Path(workdir).resolve()
        if self.allowed_workdir_roots:
            return self.allowed_workdir_roots[0]
        # No Workspace-issued boundary configured; do not fall back to cwd.
        return None

    async def run(
        self,
        cmd: str | Sequence[str],
        workdir: str | Path | None = None,
        allow_unsafe: bool = False,
    ) -> dict:
        args = self._normalize_cmd(cmd)
        workdir_path = self._resolve_workdir(workdir)
        if workdir_path is None:
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "workspace_boundary_required",
                "detail": (
                    "No Workspace-issued working directory. Bind a Workspace and enable a "
                    "Workspace-bound shell (agent.use_workspace(...) + agent.enable_shell(...)) so "
                    "the working directory is injected through the Workspace file boundary; "
                    "executors do not fall back to the process cwd."
                ),
                "diagnostics": [{"code": "shell.workspace_boundary_required"}],
            }
        if not self._is_workdir_allowed(workdir):
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "workdir_not_allowed",
                "workdir": str(workdir_path),
                "diagnostics": [{"code": "shell.workdir_not_allowed", "workdir": str(workdir_path)}],
            }
        if not self._is_cmd_allowed(args) and not allow_unsafe:
            return {
                "ok": False,
                "status": "blocked",
                "need_approval": True,
                "reason": "cmd_not_allowed",
                "cmd": args,
                "diagnostics": [{"code": "shell.cmd_not_allowed", "cmd": args}],
            }
        try:
            result = subprocess.run(
                args,
                cwd=str(workdir_path),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self.env,
            )
        except subprocess.TimeoutExpired as error:
            stdout, stdout_truncated, stdout_artifact = self._bounded_output("stdout", error.stdout or "")
            stderr, stderr_truncated, stderr_artifact = self._bounded_output("stderr", error.stderr or "")
            artifacts = [item for item in (stdout_artifact, stderr_artifact) if item is not None]
            return {
                "ok": False,
                "status": "timed_out",
                "reason": "command_timeout",
                "cmd": args,
                "timeout_seconds": self.timeout,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "output_artifacts": artifacts,
                "diagnostics": [
                    {
                        "code": "shell.command_timeout",
                        "timeout_seconds": self.timeout,
                        "cmd": args,
                    }
                ],
            }
        stdout, stdout_truncated, stdout_artifact = self._bounded_output("stdout", result.stdout)
        stderr, stderr_truncated, stderr_artifact = self._bounded_output("stderr", result.stderr)
        artifacts = [item for item in (stdout_artifact, stderr_artifact) if item is not None]
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "output_artifacts": artifacts,
            "diagnostics": [],
        }

    def _bounded_output(self, stream_name: str, value: str | bytes) -> tuple[str, bool, dict | None]:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        if len(text) <= self.max_output_chars:
            return text, False, None
        preview = text[: self.max_output_chars]
        artifact = self._write_output_artifact(stream_name, text)
        return preview, True, artifact

    def _write_output_artifact(self, stream_name: str, text: str) -> dict | None:
        if self.output_artifact_dir is None:
            return None
        self.output_artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_artifact_dir / f"{stream_name}-{uuid.uuid4().hex}.txt"
        path.write_text(text, encoding="utf-8")
        artifact: dict[str, str | int] = {
            "stream": stream_name,
            "path": str(path),
            "bytes": len(text.encode("utf-8")),
        }
        for root in self.allowed_workdir_roots:
            try:
                artifact["relative_path"] = str(path.relative_to(root))
                break
            except ValueError:
                continue
        return artifact
