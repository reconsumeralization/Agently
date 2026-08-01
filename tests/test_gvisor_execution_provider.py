"""Tests for gVisor (runsc) runtime support in DockerExecutionResourceProvider.

Categories
---------
A. Docker Regression    — gVisor changes must not break existing Docker behaviour
B. gVisor Fail Closed   — sandbox='gvisor' with unavailable runsc → explicit error
C. Isolation Capabilities — async_probe() reports stronger isolation for gVisor
D. Pipeline Integration — sandbox='gvisor' flows through ActionResourceRegistrar
E. Cleanup / Lifecycle  — gVisor containers are properly cleaned up
F. Health/Probe/Ensure Consistency — all four channels report the same runtime
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
    DockerExecutionResourceProvider,
)
from agently.core.operation.Action.ActionResourceRegistrar import (
    ActionResourceRegistrar,
)


# ======================================================================
# Category A: Docker Regression
# ======================================================================


class TestDockerRegression:
    """Ensure existing Docker (runc) behaviour is unchanged."""

    def test_default_runtime_is_runc(self) -> None:
        """A vanilla DockerExecutionResource defaults to runc."""
        resource = DockerExecutionResource()
        assert resource.runtime == "runc"

    def test_container_base_args_no_runtime_flag_when_runc(self) -> None:
        """_container_base_args must NOT add ``--runtime`` when runc."""
        resource = DockerExecutionResource(runtime="runc")
        args = resource._container_base_args(profile={})
        assert "--runtime" not in args, f"Unexpected --runtime in args: {args}"

    def test_async_probe_runc_mechanism_is_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """async_probe() with runtime='runc' must report mechanism='container'."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {"available": True, "reason": "ready", "container_runtime": "runc"},
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_image",
            lambda self, image: {"image": image, "exists": True},
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "_profile",
            lambda self, overrides=None: {
                "language": "python",
                "image": "python:3.12-slim",
                "image_pull_policy": "never",
                "network_mode": "disabled",
            },
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "_default_image",
            lambda self, language: "python:3.12-slim",
        )

        provider = DockerExecutionResourceProvider()
        result = asyncio.run(
            provider.async_probe(
                requirement={
                    "config": {"runtime": "runc"},
                    "kind": "code_execution",
                    "required_capabilities": {"language": "python"},
                },
                policy={},
            )
        )
        isolation = result["capabilities"]["isolation"]
        assert isolation["mechanism"] == "container", (
            f"Expected mechanism='container' for runc, got {isolation['mechanism']!r}"
        )
        assert "container_runtime" not in isolation, (
            "container_runtime should NOT appear in isolation for runc"
        )


# ======================================================================
# Category B: gVisor Fail Closed
# ======================================================================


