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

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.utils import Settings


class SkillsExecutor:
    """Thin core facade over the active SkillsExecutor plugin."""

    def __init__(self, plugin_manager: "PluginManager", settings: "Settings"):
        self.plugin_manager = plugin_manager
        self.settings = settings
        self._impl = self._create_impl()

    def _create_impl(self):
        plugin_name = str(self.settings.get("plugins.SkillsExecutor.activate", "AgentlySkillsExecutor"))
        plugin_class = cast(Any, self.plugin_manager.get_plugin("SkillsExecutor", plugin_name))
        return plugin_class(plugin_manager=self.plugin_manager, settings=self.settings)

    @property
    def impl(self):
        return self._impl

    @property
    def registry(self):
        return self._impl.registry

    def __getattr__(self, name: str) -> Any:
        return getattr(self._impl, name)
