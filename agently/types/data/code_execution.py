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

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal
from typing_extensions import TypedDict


CodeExecutionFileRole = Literal["source", "dependency", "input"]
CodeExecutionStepRole = Literal["build", "run"]


class CodeExecutionIsolationCapability(TypedDict, total=False):
    process_contained: bool
    host_filesystem_restricted: bool
    privilege_escalation_blocked: bool
    syscalls_restricted: bool
    mechanism: str
    network_mode: str
    container_rootfs_read_only: bool


def required_code_execution_isolation() -> dict[str, bool]:
    return {
        "process_contained": True,
        "host_filesystem_restricted": True,
        "privilege_escalation_blocked": True,
        "syscalls_restricted": True,
    }


class CodeExecutionProviderCapability(TypedDict, total=False):
    languages: list[str]
    toolchains: dict[str, Any]
    isolation: CodeExecutionIsolationCapability
    workspace_access_modes: list[Literal["snapshot", "read_only", "read_write"]]
    network: str
    safety_class: Literal["isolated", "constrained", "trusted_local"]
    build_support: bool
    output_collection: bool
    mechanism: str


class CodeExecutionResult(TypedDict, total=False):
    ok: bool
    status: Literal["success", "error", "timed_out", "blocked"] | str
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    outputs: list[str]
    log_refs: list[str]
    unsafe: bool
    diagnostics: list[dict[str, Any]]
    meta: dict[str, Any]

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_FILES = 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_MAX_ARGV = 256
_MAX_ARG_BYTES = 8192
_MAX_ENV = 128
_MAX_PATH_BYTES = 4096
_MAX_EXPECTED_OUTPUTS = 128
_WORKSPACE_URI_PREFIX = "workspace://"
_WORKSPACE_ROLES = frozenset({"source", "build", "output", "logs"})


def extract_code_toolchain_version(value: str) -> str:
    """Return the first numeric dotted version from observed tool output."""

    match = re.search(r"(?<![0-9])([0-9]+(?:\.[0-9]+)+|[0-9]+)(?![0-9])", str(value))
    return match.group(1) if match is not None else ""


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _normalize_relative_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} path must be a string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{field_name} path must not be empty")
    if len(normalized.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError(f"{field_name} path exceeds the size limit")
    if "\\" in normalized:
        raise ValueError(f"{field_name} path must use POSIX separators")
    path = PurePosixPath(normalized)
    parts = path.parts
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} path must be normalized and relative")
    if not parts or parts[0].casefold() == ".agently":
        raise ValueError(f"{field_name} path must not address private .agently data")
    return path.as_posix()


def _normalize_expected_outputs(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("expected_outputs must be a bounded sequence of paths")
    if len(values) > _MAX_EXPECTED_OUTPUTS:
        raise ValueError(
            f"expected outputs must contain no more than {_MAX_EXPECTED_OUTPUTS} entries"
        )
    normalized: list[str] = []
    folded_paths: set[str] = set()
    for value in values:
        path = _normalize_relative_path(value, field_name="expected output")
        if PurePosixPath(path).parts[0] != "output":
            raise ValueError("expected output paths must be located under output/")
        folded = path.casefold()
        if folded in folded_paths:
            raise ValueError(f"duplicate or case-collision expected output: {path!r}")
        folded_paths.add(folded)
        normalized.append(path)
    return tuple(normalized)


def resolve_code_execution_workspace_uri(
    value: str,
    *,
    roots: Mapping[str, str],
) -> str:
    if not isinstance(value, str):
        raise TypeError("code execution Workspace URI value must be a string")
    if not value.startswith(_WORKSPACE_URI_PREFIX):
        return value
    raw_path = value.removeprefix(_WORKSPACE_URI_PREFIX)
    logical_path = PurePosixPath(raw_path)
    if (
        logical_path.is_absolute()
        or len(logical_path.parts) < 1
        or any(part in {"", ".", ".."} for part in logical_path.parts)
    ):
        raise ValueError("code execution Workspace URI path must be normalized and relative")
    role = logical_path.parts[0]
    if role not in _WORKSPACE_ROLES or role not in roots:
        raise ValueError(f"code execution Workspace URI role has no provider root: {role!r}")
    resolved = Path(str(roots[role]))
    for part in logical_path.parts[1:]:
        resolved = resolved / part
    return str(resolved)


def _freeze_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze_json_value(item)
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            }
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json_value(item) for item in value)
    raise TypeError("provenance values must be JSON-compatible")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class CodeExecutionFile:
    path: str
    content: bytes
    role: CodeExecutionFileRole = "source"
    sha256: str = ""

    def __post_init__(self) -> None:
        path = _normalize_relative_path(self.path, field_name="file")
        if self.role not in {"source", "dependency", "input"}:
            raise ValueError(f"unsupported file role: {self.role!r}")
        if not isinstance(self.content, bytes):
            raise TypeError("file content must be bytes")
        if len(self.content) > _MAX_FILE_BYTES:
            raise ValueError("file content exceeds the per-file size limit")
        digest = _digest(self.content)
        if self.sha256 and (not _SHA256_PATTERN.fullmatch(self.sha256) or self.sha256 != digest):
            raise ValueError("file sha256 does not match content")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "sha256", digest)


