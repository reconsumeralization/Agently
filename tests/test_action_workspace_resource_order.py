from __future__ import annotations

import asyncio
from typing import Any, cast
from pathlib import Path
from types import SimpleNamespace

import pytest

from agently.core.TaskWorkspace import TaskWorkspace
from agently.core.operation.Action.ActionDispatcher import ActionDispatcher
from agently.core.operation.Action.ActionRegistry import ActionRegistry
from agently.core.runtime import bind_runtime_context
from agently.types.data import CodeExecutionBundle, CodeExecutionFile, CodeExecutionStep
from agently.utils import Settings


def _bundle() -> CodeExecutionBundle:
    return CodeExecutionBundle.create(
        language="python",
        files=[CodeExecutionFile(path="main.py", content=b"print('ok')\n")],
        entrypoint="main.py",
        build_steps=[],
        run_step=CodeExecutionStep(argv=("python", "main.py"), role="run"),
    )


@pytest.mark.asyncio
async def test_action_orders_workspace_grant_sandbox_bundle_execution_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingWorkspace(TaskWorkspace):
        def issue_execution_access(self, **kwargs):
            events.append("grant")
            return super().issue_execution_access(**kwargs)

        async def materialize_execution_bundle(self, grant, bundle):
            events.append("materialize")
            return await super().materialize_execution_bundle(grant, bundle)

        def close_execution_access(self, grant_id):
            events.append("close")
            return super().close_execution_access(grant_id)

    workspace = RecordingWorkspace(tmp_path / "workspace", execution_id="execution-1")

    class Resource:
        async def async_execute_code(self, *, bundle, manifest, grant, timeout):
            _ = bundle, manifest, grant, timeout
            events.append("execute")
            return {"ok": True, "status": "success"}

    class ResourceManager:
        async def async_ensure(self, requirement, owner_id):
            _ = owner_id
            events.append("ensure")
            call_id = requirement["task_workspace_access_grant"].action_call_id
            assert call_id.startswith("act_call_")
            assert requirement["action_call_id"] == call_id
            return {
                "handle_id": "handle-1",
                "kind": "code_execution",
                "scope": "action_call",
                "resource_key": "run",
                "provider_id": "recording",
                "resource": Resource(),
                "status": "ready",
            }

        async def async_release(self, handle):
            _ = handle
            events.append("release")

    import agently.base

    monkeypatch.setattr(agently.base, "execution_resource", ResourceManager())

    class Executor:
        kind = "code_execution"
        sandboxed = True

        async def execute(self, *, spec, action_call, policy, settings):
            _ = policy, settings
            grant = action_call["task_workspace_access_grants"][spec["action_id"]]
            manifest = await action_call["task_workspace"].materialize_execution_bundle(
                grant,
                _bundle(),
            )
            resource = action_call["execution_resource_resources"][spec["action_id"]]
            return await resource.async_execute_code(
                bundle=_bundle(), manifest=manifest, grant=grant, timeout=1
            )

    registry = ActionRegistry(name="order-test")
    registry.register(
        {
            "action_id": "run",
            "name": "run",
            "sandbox_required": True,
            "execution_resources": [
                {
                    "kind": "code_execution",
                    "resource_key": "run",
                    "workspace_access": {"mode": "snapshot"},
                }
            ],
        },
        Executor(),
    )
    dispatcher = ActionDispatcher(registry, Settings(name="order-test"))

    with bind_runtime_context(
        agent_execution_context=SimpleNamespace(task_workspace=workspace)
    ):
        result = await dispatcher.async_execute("run", {})

    assert result.get("ok") is True
    assert str(result.get("action_call_id", "")).startswith("act_call_")
    assert events == ["grant", "ensure", "materialize", "execute", "release", "close"]


@pytest.mark.asyncio
async def test_repeated_action_calls_receive_distinct_host_owned_call_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="execution-1")
    observed_call_ids: list[str] = []

    class ResourceManager:
        async def async_ensure(self, requirement, owner_id):
            _ = owner_id
            grant = requirement["task_workspace_access_grant"]
            observed_call_ids.append(grant.action_call_id)
            return {
                "handle_id": f"handle-{len(observed_call_ids)}",
                "kind": "code_execution",
                "scope": "action_call",
                "resource_key": "run",
                "provider_id": "recording",
                "resource": object(),
                "status": "ready",
            }

        async def async_release(self, handle):
            _ = handle

    import agently.base

    monkeypatch.setattr(agently.base, "execution_resource", ResourceManager())

    class Executor:
        kind = "code_execution"
        sandboxed = True

        async def execute(self, *, action_call, **_kwargs):
            return {
                "ok": True,
                "status": "success",
                "seen_call_id": action_call["action_call_id"],
            }

    registry = ActionRegistry(name="call-id-test")
    registry.register(
        {
            "action_id": "run",
            "name": "run",
            "sandbox_required": True,
            "execution_resources": [
                {
                    "kind": "code_execution",
                    "resource_key": "run",
                    "workspace_access": {"mode": "snapshot"},
                }
            ],
        },
        Executor(),
    )
    dispatcher = ActionDispatcher(registry, Settings(name="call-id-test"))

    with bind_runtime_context(
        agent_execution_context=SimpleNamespace(task_workspace=workspace)
    ):
        first, second = await asyncio.gather(
            dispatcher.async_execute("run", {}),
            dispatcher.async_execute("run", {}),
        )

    assert len(set(observed_call_ids)) == 2
    first_data = cast(dict[str, Any], first)
    second_data = cast(dict[str, Any], second)
    assert first_data["action_call_id"] == first_data["seen_call_id"]
    assert second_data["action_call_id"] == second_data["seen_call_id"]
    assert {first_data["action_call_id"], second_data["action_call_id"]} == set(
        observed_call_ids
    )


