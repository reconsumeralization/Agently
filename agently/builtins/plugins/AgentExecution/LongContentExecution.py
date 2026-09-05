"""Document planning, serial section writing and host-owned text assembly."""

from agently.utils import SettingsNamespace

from .modules.execution import AgentExecution
from .modules.long_content_flow import LongContentExecutionConfig, run_long_content_execution
from .modules.production import ProductionOptions


class LongContentExecution(AgentExecution):
    name = "long_content"
    producer_route = "long_content"
    supported_strategies = frozenset({"auto"})
    DEFAULT_SETTINGS = {"max_sections": 12, "continuity_chars": 4_000}

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        settings = SettingsNamespace(self.request.settings, f"plugins.AgentExecution.{self.name}")
        config = LongContentExecutionConfig(
            max_sections=_positive_int(settings.get("max_sections", 12), "max_sections"),
            continuity_chars=_positive_int(settings.get("continuity_chars", 4_000), "continuity_chars"),
        )
        return "long_content", await run_long_content_execution(self, config)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Long Content Execution setting `{name}` must be a positive integer.")
    return value
