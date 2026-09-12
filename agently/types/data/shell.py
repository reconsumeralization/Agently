"""Host-owned Shell configuration axes and soft-risk contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, TypeAlias
from typing_extensions import TypedDict

ShellLanguage: TypeAlias = Literal["bash", "powershell"]
ShellEnvironment: TypeAlias = Literal["offline", "online", "host"]
ShellApproval: TypeAlias = Literal["all", "write", "delete", "none"]
ShellEffect: TypeAlias = Literal["read", "write", "delete", "network", "privilege"]


class ShellRisk(TypedDict):
    """Advisory effects, never authorization or proof that a command is safe."""

    effects: list[ShellEffect]
    uncertainties: list[str]
    reason: str


ShellRiskHandler: TypeAlias = Callable[[Mapping[str, object]], ShellRisk | Awaitable[ShellRisk]]


class ShellResult(TypedDict):
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
