from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agently.types.data import CodeExecutionRequest, CodeExecutionStep, CodeExecutionToolchainRequirement

from ._base import BuiltinCodeRuntimeAdapter


class CppCodeRuntimeAdapter(BuiltinCodeRuntimeAdapter):
    name = "CppCodeRuntimeAdapter"
    language_id = "cpp"
    aliases = ("c++", "cplusplus")
    primary_filename = "main.cpp"
    source_suffixes = (".cpp", ".cc", ".cxx")

    def toolchain_requirements(self) -> tuple[CodeExecutionToolchainRequirement, ...]:
        return (CodeExecutionToolchainRequirement(tool="c++"),)

    def prepare(self, request: CodeExecutionRequest, policy: Mapping[str, Any]):
        files, entrypoint = self._normalize_request(request)
        return self._bundle(
            request=request,
            files=files,
            entrypoint=entrypoint,
            build_steps=(
                CodeExecutionStep(
                    argv=("c++", "-std=c++20", "-o", "../build/app", entrypoint),
                    cwd="source",
                    role="build",
                ),
            ),
            run_step=CodeExecutionStep(
                argv=("../build/app", *request.args), cwd="source", role="run"
            ),
        )
