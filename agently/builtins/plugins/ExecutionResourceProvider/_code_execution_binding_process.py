# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence

from ._bounded_process import BoundedProcessResult, _terminate_process
from ._code_execution_binding_bridge import (
    CODE_EXECUTION_BINDING_STDIO_MAGIC,
    CodeExecutionBindingBridge,
)


class BindingStdioProtocolError(RuntimeError):
    pass


class _BoundedCapture:
    def __init__(self, limit: int, *, fail_on_overflow: bool) -> None:
        self.limit = max(1, int(limit))
        self.fail_on_overflow = fail_on_overflow
        self.value = bytearray()
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        remaining = self.limit - len(self.value)
        if remaining > 0:
            self.value.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True
            if self.fail_on_overflow:
                raise BindingStdioProtocolError("binding program wrote unframed stdout beyond the log limit")


def _encode_frame(payload: bytes) -> bytes:
    return CODE_EXECUTION_BINDING_STDIO_MAGIC + f"{len(payload):08x}".encode("ascii") + b":" + payload


async def _consume_binding_stdout(
    stream: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
    *,
    bridge: CodeExecutionBindingBridge,
    max_output_bytes: int,
) -> tuple[bytes, bool]:
    if stream is None or writer is None:
        raise BindingStdioProtocolError("binding program requires owned stdin/stdout pipes")
    magic = CODE_EXECUTION_BINDING_STDIO_MAGIC
    header_bytes = len(magic) + 9
    buffer = bytearray()
    unframed = _BoundedCapture(max_output_bytes, fail_on_overflow=True)
    response_tasks: set[asyncio.Task[None]] = set()
    response_write_lock = asyncio.Lock()
    response_failure: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def settle_response_task(task: asyncio.Task[None]) -> None:
        response_tasks.discard(task)
        if task.cancelled() or response_failure.done():
            return
        error = task.exception()
        if error is not None:
            response_failure.set_exception(error)

    async def write_response(payload: bytes) -> None:
        if len(payload) > bridge.limits.max_frame_bytes:
            raise BindingStdioProtocolError("host binding response exceeds the frame limit")
        writer.write(_encode_frame(payload))
        await asyncio.wait_for(
            writer.drain(),
            timeout=bridge.limits.frame_timeout_seconds,
        )

    async def process_protocol_frame(payload: bytes) -> None:
        response = await bridge.async_process_stdio_frame(payload)
        async with response_write_lock:
            await write_response(response)

    while True:
        read_task = asyncio.create_task(stream.read(65536))
        done, _pending = await asyncio.wait(
            {read_task, response_failure},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if response_failure in done:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
            await response_failure
            raise AssertionError("unreachable")
        chunk = read_task.result()
        if chunk:
            buffer.extend(chunk)
        while buffer:
            marker = buffer.find(magic)
            if marker < 0:
                keep = min(len(buffer), len(magic) - 1)
                if len(buffer) > keep:
                    unframed.add(bytes(buffer[:-keep]))
                    del buffer[:-keep]
                break
            if marker:
                unframed.add(bytes(buffer[:marker]))
                del buffer[:marker]
            if len(buffer) < header_bytes:
                break
            if buffer[len(magic) + 8 : header_bytes] != b":":
                raise BindingStdioProtocolError("binding program emitted a malformed protocol header")
            raw_size = bytes(buffer[len(magic) : len(magic) + 8])
            try:
                frame_size = int(raw_size, 16)
            except ValueError as error:
                raise BindingStdioProtocolError("binding program emitted an invalid protocol length") from error
            if frame_size < 1 or frame_size > bridge.limits.max_frame_bytes:
                raise BindingStdioProtocolError("binding program protocol frame exceeds the byte limit")
            complete_size = header_bytes + frame_size
            if len(buffer) < complete_size:
                break
            payload = bytes(buffer[header_bytes:complete_size])
            del buffer[:complete_size]
            task = asyncio.create_task(process_protocol_frame(payload))
            response_tasks.add(task)
            task.add_done_callback(settle_response_task)
        if not chunk:
            break
    if buffer:
        if buffer.startswith(magic):
            raise BindingStdioProtocolError("binding program protocol frame ended before completion")
        unframed.add(bytes(buffer))
    if response_tasks:
        await asyncio.gather(*tuple(response_tasks))
    if response_failure.done():
        await response_failure
    return bytes(unframed.value), unframed.truncated


async def _drain_stderr(
    stream: asyncio.StreamReader | None,
    *,
    max_output_bytes: int,
) -> tuple[bytes, bool]:
    capture = _BoundedCapture(max_output_bytes, fail_on_overflow=True)
    if stream is None:
        return b"", False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        try:
            capture.add(chunk)
        except BindingStdioProtocolError as error:
            raise BindingStdioProtocolError("binding program wrote stderr beyond the log limit") from error
    return bytes(capture.value), capture.truncated


async def run_bounded_binding_process(
    argv: Sequence[str],
    *,
    bridge: CodeExecutionBindingBridge,
    timeout: float,
    max_output_bytes: int,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    on_terminate: Callable[[], Awaitable[None]] | None = None,
) -> tuple[BoundedProcessResult, str]:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("bounded binding process argv requires non-empty strings")
    create_kwargs = {
        "cwd": cwd,
        "env": dict(env) if env is not None else None,
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        create_kwargs["start_new_session"] = True
    process = await asyncio.create_subprocess_exec(*argv, **create_kwargs)
    stdout_task = asyncio.create_task(
        _consume_binding_stdout(
            process.stdout,
            process.stdin,
            bridge=bridge,
            max_output_bytes=max_output_bytes,
        )
    )
    stderr_task = asyncio.create_task(
        _drain_stderr(
            process.stderr,
            max_output_bytes=max_output_bytes,
        )
    )
    wait_task = asyncio.create_task(process.wait())
    timed_out = False
    transport_error = ""

    async def terminate_owned_process() -> None:
        process_was_running = process.returncode is None
        await _terminate_process(process)
        if process_was_running and on_terminate is not None:
            await on_terminate()

    try:
        done, _pending = await asyncio.wait(
            {wait_task, stdout_task, stderr_task},
            timeout=max(0.001, float(timeout)),
            return_when=asyncio.FIRST_EXCEPTION,
        )
        failed_stream_task = next(
            (task for task in (stdout_task, stderr_task) if task in done and task.exception() is not None),
            None,
        )
        if failed_stream_task is not None:
            error = failed_stream_task.exception()
            if error is not None:
                transport_error = f"{type(error).__name__}:{str(error)[:500]}"
                await terminate_owned_process()
                if not stdout_task.done():
                    stdout_task.cancel()
        if not wait_task.done():
            if transport_error:
                await wait_task
            else:
                timed_out = True
                await terminate_owned_process()
                if not stdout_task.done():
                    stdout_task.cancel()
                if not stderr_task.done():
                    stderr_task.cancel()
        if not stdout_task.done():
            try:
                await stdout_task
            except asyncio.CancelledError:
                if not (timed_out or transport_error):
                    raise
    except asyncio.CancelledError:
        await asyncio.shield(terminate_owned_process())
        for task in (stdout_task, stderr_task, wait_task):
            task.cancel()
        await asyncio.shield(
            asyncio.gather(
                stdout_task,
                stderr_task,
                wait_task,
                return_exceptions=True,
            )
        )
        raise
    finally:
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    stdout_result = await asyncio.gather(stdout_task, return_exceptions=True)
    if isinstance(stdout_result[0], tuple):
        stdout, stdout_truncated = stdout_result[0]
    else:
        stdout, stdout_truncated = b"", False
        if not transport_error:
            error = stdout_result[0]
            transport_error = f"{type(error).__name__}:{str(error)[:500]}"
    stderr_result = await asyncio.gather(stderr_task, return_exceptions=True)
    if isinstance(stderr_result[0], tuple):
        stderr, stderr_truncated = stderr_result[0]
    else:
        stderr, stderr_truncated = b"", False
        if not transport_error:
            error = stderr_result[0]
            transport_error = f"{type(error).__name__}:{str(error)[:500]}"
    if transport_error:
        bridge.record_transport_error(transport_error)
    await asyncio.gather(wait_task, return_exceptions=True)
    return (
        BoundedProcessResult(
            returncode=(
                124
                if timed_out
                else (1 if transport_error and int(process.returncode or 0) == 0 else int(process.returncode or 0))
            ),
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            timed_out=timed_out,
        ),
        transport_error,
    )


__all__ = [
    "BindingStdioProtocolError",
    "run_bounded_binding_process",
]
