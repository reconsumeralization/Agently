from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agently.types.data import CodeExecutionRequest, CodeExecutionStep, CodeExecutionToolchainRequirement

from ._base import BuiltinCodeRuntimeAdapter


class GoCodeRuntimeAdapter(BuiltinCodeRuntimeAdapter):
    name = "GoCodeRuntimeAdapter"
    language_id = "go"
    aliases = ("golang",)
    primary_filename = "main.go"
    source_suffixes = (".go",)
    dependency_manifests = ("go.mod", "go.sum")

    def toolchain_requirements(self) -> tuple[CodeExecutionToolchainRequirement, ...]:
        return (CodeExecutionToolchainRequirement(tool="go", minimum_version="1.25"),)

    def prepare(self, request: CodeExecutionRequest, policy: Mapping[str, Any]):
        files, entrypoint = self._normalize_request(request)
        build_steps = []
        build_env = {
            "GOCACHE": "workspace://build/go-build-cache",
            "GOMODCACHE": "workspace://build/go-mod-cache",
        }
        if "go.mod" in {item.path for item in files} and self._dependency_install_enabled(policy):
            build_steps.append(
                CodeExecutionStep(
                    argv=("go", "mod", "download"),
                    cwd="source",
                    role="build",
                    env=build_env,
                )
            )
        build_steps.append(
            CodeExecutionStep(
                argv=("go", "build", "-o", "../build/app", entrypoint),
                cwd="source",
                role="build",
                env=build_env,
            )
        )
        return self._bundle(
            request=request,
            files=files,
            entrypoint=entrypoint,
            build_steps=build_steps,
            run_step=CodeExecutionStep(
                argv=("../build/app", *request.args), cwd="source", role="run"
            ),
        )
