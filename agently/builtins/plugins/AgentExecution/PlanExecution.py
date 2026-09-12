"""Plan production with connected clarification on the carrying execution."""

from agently.utils import SettingsNamespace

from .modules.execution import AgentExecution
from .modules.plan_flow import PlanExecutionConfig, run_plan_execution
from .modules.production import ProductionOptions


class PlanExecution(AgentExecution):
    name = "plan"
    producer_route = "plan"
    supported_strategies = frozenset({"auto"})
    DEFAULT_SETTINGS = {"max_questions_per_round": 3, "max_clarification_rounds": 3}

    def _assert_rework_supported(self) -> None:
        if self._producer_state.get("kind") != "plan":
            raise RuntimeError("Plan rework requires its retained accepted clarifications.")

    async def _async_rework_produce(self, options: ProductionOptions) -> tuple[str, object]:
        return await self._async_produce(options)

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        settings = SettingsNamespace(self.request.settings, f"plugins.AgentExecution.{self.name}")
        config = PlanExecutionConfig(
            max_questions_per_round=_positive_int(settings.get("max_questions_per_round", 3), "max_questions_per_round"),
            max_clarification_rounds=_positive_int(settings.get("max_clarification_rounds", 3), "max_clarification_rounds"),
        )
        return "plan", await run_plan_execution(self, config)


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Plan Execution setting `{name}` must be a positive integer.")
    return value