@dataclass(frozen=True)
class CodeExecutionRequest:
    language: str
    source_code: bytes | None
    files: tuple[CodeExecutionFile, ...]
    entrypoint: str | None
    args: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    provenance: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        language: str,
        source_code: str | bytes | None = None,
        files: Mapping[str, str | bytes] | None = None,
        entrypoint: str | None = None,
        args: Sequence[str] = (),
        expected_outputs: Sequence[str] = (),
        provenance: Mapping[str, Any] | None = None,
    ) -> "CodeExecutionRequest":
        canonical_language = language.strip().casefold() if isinstance(language, str) else ""
        if not canonical_language:
            raise ValueError("language is required")
        if source_code is not None and not isinstance(source_code, (str, bytes)):
            raise TypeError("source_code must be text or bytes")
        encoded_source = source_code.encode("utf-8") if isinstance(source_code, str) else source_code
        request_files: list[CodeExecutionFile] = []
        for path, content in dict(files or {}).items():
            if not isinstance(content, (str, bytes)):
                raise TypeError("request file values must be text or bytes")
            data = content.encode("utf-8") if isinstance(content, str) else content
            if b"\x00" in data:
                raise ValueError("request files must not contain unsupported NUL bytes")
            request_files.append(CodeExecutionFile(path=str(path), content=data, role="source"))
        if encoded_source is None and not request_files:
            raise ValueError("source_code or files are required")
        if encoded_source is not None and b"\x00" in encoded_source:
            raise ValueError("source_code must not contain unsupported NUL bytes")
        if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
            raise TypeError("args must be a bounded sequence of strings")
        frozen_args = tuple(args)
        if len(frozen_args) > _MAX_ARGV:
            raise ValueError(f"args must contain no more than {_MAX_ARGV} entries")
        for argument in frozen_args:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ValueError("args entries must be strings without NUL bytes")
            if len(argument.encode("utf-8")) > _MAX_ARG_BYTES:
                raise ValueError("args entry exceeds the size limit")
        canonical_entrypoint = (
            _normalize_relative_path(entrypoint, field_name="entrypoint")
            if entrypoint is not None
            else None
        )
        frozen_outputs = _normalize_expected_outputs(expected_outputs)
        return cls(
            language=canonical_language,
            source_code=encoded_source,
            files=tuple(request_files),
            entrypoint=canonical_entrypoint,
            args=frozen_args,
            expected_outputs=frozen_outputs,
            provenance=_freeze_json_value(provenance or {}),
        )


@dataclass(frozen=True)
class CodeExecutionStep:
    argv: tuple[str, ...]
    role: CodeExecutionStepRole
    cwd: str = "source"
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)) or not isinstance(self.argv, Sequence):
            raise TypeError("argv must be a bounded sequence of arguments, not a shell string")
        argv = tuple(self.argv)
        if not argv or len(argv) > _MAX_ARGV:
            raise ValueError(f"argv must contain between 1 and {_MAX_ARGV} arguments")
        for argument in argv:
            if not isinstance(argument, str) or not argument or "\x00" in argument:
                raise ValueError("argv entries must be non-empty strings without NUL bytes")
            if len(argument.encode("utf-8")) > _MAX_ARG_BYTES:
                raise ValueError("argv entry exceeds the size limit")
        if self.role not in {"build", "run"}:
            raise ValueError(f"unsupported execution step role: {self.role!r}")
        cwd = _normalize_relative_path(self.cwd, field_name="cwd")
        if len(self.env) > _MAX_ENV:
            raise ValueError("execution environment exceeds the entry limit")
        env: dict[str, str] = {}
        for key, value in self.env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("execution environment keys and values must be strings")
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("execution environment contains an invalid key or value")
            env[key] = value
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "env", MappingProxyType(dict(sorted(env.items()))))


@dataclass(frozen=True)
class CodeExecutionToolchainRequirement:
    tool: str
    minimum_version: str | None = None
    exact_version: str | None = None

    def __post_init__(self) -> None:
        tool = self.tool.strip().casefold() if isinstance(self.tool, str) else ""
        if not tool or not re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,63}", tool):
            raise ValueError("toolchain tool must be a canonical identifier")
        if self.minimum_version is not None and self.exact_version is not None:
            raise ValueError("toolchain requirement cannot set both minimum and exact version")
        object.__setattr__(self, "tool", tool)


