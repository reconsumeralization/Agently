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

from uuid import uuid4

from typing import TYPE_CHECKING, Sequence

from agently.core import BaseAgent, Session

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import ChatMessage, ChatMessageDict
    from agently.utils import Settings


class SessionExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sessions: dict[str, Session] = {}
        self.activated_session: Session | None = None
        self.extension_handlers.append("request_prefixes", self._session_request_prefix)

    def _refill_agent_chat_history_with_session(self):
        if self.activated_session is None:
            return self
        if "chat_history" in self.agent_prompt:
            del self.agent_prompt["chat_history"]
        self.agent_prompt.set("chat_history", self.activated_session.context_window)
        return self

    def activate_session(self, *, session_id: str | None = None):
        if session_id is not None and session_id in self.sessions:
            self.activated_session = self.sessions[session_id]
        else:
            if session_id is None:
                session_id = uuid4().hex
            self.activated_session = Session(
                id=session_id,
                auto_resize=True,
                settings=self.settings,
            )
            self.sessions[session_id] = self.activated_session
        return self._refill_agent_chat_history_with_session()

    def deactivate_session(self):
        self.activated_session = None
        return self

    def reset_chat_history(self):
        if self.activated_session is None:
            return super().reset_chat_history()
        self.activated_session.reset_chat_history()
        return self._refill_agent_chat_history_with_session()

    def set_chat_history(
        self,
        chat_history: "Sequence[ChatMessage | ChatMessageDict]",
    ):
        if self.activated_session is None:
            return super().set_chat_history(chat_history)
        self.activated_session.set_chat_history(chat_history)
        return self._refill_agent_chat_history_with_session()

    def add_chat_history(
        self,
        chat_history: "Sequence[ChatMessage | ChatMessageDict] | ChatMessage | ChatMessageDict",
    ):
        if self.activated_session is None:
            return super().add_chat_history(chat_history)
        self.activated_session.add_chat_history(chat_history)
        return self._refill_agent_chat_history_with_session()

    def clean_context_window(self):
        if self.activated_session is None:
            if "chat_history" in self.agent_prompt:
                del self.agent_prompt["chat_history"]
            return self
        self.activated_session.clean_context_window()
        return self._refill_agent_chat_history_with_session()

    def clean_window_context(self):
        return self.clean_context_window()

    async def _session_request_prefix(self, prompt: "Prompt", _: "Settings"):
        if self.activated_session is None:
            return
        if "chat_history" in prompt:
            del prompt["chat_history"]
        prompt.set("chat_history", self.activated_session.context_window)
