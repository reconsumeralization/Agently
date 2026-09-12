"""An explicitly selected single logical request, including request-owned repair."""

from .modules.execution import AgentExecution


class RequestExecution(AgentExecution):
    name = "request"
    producer_route = "model_request"
    supported_strategies = frozenset({"auto", "direct"})
