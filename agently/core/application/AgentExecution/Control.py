"""Non-terminal signals for explicit AgentExecution lifecycle control."""

from __future__ import annotations


class AgentExecutionPaused(RuntimeError):
    """A reader reached a safe pause; use the execution's explicit resume method."""

    def __init__(self, execution_id: str, boundary: str) -> None:
        self.execution_id = execution_id
        self.boundary = boundary
        super().__init__(
            f"AgentExecution {execution_id!r} is paused at {boundary!r}; "
            "resume explicitly before reading a terminal result."
        )
