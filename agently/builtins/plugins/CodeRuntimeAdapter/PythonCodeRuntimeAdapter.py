from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agently.types.data import (
    CodeExecutionRequest,
    CodeExecutionStep,
    CodeExecutionToolchainRequirement,
)

from ._base import BuiltinCodeRuntimeAdapter


class PythonCodeRuntimeAdapter(BuiltinCodeRuntimeAdapter):
    name = "PythonCodeRuntimeAdapter"
    language_id = "python"
    aliases = ("py", "python3")
    primary_filename = "main.py"
    source_suffixes = (".py",)
    dependency_manifests = ("requirements.txt", "pyproject.toml")

    def toolchain_requirements(self) -> tuple[CodeExecutionToolchainRequirement, ...]:
        return (CodeExecutionToolchainRequirement(tool="python", minimum_version="3.10"),)

    def prepare(self, request: CodeExecutionRequest, policy: Mapping[str, Any]):
        files, entrypoint = self._normalize_request(request)
        build_steps = []
        paths = {item.path for item in files}
        run_env = {}
        if "requirements.txt" in paths and self._dependency_install_enabled(policy):
            build_steps.append(
                CodeExecutionStep(
                    argv=(
                        "python",
                        "-m",
                        "pip",
                        "install",
                        "--target",
                        "../build/python_deps",
                        "--requirement",
                        "requirements.txt",
                    ),
                    cwd="source",
                    role="build",
                )
            )
            run_env["PYTHONPATH"] = "../build/python_deps"
        return self._bundle(
            request=request,
            files=files,
            entrypoint=entrypoint,
            build_steps=build_steps,
            run_step=CodeExecutionStep(
                argv=("python", entrypoint, *request.args),
                cwd="source",
                role="run",
                env=run_env,
            ),
        )