@dataclass(frozen=True)
class CodeExecutionBundle:
    bundle_id: str
    language: str
    files: tuple[CodeExecutionFile, ...]
    entrypoint: str
    build_steps: tuple[CodeExecutionStep, ...]
    run_step: CodeExecutionStep
    expected_outputs: tuple[str, ...]
    toolchains: tuple[CodeExecutionToolchainRequirement, ...]
    provenance: Mapping[str, Any]
    bundle_digest: str

    @classmethod
    def create(
        cls,
        *,
        language: str,
        files: Sequence[CodeExecutionFile],
        entrypoint: str,
        build_steps: Sequence[CodeExecutionStep],
        run_step: CodeExecutionStep,
        expected_outputs: Sequence[str] = (),
        toolchains: Sequence[CodeExecutionToolchainRequirement] = (),
        provenance: Mapping[str, Any] | None = None,
    ) -> "CodeExecutionBundle":
        canonical_language = language.strip().casefold() if isinstance(language, str) else ""
        if not canonical_language or not re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,63}", canonical_language):
            raise ValueError("language must be a canonical identifier")
        frozen_files = tuple(files)
        if not frozen_files or len(frozen_files) > _MAX_FILES:
            raise ValueError(f"bundle files must contain between 1 and {_MAX_FILES} entries")
        if any(not isinstance(item, CodeExecutionFile) for item in frozen_files):
            raise TypeError("bundle files must contain CodeExecutionFile values")
        if sum(len(item.content) for item in frozen_files) > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle files exceed the total size limit")
        folded_paths: set[str] = set()
        for item in frozen_files:
            folded = item.path.casefold()
            if folded in folded_paths:
                raise ValueError(f"duplicate or case-collision file path: {item.path!r}")
            folded_paths.add(folded)

        canonical_entrypoint = _normalize_relative_path(entrypoint, field_name="entrypoint")
        if canonical_entrypoint.casefold() not in folded_paths:
            raise ValueError("entrypoint path must identify one bundle file")

        frozen_build_steps = tuple(build_steps)
        if any(step.role != "build" for step in frozen_build_steps):
            raise ValueError("build_steps may contain only build-role steps")
        if run_step.role != "run":
            raise ValueError("run_step must have the run role")

        frozen_outputs = _normalize_expected_outputs(expected_outputs)

        frozen_toolchains = tuple(toolchains)
        if any(not isinstance(item, CodeExecutionToolchainRequirement) for item in frozen_toolchains):
            raise TypeError("toolchains must contain CodeExecutionToolchainRequirement values")
        frozen_provenance = _freeze_json_value(provenance or {})

        canonical = {
            "language": canonical_language,
            "files": [
                {
                    "path": item.path,
                    "role": item.role,
                    "sha256": item.sha256,
                    "bytes": len(item.content),
                }
                for item in frozen_files
            ],
            "entrypoint": canonical_entrypoint,
            "build_steps": [
                {
                    "argv": list(step.argv),
                    "cwd": step.cwd,
                    "env": dict(step.env),
                    "role": step.role,
                }
                for step in frozen_build_steps
            ],
            "run_step": {
                "argv": list(run_step.argv),
                "cwd": run_step.cwd,
                "env": dict(run_step.env),
                "role": run_step.role,
            },
            "expected_outputs": list(frozen_outputs),
            "toolchains": [
                {
                    "tool": item.tool,
                    "minimum_version": item.minimum_version,
                    "exact_version": item.exact_version,
                }
                for item in frozen_toolchains
            ],
            "provenance": _json_value(frozen_provenance),
        }
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        bundle_digest = _digest(serialized)
        return cls(
            bundle_id=f"bundle-{bundle_digest.removeprefix('sha256:')[:24]}",
            language=canonical_language,
            files=frozen_files,
            entrypoint=canonical_entrypoint,
            build_steps=frozen_build_steps,
            run_step=run_step,
            expected_outputs=frozen_outputs,
            toolchains=frozen_toolchains,
            provenance=frozen_provenance,
            bundle_digest=bundle_digest,
        )


__all__ = [
    "CodeExecutionBundle",
    "CodeExecutionFile",
    "CodeExecutionFileRole",
    "CodeExecutionProviderCapability",
    "CodeExecutionRequest",
    "CodeExecutionResult",
    "CodeExecutionStep",
    "CodeExecutionStepRole",
    "CodeExecutionToolchainRequirement",
    "resolve_code_execution_workspace_uri",
]
