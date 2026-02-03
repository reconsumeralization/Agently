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

import inspect
import json
import yaml

from typing import Any, Sequence, TYPE_CHECKING

from agently.core import BaseAgent
from agently.core.Session import Session
from agently.utils import DataPathBuilder, FunctionShifter

if TYPE_CHECKING:
    from agently.types.data import ChatMessage
    from agently.core import Prompt
    from agently.core.ModelRequest import ModelResponseResult
    from agently.utils import Settings


class SessionExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session: Session | None = None
        self._record_handler = None

        self.settings.setdefault("session.record.input.paths", [], inherit=True)
        self.settings.setdefault("session.record.input.mode", "all", inherit=True)
        self.settings.setdefault("session.record.output.paths", [], inherit=True)
        self.settings.setdefault("session.record.output.mode", "all", inherit=True)

        self.extension_handlers.append("request_prefixes", self._session_request_prefix)
        self.extension_handlers.append("finally", self._session_finally)

        # Sync wrappers for async-first methods
        self.set_chat_history = FunctionShifter.syncify(self.async_set_chat_history)
        self.add_chat_history = FunctionShifter.syncify(self.async_add_chat_history)
        self.reset_chat_history = FunctionShifter.syncify(self.async_reset_chat_history)

    @property
    def session(self) -> Session | None:
        return self._session

    def attach_session(self, session: Session | None = None, *, mode: str | None = None):
        if session is None:
            session = Session(parent_settings=self.settings, agent=self, plugin_manager=self.plugin_manager)
        if mode is not None:
            session.configure(mode=mode)
        self._session = session
        return self

    def enable_quick_session(self, session: Session | None = None, *, load: dict[str, Any] | str | None = None):
        if self._session is None:
            self.attach_session(session=session)
        assert self._session is not None
        if load is not None:
            if isinstance(load, dict):
                self._session.load_json(json.dumps(load, ensure_ascii=True))
            elif isinstance(load, str):
                self._session.load_yaml(load)
            else:
                raise TypeError(f"Invalid session load data type: {type(load)}")
        # Minimal session: no memo, no truncation, only record full chat history.
        self._session.configure(
            mode="lite",
            limit={"chars": 0, "messages": 0},
            every_n_turns=0,
        )
        return self

    def disable_quick_session(self):
        return self.detach_session()

    def detach_session(self):
        self._session = None
        return self

    def set_record_handler(self, handler):
        self._record_handler = handler
        return self

    def enable_session_lite(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
        every_n_turns: int | None = None,
        session: Session | None = None,
    ):
        if self._session is None:
            self.attach_session(session=session)
        assert self._session is not None
        self._session.use_lite(chars=chars, messages=messages, every_n_turns=every_n_turns)
        return self

    def enable_session_memo(
        self,
        *,
        chars: int | None = None,
        messages: int | None = None,
        every_n_turns: int | None = None,
        session: Session | None = None,
    ):
        if self._session is None:
            self.attach_session(session=session)
        assert self._session is not None
        self._session.use_memo(chars=chars, messages=messages, every_n_turns=every_n_turns)
        return self

    def _normalize_chat_history(
        self, chat_history: Sequence[dict[str, Any] | ChatMessage] | dict[str, Any] | ChatMessage
    ):
        messages: list[ChatMessage] = []
        if not isinstance(chat_history, Sequence):
            chat_history = [chat_history]
        for message in chat_history:
            if not isinstance(message, ChatMessage):
                messages.append(ChatMessage(role=message["role"], content=message["content"]))
            else:
                messages.append(message)
        return messages

    def _stringify_content(self, content: Any):
        if content is None:
            return None
        if isinstance(content, dict):
            return yaml.safe_dump(content)
        if isinstance(content, (str, list)):
            return content
        return str(content)

    def _collect_record_input(self, prompt: "Prompt") -> list[dict[str, Any]]:
        record_input_paths = self.settings.get("session.record.input.paths", [])
        record_input_mode = self.settings.get("session.record.input.mode", "all")

        if isinstance(record_input_paths, str):
            record_input_paths = [record_input_paths]

        if not isinstance(record_input_paths, list) or len(record_input_paths) == 0:
            user_input = prompt.get("input", None)
            if user_input not in (None, ""):
                content = self._stringify_content(user_input)
                if content not in (None, ""):
                    return [{"role": "user", "content": content}]
            try:
                messages = prompt.to_messages()
            except Exception:
                return []
            if messages:
                content = messages[-1].get("content")
                if content not in (None, ""):
                    return [{"role": "user", "content": content}]
            return []

        content: Any = {}
        for entry in record_input_paths:
            if isinstance(entry, str):
                prompt_key, path = entry, None
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                prompt_key, path = entry[0], entry[1]
            else:
                continue

            if path is None:
                value = prompt.get(str(prompt_key))
                if value is None:
                    continue
                if record_input_mode == "first":
                    return [{"role": "user", "content": self._stringify_content(value)}]
                if not isinstance(content, dict):
                    content = {}
                content[prompt_key] = value
            else:
                prompt_value = prompt.get(str(prompt_key))
                if isinstance(prompt_value, dict):
                    path_value = DataPathBuilder.get_value_by_path(prompt_value, str(path))
                    if path_value is None:
                        continue
                    if record_input_mode == "first":
                        return [{"role": "user", "content": self._stringify_content(path_value)}]
                    if not isinstance(content, dict):
                        content = {}
                    if prompt_key not in content or not isinstance(content[prompt_key], dict):
                        content[prompt_key] = {}
                    content[prompt_key][str(path)] = path_value

        if content in (None, {}, []):
            return []
        return [{"role": "user", "content": self._stringify_content(content)}]

    def _get_result_text(self, result: "ModelResponseResult"):
        return result.full_result_data.get("text_result")

    async def _collect_record_output(self, result: "ModelResponseResult") -> list[dict[str, Any]]:
        record_output_paths = self.settings.get("session.record.output.paths", [])
        record_output_mode = self.settings.get("session.record.output.mode", "all")

        if isinstance(record_output_paths, str):
            record_output_paths = [record_output_paths]

        if not isinstance(record_output_paths, list) or len(record_output_paths) == 0:
            assistant_text = self._get_result_text(result)
            if assistant_text not in (None, ""):
                return [{"role": "assistant", "content": assistant_text}]
            return []

        parsed_result = result.full_result_data.get("parsed_result")
        if isinstance(parsed_result, dict):
            content: Any = {}
            for path in record_output_paths:
                if not isinstance(path, str):
                    continue
                path_key = DataPathBuilder.convert_slash_to_dot(path) if "/" in path else path
                path_value = DataPathBuilder.get_value_by_path(parsed_result, path_key)
                if path_value is None:
                    continue
                if record_output_mode == "first":
                    return [{"role": "assistant", "content": self._stringify_content(path_value)}]
                if not isinstance(content, dict):
                    content = {}
                content[path_key] = path_value
            if isinstance(content, dict) and content:
                return [{"role": "assistant", "content": self._stringify_content(content)}]

        assistant_text = self._get_result_text(result)
        if assistant_text not in (None, ""):
            return [{"role": "assistant", "content": assistant_text}]
        return []

    def _reset_session_history(self):
        assert self._session is not None
        self._session.full_chat_history = []
        self._session.current_chat_history = []
        self._session._turns = 0
        self._session._last_resize_turn = 0
        self._session._memo_cursor = 0

    async def async_set_chat_history(self, chat_history: Sequence[dict[str, Any] | ChatMessage]):
        if self._session is None:
            return super().set_chat_history(chat_history)
        self._reset_session_history()
        for message in self._normalize_chat_history(chat_history):
            self._session.append_message(message)
        return self

    async def async_add_chat_history(
        self,
        chat_history: Sequence[dict[str, Any] | ChatMessage] | dict[str, Any] | ChatMessage,
    ):
        if self._session is None:
            return super().add_chat_history(chat_history)
        messages = self._normalize_chat_history(chat_history)
        if not messages:
            return self
        for message in messages:
            self._session.append_message(message)
        await self._session.async_resize()
        return self

    async def async_reset_chat_history(self):
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
        if self._record_handler is not None:
            handler_result = self._record_handler(result)
            if inspect.isawaitable(handler_result):
                handler_result = await handler_result
            if handler_result:
                await self.async_add_chat_history(handler_result)
            return

        prompt = result.prompt
        messages: list[dict[str, Any]] = []
        messages.extend(self._collect_record_input(prompt))
        messages.extend(await self._collect_record_output(result))
        if messages:
            await self.async_add_chat_history(messages)
