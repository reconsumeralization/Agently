"""Explicit long-task selection sharing the automatic execution's task producer."""

from .modules.execution import AgentExecution


class LongTaskExecution(AgentExecution):
    name = "long_task"
    producer_route = "agent_task"
    supported_strategies = frozenset({"auto", "task", "task_loop", "long_task", "flat", "taskboard"})
