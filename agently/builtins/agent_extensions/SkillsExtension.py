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

from agently.core import BaseAgent
from agently.skills.core import AgentSkillsMixin

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.utils import Settings


class SkillsExtension(AgentSkillsMixin, BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from agently.base import skills as global_skills

        self._init_skills(global_skills.registry)

        request_prefixes = self.extension_handlers.get("request_prefixes", [])
        if not isinstance(request_prefixes, list):
            request_prefixes = []
        self.extension_handlers.set("request_prefixes", [self.__request_prefix, *request_prefixes])
        self.extension_handlers.append("finally", self.__finally)

    async def __request_prefix(self, prompt: "Prompt", _settings: "Settings"):
        await self._apply_skill_cards_to_prompt(prompt)

    async def __finally(self, *_):
        self._clear_request_skill_selectors()
