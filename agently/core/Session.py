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

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from agently.utils import Settings

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.base import Agent
    from agently.types.plugins import (
        SessionProtocol,
        MemoResizePolicyHandler,
        MemoResizeHandler,
        AttachmentSummaryHandler,
        MemoUpdateHandler,
    )


class Session:
    def __init__(
        self,
        *,
        policy_handler: "MemoResizePolicyHandler | None" = None,
        resize_handlers: "dict[Literal['lite', 'deep'] | str, MemoResizeHandler] | None" = None,
        attachment_summary_handler: "AttachmentSummaryHandler | None" = None,
        memo_update_handler: "MemoUpdateHandler | None" = None,
        parent_settings: Settings | None = None,
        agent: "Agent | None" = None,
        plugin_manager: "PluginManager | None" = None,
    ):
        if plugin_manager is None:
            from agently.base import plugin_manager as global_plugin_manager, settings as global_settings

            plugin_manager = global_plugin_manager
            if parent_settings is None:
                parent_settings = global_settings

        if parent_settings is None:
            parent_settings = Settings(name="Session-Settings")

        session_settings = Settings(
            name="Session-Settings",
            parent=parent_settings,
        )
        plugin_name = str(session_settings.get("plugins.Session.activate", "AgentlyMemoSession"))
        SessionPlugin = cast(
            type["SessionProtocol"],
            plugin_manager.get_plugin("Session", plugin_name),
        )
        impl = SessionPlugin(
            policy_handler=policy_handler,
            resize_handlers=resize_handlers,
            attachment_summary_handler=attachment_summary_handler,
            memo_update_handler=memo_update_handler,
            parent_settings=session_settings,
            agent=agent,
        )
        object.__setattr__(self, "_impl", impl)
        object.__setattr__(self, "settings", impl.settings)
        object.__setattr__(self, "plugin_manager", plugin_manager)

    def __getattr__(self, name: str):
        return getattr(self._impl, name)

    def __setattr__(self, name: str, value):
        if name in {"_impl", "settings", "plugin_manager"}:
            object.__setattr__(self, name, value)
            return
        if "_impl" in self.__dict__ and hasattr(self._impl, name):
            setattr(self._impl, name, value)
            return
        object.__setattr__(self, name, value)
