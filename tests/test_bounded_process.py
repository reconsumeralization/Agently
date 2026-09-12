from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from agently.builtins.plugins.ExecutionResourceProvider._bounded_process import run_bounded_process
from agently.builtins.plugins.ExecutionResourceProvider._windows_job import WindowsJob


def test_windows_startup_diagnostic_preserves_oserror(tmp_path: Path) -> None:
    job = WindowsJob.__new__(WindowsJob)
    job._error_path = tmp_path / "startup.json"
    assert job.startup_error() is None
    job._error_path.write_text('{"errno":2,"message":"missing","filename":"tool.exe","winerror":null}')
    error = job.startup_error()
    assert isinstance(error, FileNotFoundError) and error.filename == "tool.exe"
    job._error_path.write_text('{"errno":true,"message":"bad"}')
    with pytest.raises(RuntimeError, match="Invalid"):
        job.startup_error()
    job._error_path.write_bytes(b"x" * 8193)
    with pytest.raises(RuntimeError, match="exceeds"):
        job.startup_error()


def test_windows_startup_diagnostic_rejects_symlink(tmp_path: Path) -> None:
    job = WindowsJob.__new__(WindowsJob)
    job._error_path = tmp_path / "startup.json"
    (tmp_path / "target").write_text("private")
    job._error_path.symlink_to(tmp_path / "target")
    with pytest.raises(RuntimeError, match="non-reparse"):
        job.startup_error()


@pytest.mark.asyncio
async def test_bounded_output_drains_both_channels() -> None:
    result = await run_bounded_process(
        [sys.executable, "-c", "import os; os.write(1,b'a'*200000); os.write(2,b'b'*200000)"],
        timeout=5, max_output_bytes=31,
    )
    assert result.returncode == 0
    assert result.stdout == b"a" * 31 and result.stderr == b"b" * 31
    assert result.stdout_truncated and result.stderr_truncated


@pytest.mark.asyncio
async def test_empty_argv_argument_is_literal() -> None:
    result = await run_bounded_process(
        [sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", ""],
        timeout=5, max_output_bytes=100,
    )
    assert result.stdout == b"''\n"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group evidence")
@pytest.mark.parametrize("cancel", [False, True])
async def test_exited_parent_with_open_descendant_pipe_is_settled(tmp_path: Path, cancel: bool) -> None:
    marker = tmp_path / "child.pid"
    source = (
        "import os,time,pathlib\n"
        "pid=os.fork()\n"
        "if pid: os._exit(0)\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "print('ready',flush=True)\n"
        "time.sleep(30)\n"
    )
    calls: list[str] = []

    async def cleanup() -> None:
        calls.append("cleanup")

    task = asyncio.create_task(run_bounded_process(
        [sys.executable, "-c", source], timeout=1, max_output_bytes=100, on_terminate=cleanup,
    ))
    try:
        async with _Deadline(5):
            while not marker.exists():
                await asyncio.sleep(0.01)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert result.timed_out and result.returncode == 124
                assert result.stdout == b"ready\n"
        assert calls == ["cleanup"]
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _Deadline:
    """Test-only 3.10-compatible watchdog, never a task strategy limit."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def __aenter__(self) -> None:
        self.task = asyncio.current_task()
        assert self.task is not None
        self.timer = asyncio.get_running_loop().call_later(self.seconds, self.task.cancel)

    async def __aexit__(self, *args: object) -> None:
        self.timer.cancel()


@pytest.mark.asyncio
async def test_cancel_during_spawn_keeps_process_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    real_spawn = asyncio.create_subprocess_exec
    created = asyncio.Event()
    deliver = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def delayed(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        created.set()
        await deliver.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=5, max_output_bytes=100,
    ))
    await asyncio.wait_for(created.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    deliver.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_single_cleanup() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def cleanup() -> None:
        calls.append("start")
        entered.set()
        await release.wait()
        calls.append("end")

    task = asyncio.create_task(run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.1, max_output_bytes=100, on_terminate=cleanup,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert calls == ["start", "end"]


@pytest.mark.asyncio
async def test_cleanup_failure_is_not_success() -> None:
    calls: list[str] = []

    async def cleanup() -> None:
        calls.append("cleanup")
        raise RuntimeError("provider cleanup failed")

    with pytest.raises(RuntimeError, match="provider cleanup failed"):
        await run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.1, max_output_bytes=100, on_terminate=cleanup,
        )
    assert calls == ["cleanup"]


@pytest.mark.asyncio
async def test_missing_executable_has_no_cleanup_callback(tmp_path: Path) -> None:
    async def cleanup() -> None:
        raise AssertionError("no process was created")

    with pytest.raises(FileNotFoundError):
        await run_bounded_process(
            [str(tmp_path / "missing")], timeout=5, max_output_bytes=100, on_terminate=cleanup,
        )
