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

from agently.builtins.plugins.SkillsExecutor import AgentlySkillsExecutor


class AgentlySkillsManager(AgentlySkillsExecutor):
    """Builtin SkillsManager implementation.

    The implementation is temporarily shared with the legacy
    ``AgentlySkillsExecutor`` class while internal dependencies migrate to the
    Manager facade and protocol.
    """

    name = "AgentlySkillsManager"
