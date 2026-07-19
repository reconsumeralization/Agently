# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter
from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.application.SkillLibrary import SkillLibrary
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
        revision_ref = str(meta.get("skill_revision_ref", "")).strip()
        resource_path = str(meta.get("skill_resource_path", "")).strip()
        if revision_ref or resource_path:
            if not revision_ref or not resource_path:
                raise ValueError("Skill script execution requires an exact revision and resource path.")
            library = self.skill_library or action_call.get("skill_library")
            if not isinstance(library, SkillLibrary):
                raise TypeError("Skill script execution requires the bound SkillLibrary.")
            package = library.resolve(revision_ref)
            if package.revision_ref != revision_ref:
                raise ValueError("Skill script revision did not resolve exactly.")
            if package.trust != "trusted":
                raise PermissionError("Skill script execution requires a trusted package revision.")
            descriptor = package.resource(resource_path)
            expected_digest = str(meta.get("skill_resource_sha256", ""))
            if descriptor.kind != "script" or not descriptor.executable:
                raise PermissionError("Skill resource is not an executable script.")
            if expected_digest != descriptor.sha256:
                raise ValueError("Skill script descriptor digest does not match the bound action.")
            read = library.read_resource(
                package,
                resource_path,
                max_bytes=descriptor.size + 1,
            )
            if read.truncated or read.total_bytes != descriptor.size:
                raise ValueError("Skill script bytes could not be read completely.")
            actual_digest = hashlib.sha256(read.data).hexdigest()
            if actual_digest != descriptor.sha256:
                raise ValueError("Skill script bytes do not match the installed revision digest.")
            return CodeExecutionRequest.create(
                language=self.language,
                files={resource_path: read.data},
                entrypoint=resource_path,
                args=action_input.get("args", ()),
                expected_outputs=action_input.get(
                    "expected_outputs",
                    meta.get("expected_outputs", ()),
                ),
                provenance={
                    "kind": "skill",
                    "revision_ref": package.revision_ref,
                    "resource_path": resource_path,
                    "resource_sha256": descriptor.sha256,
                },
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
