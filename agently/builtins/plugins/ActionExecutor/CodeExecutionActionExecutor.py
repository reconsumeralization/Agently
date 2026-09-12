# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.application.SkillLibrary import (
    SkillBinding,
    SkillLibrary,
    SkillPackageRevision,
    SkillResourceDescriptor,
)
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessGrant


class CodeExecutionActionExecutor:
    name = "CodeExecutionActionExecutor"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    kind = "code_execution"
    sandboxed = False
    resource_isolation_managed = True

    def __init__(
        self,
        *,
        language: str,
        timeout: int = 60,
        skill_library: SkillLibrary | None = None,
    ):
        self.adapter = get_code_runtime_adapter(language)
        self.language = self.adapter.language_id
        self.timeout = timeout
        self.skill_library = skill_library

    _SKILL_SCRIPT_LANGUAGES = {
        ".py": "python",
        ".js": "nodejs",
        ".mjs": "nodejs",
        ".cjs": "nodejs",
        ".go": "go",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
    }

    @classmethod
    def skill_script_language(
        cls,
        resource_path: str,
        *,
        strict: bool = True,
    ) -> str | None:
        suffix = PurePosixPath(resource_path).suffix.casefold()
        language = cls._SKILL_SCRIPT_LANGUAGES.get(suffix)
        if language is None and strict:
            raise ValueError(
                f"Skill script language is unsupported: {suffix or resource_path!r}"
            )
        return language

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    def _request_from_action(
        self,
        *,
        spec: dict[str, Any],
        action_call: dict[str, Any],
    ) -> CodeExecutionRequest:
        action_input = self._mapping(action_call.get("action_input"))
        meta = self._mapping(spec.get("meta"))
        if meta.get("component") == "skill_script_exec":
            authorization = self._current_skill_script_authorization(
                action_id=str(spec.get("action_id", "")),
            )
            return self._request_from_bound_skill(
                action_input=action_input,
                expected_outputs=authorization.get("expected_outputs", ()),
                skill_bindings=authorization.get("bindings", ()),
                skill_execution_id=str(authorization.get("execution_id", "")),
            )
        revision_ref = str(meta.get("skill_revision_ref", "")).strip()
        resource_path = str(meta.get("skill_resource_path", "")).strip()
        if revision_ref or resource_path:
            return self._request_from_compat_bound_skill(
                action_input=action_input,
                revision_ref=revision_ref,
                resource_path=resource_path,
                expected_digest=str(meta.get("skill_resource_sha256", "")),
                expected_outputs=meta.get("expected_outputs", ()),
                library=self.skill_library or action_call.get("skill_library"),
            )

        raw_files = action_input.get("files")
        files = raw_files if isinstance(raw_files, dict) else None
        return CodeExecutionRequest.create(
            language=self.language,
            source_code=action_input.get("source_code"),
            files=files,
            entrypoint=(
                str(action_input["entrypoint"])
                if action_input.get("entrypoint") is not None
                else None
            ),
            args=action_input.get("args", ()),
            expected_outputs=action_input.get("expected_outputs", ()),
            provenance={"kind": "action_input"},
        )

    def _current_skill_script_authorization(
        self,
        *,
        action_id: str,
    ) -> dict[str, Any]:
        from agently.core.runtime import get_current_agent_execution_context

        context = get_current_agent_execution_context()
        get_authorization = getattr(
            context,
            "get_skill_script_exec_authorization",
            None,
        )
        authorization = (
            get_authorization(action_id)
            if callable(get_authorization)
            else None
        )
        if not isinstance(authorization, dict):
            raise PermissionError(
                "Skill script Action is not authorized for the current AgentExecution."
            )
        if (
            str(authorization.get("execution_id") or "")
            != str(getattr(context, "execution_id", ""))
        ):
            raise PermissionError(
                "Skill script authorization belongs to another AgentExecution."
            )
        if str(authorization.get("language") or "") != self.language:
            raise PermissionError(
                "Skill script authorization does not match the Action language."
            )
        return authorization

    def _request_from_bound_skill(
        self,
        *,
        action_input: dict[str, Any],
        expected_outputs: Any,
        skill_bindings: Any,
        skill_execution_id: str,
    ) -> CodeExecutionRequest:
        library = self.skill_library
        if not isinstance(library, SkillLibrary):
            raise TypeError("Skill script execution requires the bound SkillLibrary.")
        resource_path = str(action_input.get("script_path", "")).strip()
        if not resource_path:
            raise ValueError("script_path is required.")
        matches: list[tuple[SkillBinding, Any, Any]] = []
        bindings = (
            tuple(skill_bindings)
            if isinstance(skill_bindings, (list, tuple))
            else ()
        )
        if not bindings or not skill_execution_id:
            raise PermissionError(
                "Skill script Action has no exact bindings for the current AgentExecution."
            )
        for binding in bindings:
            if not isinstance(binding, SkillBinding):
                raise TypeError("Skill script authorization contains an invalid binding.")
            if binding.task_id != skill_execution_id:
                raise PermissionError("Skill binding belongs to another task execution.")
            package = library.resolve(binding.revision_ref)
            try:
                descriptor = package.resource(resource_path)
            except KeyError:
                continue
            matches.append((binding, package, descriptor))
        if len(matches) != 1:
            raise PermissionError(
                "script_path must identify one executable resource in this "
                "AgentExecution's exact Skill bindings; narrow the Skill scope or "
                "use the explicit script binder when paths are ambiguous."
            )
        binding, package, descriptor = matches[0]
        if package.revision_ref != binding.revision_ref:
            raise PermissionError("Skill script execution requires an exact revision.")
        return self._request_from_skill_resource(
            package=package,
            descriptor=descriptor,
            args=action_input.get("args", ()),
            expected_outputs=expected_outputs,
            binding_id=binding.binding_id,
        )

    def _request_from_compat_bound_skill(
        self,
        *,
        action_input: dict[str, Any],
        revision_ref: str,
        resource_path: str,
        expected_digest: str,
        expected_outputs: Any,
        library: Any,
    ) -> CodeExecutionRequest:
        if not revision_ref or not resource_path:
            raise ValueError(
                "Skill script execution requires an exact revision and resource path."
            )
        if not isinstance(library, SkillLibrary):
            raise TypeError("Skill script execution requires the bound SkillLibrary.")
        package = library.resolve(revision_ref)
        if package.revision_ref != revision_ref:
            raise ValueError("Skill script revision did not resolve exactly.")
        descriptor = package.resource(resource_path)
        return self._request_from_skill_resource(
            package=package,
            descriptor=descriptor,
            args=action_input.get("args", ()),
            expected_outputs=expected_outputs,
            expected_digest=expected_digest,
            library=library,
        )

    def _request_from_skill_resource(
        self,
        *,
        package: SkillPackageRevision,
        descriptor: SkillResourceDescriptor,
        args: Any,
        expected_outputs: Any,
        expected_digest: str | None = None,
        binding_id: str | None = None,
        library: SkillLibrary | None = None,
    ) -> CodeExecutionRequest:
        selected_library = library or self.skill_library
        if not isinstance(selected_library, SkillLibrary):
            raise TypeError("Skill script execution requires the bound SkillLibrary.")
        if package.trust != "trusted":
            raise PermissionError(
                "Skill script execution requires a trusted package revision."
            )
        if descriptor.kind != "script" or not descriptor.executable:
            raise PermissionError("Skill resource is not an executable script.")
        descriptor_language = self.skill_script_language(descriptor.path)
        if descriptor_language != self.language:
            raise ValueError(
                f"Skill script requires {descriptor_language!r}, not {self.language!r}."
            )
        if expected_digest is not None and expected_digest != descriptor.sha256:
            raise ValueError(
                "Skill script descriptor digest does not match the bound action."
            )
        read = selected_library.read_resource(
            package,
            descriptor.path,
            max_bytes=descriptor.size + 1,
        )
        if read.truncated or read.total_bytes != descriptor.size:
            raise ValueError("Skill script bytes could not be read completely.")
        actual_digest = hashlib.sha256(read.data).hexdigest()
        if actual_digest != descriptor.sha256:
            raise ValueError(
                "Skill script bytes do not match the installed revision digest."
            )
        outputs = expected_outputs if isinstance(expected_outputs, (list, tuple)) else ()
        provenance = {
            "kind": "skill",
            "revision_ref": package.revision_ref,
            "resource_path": descriptor.path,
            "resource_sha256": descriptor.sha256,
        }
        if binding_id is not None:
            provenance["binding_id"] = binding_id
        return CodeExecutionRequest.create(
            language=self.language,
            files={descriptor.path: read.data},
            entrypoint=descriptor.path,
            args=args,
            expected_outputs=outputs,
            provenance=provenance,
        )

    @staticmethod
    def _artifact(item: Any) -> dict[str, Any]:
        media_type = mimetypes.guess_type(str(item.path))[0] or "application/octet-stream"
        return {
            "artifact_type": "file",
            "role": "output",
            "path": item.path,
            "media_type": media_type,
            "size": item.bytes,
            "bytes": item.bytes,
            "sha256": item.sha256,
            "available": True,
            "meta": {"host_path": item.host_path},
        }

    @classmethod
    def _adapter_policy(
        cls,
        *,
        spec: dict[str, Any],
    ) -> dict[str, Any]:
        requirements = spec.get("execution_resources", [])
        if not isinstance(requirements, list):
            return {"dependency_install": "deny"}
        for requirement in requirements:
            if not isinstance(requirement, dict) or requirement.get("kind") != "code_execution":
                continue
            config = cls._mapping(requirement.get("config"))
            dependency_policy = config.get("dependency_policy", {})
            if isinstance(dependency_policy, dict):
                mode = str(dependency_policy.get("mode", "deny"))
            else:
                mode = str(dependency_policy or "deny")
            if mode not in {"deny", "request", "install"}:
                raise ValueError("CodeExecution dependency policy is invalid.")
            return {"dependency_install": mode}
        return {"dependency_install": "deny"}

    @classmethod
    def _provider_facts(
        cls,
        *,
        action_call: dict[str, Any],
        action_id: str,
    ) -> dict[str, Any]:
        handles = action_call.get("execution_resource_handles", {})
        handle = handles.get(action_id) if isinstance(handles, dict) else None
        if not isinstance(handle, dict):
            return {}
        provider_id = str(handle.get("provider_id", "")).strip()
        handle_meta = cls._mapping(handle.get("meta"))
        probes = handle_meta.get("provider_probes", [])
        selected_probe: dict[str, Any] = {}
        if isinstance(probes, list):
            for item in reversed(probes):
                if isinstance(item, dict) and str(item.get("provider_id", "")) == provider_id:
                    selected_probe = item
                    break
        facts: dict[str, Any] = {}
        if provider_id:
            facts["provider_id"] = provider_id
        capabilities = selected_probe.get("capabilities")
        if isinstance(capabilities, dict):
            facts["provider_capabilities"] = dict(capabilities)
        reason = str(selected_probe.get("reason", "")).strip()
        if reason:
            facts["provider_probe_reason"] = reason
        return facts

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = settings
        action_id = str(spec.get("action_id", "run_code"))
        workspace = action_call.get("task_workspace")
        if not isinstance(workspace, TaskWorkspace):
            raise TypeError("CodeExecutionAction requires a TaskWorkspace binding.")
        grants = action_call.get("task_workspace_access_grants", {})
        grant = grants.get(action_id) if isinstance(grants, dict) else None
        if not isinstance(grant, TaskWorkspaceAccessGrant):
            raise TypeError("CodeExecutionAction requires a TaskWorkspace execution grant.")
        resources = action_call.get("execution_resource_resources", {})
        resource = resources.get(action_id) if isinstance(resources, dict) else None
        if resource is None or not hasattr(resource, "async_execute_code"):
            raise RuntimeError("Code execution resource is not available.")

        request = self._request_from_action(spec=spec, action_call=action_call)
        bundle = self.adapter.prepare(
            request,
            policy=self._adapter_policy(spec=spec),
        )
        manifest = await workspace.materialize_execution_bundle(grant, bundle)
        timeout = int(policy.get("timeout_seconds", self.timeout))
        raw_result = await resource.async_execute_code(
            bundle=bundle,
            manifest=manifest,
            grant=grant,
            timeout=timeout,
        )
        result = dict(raw_result) if isinstance(raw_result, dict) else {"result": raw_result}
        raw_outputs = result.get("outputs", ())
        output_paths: list[str] = []
        if isinstance(raw_outputs, (list, tuple)):
            for item in raw_outputs:
                if isinstance(item, str):
                    output_paths.append(item)
                elif isinstance(item, dict) and item.get("path"):
                    output_paths.append(str(item["path"]))
        if not output_paths:
            area = Path(grant.execution_area)
            output_paths = [
                path
                for path in manifest.expected_outputs
                if (area / Path(path)).is_file()
            ]
        collected = (
            await workspace.collect_execution_outputs(grant, output_paths)
            if output_paths
            else ()
        )
        area = Path(grant.execution_area)
        collected_paths = {
            Path(item.host_path).relative_to(area).as_posix().casefold()
            for item in collected
        }
        missing_expected_outputs = [
            path
            for path in manifest.expected_outputs
            if path.casefold() not in collected_paths
        ]
        if missing_expected_outputs:
            result["ok"] = False
            result["status"] = "error"
            result["error"] = "Declared code execution outputs were not produced."
            result["missing_expected_outputs"] = missing_expected_outputs
            diagnostics = result.get("diagnostics")
            diagnostics = diagnostics if isinstance(diagnostics, list) else []
            diagnostics.append(
                {
                    "source": self.name,
                    "severity": "error",
                    "code": "code_execution.expected_output_missing",
                    "message": result["error"],
                    "meta": {
                        "missing_expected_outputs": missing_expected_outputs,
                    },
                }
            )
            result["diagnostics"] = diagnostics
        result.setdefault("ok", result.get("status") == "success")
        result.setdefault("status", "success" if result.get("ok") else "error")
        result["artifacts"] = [self._artifact(item) for item in collected]
        result["meta"] = {
            **self._mapping(result.get("meta")),
            **self._provider_facts(action_call=action_call, action_id=action_id),
            "provider_contract": "workspace_code_execution_v1",
            "bundle_id": bundle.bundle_id,
            "bundle_digest": bundle.bundle_digest,
            "grant_id": grant.grant_id,
            "provenance": dict(bundle.provenance),
        }
        data = {
            key: value
            for key, value in result.items()
            if key not in {"ok", "status", "artifacts"}
        }
        return {
            "ok": bool(result.get("ok")),
            "status": str(result.get("status", "error")),
            "data": data,
            "result": data,
            "artifacts": result["artifacts"],
            "meta": result["meta"],
            "error": (
                ""
                if result.get("ok")
                else str(result.get("error") or result.get("stderr") or "Code execution failed.")
            ),
        }


__all__ = ["CodeExecutionActionExecutor"]
