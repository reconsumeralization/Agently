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
    from agently.core import PluginManager, BaseAgent
    from agently.types.plugins import (
        SessionProtocol,
        SessionMode,
        SessionLimit,
        MemoResizeDecision,
        MemoResizeType,
        MemoResizePolicyHandler,
        MemoResizeHandler,
        AttachmentSummaryHandler,
        MemoUpdateHandler,
    )
    from agently.types.data import ChatMessage, ChatMessageDict, SerializableValue, SerializableData


class Session:
    _impl: "SessionProtocol"
    settings: Settings
    plugin_manager: "PluginManager"
    id: str
    memo: "SerializableData"
    full_chat_history: "list[ChatMessage]"
    current_chat_history: "list[ChatMessage]"

    def __init__(
        self,
        *,
        policy_handler: "MemoResizePolicyHandler | None" = None,
        resize_handlers: "dict[MemoResizeType, MemoResizeHandler] | None" = None,
        attachment_summary_handler: "AttachmentSummaryHandler | None" = None,
        memo_update_handler: "MemoUpdateHandler | None" = None,
        parent_settings: Settings | None = None,
        agent: "BaseAgent | None" = None,
        plugin_manager: "PluginManager | None" = None,
    ):
        if agent is not None:
            if plugin_manager is None and hasattr(agent, "plugin_manager"):
                plugin_manager = agent.plugin_manager
            if parent_settings is None and hasattr(agent, "settings"):
                parent_settings = agent.settings
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

    def configure(
        self,
        *,
        mode: "SessionMode | None" = None,
        limit: "SessionLimit | None" = None,
        every_n_turns: int | None = None,
    ) -> "Session":
        self._impl.configure(
            mode=mode,
            limit=limit,
            every_n_turns=every_n_turns,
        )
        return self

    def set_limit(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
    ) -> "Session":
        self._impl.set_limit(chars=chars, messages=messages)
        return self

    def use_lite(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
        every_n_turns: int | None = None,
    ) -> "Session":
        self._impl.use_lite(chars=chars, messages=messages, every_n_turns=every_n_turns)
        return self

    def use_memo(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
        every_n_turns: int | None = None,
    ) -> "Session":
        self._impl.use_memo(chars=chars, messages=messages, every_n_turns=every_n_turns)
        return self

    def append_message(self, message: "ChatMessage | ChatMessageDict") -> "Session":
        self._impl.append_message(message)
        return self

    def set_settings(
        self,
        key: str,
        value: "SerializableValue",
        *,
        auto_load_env: bool = False,
    ) -> Settings:
        return self._impl.set_settings(key, value, auto_load_env=auto_load_env)

    def set_policy_handler(self, policy_handler: "MemoResizePolicyHandler") -> "Session":
        self._impl.set_policy_handler(policy_handler)
        return self

    def set_resize_handlers(
        self,
        resize_type: "MemoResizeType",
        resize_handler: "MemoResizeHandler",
    ) -> "Session":
        self._impl.set_resize_handlers(resize_type, resize_handler)
        return self

    def set_attachment_summary_handler(
        self,
        attachment_summary_handler: "AttachmentSummaryHandler",
    ) -> "Session":
        self._impl.set_attachment_summary_handler(attachment_summary_handler)
        return self

    def set_memo_update_handler(
        self,
        memo_update_handler: "MemoUpdateHandler",
    ) -> "Session":
        self._impl.set_memo_update_handler(memo_update_handler)
        return self

    def judge_resize(
        self,
        force: "Literal['lite', 'deep', False, None] | str" = False,
    ) -> "MemoResizeDecision | None":
        return self._impl.judge_resize(force=force)

    def resize(
        self,
        force: "Literal['lite', 'deep', False, None] | str" = False,
    ) -> "list[ChatMessage]":
        return self._impl.resize(force=force)

    async def async_judge_resize(
        self,
        force: "Literal['lite', 'deep', False, None] | str" = False,
    ) -> "MemoResizeDecision | None":
        return await self._impl.async_judge_resize(force=force)

    async def async_resize(
        self,
        force: "Literal['lite', 'deep', False, None] | str" = False,
    ) -> "list[ChatMessage]":
        return await self._impl.async_resize(force=force)

    def to_json(self) -> str:
        return self._impl.to_json()

    def to_yaml(self) -> str:
        return self._impl.to_yaml()

    def load_json(self, value: str) -> "Session":
        self._impl.load_json(value)
        return self

    def load_yaml(self, value: str) -> "Session":
        self._impl.load_yaml(value)
        return self

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