@pytest.mark.asyncio
async def test_action_timeout_releases_resource_and_workspace_grant_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingWorkspace(TaskWorkspace):
        def issue_execution_access(self, **kwargs):
            events.append("grant")
            return super().issue_execution_access(**kwargs)

        def close_execution_access(self, grant_id):
            events.append("close")
            return super().close_execution_access(grant_id)

    workspace = RecordingWorkspace(tmp_path / "workspace", execution_id="execution-1")

    class ResourceManager:
        async def async_ensure(self, requirement, owner_id):
            _ = requirement, owner_id
            events.append("ensure")
            return {
                "handle_id": "handle-1",
                "kind": "code_execution",
                "scope": "action_call",
                "resource_key": "run",
                "provider_id": "recording",
                "resource": object(),
                "status": "ready",
            }

        async def async_release(self, handle):
            _ = handle
            events.append("release")

    import agently.base

    monkeypatch.setattr(agently.base, "execution_resource", ResourceManager())

    class Executor:
        kind = "code_execution"
        sandboxed = True

        async def execute(self, **kwargs):
            _ = kwargs
            events.append("execute")
            await asyncio.sleep(1)

    registry = ActionRegistry(name="timeout-order-test")
    registry.register(
        {
            "action_id": "run",
            "name": "run",
            "sandbox_required": True,
            "execution_resources": [
                {
                    "kind": "code_execution",
                    "resource_key": "run",
                    "workspace_access": {"mode": "snapshot"},
                }
            ],
        },
        Executor(),
    )
    dispatcher = ActionDispatcher(registry, Settings(name="timeout-order-test"))

    with bind_runtime_context(
        agent_execution_context=SimpleNamespace(task_workspace=workspace)
    ):
        result = await dispatcher.async_execute(
            "run",
            {},
            trusted_policy_override={"timeout_seconds": 0.01},
        )

    assert result.get("status") == "error"
    assert events == ["grant", "ensure", "execute", "release", "close"]


@pytest.mark.asyncio
async def test_action_reports_release_failure_and_still_revokes_workspace_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingWorkspace(TaskWorkspace):
        def close_execution_access(self, grant_id):
            events.append("close")
            return super().close_execution_access(grant_id)

    workspace = RecordingWorkspace(tmp_path / "workspace", execution_id="execution-1")

    class ResourceManager:
        async def async_ensure(self, requirement, owner_id):
            _ = requirement, owner_id
            return {
                "handle_id": "handle-1",
                "kind": "code_execution",
                "scope": "action_call",
                "resource_key": "run",
                "provider_id": "recording",
                "resource": object(),
                "status": "ready",
            }

        async def async_release(self, handle):
            _ = handle
            events.append("release")
            raise RuntimeError("cleanup failed")

    import agently.base

    monkeypatch.setattr(agently.base, "execution_resource", ResourceManager())

    class Executor:
        kind = "code_execution"
        sandboxed = True

        async def execute(self, **_kwargs):
            return {"ok": True, "status": "success", "value": "done"}

    registry = ActionRegistry(name="release-failure-test")
    registry.register(
        {
            "action_id": "run",
            "name": "run",
            "execution_resources": [
                {
                    "kind": "code_execution",
                    "resource_key": "run",
                    "workspace_access": {"mode": "snapshot"},
                }
            ],
        },
        Executor(),
    )
    dispatcher = ActionDispatcher(registry, Settings(name="release-failure-test"))

    with bind_runtime_context(
        agent_execution_context=SimpleNamespace(task_workspace=workspace)
    ):
        result = await dispatcher.async_execute("run", {})

    assert result.get("status") == "error"
    assert result.get("success") is False
    assert any(
        item.get("code") == "execution_resource.release_failed"
        for item in result.get("diagnostics", [])
    )
    assert events == ["release", "close"]


@pytest.mark.asyncio
async def test_action_timeout_result_includes_release_failure_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="execution-1")

    class ResourceManager:
        async def async_ensure(self, requirement, owner_id):
            _ = requirement, owner_id
            return {
                "handle_id": "handle-timeout-release-failure",
                "kind": "code_execution",
                "scope": "action_call",
                "resource_key": "run",
                "provider_id": "recording",
                "resource": object(),
                "status": "ready",
            }

        async def async_release(self, handle):
            _ = handle
            raise RuntimeError("cleanup failed after timeout")

    import agently.base

    monkeypatch.setattr(agently.base, "execution_resource", ResourceManager())

    class Executor:
        kind = "code_execution"
        sandboxed = True

        async def execute(self, **_kwargs):
            await asyncio.sleep(1)

    registry = ActionRegistry(name="timeout-release-failure-test")
    registry.register(
        {
            "action_id": "run",
            "name": "run",
            "execution_resources": [
                {
                    "kind": "code_execution",
                    "resource_key": "run",
                    "workspace_access": {"mode": "snapshot"},
                }
            ],
        },
        Executor(),
    )
    dispatcher = ActionDispatcher(
        registry,
        Settings(name="timeout-release-failure-test"),
    )

    with bind_runtime_context(
        agent_execution_context=SimpleNamespace(task_workspace=workspace)
    ):
        result = await dispatcher.async_execute(
            "run",
            {},
            trusted_policy_override={"timeout_seconds": 0.01},
        )

    diagnostic_codes = {
        item.get("code") for item in result.get("diagnostics", [])
    }
    assert result.get("status") == "error"
    assert "action.execution.timeout" in diagnostic_codes
    assert "execution_resource.release_failed" in diagnostic_codes
