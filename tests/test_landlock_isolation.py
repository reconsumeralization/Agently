"""Acceptance tests for the bounded-helper Landlock provider."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
    LandlockCodeExecutionResource,
    LandlockExecutionResourceProvider,
)
from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionHelper import (
    LANDLOCK_ACCESS_FS_MAKE_DIR,
    LANDLOCK_ACCESS_FS_READ_DIR,
    access_for_path,
)
from agently.builtins.plugins.ExecutionResourceProvider._bounded_process import (
    BoundedProcessResult,
)
from agently.core import ExecutionResourceError
from agently.core.operation.Action.ActionResourceRegistrar import ActionResourceRegistrar
from agently.types.data import (
    TaskWorkspaceAccessGrant,
    TaskWorkspaceAccessRoot,
    TaskWorkspaceAccessRootRole,
)


landlock_module = importlib.import_module(
    "agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider"
)


def _grant(tmp_path: Path) -> TaskWorkspaceAccessGrant:
    area = tmp_path / "execution"
    roots = []
    root_specs: tuple[
        tuple[TaskWorkspaceAccessRootRole, Literal["read_only", "read_write"]],
        ...,
    ] = (
        ("source", "read_only"),
        ("build", "read_write"),
        ("output", "read_write"),
        ("logs", "read_write"),
    )
    for role, mode in root_specs:
        path = area / role
        path.mkdir(parents=True, exist_ok=True)
        roots.append(TaskWorkspaceAccessRoot(role=role, host_path=str(path), access_mode=mode))
    return TaskWorkspaceAccessGrant(
        grant_id="landlock-grant",
        task_workspace_id="workspace",
        execution_id="execution",
        action_call_id="run",
        mode="snapshot",
        execution_area=str(area),
        roots=tuple(roots),
        issued_at="2026-08-17T00:00:00Z",
    )


class _Settings:
    def get(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Action:
    def __init__(self) -> None:
        self.settings = _Settings()
        self.registered: dict[str, Any] = {}

    def _normalize_tags(self, _tags: Any) -> list[str]:
        return []

    def _create_executor(self, *_args: Any, **_kwargs: Any) -> object:
        return object()

    def register_action(self, **kwargs: Any) -> None:
        self.registered = kwargs


class _CapabilityProbe(TypedDict):
    capabilities: dict[str, Any]


def test_landlock_resource_accepts_no_arbitrary_path_or_abi_configuration(tmp_path: Path) -> None:
    grant = _grant(tmp_path)

    with pytest.raises(TypeError):
        LandlockCodeExecutionResource(grant=grant, allowed_write_dirs=["/"])  # type: ignore[call-arg]


def test_landlock_regular_file_rule_excludes_directory_only_access_bits(tmp_path: Path) -> None:
    path = tmp_path / "ld.so.cache"
    path.write_bytes(b"cache")

    access = access_for_path(abi_version=7, mode="read", path=path)

    assert access & LANDLOCK_ACCESS_FS_READ_DIR == 0
    assert access & LANDLOCK_ACCESS_FS_MAKE_DIR == 0


@pytest.mark.asyncio
async def test_landlock_parent_launches_bounded_helper_instead_of_preexec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    async def bounded(argv: list[str], **_kwargs: Any) -> BoundedProcessResult:
        calls.append(list(argv))
        return BoundedProcessResult(
            returncode=0,
            stdout=b"ok",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(landlock_module, "run_bounded_process", bounded, raising=False)
    resource = LandlockCodeExecutionResource(grant=_grant(tmp_path))
    result = await resource._run_with_landlock(
        ["python3", "--version"],
        cwd=str(tmp_path / "execution" / "source"),
        env={"PATH": "/usr/bin:/bin"},
        timeout=5,
    )

    assert result["ok"] is True
    assert calls
    assert calls[0][0] == sys.executable
    assert "LandlockExecutionHelper.py" in calls[0][1]
    assert "--manifest" in calls[0]


@pytest.mark.asyncio
async def test_landlock_probe_reports_filesystem_only_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        landlock_module,
        "inspect_landlock_availability",
        lambda: {"available": True, "reason": "ready", "abi_version": 3},
    )
    monkeypatch.setattr(
        LandlockExecutionResourceProvider,
        "_tool_facts",
        lambda _self: {"python": {"tool": "python", "available": True, "binary": "/usr/bin/python3", "version": "3.10", "raw_version": "Python 3.10"}},
    )

    probe = cast(
        _CapabilityProbe,
        await LandlockExecutionResourceProvider().async_probe(
            requirement={"kind": "code_execution"},
            policy={},
        ),
    )

    isolation = probe["capabilities"]["isolation"]
    assert isolation["process_contained"] is False
    assert isolation["host_filesystem_restricted"] is True
    assert isolation["syscalls_restricted"] is False
    assert probe["capabilities"]["safety_class"] == "filesystem_only"
    assert "configurable_restrictions" not in probe["capabilities"]


@pytest.mark.asyncio
async def test_landlock_rejects_arbitrary_provider_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        landlock_module,
        "inspect_landlock_availability",
        lambda: {"available": True, "reason": "ready", "abi_version": 3},
    )

    with pytest.raises(ExecutionResourceError) as raised:
        await LandlockExecutionResourceProvider().async_ensure(
            requirement={
                "kind": "code_execution",
                "task_workspace_access_grant": _grant(tmp_path),
                "config": {"allowed_write_dirs": ["/"]},
            },
            policy={},
        )

    assert raised.value.code == "execution_resource.landlock_config_invalid"


@pytest.mark.asyncio
async def test_landlock_health_rechecks_real_mechanism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = LandlockCodeExecutionResource(grant=_grant(tmp_path))
    monkeypatch.setattr(
        landlock_module,
        "inspect_landlock_availability",
        lambda: {"available": False, "reason": "landlock_enforcement_failed"},
    )

    status = await LandlockExecutionResourceProvider().async_health_check(
        {"resource": resource, "meta": {"mechanism_verified": True}}
    )

    assert status == "unhealthy"


def test_generic_selection_uses_only_the_landlock_provider() -> None:
    action = _Action()
    ActionResourceRegistrar(cast(Any, action)).register_code_runtime_action(
        language="python",
        providers=["landlock"],
        isolation="preferred",
    )

    requirement = action.registered["execution_resources"][0]
    assert [item["provider_id"] for item in requirement["provider_candidates"]] == ["landlock"]
    assert requirement["meta"]["isolation_preference"] == "preferred"
