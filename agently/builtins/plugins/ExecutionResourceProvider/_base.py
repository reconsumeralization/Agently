from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from agently.types.data import ExecutionResourceProviderProbe


class BuiltinExecutionResourceProvider:
    """Preferred probe surface shared by builtin resource providers."""

    name = "BuiltinExecutionResourceProvider"
    kind = ""
    capabilities: dict[str, object] = {}

    @property
    def provider_id(self) -> str:
        return self.name

    @property
    def supported_kinds(self) -> tuple[str, ...]:
        return (self.kind,)

    async def async_probe(
        self, *, requirement: Any, policy: Any
    ) -> "ExecutionResourceProviderProbe":
        _ = requirement, policy
        return {
            "provider_id": self.provider_id,
            "available": True,
            "supported_kinds": list(self.supported_kinds),
            "capabilities": dict(self.capabilities),
            "reason": "provider registered",
        }


__all__ = ["BuiltinExecutionResourceProvider"]
