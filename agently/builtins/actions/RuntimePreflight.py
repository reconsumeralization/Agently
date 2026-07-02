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

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


DEFAULT_CODE_RUNTIME_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "runtime_id": "python_current",
        "language": "Python",
        "commands": [sys.executable],
        "source_file": "reconcile.py",
        "run_commands": ["{command} reconcile.py"],
        "reason": "current Python interpreter",
    },
    {
        "runtime_id": "python3",
        "language": "Python",
        "commands": ["python3"],
        "source_file": "reconcile.py",
        "run_commands": ["python3 reconcile.py"],
    },
    {
        "runtime_id": "python",
        "language": "Python",
        "commands": ["python"],
        "source_file": "reconcile.py",
        "run_commands": ["python reconcile.py"],
    },
    {
        "runtime_id": "node",
        "language": "Node.js",
        "commands": ["node"],
        "source_file": "reconcile.js",
        "run_commands": ["node reconcile.js"],
    },
    {
        "runtime_id": "go",
        "language": "Go",
        "commands": ["go"],
        "source_file": "reconcile.go",
        "run_commands": ["go run reconcile.go"],
    },
    {
        "runtime_id": "gpp_cpp",
        "language": "C++",
        "commands": ["g++"],
        "source_file": "reconcile.cpp",
        "compile_commands": ["g++ reconcile.cpp -std=c++17 -O2 -o reconcile"],
        "run_commands": ["./reconcile"],
    },
    {
        "runtime_id": "clang_cpp",
        "language": "C++",
        "commands": ["clang++"],
        "source_file": "reconcile.cpp",
        "compile_commands": ["clang++ reconcile.cpp -std=c++17 -O2 -o reconcile"],
        "run_commands": ["./reconcile"],
    },
)


class RuntimePreflight:
    def __init__(
        self,
        *,
        candidates: Sequence[Mapping[str, Any]] | None = None,
        install_policy: str = "not_allowed",
        package_manager_policy: str = "not_allowed",
    ):
        self.candidates = [dict(candidate) for candidate in (candidates or DEFAULT_CODE_RUNTIME_CANDIDATES)]
        self.install_policy = str(install_policy or "not_allowed")
        self.package_manager_policy = str(package_manager_policy or "not_allowed")

    def register_actions(
        self,
        action,
        *,
        tags: str | list[str] | None = None,
        action_id: str = "inspect_code_runtimes",
        expose_to_model: bool = True,
        desc: str | None = None,
    ) -> list[str]:
        action.register_action(
            action_id=action_id,
            desc=desc
            or (
                "Inspect common local coding runtimes and compilers for Python, Node.js, Go, and C++ "
                "without installing software. Returns structured availability facts, source-file hints, "
                "and compile/run command templates. Installation and package-manager use are not allowed."
            ),
            kwargs={
                "include_unavailable": (bool, "Include unavailable runtime candidates. Default: True."),
            },
            func=self.inspect,
            tags=tags,
            side_effect_level="read",
            replay_safe=True,
            expose_to_model=expose_to_model,
            meta={
                "component": "builtins.actions.RuntimePreflight",
                "install_policy": self.install_policy,
                "package_manager_policy": self.package_manager_policy,
            },
        )
        return [action_id]

    def inspect(self, include_unavailable: bool = True) -> dict[str, Any]:
        inspected = [self._inspect_candidate(candidate) for candidate in self.candidates]
        visible = inspected if include_unavailable else [candidate for candidate in inspected if candidate["available"]]
        available = [candidate for candidate in inspected if candidate["available"]]
        payload = {
            "schema_version": "code_runtime_environment/v1",
            "install_policy": self.install_policy,
            "package_manager_policy": self.package_manager_policy,
            "candidate_order": [str(candidate.get("runtime_id") or "") for candidate in inspected],
            "available_runtime_ids": [str(candidate.get("runtime_id") or "") for candidate in available],
            "selected_runtime_hint": str(available[0].get("runtime_id") or "") if available else "",
            "candidates": visible,
            "notes": [
                "Use stdlib code unless the host explicitly enables package installation.",
                "Do not install runtimes, compilers, package managers, or third-party packages from this action.",
                "If no candidate is available, report environment_capability_gap instead of fabricating execution evidence.",
            ],
        }
        return {
            **payload,
            "ok": True,
            "success": True,
            "status": "success",
            "data": payload,
            "result": payload,
        }

    def _inspect_candidate(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        commands = self._normalize_strings(candidate.get("commands"))
        resolved_command = ""
        for command in commands:
            resolved_command = self._resolve_command(command)
            if resolved_command:
                break
        available = bool(resolved_command)
        source_file = str(candidate.get("source_file") or "")
        return {
            "runtime_id": str(candidate.get("runtime_id") or ""),
            "language": str(candidate.get("language") or ""),
            "available": available,
            "command": resolved_command,
            "source_file": source_file,
            "compile_commands": self._format_commands(candidate.get("compile_commands"), resolved_command),
            "run_commands": self._format_commands(candidate.get("run_commands"), resolved_command) if available else [],
            "reason": str(candidate.get("reason") or ("available" if available else "not found on PATH")),
        }

    def _resolve_command(self, command: str) -> str:
        value = str(command or "").strip()
        if not value:
            return ""
        path = Path(value).expanduser()
        if path.is_absolute() or os.sep in value:
            resolved = path.resolve()
            if resolved.is_file() and os.access(resolved, os.X_OK):
                return str(resolved)
            return ""
        return shutil.which(value) or ""

    def _format_commands(self, commands: Any, resolved_command: str) -> list[str]:
        formatted: list[str] = []
        for command in self._normalize_strings(commands):
            formatted.append(command.replace("{command}", resolved_command))
        return formatted

    def _normalize_strings(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return [str(item) for item in value if str(item or "").strip()]
        return []
