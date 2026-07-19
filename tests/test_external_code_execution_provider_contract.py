from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.builtins.plugins.CodeRuntimeAdapter import PythonCodeRuntimeAdapter
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
    DockerExecutionResourceProvider,
)
from agently.core import ExecutionResourceManager
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessRequirement
from agently.types.data.code_execution import required_code_execution_isolation
from agently.types.plugins import CodeExecutionResource
from agently.utils import Settings


class _ExternalResource:
    def __init__(self, grant, mechanism: str) -> None:
        self.grant = grant
        self.mechanism = mechanism

    async def async_execute_code(self, *, bundle, manifest, grant, timeout):
        _ = timeout
        assert grant == self.grant
        assert manifest.grant_id == grant.grant_id
        assert manifest.bundle_digest == bundle.bundle_digest
        return {"ok": True, "status": "success", "outputs": []}


class _ExternalProvider:
    DEFAULT_SETTINGS: dict[str, Any] = {}
    supported_kinds = ("code_execution",)

    def __init__(self, provider_id: str, mechanism: str) -> None:
        self.provider_id = provider_id
        self.name = provider_id
        self.mechanism = mechanism

    async def async_probe(self, *, requirement, policy):
        _ = requirement, policy
        return {
            "provider_id": self.provider_id,
            "available": True,
            "supported_kinds": ["code_execution"],
            "capabilities": {
                "languages": ["python"],
                "toolchains": {"python": {"version": "3.10.13"}},
                "isolation": {
                    **required_code_execution_isolation(),
                    "mechanism": self.mechanism,
                },
                "mechanism": self.mechanism,
            },
            "reason": "synthetic external-provider fixture",
        }

    async def async_ensure(self, *, requirement, policy, existing_handle=None):
        _ = policy, existing_handle
        grant = requirement["task_workspace_access_grant"]
        return {
            "handle_id": f"{self.provider_id}:1",
            "resource": _ExternalResource(grant, self.mechanism),
            "status": "ready",
        }

    async def async_health_check(self, handle):
        return "ready" if isinstance(handle.get("resource"), _ExternalResource) else "unhealthy"

    async def async_release(self, handle):
        _ = handle


class _AlternativeContainerRuntimeResource(DockerExecutionResource):
    pass


class _AlternativeContainerRuntimeProvider(DockerExecutionResourceProvider):
    def __init__(self) -> None:
        self.created_resources: list[dict[str, Any]] = []

    @property
    def provider_id(self) -> str:
        return "external_container_runtime"

    def create_resource(self, **kwargs: Any) -> DockerExecutionResource:
        self.created_resources.append(dict(kwargs))
        return _AlternativeContainerRuntimeResource(**kwargs)


@pytest.mark.parametrize("mechanism", ["container_runtime", "host_policy"])
@pytest.mark.asyncio
async def test_external_provider_needs_no_core_changes_for_grant_bundle_execution(
    tmp_path: Path,
    mechanism: str,
) -> None:
    manager = ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=Settings(name=f"external-{mechanism}", parent=Agently.settings),
        event_center=Agently.event_center,
    )
    provider = _ExternalProvider(f"external_{mechanism}", mechanism)
    manager.register_provider(provider)
    workspace = TaskWorkspace(tmp_path / mechanism, execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="run",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(language="python", source_code="print('ok')\n"),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_id": provider.provider_id,
            "required_capabilities": {
                "language": "python",
                "toolchains": {"python": {"minimum_version": "3.10"}},
                "isolation": required_code_execution_isolation(),
                "mechanism": mechanism,
            },
            "task_workspace_access_grant": grant,
        }
    )
    resource = handle["resource"]
    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=10,
    )

    assert isinstance(resource, CodeExecutionResource)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_docker_runtime_variant_reuses_provider_lifecycle_through_resource_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_availability",
        lambda self: {
            "available": True,
            "binary_available": True,
            "daemon_available": True,
            "reason": "synthetic provider-composition probe",
        },
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "inspect_image",
        lambda self, image: {"image": image, "exists": True},
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "ensure_available",
        lambda self: {"available": True, "reason": "synthetic"},
    )
    monkeypatch.setattr(
        DockerExecutionResource,
        "ensure_image_ready",
        lambda self, image, profile=None: {
            "image": image,
            "exists": True,
            "profile": profile,
        },
    )

    provider = _AlternativeContainerRuntimeProvider()
    manager = ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=Settings(name="external-container-composition", parent=Agently.settings),
        event_center=Agently.event_center,
    )
    manager.register_provider(provider)
    workspace = TaskWorkspace(tmp_path / "container-runtime", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="run",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )

    handle = await manager.async_ensure(
        {
            "kind": "code_execution",
            "provider_id": provider.provider_id,
            "required_capabilities": {
                "language": "python",
                "isolation": required_code_execution_isolation(),
            },
            "task_workspace_access_grant": grant,
            "config": {
                "runtime_profile": {
                    "image": "synthetic-python:3.10",
                    "image_pull_policy": "never",
                }
            },
        }
    )

    assert isinstance(handle["resource"], _AlternativeContainerRuntimeResource)
    # Selection probes once, the manager re-probes immediately before ensure,
    # and ensure creates the grant-bound live resource.
    assert len(provider.created_resources) == 3
    assert provider.created_resources[-1]["workspace_grant"] == grant
    assert provider.created_resources[-1]["runtime_profile"]["image"] == "synthetic-python:3.10"
