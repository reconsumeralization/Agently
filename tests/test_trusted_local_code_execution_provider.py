from __future__ import annotations

from pathlib import Path
import asyncio
import os
import signal

import pytest

from agently.builtins.plugins.CodeRuntimeAdapter import PythonCodeRuntimeAdapter
from agently.builtins.plugins.ExecutionResourceProvider.TrustedLocalExecutionResourceProvider import (
    TrustedLocalExecutionResourceProvider,
)
from agently.core import ExecutionResourceError
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessRequirement


@pytest.mark.asyncio
async def test_trusted_local_probe_is_explicitly_unsafe_and_reports_real_tools() -> None:
    provider = TrustedLocalExecutionResourceProvider()

    probe = await provider.async_probe(
        requirement={"kind": "code_execution"},
        policy={},
    )

    assert probe["provider_id"] == "trusted_local"
    assert probe["available"] is True
    assert probe["capabilities"]["isolation"] == {
        "process_contained": False,
        "host_filesystem_restricted": False,
        "privilege_escalation_blocked": False,
        "syscalls_restricted": False,
        "mechanism": "trusted_local",
    }
    assert "python" in probe["capabilities"]["languages"]
    python_toolchain = probe["capabilities"]["toolchains"]["python"]
    assert python_toolchain["available"] is True
    assert python_toolchain["version"].startswith("3.")
    assert python_toolchain["raw_version"]


@pytest.mark.asyncio
async def test_trusted_local_requires_host_unsafe_opt_in() -> None:
    provider = TrustedLocalExecutionResourceProvider()

    with pytest.raises(ExecutionResourceError) as raised:
        await provider.async_ensure(
            requirement={"kind": "code_execution", "config": {}},
            policy={},
        )

    assert raised.value.code == "execution_resource.trusted_local_not_allowed"


@pytest.mark.asyncio
async def test_trusted_local_executes_only_the_materialized_workspace_bundle(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="run-1")
    grant = workspace.issue_execution_access(
        action_call_id="run",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="python",
            source_code=(
                "from pathlib import Path\n"
                "Path('../output/result.txt').write_text('local-ok')\n"
                "print('stdout-ok')\n"
            ),
            expected_outputs=["output/result.txt"],
        ),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    provider = TrustedLocalExecutionResourceProvider()
    handle = await provider.async_ensure(
        requirement={
            "kind": "code_execution",
            "config": {"allow_unsafe_local": True},
            "task_workspace_access_grant": grant,
        },
        policy={"max_output_bytes": 10000},
    )

    result = await handle["resource"].async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=10,
    )

    assert result["ok"] is True
    assert result["stdout"] == "stdout-ok\n"
    assert result["outputs"] == ["output/result.txt"]
    assert (Path(grant.execution_area) / "output" / "result.txt").read_text() == "local-ok"


@pytest.mark.asyncio
async def test_trusted_local_bounds_generated_output_and_persisted_logs(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="run-bounded")
    grant = workspace.issue_execution_access(
        action_call_id="act_call_bounded",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="python",
            source_code="print('x' * 200000)\n",
        ),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    provider = TrustedLocalExecutionResourceProvider()
    handle = await provider.async_ensure(
        requirement={
            "kind": "code_execution",
            "config": {"allow_unsafe_local": True},
            "task_workspace_access_grant": grant,
        },
        policy={"max_output_bytes": 128},
    )

    result = await handle["resource"].async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=10,
    )

    stdout_log = Path(grant.execution_area) / result["log_refs"][0]
    assert result["ok"] is True
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode()) <= 128
    assert stdout_log.stat().st_size <= 128


@pytest.mark.asyncio
async def test_trusted_local_cancellation_terminates_the_process_group(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="run-cancel")
    grant = workspace.issue_execution_access(
        action_call_id="act_call_cancel",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="python",
            source_code=(
                "import os, time\n"
                "from pathlib import Path\n"
                "Path('../output/pid.txt').write_text(str(os.getpid()))\n"
                "while True:\n"
                "    print('running', flush=True)\n"
                "    time.sleep(0.05)\n"
            ),
            expected_outputs=["output/pid.txt"],
        ),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    provider = TrustedLocalExecutionResourceProvider()
    handle = await provider.async_ensure(
        requirement={
            "kind": "code_execution",
            "config": {"allow_unsafe_local": True},
            "task_workspace_access_grant": grant,
        },
        policy={"max_output_bytes": 1024},
    )
    task = asyncio.create_task(
        handle["resource"].async_execute_code(
            bundle=bundle,
            manifest=manifest,
            grant=grant,
            timeout=30,
        )
    )
    pid_path = Path(grant.execution_area) / "output" / "pid.txt"
    for _ in range(100):
        if pid_path.is_file():
            break
        await asyncio.sleep(0.02)
    assert pid_path.is_file()
    pid = int(pid_path.read_text())

    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
