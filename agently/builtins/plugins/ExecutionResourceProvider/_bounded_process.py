from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from ._windows_job import WindowsJob


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool = False


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        # One kill of the entire owned group, even after its leader exited.
        # Do not signal it again after reaping: an orphan/zombie group may no
        # longer be signalable by this account (notably on macOS).
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        return
    if process.returncode is not None and os.name != "posix":
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass
    try:
        if process.returncode is None:
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None,
    max_output_bytes: int | None,
    on_terminate: Callable[[], Awaitable[None]] | None = None,
    stdin: int | None = asyncio.subprocess.DEVNULL,
) -> BoundedProcessResult:
    if not argv or not argv[0] or any(not isinstance(item, str) or "\0" in item for item in argv):
        raise ValueError("bounded process argv requires an executable and NUL-free string arguments")
    limit = max(1, int(max_output_bytes)) if max_output_bytes is not None else None
    create_kwargs = {
        "cwd": cwd,
        "env": dict(env) if env is not None else None,
        "stdin": stdin,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        create_kwargs["start_new_session"] = True
    job = WindowsJob() if os.name == "nt" else None
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*(job.argv(list(argv)) if job else argv), **create_kwargs))

    async def drain(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        captured = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            remaining = limit - len(captured) if limit is not None else len(chunk)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(captured), truncated

    async def collect(process: asyncio.subprocess.Process) -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
        streams = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
        waited = asyncio.create_task(process.wait())
        try:
            stdout, stderr = await asyncio.gather(*streams)
            await waited
            return stdout, stderr
        finally:
            for task in [*streams, waited]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(streams[0], streams[1], waited, return_exceptions=True)

    output: asyncio.Task[tuple[tuple[bytes, bool], tuple[bytes, bool]]] | None = None
    cleanup: asyncio.Task[None] | None = None

    async def stop() -> None:
        process = await spawn
        try:
            if job is not None:
                # Stop the bootstrap before terminating its job: it must not
                # join/start a target between job termination and pipe drain.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                job.terminate()
            await _terminate_process(process)
            if on_terminate is not None:
                await on_terminate()
        finally:
            # Even a failed provider cleanup must settle the local pipes.
            if output is not None:
                await output
            else:
                await collect(process)

    async def settle() -> None:
        nonlocal cleanup
        if cleanup is None:
            cleanup = asyncio.create_task(stop())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    timed_out = False
    startup_error: OSError | None = None
    try:
        process = await asyncio.shield(spawn)
        output = asyncio.create_task(collect(process))
        try:
            await asyncio.wait_for(asyncio.shield(output), timeout=max(0.001, float(timeout)) if timeout is not None else None)
        except asyncio.TimeoutError:
            timed_out = True
            await settle()
        if cleanup is None and os.name == "posix":
            # Detached background work is not part of this one-shot contract.
            # Even closed descendant pipes do not transfer process ownership.
            await _terminate_process(process)
    except BaseException:
        await settle()
        raise
    finally:
        if job is not None:
            try:
                job.terminate()
                startup_error = job.startup_error()
            finally:
                job.close()
    if startup_error is not None:
        raise startup_error
    (stdout, stdout_truncated), (stderr, stderr_truncated) = output.result()
    return BoundedProcessResult(
        returncode=(124 if timed_out else int(process.returncode or 0)),
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        timed_out=timed_out,
    )


__all__ = ["BoundedProcessResult", "run_bounded_process"]
