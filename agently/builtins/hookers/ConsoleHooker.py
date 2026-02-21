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

import warnings
from typing import TYPE_CHECKING

from agently.types.plugins import EventHooker

if TYPE_CHECKING:
    from agently.types.data.event import EventMessage


_DEPRECATION_MESSAGE = (
    "ConsoleHooker is deprecated and no longer active. " "Use default logger/system-message hooks instead."
)


class ConsoleHooker(EventHooker):
    """Deprecated no-op hooker kept for backward compatibility."""

    name = "ConsoleHooker"
    events = []

    @staticmethod
    def _on_register():
        warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=3)

    @staticmethod
    def _on_unregister():
        return

    @staticmethod
    async def handler(message: "EventMessage"):
        return
