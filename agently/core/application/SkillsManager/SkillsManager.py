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

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .adapter import RegistrySkillSource, SkillCapabilityAdapter

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.utils import Settings


class SkillsManager:
    """Internal core entrypoint over the active SkillsManager plugin."""

    def __init__(self, plugin_manager: "PluginManager", settings: "Settings"):
        self.plugin_manager = plugin_manager
        self.settings = settings
        self._impl = self._create_impl()

    def _create_impl(self):
        plugin_name = str(
            self.settings.get(
                "plugins.SkillsManager.activate",
                self.settings.get("plugins.SkillsExecutor.activate", "AgentlySkillsManager"),
            )
        )
        try:
            plugin_class = cast(Any, self.plugin_manager.get_plugin("SkillsManager", plugin_name))
        except Exception:
            legacy_name = "AgentlySkillsExecutor" if plugin_name == "AgentlySkillsManager" else plugin_name
            plugin_class = cast(Any, self.plugin_manager.get_plugin("SkillsExecutor", legacy_name))
        return plugin_class(plugin_manager=self.plugin_manager, settings=self.settings)

    @property
    def impl(self):
        return self._impl

    @property
    def registry(self):
        return self._impl.registry

    def capability_adapter(self) -> SkillCapabilityAdapter:
        factory = getattr(self._impl, "capability_adapter", None)
        if callable(factory):
            return cast(SkillCapabilityAdapter, factory())
        return SkillCapabilityAdapter(RegistrySkillSource(self.registry))

    def discover_skill_capabilities(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.capability_adapter().discover(limit=limit)

    def activate_skill(self, skill_id: str, *, task: str | None = None, budget_chars: int = 4000):
        return self.capability_adapter().activate(skill_id, task=task, budget_chars=budget_chars)

    def configure(
        self,
        *,
        registry_root: str | Path | None = None,
        allowed_trust_levels: list[str] | None = None,
    ) -> "SkillsManager":
        if registry_root is not None:
            self.settings.set("skills.registry.root", str(registry_root))
        if allowed_trust_levels is not None:
            self.settings._set_item_by_dot_path("skills.allowed_trust_levels", list(allowed_trust_levels), cover=True)
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._impl, name)
