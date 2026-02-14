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

from typing import TYPE_CHECKING, Any, Sequence

import yaml

from agently.core import BaseAgent, Session
from agently.utils import DataLocator

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.core.ModelRequest import ModelResponseResult
    from agently.types.data import ChatMessage, ChatMessageDict
    from agently.utils import Settings


class SessionExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sessions: dict[str, Session] = {}
        self.activated_session: Session | None = None
        self.settings.setdefault("session.input_keys", None, inherit=True)
        self.settings.setdefault("session.reply_keys", None, inherit=True)
        self.extension_handlers.append("request_prefixes", self._session_request_prefix)
        self.extension_handlers.append("finally", self._session_finally)

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
        if "chat_history" in self.agent_prompt:
            del self.agent_prompt["chat_history"]
        self.agent_prompt.set("chat_history", [])
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

    def _normalize_keys(self, keys: Any):
        if keys is None:
            return None
        if isinstance(keys, str):
            return [keys]
        if isinstance(keys, Sequence):
            return [str(key) for key in keys if key is not None]
        return []

    def _extract_by_path(self, data: Any, key: str):
        sentinel = object()
        if isinstance(data, dict):
            if key in data:
                return True, data[key]
            style = "slash" if "/" in key else "dot"
            value = DataLocator.locate_path_in_dict(data, key, style=style, default=sentinel)
            if value is not sentinel:
                return True, value
        return False, None

    def _extract_input_value(self, prompt_data: dict[str, Any], key: str):
        key = key.strip()
        if key == "":
            return False, None

        if key == ".request":
            return True, prompt_data

        if key.startswith(".request."):
            return self._extract_by_path(prompt_data, key[len(".request.") :])

        if key == ".agent":
            return True, self.agent_prompt.to_serializable_prompt_data()

        if key.startswith(".agent."):
            return self._extract_by_path(
                self.agent_prompt.to_serializable_prompt_data(),
                key[len(".agent.") :],
            )

        found, value = self._extract_by_path(prompt_data, key)
        if found:
            return found, value

        input_data = prompt_data.get("input")
        if isinstance(input_data, dict):
            if key.startswith("input."):
                return self._extract_by_path(input_data, key[len("input.") :])
            return self._extract_by_path(input_data, key)

        return False, None

    def _format_value(self, value: Any):
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)) or value is None:
            return str(value)
        try:
            return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).rstrip("\n")
        except Exception:
            return str(value)

    def _format_keyed_content(self, items: list[tuple[str, Any]]):
        lines: list[str] = []
        for key, value in items:
            lines.append(f"[{ key }]:")
            lines.append(self._format_value(value))
        return "\n".join(lines).strip()

    async def _session_request_prefix(self, prompt: "Prompt", _: "Settings"):
        if self.activated_session is None:
            return
        if "chat_history" in prompt:
            del prompt["chat_history"]
        prompt.set("chat_history", self.activated_session.context_window)

    async def _session_finally(self, result: "ModelResponseResult", settings: "Settings"):
        if self.activated_session is None:
            return

        input_keys = self._normalize_keys(settings.get("session.input_keys", None))
        reply_keys = self._normalize_keys(settings.get("session.reply_keys", None))

        user_content: str | None = None
        if input_keys is None:
            user_content = str(result.prompt.to_text())
        else:
            prompt_data = result.prompt.to_serializable_prompt_data()
            user_items: list[tuple[str, Any]] = []
            for input_key in input_keys:
                found, value = self._extract_input_value(dict(prompt_data), input_key)
                if found:
                    user_items.append((input_key, value))
            if user_items:
                user_content = self._format_keyed_content(user_items)

        assistant_content: str | None = None
        result_data = await result.async_get_data()
        if reply_keys is None:
            assistant_content = self._format_value(result_data)
        else:
            assistant_items: list[tuple[str, Any]] = []
            for reply_key in reply_keys:
                found, value = self._extract_by_path(result_data, reply_key)
                if found:
                    assistant_items.append((reply_key, value))
            if assistant_items:
                assistant_content = self._format_keyed_content(assistant_items)

        if user_content is not None and user_content != "":
            self.add_chat_history({"role": "user", "content": user_content})
        if assistant_content is not None and assistant_content != "":
            self.add_chat_history({"role": "assistant", "content": assistant_content})
