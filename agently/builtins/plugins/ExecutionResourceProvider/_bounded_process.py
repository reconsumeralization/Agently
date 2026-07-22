from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool = False


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
    max_output_bytes: int,
    on_terminate: Callable[[], Awaitable[None]] | None = None,
) -> BoundedProcessResult:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("bounded process argv requires non-empty strings")
    limit = max(1, int(max_output_bytes))
    create_kwargs = {
        "cwd": cwd,
        "env": dict(env) if env is not None else None,
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        create_kwargs["start_new_session"] = True
    process = await asyncio.create_subprocess_exec(*argv, **create_kwargs)

    async def drain(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        captured = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            remaining = limit - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(captured), truncated

    stdout_task = asyncio.create_task(drain(process.stdout))
    stderr_task = asyncio.create_task(drain(process.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.001, float(timeout)))
    except asyncio.TimeoutError:
        timed_out = True
        await _terminate_process(process)
        if on_terminate is not None:
            await on_terminate()
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process(process))
        if on_terminate is not None:
            await asyncio.shield(on_terminate())
        await asyncio.shield(
            asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        )
        raise
    (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
        stdout_task,
        stderr_task,
    )
    return BoundedProcessResult(
        returncode=(124 if timed_out else int(process.returncode or 0)),
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        timed_out=timed_out,
    )


__all__ = ["BoundedProcessResult", "run_bounded_process"]
