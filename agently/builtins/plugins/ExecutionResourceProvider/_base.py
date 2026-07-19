from __future__ import annotations


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

    async def async_probe(self, *, requirement, policy):
        _ = requirement, policy
        return {
            "provider_id": self.provider_id,
            "available": True,
            "supported_kinds": list(self.supported_kinds),
            "capabilities": dict(self.capabilities),
            "reason": "provider registered",
        }


__all__ = ["BuiltinExecutionResourceProvider"]
