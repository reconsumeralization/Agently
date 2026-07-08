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

from typing import TYPE_CHECKING, Any

from agently.core.application.SkillsManager import SkillsManager
from agently.utils import DeprecationWarnings

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.utils import Settings


class SkillsExecutor:
    """Deprecated compatibility facade over ``SkillsManager``."""

    def __init__(
        self,
        plugin_manager: "PluginManager",
        settings: "Settings",
        *,
        manager: SkillsManager | None = None,
    ):
        self.manager = manager or SkillsManager(plugin_manager, settings)
        self.plugin_manager = self.manager.plugin_manager
        self.settings = self.manager.settings

    @staticmethod
    def _warn_deprecated() -> None:
        DeprecationWarnings.warn_deprecated_once(
            "core.application.SkillsExecutor",
            "SkillsExecutor is deprecated as an internal dependency; use SkillsManager internally. "
            "Agently.skills_executor remains a compatibility facade.",
            stacklevel=3,
        )

    @property
    def impl(self):
        self._warn_deprecated()
        return self.manager.impl

    @property
    def registry(self):
        self._warn_deprecated()
        return self.manager.registry

    def configure(self, *args: Any, **kwargs: Any) -> "SkillsExecutor":
        self._warn_deprecated()
        self.manager.configure(*args, **kwargs)
        return self

    def __getattr__(self, name: str) -> Any:
        self._warn_deprecated()
        return getattr(self.manager, name)
