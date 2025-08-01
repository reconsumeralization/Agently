# Copyright 2023-2025 AgentEra(Agently.Tech)
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

from ._entrypoint import AgentlyMain
from ._default_init import load_default_settings, load_default_plugins, hook_default_event_handlers

Agently = AgentlyMain()
load_default_plugins(Agently)
load_default_settings(Agently)
hook_default_event_handlers(Agently)

__all__ = ["Agently"]
