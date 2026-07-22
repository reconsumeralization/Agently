from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agently.types.data import CodeExecutionRequest, CodeExecutionStep, CodeExecutionToolchainRequirement

from ._base import BuiltinCodeRuntimeAdapter


class NodeCodeRuntimeAdapter(BuiltinCodeRuntimeAdapter):
    name = "NodeCodeRuntimeAdapter"
    language_id = "nodejs"
    aliases = ("node", "javascript", "js")
    primary_filename = "main.js"
    source_suffixes = (".js", ".mjs", ".cjs")
    dependency_manifests = ("package.json", "package-lock.json")

    def toolchain_requirements(self) -> tuple[CodeExecutionToolchainRequirement, ...]:
        return (CodeExecutionToolchainRequirement(tool="node", minimum_version="18"),)

    def prepare(self, request: CodeExecutionRequest, policy: Mapping[str, Any]):
        files, entrypoint = self._normalize_request(request)
        build_steps = []
        if "package.json" in {item.path for item in files} and self._dependency_install_enabled(policy):
            copy_script = (
                "const fs=require('fs');"
                "const target='../build/node_deps';"
                "fs.mkdirSync(target,{recursive:true});"
                "for(const name of ['package.json','package-lock.json']){"
                "if(fs.existsSync(name))fs.copyFileSync(name,`${target}/${name}`);"
                "}"
            )
            build_steps.extend(
                [
                    CodeExecutionStep(
                        argv=("node", "-e", copy_script),
                        cwd="source",
                        role="build",
                    ),
                    CodeExecutionStep(
                        argv=("npm", "install", "--prefix", "../build/node_deps"),
                        cwd="source",
                        role="build",
                    ),
                ]
            )
        return self._bundle(
            request=request,
            files=files,
            entrypoint=entrypoint,
            build_steps=build_steps,
            run_step=CodeExecutionStep(
                argv=("node", entrypoint, *request.args),
                cwd="source",
                role="run",
                env={"NODE_PATH": "workspace://build/node_deps/node_modules"},
            ),
        )