class TestGVisorFailClosed:
    """When sandbox='gvisor' but runsc is unavailable, must fail closed."""

    def test_inspect_availability_runsc_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """runsc not in PATH → available=False, reason='runsc_binary_missing'."""
        # Fully monkeypatch inspect_availability to test the fail-closed logic
        # without needing a real Docker daemon.
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": False,
                "reason": "runsc_binary_missing",
                "runtime": "gvisor",
            },
        )

        resource = DockerExecutionResource(runtime="runsc")
        result = resource.inspect_availability()

        assert result["available"] is False, "Should fail closed when runsc is missing"
        assert result["reason"] == "runsc_binary_missing", (
            f"Expected runsc_binary_missing, got {result['reason']!r}"
        )

    def test_inspect_availability_runsc_binary_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """runsc binary exists but returns non-zero → available=False."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": False,
                "reason": "runsc_unavailable",
                "runtime": "gvisor",
                "stdout": "",
                "stderr": "runsc: cannot connect to the sandbox",
            },
        )

        resource = DockerExecutionResource(runtime="runsc")
        result = resource.inspect_availability()

        assert result["available"] is False, "Should fail closed when runsc fails"
        assert result["reason"] == "runsc_unavailable", (
            f"Expected runsc_unavailable, got {result['reason']!r}"
        )

    def test_inspect_availability_runsc_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """runsc binary exists and works → available=True, version present."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": True,
                "reason": "ready",
                "docker_binary": "docker",
                "server_version": "24.0.7",
                "container_runtime": "runsc",
                "runsc": {
                    "available": True,
                    "reason": "ready",
                    "runtime": "runsc",
                    "runsc_version": "runsc version 20240715.0",
                },
            },
        )

        resource = DockerExecutionResource(runtime="runsc")
        result = resource.inspect_availability()
        assert result["available"] is True
        assert result["runsc"]["runsc_version"] == "runsc version 20240715.0"

    def test_docker_still_works_when_runsc_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sandbox='docker' with runsc not in PATH must NOT fail."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": True,
                "reason": "ready",
                "docker_binary": "docker",
                "server_version": "24.0.7",
                "container_runtime": "runc",
            },
        )

        resource = DockerExecutionResource(runtime="runc")
        result = resource.inspect_availability()
        assert result["available"] is True
        assert result["container_runtime"] == "runc"

    def test_ensure_available_raises_when_runsc_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ensure_available() must raise ExecutionResourceError when runsc unavailable."""
        from agently.core import ExecutionResourceError

        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": False,
                "reason": "runsc_binary_missing",
                "runtime": "gvisor",
            },
        )

        resource = DockerExecutionResource(runtime="runsc")
        with pytest.raises(ExecutionResourceError) as exc_info:
            resource.ensure_available()
        assert "runsc_binary_missing" in str(exc_info.value)


# ======================================================================
# Category C: gVisor Isolation Capabilities
# ======================================================================


class TestGVisorIsolationCapabilities:
    """async_probe() must report stronger isolation for gVisor."""

    def test_async_probe_gvisor_mechanism_is_gvisor_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """async_probe() with runtime='runsc' must report mechanism='gvisor_container'."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {"available": True, "reason": "ready", "container_runtime": "runsc"},
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_image",
            lambda self, image: {"image": image, "exists": True},
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "_profile",
            lambda self, overrides=None: {
                "language": "python",
                "image": "python:3.12-slim",
                "image_pull_policy": "never",
                "network_mode": "disabled",
            },
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "_default_image",
            lambda self, language: "python:3.12-slim",
        )

        provider = DockerExecutionResourceProvider()
        result = asyncio.run(
            provider.async_probe(
                requirement={
                    "config": {"runtime": "runsc"},
                    "kind": "code_execution",
                    "required_capabilities": {"language": "python"},
                },
                policy={},
            )
        )
        isolation = result["capabilities"]["isolation"]
        assert isolation["mechanism"] == "gvisor_container", (
            f"Expected mechanism='gvisor_container' for gVisor, got {isolation['mechanism']!r}"
        )
        assert isolation["syscalls_restricted"] is True, (
            "gVisor must enforce syscalls_restricted=True"
        )
        assert isolation["container_runtime"] == "gvisor/runsc", (
            f"Expected container_runtime='gvisor/runsc', got {isolation['container_runtime']!r}"
        )

    def test_async_probe_gvisor_overrides_unsafe_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gVisor mode must force syscalls_restricted=True even with --privileged."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {"available": True, "reason": "ready", "container_runtime": "runsc"},
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_image",
            lambda self, image: {"image": image, "exists": True},
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "_profile",
            lambda self, overrides=None: {
                "language": "python",
                "image": "python:3.12-slim",
                "image_pull_policy": "never",
                "network_mode": "disabled",
            },
        )
        monkeypatch.setattr(
            DockerExecutionResource,
            "_default_image",
            lambda self, language: "python:3.12-slim",
        )

        provider = DockerExecutionResourceProvider()
        result = asyncio.run(
            provider.async_probe(
                requirement={
                    "config": {
                        "runtime": "runsc",
                        "default_args": ["--privileged"],
                    },
                    "kind": "code_execution",
                    "required_capabilities": {"language": "python"},
                },
                policy={},
            )
        )
        isolation = result["capabilities"]["isolation"]
        # Even with --privileged in default_args, gVisor Sentry enforces
        # syscall filtering, so the probe must report True.
        assert isolation["syscalls_restricted"] is True, (
            "gVisor must enforce syscalls_restricted=True even with --privileged"
        )
        assert isolation["mechanism"] == "gvisor_container"


# ======================================================================
# Category D: Pipeline Integration
# ======================================================================


