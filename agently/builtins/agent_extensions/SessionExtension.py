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

from typing import Any, TYPE_CHECKING

from agently.core import BaseAgent
from agently.core.Session import Session

if TYPE_CHECKING:
    from agently.types.data import ChatMessage
    from agently.core import Prompt
    from agently.core.ModelRequest import ModelResponseResult
    from agently.utils import Settings


class SessionExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session: Session | None = None

        self.extension_handlers.append("request_prefixes", self._session_request_prefix)
        self.extension_handlers.append("finally", self._session_finally)

    @property
    def session(self) -> Session | None:
        return self._session

    def attach_session(self, session: Session | None = None, *, mode: str | None = None):
        if session is None:
            session = Session(parent_settings=self.settings, agent=self)
        if mode is not None:
            session.configure(mode=mode)
        self._session = session
        return self

    def detach_session(self):
        self._session = None
        return self

    def _normalize_chat_history(self, chat_history: list[dict[str, Any] | ChatMessage] | dict[str, Any] | ChatMessage):
        if not isinstance(chat_history, list):
            return [chat_history]
        return chat_history

    def _reset_session_history(self):
        assert self._session is not None
        self._session.full_chat_history = []
        self._session.current_chat_history = []
        self._session._turns = 0
        self._session._last_resize_turn = 0
        self._session._memo_cursor = 0

    def set_chat_history(self, chat_history: list[dict[str, Any] | ChatMessage]):
        if self._session is None:
            return super().set_chat_history(chat_history)
        self._reset_session_history()
        for message in self._normalize_chat_history(chat_history):
            self._session.append_message(message)
        return self

    def add_chat_history(self, chat_history: list[dict[str, Any] | ChatMessage] | dict[str, Any] | ChatMessage):
        if self._session is None:
            return super().add_chat_history(chat_history)
        for message in self._normalize_chat_history(chat_history):
            self._session.append_message(message)
        return self

    def reset_chat_history(self):
        if self._session is None:
            return super().reset_chat_history()
        self._reset_session_history()
        return self

    async def _session_request_prefix(self, prompt: "Prompt", _settings: "Settings"):
        if self._session is None:
            return
        prompt.set("chat_history", self._session.current_chat_history)

    async def _session_finally(self, result: "ModelResponseResult", _settings: "Settings"):
        if self._session is None:
            return
        prompt = result.prompt
        user_input = prompt.get("input", None)
        if user_input not in (None, ""):
            self._session.append_message({"role": "user", "content": user_input})

        assistant_text = None
        if hasattr(result, "async_get_text"):
            assistant_text = await result.async_get_text()
        elif hasattr(result, "get_text"):
            assistant_text = result.get_text()

        if assistant_text not in (None, ""):
            self._session.append_message({"role": "assistant", "content": assistant_text})
