# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from typing import TYPE_CHECKING

from agently.utils import SettingsNamespace

from .modules import PlanPatternConfig, run_plan_pattern

if TYPE_CHECKING:
    from agently.types.plugins import AgentExecution, AgentPatternContinuation
    from agently.utils import Settings


class PlanPattern:
    name = "plan"
    DEFAULT_SETTINGS = {
        "max_questions_per_round": 3,
        "max_clarification_rounds": 3,
    }

    def __init__(self, *, plugin_manager, settings: "Settings") -> None:
        self.plugin_manager = plugin_manager
        self.settings = settings
        plugin_settings = SettingsNamespace(
            settings,
            f"plugins.AgentPattern.{self.name}",
        )
        self.config = PlanPatternConfig(
            max_questions_per_round=_positive_int(
                plugin_settings.get("max_questions_per_round", 3),
                name="max_questions_per_round",
            ),
            max_clarification_rounds=_positive_int(
                plugin_settings.get("max_clarification_rounds", 3),
                name="max_clarification_rounds",
            ),
        )

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    async def run(
        self,
        execution: "AgentExecution",
        _run_default: "AgentPatternContinuation",
        /,
    ):
        return await run_plan_pattern(execution, self.config)


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"Plan Pattern setting `{name}` must be a positive integer."
        )
    return value


__all__ = ["PlanPattern"]
