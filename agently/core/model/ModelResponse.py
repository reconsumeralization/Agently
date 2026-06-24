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

"""Deprecated compatibility facade for the historical ModelResponse class."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agently.utils import DeprecationWarnings, Settings

from .ModelRequestRunner import ModelRequestRunner
from .Prompt import Prompt

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.core.extension import ExtensionHandlers
    from agently.types.data import RunContext


class ModelResponse(ModelRequestRunner):
    """Deprecated alias for the non-public ModelRequestRunner implementation."""

    def __init__(
        self,
        agent_name: str,
        plugin_manager: "PluginManager",
        settings: Settings,
        prompt: Prompt,
        extension_handlers: "ExtensionHandlers",
        *,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        agent_execution_run_context: "RunContext | None" = None,
        attempt_index: int = 1,
        warn_deprecated: bool = True,
    ) -> None:
        if warn_deprecated:
            DeprecationWarnings.warn_deprecated_once(
                "ModelResponse",
                "ModelResponse is deprecated and will be removed in Agently 4.2. "
                "Use ModelRequestResult returned by get_result() instead.",
                stacklevel=2,
            )
        super().__init__(
            agent_name,
            plugin_manager,
            settings,
            prompt,
            extension_handlers,
            run_context=run_context,
            parent_run_context=parent_run_context,
            agent_execution_run_context=agent_execution_run_context,
            attempt_index=attempt_index,
        )


__all__ = ["ModelResponse"]
