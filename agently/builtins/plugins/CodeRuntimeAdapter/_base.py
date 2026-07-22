# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agently.types.data import (
    CodeExecutionBundle,
    CodeExecutionFile,
    CodeExecutionRequest,
    CodeExecutionStep,
    CodeExecutionToolchainRequirement,
)


class BuiltinCodeRuntimeAdapter:
    name = "BuiltinCodeRuntimeAdapter"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    language_id = ""
    aliases: tuple[str, ...] = ()
    primary_filename = ""
    source_suffixes: tuple[str, ...] = ()
    dependency_manifests: tuple[str, ...] = ()

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    def _normalize_request(
        self,
        request: CodeExecutionRequest,
    ) -> tuple[tuple[CodeExecutionFile, ...], str]:
        if request.language not in {self.language_id, *self.aliases}:
            raise ValueError(
                f"runtime adapter {self.language_id!r} cannot prepare language {request.language!r}"
            )
        files: list[CodeExecutionFile] = []
        if request.source_code is not None:
            source_path = request.entrypoint or self.primary_filename
            files.append(
                CodeExecutionFile(path=source_path, content=request.source_code, role="source")
            )
        for item in request.files:
            role = "dependency" if item.path in self.dependency_manifests else "source"
            files.append(CodeExecutionFile(path=item.path, content=item.content, role=role))
        folded: set[str] = set()
        for item in files:
            if item.path.casefold() in folded:
                raise ValueError(f"duplicate or collision request file path: {item.path!r}")
            folded.add(item.path.casefold())
        entrypoint = request.entrypoint
        if entrypoint is None:
            if request.source_code is not None:
                entrypoint = self.primary_filename
            else:
                entrypoint = next(
                    (
                        item.path
                        for item in files
                        if item.path.casefold().endswith(self.source_suffixes)
                    ),
                    None,
                )
        if entrypoint is None or entrypoint.casefold() not in folded:
            raise ValueError("request entrypoint must identify a supported source file")
        if not entrypoint.casefold().endswith(self.source_suffixes):
            raise ValueError(
                f"entrypoint {entrypoint!r} is not supported by the {self.language_id} adapter"
            )
        return tuple(files), entrypoint

    @staticmethod
    def _dependency_install_enabled(policy: Mapping[str, Any]) -> bool:
        mode = str(policy.get("dependency_install", "deny"))
        if mode not in {"deny", "request", "install"}:
            raise ValueError("dependency_install policy must be deny, request, or install")
        return mode in {"request", "install"}

    def _bundle(
        self,
        *,
        request: CodeExecutionRequest,
        files: Sequence[CodeExecutionFile],
        entrypoint: str,
        build_steps: Sequence[CodeExecutionStep],
        run_step: CodeExecutionStep,
    ) -> CodeExecutionBundle:
        return CodeExecutionBundle.create(
            language=self.language_id,
            files=files,
            entrypoint=entrypoint,
            build_steps=build_steps,
            run_step=run_step,
            expected_outputs=request.expected_outputs,
            toolchains=self.toolchain_requirements(),
            provenance=request.provenance,
        )

    def toolchain_requirements(self) -> tuple[CodeExecutionToolchainRequirement, ...]:
        raise NotImplementedError