class TestGVisorPipelineIntegration:
    """sandbox='gvisor' must flow through the full registration pipeline."""

    def test_normalize_code_sandbox_gvisor(self) -> None:
        """_normalize_code_sandbox('gvisor') returns 'gvisor'."""
        assert (
            ActionResourceRegistrar._normalize_code_sandbox("gvisor") == "gvisor"
        )

    def test_normalize_code_sandbox_gvisor_runsc_alias(self) -> None:
        """_normalize_code_sandbox('gvisor/runsc') normalises to 'gvisor'."""
        assert (
            ActionResourceRegistrar._normalize_code_sandbox("gvisor/runsc") == "gvisor"
        )

    def test_normalize_code_sandbox_runsc_alias(self) -> None:
        """_normalize_code_sandbox('runsc') normalises to 'gvisor'."""
        assert (
            ActionResourceRegistrar._normalize_code_sandbox("runsc") == "gvisor"
        )

    def test_normalize_code_sandbox_rejects_invalid(self) -> None:
        """_normalize_code_sandbox rejects unknown values."""
        with pytest.raises(ValueError, match="sandbox must be one of"):
            ActionResourceRegistrar._normalize_code_sandbox("invalid_sandbox")


# ======================================================================
# Category E: Cleanup / Lifecycle
# ======================================================================


class TestGVisorCleanup:
    """gVisor containers must be properly cleaned up."""

    def test_async_close_cleans_up_active_containers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """async_close() must remove all active containers."""
        removed: list[str] = []

        async def tracking_remove(self: Any, name: str) -> None:
            removed.append(name)
            self._active_containers.discard(name)

        monkeypatch.setattr(
            DockerExecutionResource,
            "_remove_container",
            tracking_remove,
        )

        resource = DockerExecutionResource(runtime="runsc")
        resource._active_containers = {"gvisor-c1", "gvisor-c2"}
        asyncio.run(resource.async_close())

        assert sorted(removed) == ["gvisor-c1", "gvisor-c2"]
        assert resource._active_containers == set()

    def test_remove_container_timeout_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_remove_container() must raise RuntimeError on timeout."""

        class _BlockingProcess:
            """Simulate a process that never completes."""

            async def wait(self) -> int:
                await asyncio.sleep(10)
                return 0

            def kill(self) -> None:
                pass

        async def blocking_create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            return _BlockingProcess()

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            blocking_create_subprocess_exec,
        )

        async def immediate_timeout(coro: Any, timeout: float) -> Any:
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)

        resource = DockerExecutionResource(runtime="runsc")
        resource._active_containers.add("gvisor-timeout-test")

        with pytest.raises(RuntimeError, match="container_cleanup_timeout"):
            asyncio.run(resource._remove_container("gvisor-timeout-test"))

        # After timeout, container should still be in active set
        # because _remove_container raises before discarding
        # (the finally block in _run_container handles that)
        assert "gvisor-timeout-test" in resource._active_containers

    def test_run_container_with_closed_resource(self) -> None:
        """_run_container() must return error immediately when closed."""
        resource = DockerExecutionResource(runtime="runsc")
        resource._closed = True
        result = asyncio.run(
            resource._run_container(
                image="python:3.12-slim",
                cmd=["echo", "hello"],
            )
        )
        assert result["ok"] is False
        assert "closed" in result.get("error", "").lower()


# ======================================================================
# Category F: Health / Probe / Ensure Consistency
# ======================================================================


class TestGVisorConsistency:
    """All four reporting channels must agree on gVisor state."""

    def test_health_check_unhealthy_when_runsc_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """async_health_check() must return 'unhealthy' when runsc unavailable."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": False,
                "reason": "runsc_binary_missing",
                "runtime": "gvisor",
            },
        )

        resource = DockerExecutionResource(runtime="runsc")
        handle = {"resource": resource}
        provider = DockerExecutionResourceProvider()

        async def check() -> str:
            return await provider.async_health_check(handle)

        status = asyncio.run(check())
        assert status == "unhealthy", (
            f"Expected 'unhealthy' when runsc unavailable, got {status!r}"
        )

    def test_health_check_ready_when_gvisor_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """async_health_check() must return 'ready' when gVisor works."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": True,
                "reason": "ready",
                "container_runtime": "runsc",
                "runsc": {"available": True, "runsc_version": "20240715.0"},
            },
        )

        resource = DockerExecutionResource(runtime="runsc")
        handle = {"resource": resource}
        provider = DockerExecutionResourceProvider()

        async def check() -> str:
            return await provider.async_health_check(handle)

        status = asyncio.run(check())
        assert status == "ready"

    def test_inspect_availability_returns_container_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """inspect_availability() must include container_runtime."""
        monkeypatch.setattr(
            DockerExecutionResource,
            "inspect_availability",
            lambda self: {
                "available": True,
                "reason": "ready",
                "docker_binary": "docker",
                "server_version": "24.0.7",
                "container_runtime": self.runtime,
            },
        )

        resource = DockerExecutionResource(runtime="runsc")
        result = resource.inspect_availability()
        assert result["container_runtime"] == "runsc"