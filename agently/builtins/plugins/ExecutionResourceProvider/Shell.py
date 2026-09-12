# Copyright 2023-2026 AgentEra(Agently.Tech)
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
"""Native execution resources, not sandbox or approval grants.

These implementation classes are not automatically registered as model tools.
The caller owns authorization. Each call owns and settles its process; providers
can compose this resource without depending on the legacy Cmd Action package.
"""

from __future__ import annotations

import asyncio
import base64
import locale
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


class Shell:
    """Shared native argv execution; no shell parsing or implicit working root."""

    @staticmethod
    def _kill(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                # A finished parent can leave descendants holding its pipes.
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                # Native Windows process-tree containment is a separate backend
                # acceptance item. This preserves Cmd's existing parent cleanup.
                process.kill()
        except ProcessLookupError:
            pass

    async def run_argv(
        self,
        argv: Sequence[str],
        *,
        workdir: Path,
        timeout: float | None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run exact tokens, including empty arguments, without invoking a shell.

        Nonzero exit codes are returned. TimeoutExpired retains partial bytes;
        cancellation propagates only after owned cleanup has settled. Output is
        collected in full for legacy artifact compatibility; bounded streaming
        capture is not yet provided by this resource.
        """
        args = list(argv)
        if not args or not args[0] or any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise ValueError("argv requires an executable and string arguments without NUL bytes")
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *args,
                cwd=str(workdir),
                env=dict(env) if env is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        )
        output: asyncio.Task[tuple[bytes, bytes]] | None = None

        async def stop() -> None:
            process = await spawn
            self._kill(process)
            if output is None:
                await process.communicate()
            else:
                await output
            await process.wait()

        timed_out = False
        try:
            # Shield creation too: cancellation must not lose a just-created
            # process whose handle has not yet reached this coroutine.
            process = await asyncio.shield(spawn)
            output = asyncio.create_task(process.communicate())
            try:
                stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
                self._kill(process)
                stdout, stderr = await asyncio.shield(output)
        except BaseException:
            cleanup = asyncio.create_task(stop())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated cancellation must not abandon owned cleanup.
                    continue
            cleanup.result()
            raise
        if timed_out:
            assert timeout is not None
            raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
        assert process.returncode is not None
        encoding = locale.getpreferredencoding(False)

        def text(value: bytes) -> str:
            return value.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")

        return subprocess.CompletedProcess(args, process.returncode, text(stdout), text(stderr))

    @staticmethod
    def _check(command: str) -> None:
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("command must be non-empty shell source without NUL bytes")
        if len(command.encode("utf-8")) > 65536:
            raise ValueError("command exceeds 65536 UTF-8 bytes")


class BashExecutor(Shell):
    """One native Bash script. Filesystem and network access are not sandboxed."""

    def __init__(self, binary: str = "bash") -> None:
        self.binary = binary

    async def run(
        self,
        command: str,
        *,
        workdir: Path,
        timeout: float | None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._check(command)
        # The source is one argv item. Do not split it or concatenate it into
        # an outer shell command. Bash owns pipes, quoting and exit semantics.
        return await self.run_argv(
            [self.binary, "--noprofile", "--norc", "-c", command],
            workdir=workdir,
            timeout=timeout,
            env=env,
        )


class PowerShellExecutor(Shell):
    """Native PowerShell transport; not a Bash adapter or a Windows sandbox."""

    def __init__(self, binary: str = "powershell.exe") -> None:
        # Host code can explicitly select pwsh. No interpreter fallback.
        self.binary = binary

    async def run(
        self,
        command: str,
        *,
        workdir: Path,
        timeout: float | None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._check(command)
        source = command.encode("utf-16-le")
        # Keep the encoded argument below Windows' command-line limit rather
        # than starting a partial script. Longer source needs a file transport.
        if len(source) > 16384:
            raise ValueError("PowerShell command exceeds 16384 UTF-16LE bytes")
        encoded = base64.b64encode(source).decode("ascii")
        return await self.run_argv(
            [self.binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded],
            workdir=workdir,
            timeout=timeout,
            env=env,
        )
