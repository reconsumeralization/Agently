from __future__ import annotations

from typing import Protocol, runtime_checkable

from agently.types.data.shell import ShellResult


@runtime_checkable
class ShellResource(Protocol):
    """A provider-bound command surface; policy and mounts are not model inputs."""

    async def async_run(self, command: str, *, workdir: str = ".") -> ShellResult: ...
